# app/models/transcription_catalog.py
# Centralized catalog for transcription models and supported languages.
# Provides a single source of truth backed by MySQL tables.

import logging
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app
from mysql.connector import Error as MySQLError

from app.database import get_db, get_cursor

logger = logging.getLogger(__name__)

# Table names kept in constants to avoid typos.
MODELS_TABLE = "transcription_models_catalog"
PROVIDERS_TABLE = "transcription_providers_catalog"
LANGUAGES_TABLE = "transcription_languages_catalog"

# One catalog row serves every usage kind through a comma-separated purpose
# set (mirroring user_api_keys.model_purposes), so saving a live key can no
# longer clobber the transcription purpose of the same model identity.
VALID_MODEL_PURPOSES = frozenset({"transcription", "live"})
DEFAULT_MODEL_PURPOSE = "transcription"


def canonicalize_model_purposes(value: Any) -> str:
    """Return a canonical comma string for a purpose value or list.

    Accepts ``"Live, Transcription"``, ``["live"]``, ``None``, ... Unknown
    purposes are dropped; an empty result falls back to
    ``DEFAULT_MODEL_PURPOSE`` so legacy single-purpose callers keep working.
    The canonical order is fixed (``transcription`` first) so every writer
    stores the same normalized set regardless of merge order.
    """
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw_items = list(value)
    else:
        raw_items = []
    purposes = {
        str(item).strip().lower()
        for item in raw_items
        if str(item).strip().lower() in VALID_MODEL_PURPOSES
    }
    if not purposes:
        purposes = {DEFAULT_MODEL_PURPOSE}
    return ",".join(
        purpose for purpose in ("transcription", "live") if purpose in purposes
    )


# Provider metadata is deliberately separate from selectable model rows. The
# supported provider adapters remain fixed in application code, while model
# identifiers/display names can be registered as data at runtime.
_PROVIDER_METADATA: Dict[str, Dict[str, Optional[str]]] = {
    "assemblyai": {
        "display_name": "AssemblyAI",
        "permission_key": "use_api_assemblyai",
        "required_api_key": "assemblyai",
        "client_kind": "assemblyai",
    },
    "openai": {
        "display_name": "OpenAI",
        "permission_key": "use_api_openai",
        "required_api_key": "openai",
        "client_kind": "openai",
    },
    "openrouter": {
        "display_name": "OpenRouter",
        "permission_key": "use_api_openrouter",
        "required_api_key": "openrouter",
        "client_kind": "openrouter",
    },
}

# Provider labels and retired identifiers are never selectable models. The
# provider table remains the source of provider metadata; these sets are only
# defensive compatibility filters for databases being migrated.
PROVIDER_ONLY_MODEL_CODES = frozenset({"openai", "assemblyai", "openrouter"})
DEPRECATED_MODEL_CODES = frozenset({"whisper", "gpt-4o-transcribe-diarize"})
MODEL_KEY_SEPARATOR = ":"


def make_model_key(provider_code: Optional[str], model_code: Optional[str]) -> str:
    """Build the stable identity used outside the catalog table.

    ``code`` remains provider-local so a provider can introduce a model with
    the same identifier as another provider. Callers that persist or submit a
    selectable model use ``provider:model`` instead.
    """
    provider = str(provider_code or "").strip().lower()
    code = str(model_code or "").strip()
    if provider and code:
        return f"{provider}{MODEL_KEY_SEPARATOR}{code}"
    return code


def split_model_reference(
    reference: Optional[str],
    provider_code: Optional[str] = None,
) -> Tuple[Optional[str], str]:
    """Return ``(provider, provider_local_code)`` for old or new references.

    Existing database values may still contain a bare model code. A qualified
    value is recognized only when its prefix is a known transcription provider,
    so provider-local names that happen to contain a colon remain untouched.
    """
    value = str(reference or "").strip()
    provider_hint = str(provider_code or "").strip().lower() or None
    if not value:
        return provider_hint, ""
    if MODEL_KEY_SEPARATOR in value:
        prefix, local_code = value.split(MODEL_KEY_SEPARATOR, 1)
        prefix = prefix.strip().lower()
        if prefix in _PROVIDER_METADATA and local_code.strip():
            return prefix, local_code.strip()
    return provider_hint, value


def _row_provider(row: Dict[str, Any]) -> str:
    return str(
        row.get("provider_code")
        or row.get("required_api_key")
        or ""
    ).strip().lower()


def _row_to_model(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a joined catalog row into the shared model representation."""
    code = str(row.get("code") or "").strip()
    provider = _row_provider(row)
    return {
        "code": code,
        "model_key": make_model_key(provider, code),
        "display_name": row.get("display_name"),
        "provider_code": provider or None,
        "permission_key": row.get("permission_key"),
        "required_api_key": row.get("required_api_key"),
        "is_default": bool(row.get("is_default", False)),
        "is_active": bool(row.get("is_active", True)),
        "model_purposes": canonicalize_model_purposes(row.get("model_purposes")),
    }


def init_db_command() -> None:
    """
    Ensures the catalog tables exist and are seeded with defaults derived from config.
    Safe to call multiple times.
    """
    cursor = get_cursor()
    log_prefix = "[DB:Catalog]"
    logger.info(f"{log_prefix} Ensuring transcription catalog tables exist.")

    try:
        _ensure_providers_table(cursor)
        _ensure_models_table(cursor)
        _ensure_languages_table(cursor)
        get_db().commit()
    except MySQLError as err:
        get_db().rollback()
        logger.error(f"{log_prefix} Failed to initialize catalog tables: {err}", exc_info=True)
        raise

    # Seed provider metadata, then normalize legacy model rows and restore
    # dynamic model rows from saved credentials before seeding languages.
    try:
        _seed_providers_from_config()
        _normalize_legacy_model_rows()
        _sync_models_from_saved_keys()
        _heal_stale_purpose_sets()
        seed_from_config()
    except Exception as seed_err:
        logger.error(f"{log_prefix} Failed to seed catalog tables: {seed_err}", exc_info=True)
        raise


def seed_from_config() -> None:
    """
    Seeds supported languages from the current Flask config.

    Transcription models are intentionally NOT pre-seeded: models are
    registered at runtime when API keys are saved (see
    ``register_model_from_provider``). The provider list (``TRANSCRIPTION_PROVIDERS``)
    is fixed and cannot be extended by admins.
    """
    _seed_languages_from_config()


def get_active_models() -> List[Dict[str, Any]]:
    """
    Returns active normal transcription models sorted by configured order.

    Models are registered at runtime from saved API keys; nothing is
    pre-seeded (see ``register_model_from_provider``). ``model_purposes``
    distinguishes normal (``transcription``) from ``live`` models; a model
    serving both purposes is returned here and by ``get_live_models``.
    """
    if not _table_has_rows(MODELS_TABLE):
        seed_from_config()

    cursor = get_cursor()
    sql = f"""
        SELECT
            m.code,
            m.display_name,
            COALESCE(NULLIF(m.provider_code, ''), p.provider_code, m.required_api_key) AS provider_code,
            COALESCE(p.permission_key, m.permission_key) AS permission_key,
            COALESCE(p.required_api_key, m.required_api_key) AS required_api_key,
            m.is_default,
            m.model_purposes
        FROM {MODELS_TABLE} AS m
        LEFT JOIN {PROVIDERS_TABLE} AS p
            ON p.provider_code = COALESCE(NULLIF(m.provider_code, ''), m.required_api_key)
        WHERE m.is_active = TRUE
          AND FIND_IN_SET('transcription', m.model_purposes)
          AND LOWER(m.code) NOT IN ('openai', 'assemblyai', 'openrouter', 'whisper', 'gpt-4o-transcribe-diarize')
        ORDER BY m.sort_order ASC, m.display_name ASC
    """
    cursor.execute(sql)
    rows = cursor.fetchall() or []
    models: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    for row in rows:
        model = _row_to_model(row)
        model_key = model["model_key"]
        if not model["code"] or model_key in seen_keys:
            continue
        seen_keys.add(model_key)
        models.append(model)
    return models


def get_all_active_models(
    model_purpose: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return active catalog rows, optionally filtered by ``model_purpose``.

    Unlike ``get_active_models`` (used for the normal transcription dropdown),
    this keeps both ``transcription`` and ``live`` rows so the admin Models
    page can render every registered model for renaming.
    """
    if not _table_has_rows(MODELS_TABLE):
        seed_from_config()

    cursor = get_cursor()
    sql = f"""
        SELECT
            m.code,
            m.display_name,
            COALESCE(NULLIF(m.provider_code, ''), p.provider_code, m.required_api_key) AS provider_code,
            COALESCE(p.permission_key, m.permission_key) AS permission_key,
            COALESCE(p.required_api_key, m.required_api_key) AS required_api_key,
            m.is_default,
            m.model_purposes
        FROM {MODELS_TABLE} AS m
        LEFT JOIN {PROVIDERS_TABLE} AS p
            ON p.provider_code = COALESCE(NULLIF(m.provider_code, ''), m.required_api_key)
        WHERE m.is_active = TRUE
          AND LOWER(m.code) NOT IN ('openai', 'assemblyai', 'openrouter', 'whisper', 'gpt-4o-transcribe-diarize')
    """
    params: List[str] = []
    purpose = str(model_purpose or "").strip().lower()
    if purpose in {'transcription', 'live'}:
        sql += " AND FIND_IN_SET(%s, m.model_purposes)"
        params.append(purpose)
    sql += " ORDER BY sort_order ASC, display_name ASC"
    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall() or []
    models: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    for row in rows:
        model = _row_to_model(row)
        model_key = model["model_key"]
        if not model["code"] or model_key in seen_keys:
            continue
        seen_keys.add(model_key)
        models.append(model)
    return models


def get_live_models(key_status: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Return the de-duplicated live transcription model catalog for UI consumers."""
    live_models: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()

    def append_model(code: str, display_name: Optional[str] = None, provider: Optional[str] = None) -> None:
        normalized_code = str(code or "").strip()
        provider_code = str(provider or ("openrouter" if "/" in normalized_code else "openai")).strip().lower()
        if (
            not normalized_code
            or normalized_code.casefold() in PROVIDER_ONLY_MODEL_CODES
            or normalized_code.casefold() in DEPRECATED_MODEL_CODES
        ):
            return
        model_key = make_model_key(provider_code, normalized_code)
        if model_key in seen_keys:
            return
        seen_keys.add(model_key)
        live_models.append({
            "code": normalized_code,
            "model_key": model_key,
            "display_name": display_name or normalized_code,
            "provider": provider_code,
            "provider_code": provider_code,
            "required_api_key": provider_code,
        })

    try:
        cursor = get_cursor()
        cursor.execute(
            f"""
            SELECT
                m.code,
                m.display_name,
                COALESCE(NULLIF(m.provider_code, ''), p.provider_code, m.required_api_key) AS provider_code,
                COALESCE(p.required_api_key, m.required_api_key) AS required_api_key
            FROM {MODELS_TABLE} AS m
            LEFT JOIN {PROVIDERS_TABLE} AS p
                ON p.provider_code = COALESCE(NULLIF(m.provider_code, ''), m.required_api_key)
            WHERE m.is_active = TRUE
              AND FIND_IN_SET('live', m.model_purposes)
            ORDER BY m.sort_order ASC, m.display_name ASC
            """
        )
        for row in cursor.fetchall() or []:
            append_model(
                row["code"],
                row["display_name"],
                row.get("provider_code") or row.get("required_api_key"),
            )
    except MySQLError as err:
        logger.warning("[Catalog] Failed to load live models from catalog: %s", err, exc_info=True)

    # Keep OpenRouter live slugs contributed by saved keys as a fallback for
    # legacy providers whose catalog rows were never registered.
    status = key_status or {}
    provider_keys = status.get("provider_keys") or {}
    for entry in provider_keys.get("openrouter", []) or []:
        if not isinstance(entry, dict):
            continue
        purposes = entry.get("model_purposes") or []
        if isinstance(purposes, str):
            purposes = purposes.split(",")
        if "live" not in purposes:
            continue
        append_model(
            str(entry.get("model_slug") or entry.get("model_name") or "").strip(),
            provider="openrouter",
        )

    return live_models


def _model_purposes(entry: Dict[str, Any]) -> set[str]:
    """Return normalized purposes for a saved key status entry."""
    raw_purposes = entry.get("model_purposes")
    if isinstance(raw_purposes, str):
        raw_purposes = raw_purposes.split(",")
    if not isinstance(raw_purposes, (list, tuple, set)):
        return set()
    return {
        str(purpose).strip().lower()
        for purpose in raw_purposes
        if str(purpose).strip().lower() in {"transcription", "llm", "live"}
    }


def _is_transcription_key(entry: Dict[str, Any]) -> bool:
    """Keep legacy entries while excluding keys explicitly scoped elsewhere."""
    purposes = _model_purposes(entry)
    return not purposes or "transcription" in purposes


def _candidate_key_names(provider: str, required_key: str) -> List[str]:
    """Return the key-status names that can serve a catalog model.

    The names are used to find explicit model rows. Provider booleans and
    provider-wide rows are intentionally not sufficient to unlock a model.
    """
    names = [provider, required_key]
    return [name for name in names if name]


def _key_entries_for_model(
    model: Dict[str, Optional[str]],
    status: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Collect key metadata for a catalog model without duplicating aliases."""
    provider = _model_provider(model)
    required_key = str(model.get("required_api_key") or "").strip().lower()
    provider_keys = status.get("provider_keys") or {}
    candidate_names = _candidate_key_names(provider, required_key)

    entries: List[Dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in candidate_names:
        if not candidate:
            continue
        raw_entries = list(provider_keys.get(candidate) or [])
        legacy_entries = status.get(f"{candidate}_keys")
        if isinstance(legacy_entries, list):
            raw_entries.extend(legacy_entries)
        if candidate == "openrouter":
            raw_entries.extend(status.get("openrouter_keys") or [])
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("model_name") or entry.get("model_slug") or "").strip()
            purposes = ",".join(sorted(_model_purposes(entry)))
            identity = (name, str(bool(entry.get("provider_wide"))), purposes)
            if identity in seen:
                continue
            seen.add(identity)
            entries.append(entry)

    # Single-user deployments expose provider availability as a boolean because
    # the key comes from the environment rather than user_api_keys. Treat that
    # boolean as a provider-wide credential, but only when no structured
    # provider-key map exists; structured multi-user entries must still honour
    # their purpose scope and exact model names.
    if not entries and "provider_keys" not in status and status.get(provider):
        entries.append({
            "model_name": provider,
            "provider_wide": True,
            "model_purposes": ["transcription"],
        })
    return entries


def _model_provider(model: Dict[str, Optional[str]]) -> str:
    """Return the explicit provider for a catalog model."""
    return str(model.get("provider_code") or model.get("required_api_key") or "").strip().lower()


def expand_models_for_ui(
    models: List[Dict[str, Optional[str]]],
    key_status: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Optional[str]]]:
    """Return one selectable entry per catalog model or configured OpenRouter slug.

    The database catalog contains the canonical transcription models. A saved
    provider key is only allowed to expand the matching catalog model; otherwise
    every OpenAI key would be copied onto every OpenAI transcription option.
    OpenRouter remains the one provider whose user-entered vendor/model slugs
    are selectable additions to the catalog.

    Catalog models without a usable provider key are omitted entirely: on a
    fresh install (no keys anywhere) this produces an empty list, which is the
    first-run signal that drives admins to Manage API Keys. ``key_status`` is
    the per-provider boolean map the single/multi-user builders produce
    (``{"openai": True, ..., "provider_keys": {...}}``); test callers may pass
    a bare ``{"provider_keys": {...}}`` dict.
    """
    status = key_status or {}
    expanded: List[Dict[str, Optional[str]]] = []
    seen_entries: set[tuple[str, str]] = set()

    def append_once(entry: Dict[str, Optional[str]]) -> None:
        code = str(entry.get("code") or "").strip()
        provider = _model_provider(entry)
        model_key = str(entry.get("model_key") or make_model_key(provider, code)).strip()
        identity = (model_key, str(entry.get("model_name") or "").strip())
        if code and identity not in seen_entries:
            entry["model_key"] = model_key
            seen_entries.add(identity)
            expanded.append(entry)

    for model in models:
        catalog_code = str(model.get("code") or "").strip()
        if not catalog_code:
            continue
        if catalog_code.casefold() in PROVIDER_ONLY_MODEL_CODES or catalog_code.casefold() in DEPRECATED_MODEL_CODES:
            continue
        provider = _model_provider(model)
        entries = [
            entry
            for entry in _key_entries_for_model(model, status)
            if _is_transcription_key(entry)
        ]

        required_key = str(model.get("required_api_key") or provider).strip().lower()
        if not required_key:
            # Models without a provider credential remain available.
            append_once(dict(model))
            continue
        matching_entry = next(
            (
                entry
                for entry in entries
                if entry.get("provider_wide")
                or str(entry.get("model_name") or entry.get("model_slug") or "").strip() == catalog_code
            ),
            None,
        )
        if matching_entry:
            append_once({
                **model,
                "provider_code": provider,
                "model_name": catalog_code,
                "model_slug": catalog_code if provider == "openrouter" else None,
                "display_name": model.get("display_name") or catalog_code,
            })

    return expanded


def build_model_options(
    catalog_models: List[Dict[str, Optional[str]]],
    key_status: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Optional[str]]]:
    """Return the canonical de-duplicated transcription model option list.

    Every provider-local model identity appears at most once. OpenRouter entries
    stay per slug so each configured transcription model remains selectable.
    All dropdown consumers (home page, user settings modal, admin role form,
    costs page) must source their options from here so the lists cannot drift
    apart.
    """
    expanded = expand_models_for_ui(catalog_models, key_status)

    # Sort only after provider expansion and deduplication. The display name is
    # the user-visible contract; catalog sort_order must not override A–Z.
    options: List[Dict[str, Optional[str]]] = []
    seen: set[str] = set()
    for entry in expanded:
        model_key = str(
            entry.get("model_key")
            or make_model_key(_model_provider(entry), entry.get("code"))
        ).strip()
        if not model_key or model_key in seen:
            continue
        seen.add(model_key)
        entry["model_key"] = model_key
        options.append(entry)
    return sorted(
        options,
        key=lambda entry: (
            str(entry.get("display_name") or entry.get("code") or "").casefold(),
            str(entry.get("code") or "").casefold(),
            _model_provider(entry),
        ),
    )


def get_model_by_code(
    code: str,
    provider_code: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve a canonical ``provider:model`` or a legacy bare model code.

    Bare values remain readable for old users, roles, jobs, and API clients.
    New callers should persist the returned ``model_key`` so two providers can
    safely expose the same provider-local model code.
    """
    raw_reference = str(code or "").strip()
    if not raw_reference:
        return None
    provider, local_code = split_model_reference(raw_reference, provider_code)
    if not local_code:
        return None

    cursor = get_cursor()
    sql = f"""
        SELECT
            m.code,
            m.display_name,
            COALESCE(NULLIF(m.provider_code, ''), p.provider_code, m.required_api_key) AS provider_code,
            COALESCE(p.permission_key, m.permission_key) AS permission_key,
            COALESCE(p.required_api_key, m.required_api_key) AS required_api_key,
            m.is_default,
            m.is_active,
            m.model_purposes
        FROM {MODELS_TABLE} AS m
        LEFT JOIN {PROVIDERS_TABLE} AS p
            ON p.provider_code = COALESCE(NULLIF(m.provider_code, ''), m.required_api_key)
        WHERE m.code = %s
    """
    params: List[str] = [local_code]
    if provider:
        sql += " AND COALESCE(NULLIF(m.provider_code, ''), p.provider_code, m.required_api_key) = %s"
        params.append(provider)
    sql += " ORDER BY m.is_active DESC, m.is_default DESC, m.id ASC LIMIT 2"
    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall() or []
    if not rows:
        return None
    if not provider and len(rows) > 1:
        logger.warning(
            "[Catalog] Bare model reference '%s' matched multiple providers; "
            "use the canonical provider:model key.",
            raw_reference,
        )
        return None
    return _row_to_model(rows[0])


def get_default_model_code() -> Optional[str]:
    models = get_active_models()
    for model in models:
        if model.get("is_default"):
            return model.get("model_key") or model["code"]
    # Fallback to the first active model if no explicit default is set.
    if models:
        return models[0].get("model_key") or models[0]["code"]
    return None


def get_active_languages() -> List[Dict[str, Optional[str]]]:
    if not _table_has_rows(LANGUAGES_TABLE):
        seed_from_config()

    cursor = get_cursor()
    sql = f"""
        SELECT code, display_name, is_default
        FROM {LANGUAGES_TABLE}
        WHERE is_active = TRUE
        ORDER BY sort_order ASC, display_name ASC
    """
    cursor.execute(sql)
    rows = cursor.fetchall() or []
    languages: List[Dict[str, Optional[str]]] = []
    for row in rows:
        languages.append(
            {
                "code": row["code"],
                "display_name": row["display_name"],
                "is_default": bool(row.get("is_default", False)),
            }
        )
    return languages


def get_language_map() -> Dict[str, str]:
    """
    Returns a dict mapping language code to display name for active languages.
    """
    return {lang["code"]: lang["display_name"] for lang in get_active_languages()}


def get_default_language_code() -> Optional[str]:
    languages = get_active_languages()
    for lang in languages:
        if lang.get("is_default"):
            return lang["code"]
    if languages:
        return languages[0]["code"]
    return None


# ----- Internal Helpers -----

def _ensure_providers_table(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PROVIDERS_TABLE} (
            provider_code VARCHAR(80) PRIMARY KEY,
            display_name VARCHAR(120) NOT NULL,
            required_api_key VARCHAR(80) NOT NULL,
            permission_key VARCHAR(120) DEFAULT NULL,
            client_kind VARCHAR(80) NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
    )


def _seed_providers_from_config() -> None:
    """Upsert the fixed provider adapters without creating model rows."""
    configured = current_app.config.get("TRANSCRIPTION_PROVIDERS") or _PROVIDER_METADATA.keys()
    provider_codes = {
        str(provider).strip().lower()
        for provider in configured
        if str(provider).strip().lower() in _PROVIDER_METADATA
    }
    provider_codes.update(_PROVIDER_METADATA.keys())

    cursor = get_cursor()
    for provider_code in sorted(provider_codes):
        metadata = _PROVIDER_METADATA[provider_code]
        cursor.execute(
            f"""
            INSERT INTO {PROVIDERS_TABLE} (
                provider_code, display_name, required_api_key, permission_key,
                client_kind, is_active
            ) VALUES (%s, %s, %s, %s, %s, TRUE)
            ON DUPLICATE KEY UPDATE
                display_name = VALUES(display_name),
                required_api_key = VALUES(required_api_key),
                permission_key = VALUES(permission_key),
                client_kind = VALUES(client_kind),
                is_active = TRUE
            """,
            (
                provider_code,
                metadata["display_name"],
                metadata["required_api_key"],
                metadata["permission_key"],
                metadata["client_kind"],
            ),
        )
    get_db().commit()


def _normalize_legacy_model_rows() -> None:
    """Backfill provider links and retire provider-shaped catalog rows.

    This is deliberately idempotent and keeps old rows for historical joins;
    it only changes whether a row is an active selectable model. The legacy
    AssemblyAI row is converted to the real ``universal`` model when needed.
    """
    cursor = get_cursor()

    # Older rows stored the provider only in required_api_key. Slash-containing
    # model identifiers are the legacy OpenRouter convention.
    cursor.execute(
        f"""
        UPDATE {MODELS_TABLE}
        SET provider_code = LOWER(required_api_key)
        WHERE (provider_code IS NULL OR TRIM(provider_code) = '')
          AND LOWER(COALESCE(required_api_key, '')) IN ('openai', 'assemblyai', 'openrouter')
        """
    )
    cursor.execute(
        f"""
        UPDATE {MODELS_TABLE}
        SET provider_code = 'openrouter'
        WHERE (provider_code IS NULL OR TRIM(provider_code) = '')
          AND code LIKE '%/%'
        """
    )
    cursor.execute(
        f"""
        UPDATE {MODELS_TABLE} AS m
        INNER JOIN {PROVIDERS_TABLE} AS p
            ON p.provider_code = m.provider_code
        SET m.permission_key = COALESCE(m.permission_key, p.permission_key),
            m.required_api_key = COALESCE(m.required_api_key, p.required_api_key)
        WHERE m.provider_code IN ('openai', 'assemblyai', 'openrouter')
        """
    )

    # ``assemblyai`` used to be a provider-shaped model row. Preserve a real
    # Universal model row for existing installations and keep the old row only
    # as inactive history.
    cursor.execute(
        f"""
        UPDATE {MODELS_TABLE}
        SET provider_code = 'assemblyai',
            permission_key = 'use_api_assemblyai',
            required_api_key = 'assemblyai',
            display_name = CASE
                WHEN LOWER(TRIM(display_name)) IN ('assemblyai', 'universal')
                THEN 'AssemblyAI Universal'
                ELSE display_name
            END
        WHERE LOWER(code) = 'universal'
          AND (provider_code IS NULL OR TRIM(provider_code) = ''
               OR LOWER(COALESCE(required_api_key, '')) = 'assemblyai')
        """
    )
    cursor.execute(
        f"""
        INSERT INTO {MODELS_TABLE} (
            code, provider_code, display_name, permission_key, required_api_key,
            sort_order, is_active, is_default, model_purposes
        )
        SELECT 'universal', 'assemblyai', 'AssemblyAI Universal',
               'use_api_assemblyai', 'assemblyai', 0, TRUE, FALSE, 'transcription'
        FROM DUAL
        WHERE EXISTS (
            SELECT 1 FROM {MODELS_TABLE}
            WHERE LOWER(code) = 'assemblyai'
        )
          AND NOT EXISTS (
            SELECT 1 FROM {MODELS_TABLE}
            WHERE LOWER(code) = 'universal'
          )
        """
    )
    cursor.execute(
        f"""
        UPDATE {MODELS_TABLE}
        SET is_active = FALSE
        WHERE LOWER(code) IN (
            'openai', 'assemblyai', 'openrouter', 'whisper',
            'gpt-4o-transcribe-diarize'
        )
        """
    )

    _normalize_persisted_model_references(cursor)
    _ensure_model_identity_index(cursor)
    get_db().commit()


def _ensure_model_identity_index(cursor) -> None:
    """Replace the legacy global code uniqueness with provider-local identity."""
    cursor.execute(f"SHOW COLUMNS FROM {MODELS_TABLE} LIKE 'code'")
    code_column = cursor.fetchone()
    code_type = ""
    if isinstance(code_column, dict):
        code_type = str(code_column.get("Type") or "")
    elif isinstance(code_column, (tuple, list)) and len(code_column) > 1:
        code_type = str(code_column[1])
    if "255" not in code_type:
        cursor.execute(
            f"ALTER TABLE {MODELS_TABLE} MODIFY COLUMN code VARCHAR(255) NOT NULL"
        )

    cursor.execute(f"SHOW INDEX FROM {MODELS_TABLE}")
    index_rows = cursor.fetchall() or []
    index_columns: Dict[str, List[str]] = {}
    unique_indexes: set[str] = set()
    for row in index_rows:
        if isinstance(row, dict):
            name = str(row.get("Key_name") or "")
            column = str(row.get("Column_name") or "")
            non_unique = row.get("Non_unique")
        else:
            name = str(row[2]) if len(row) > 2 else ""
            column = str(row[4]) if len(row) > 4 else ""
            non_unique = row[1] if len(row) > 1 else 1
        if not name:
            continue
        index_columns.setdefault(name, []).append(column)
        if not bool(non_unique):
            unique_indexes.add(name)

    for name, columns in index_columns.items():
        if name != "PRIMARY" and name in unique_indexes and columns == ["code"]:
            safe_name = "".join(char for char in name if char.isalnum() or char in "_$")
            if safe_name:
                cursor.execute(f"ALTER TABLE {MODELS_TABLE} DROP INDEX `{safe_name}`")

    cursor.execute(
        f"SHOW INDEX FROM {MODELS_TABLE} WHERE Key_name = 'uq_transcription_provider_model'"
    )
    if not (cursor.fetchall() or []):
        cursor.execute(
            f"ALTER TABLE {MODELS_TABLE} ADD UNIQUE INDEX uq_transcription_provider_model (provider_code, code)"
        )


def _normalize_persisted_model_references(cursor) -> None:
    """Repair current defaults without rewriting historical transcription jobs."""
    for table_name in ("users", "roles"):
        try:
            cursor.execute(
                f"""
                UPDATE {table_name}
                SET default_transcription_model = 'assemblyai:universal'
                WHERE LOWER(COALESCE(default_transcription_model, '')) IN ('assemblyai', 'universal')
                """
            )
            cursor.execute(
                f"""
                UPDATE {table_name} AS target
                INNER JOIN {MODELS_TABLE} AS model
                    ON model.code = target.default_transcription_model
                   AND model.is_active = TRUE
                SET target.default_transcription_model = CONCAT(model.provider_code, ':', model.code)
                WHERE target.default_transcription_model NOT LIKE '%:%'
                  AND model.provider_code IS NOT NULL
                  AND (
                      SELECT COUNT(*) FROM {MODELS_TABLE} AS candidate
                      WHERE candidate.code = target.default_transcription_model
                        AND candidate.is_active = TRUE
                  ) = 1
                """
            )
            cursor.execute(
                f"""
                UPDATE {table_name}
                SET default_transcription_model = NULL
                WHERE LOWER(COALESCE(default_transcription_model, '')) IN (
                    'openrouter', 'whisper', 'gpt-4o-transcribe-diarize'
                )
                """
            )
            cursor.execute(
                f"""
                UPDATE {table_name}
                SET default_live_transcription_model = 'assemblyai:universal'
                WHERE LOWER(COALESCE(default_live_transcription_model, '')) IN ('assemblyai', 'universal')
                """
            )
            cursor.execute(
                f"""
                UPDATE {table_name} AS target
                INNER JOIN {MODELS_TABLE} AS model
                    ON model.code = target.default_live_transcription_model
                   AND model.is_active = TRUE
                   AND FIND_IN_SET('live', model.model_purposes)
                SET target.default_live_transcription_model = CONCAT(model.provider_code, ':', model.code)
                WHERE target.default_live_transcription_model NOT LIKE '%:%'
                  AND model.provider_code IS NOT NULL
                  AND (
                      SELECT COUNT(*) FROM {MODELS_TABLE} AS candidate
                      WHERE candidate.code = target.default_live_transcription_model
                        AND candidate.is_active = TRUE
                        AND FIND_IN_SET('live', candidate.model_purposes)
                  ) = 1
                """
            )
        except MySQLError as err:
            if getattr(err, "errno", None) == 1146:
                continue
            raise

    # Preserve a legacy AssemblyAI price by copying it to the canonical model
    # key only when the new key does not already have an explicit price.
    try:
        cursor.execute(
            """
            INSERT IGNORE INTO pricing (catalog_code, price, item_type)
            SELECT 'assemblyai:universal', price, item_type
            FROM pricing
            WHERE catalog_code IN ('assemblyai', 'universal')
              AND item_type = 'transcription'
            """
        )
    except MySQLError as err:
        if getattr(err, "errno", None) != 1146:
            raise


def _sync_models_from_saved_keys() -> None:
    """Restore dynamic model rows that predate the normalized catalog.

    The key table already stores the provider and provider-local model slug, so
    this covers previously saved OpenAI, AssemblyAI, and OpenRouter models
    without requiring a hard-coded model list.
    """
    cursor = get_cursor()
    try:
        cursor.execute(
            """
            SELECT DISTINCT provider_code, TRIM(model_slug) AS model_slug, model_purposes
            FROM user_api_keys
            WHERE model_slug IS NOT NULL AND TRIM(model_slug) <> ''
            """
        )
        rows = cursor.fetchall() or []
    except MySQLError as err:
        if getattr(err, "errno", None) == 1146:
            logger.info("[Catalog] user_api_keys is not available yet; skipping model backfill.")
            return
        raise

    for row in rows:
        provider = str(row.get("provider_code") or "").strip().lower()
        model_slug = str(row.get("model_slug") or "").strip()
        if provider not in _PROVIDER_METADATA or not model_slug:
            continue
        raw_model_purposes = row.get("model_purposes")
        if raw_model_purposes is None or not str(raw_model_purposes).strip():
            purposes = {"transcription"}
        else:
            purposes = {
                purpose.strip().lower()
                for purpose in str(raw_model_purposes).split(",")
                if purpose.strip().lower() in VALID_MODEL_PURPOSES
            }
            if not purposes:
                # LLM-only (or otherwise unsupported) keys do not belong in
                # the transcription catalog.
                continue
        # One registration carrying the full purpose set: purposes accumulate
        # on the catalog row, so a key saved for both file transcription and
        # live keeps the model listed in both dropdowns after every restart.
        register_model_from_provider(
            provider=provider,
            code=model_slug,
            display_name=model_slug,
            model_purpose=canonicalize_model_purposes(purposes),
        )


def _ensure_models_table(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MODELS_TABLE} (
            id INT PRIMARY KEY AUTO_INCREMENT,
            code VARCHAR(255) NOT NULL,
            provider_code VARCHAR(80) DEFAULT NULL,
            display_name VARCHAR(120) NOT NULL,
            permission_key VARCHAR(120) DEFAULT NULL,
            required_api_key VARCHAR(80) DEFAULT NULL,
            sort_order INT NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            model_purposes VARCHAR(64) NOT NULL DEFAULT 'transcription',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_transcription_provider_model (provider_code, code)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
    )

    cursor.execute(f"SHOW COLUMNS FROM {MODELS_TABLE} LIKE 'provider_code'")
    if cursor.fetchone() is None:
        logger.info("[DB:Catalog] Adding 'provider_code' column to '%s'.", MODELS_TABLE)
        cursor.execute(
            f"ALTER TABLE {MODELS_TABLE} ADD COLUMN provider_code VARCHAR(80) DEFAULT NULL AFTER code"
        )

    cursor.execute(
        f"SHOW INDEX FROM {MODELS_TABLE} WHERE Key_name = 'idx_transcription_models_provider'"
    )
    if cursor.fetchone() is None:
        cursor.execute(
            f"ALTER TABLE {MODELS_TABLE} ADD INDEX idx_transcription_models_provider (provider_code)"
        )

    # Migration-safe: convert the legacy single-valued model_purpose column
    # into the model_purposes comma set (mirrors user_api_keys.model_purposes
    # and migrations/V20260821_2__catalog_model_purposes_set.py).
    cursor.execute(
        f"SHOW COLUMNS FROM {MODELS_TABLE} LIKE 'model_purpose'"
    )
    legacy_purpose_present = cursor.fetchone() is not None

    cursor.execute(
        f"SHOW COLUMNS FROM {MODELS_TABLE} LIKE 'model_purposes'"
    )
    purposes_present = cursor.fetchone() is not None

    if not purposes_present:
        logger.info("[DB:Catalog] Adding 'model_purposes' set column to '%s'.", MODELS_TABLE)
        cursor.execute(
            f"""
            ALTER TABLE {MODELS_TABLE}
            ADD COLUMN model_purposes VARCHAR(64) NOT NULL DEFAULT 'transcription'
            AFTER is_default
            """
        )

    if legacy_purpose_present:
        # Fold each legacy value into the new set, then drop the old column.
        cursor.execute(f"SELECT id, model_purpose, model_purposes FROM {MODELS_TABLE}")
        for row in cursor.fetchall() or []:
            if isinstance(row, dict):
                row_id = row.get("id")
                merged = canonicalize_model_purposes(
                    [row.get("model_purpose"), row.get("model_purposes")]
                )
            else:
                row_id = row[0]
                merged = canonicalize_model_purposes([row[1], row[2]])
            cursor.execute(
                f"UPDATE {MODELS_TABLE} SET model_purposes = %s WHERE id = %s",
                (merged, row_id),
            )
        cursor.execute(
            f"ALTER TABLE {MODELS_TABLE} DROP COLUMN model_purpose"
        )
        logger.info("[DB:Catalog] Migrated legacy 'model_purpose' into 'model_purposes'.")


def _ensure_languages_table(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {LANGUAGES_TABLE} (
            id INT PRIMARY KEY AUTO_INCREMENT,
            code VARCHAR(20) NOT NULL UNIQUE,
            display_name VARCHAR(120) NOT NULL,
            sort_order INT NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
    )


def _heal_stale_purpose_sets() -> None:
    """Startup pass: shrink catalog purpose sets that outlived their keys.

    ``register_model_from_provider`` accumulates purposes, so a key that lost
    a purpose while the app was running leaves the catalog row behind until
    the next restart. This recomputes every registered model row from the
    surviving key rows, which also repairs rows left over from incidents
    before per-save reconciliation existed.
    """
    cursor = get_cursor()
    try:
        cursor.execute(
            f"""
            SELECT provider_code, code
            FROM {MODELS_TABLE}
            WHERE is_active = TRUE
              AND LOWER(code) NOT IN ('openai', 'assemblyai', 'openrouter', 'whisper',
                                      'gpt-4o-transcribe-diarize')
            """
        )
        rows = cursor.fetchall() or []
    except MySQLError as err:
        logger.warning("[Catalog] Purpose healing skipped: %s", err, exc_info=True)
        return

    for row in rows:
        provider = str(row.get("provider_code") or "").strip().lower() if isinstance(row, dict) else ""
        code = str(row.get("code") or "").strip() if isinstance(row, dict) else ""
        if not provider or not code:
            continue
        reconcile_model_purposes(provider, code)


def register_model_from_provider(
    provider: str,
    code: str,
    display_name: Optional[str] = None,
    model_purpose: str = 'transcription',
) -> None:
    """Register one real provider-local model in the shared catalog.

    Provider metadata is stored in ``transcription_providers_catalog``; this
    function only creates a model row. Saving an API key is one way to invoke
    it, and future admin/configuration/discovery flows can use the same path.

    Purposes accumulate on the ``model_purposes`` set (like
    ``user_api_keys.model_purposes``): registering the same model for another
    purpose merges into the existing row instead of overwriting it, so a live
    key save can never remove the model from transcription dropdowns (or vice
    versa).
    """
    provider = str(provider or "").strip().lower()
    if provider not in _PROVIDER_METADATA:
        logger.warning("[Catalog] Ignoring model registration for unknown provider '%s'.", provider)
        return

    code = str(code or "").strip()
    if provider == "assemblyai" and code.casefold() == "assemblyai":
        # Legacy provider-wide AssemblyAI rows represented Universal.
        code = "universal"
        if not display_name or str(display_name).strip().casefold() in {"assemblyai", "universal"}:
            display_name = "AssemblyAI Universal"
    if not code:
        logger.warning("[Catalog] Ignoring model registration with empty code (provider '%s').", provider)
        return
    if code.casefold() in PROVIDER_ONLY_MODEL_CODES or code.casefold() in DEPRECATED_MODEL_CODES:
        logger.warning("[Catalog] Ignoring provider/retired model code '%s'.", code)
        return

    requested_purposes = [
        item.strip().lower()
        for item in str(model_purpose or 'transcription').split(",")
        if item.strip()
    ]
    if not requested_purposes or any(
        item not in VALID_MODEL_PURPOSES for item in requested_purposes
    ):
        logger.warning("[Catalog] Ignoring model registration with invalid purpose '%s'.", model_purpose)
        return

    metadata = _PROVIDER_METADATA[provider]
    purposes = canonicalize_model_purposes(requested_purposes)
    cursor = get_cursor()
    cursor.execute(
        f"""
        INSERT INTO {MODELS_TABLE} (
            code, provider_code, display_name, permission_key, required_api_key,
            sort_order, is_active, is_default, model_purposes
        ) VALUES (%s, %s, %s, %s, %s, 0, 1, 0, %s)
        ON DUPLICATE KEY UPDATE
            provider_code = VALUES(provider_code),
            permission_key = VALUES(permission_key),
            required_api_key = VALUES(required_api_key),
            is_active = 1,
            model_purposes = CONCAT_WS(
                ',',
                IF(
                    FIND_IN_SET('transcription', model_purposes)
                    OR FIND_IN_SET('transcription', VALUES(model_purposes)),
                    'transcription',
                    NULL
                ),
                IF(
                    FIND_IN_SET('live', model_purposes)
                    OR FIND_IN_SET('live', VALUES(model_purposes)),
                    'live',
                    NULL
                )
            )
        """,
        (
            code,
            provider,
            _coerce_string(display_name) or code,
            metadata.get("permission_key"),
            metadata.get("required_api_key"),
            purposes,
        ),
    )
    get_db().commit()
    logger.info("[Catalog] Registered model '%s' (provider '%s', purpose '%s').", code, provider, purposes)


def _update_model_purposes(provider: str, code: str, purposes: str) -> None:
    """Overwrite one catalog row's purpose set (reconciliation path only).

    Unlike ``register_model_from_provider`` this is a plain assignment: it is
    only called with a set recomputed from the surviving key rows.
    """
    cursor = get_cursor()
    cursor.execute(
        f"""
        UPDATE {MODELS_TABLE}
        SET model_purposes = %s
        WHERE provider_code = %s AND code = %s
        """,
        (purposes, provider, code),
    )
    get_db().commit()


def reconcile_model_purposes(provider: str, code: str) -> None:
    """Recompute a model's catalog purpose set from the saved keys.

    ``register_model_from_provider`` only ever accumulates purposes so one
    key save can never clobber another purpose. The flip side is that a key
    losing a purpose (deleted, or re-saved for one purpose only) must shrink
    the catalog row actively, or the model lingers in the live dropdown
    forever. This recomputes the set from every remaining key row for the
    identity; with no keys left it falls back to the transcription default.
    """
    provider = str(provider or "").strip().lower()
    code = str(code or "").strip()
    if provider not in _PROVIDER_METADATA or not code:
        return
    if provider == "assemblyai" and code.casefold() == "assemblyai":
        # Legacy provider-wide AssemblyAI identity is stored as universal.
        # Normalized before the provider/retired-code guard, mirroring
        # register_model_from_provider.
        code = "universal"
    if code.casefold() in PROVIDER_ONLY_MODEL_CODES or code.casefold() in DEPRECATED_MODEL_CODES:
        return

    purposes: set[str] = set()
    try:
        cursor = get_cursor()
        cursor.execute(
            """
            SELECT DISTINCT model_purposes
            FROM user_api_keys
            WHERE provider_code = %s
              AND model_slug = %s
              AND model_purposes IS NOT NULL
              AND TRIM(model_purposes) <> ''
            """,
            (provider, code),
        )
        for row in cursor.fetchall() or []:
            raw = row.get("model_purposes") if isinstance(row, dict) else row[0]
            for item in str(raw or "").split(","):
                purpose = item.strip().lower()
                if purpose in VALID_MODEL_PURPOSES:
                    purposes.add(purpose)
    except MySQLError as err:
        logger.warning(
            "[Catalog] Could not reconcile purposes for %s:%s: %s",
            provider,
            code,
            err,
            exc_info=True,
        )
        return

    if not purposes:
        purposes = {DEFAULT_MODEL_PURPOSE}

    canonical = canonicalize_model_purposes(purposes)
    try:
        _update_model_purposes(provider, code, canonical)
        logger.info(
            "[Catalog] Reconciled model '%s' (provider '%s') to purposes '%s'.",
            code,
            provider,
            canonical,
        )
    except MySQLError as err:
        get_db().rollback()
        logger.warning(
            "[Catalog] Could not store reconciled purposes for %s:%s: %s",
            provider,
            code,
            err,
            exc_info=True,
        )


def rename_model(code: str, display_name: str) -> bool:
    """Rename one provider-local catalog model by canonical or legacy key."""
    reference = str(code or "").strip()
    cleaned = _coerce_string(display_name)
    if not reference or not cleaned:
        return False

    model = get_model_by_code(reference)
    if not model:
        return False
    provider = str(model.get("provider_code") or "").strip().lower()
    local_code = str(model.get("code") or "").strip()
    if not local_code:
        return False

    cursor = get_cursor()
    if provider:
        cursor.execute(
            f"""
            UPDATE {MODELS_TABLE}
            SET display_name = %s
            WHERE code = %s
              AND COALESCE(NULLIF(provider_code, ''), required_api_key) = %s
            """,
            (cleaned, local_code, provider),
        )
    else:
        cursor.execute(
            f"UPDATE {MODELS_TABLE} SET display_name = %s WHERE code = %s",
            (cleaned, local_code),
        )
    get_db().commit()
    return cursor.rowcount > 0


def _seed_languages_from_config() -> None:
    config = current_app.config
    codes: List[str] = config.get("SUPPORTED_LANGUAGE_CODES", [])
    names_map: Dict[str, str] = config.get("SUPPORTED_LANGUAGE_NAMES", {})
    default_code: Optional[str] = config.get("DEFAULT_LANGUAGE")

    ordered_codes: List[str] = []

    def _append_code(code: Optional[str]) -> None:
        if code and code not in ordered_codes:
            ordered_codes.append(code)

    if 'auto' in names_map:
        _append_code('auto')

    for code in codes:
        _append_code(code)

    for code in names_map.keys():
        if code != 'auto':
            _append_code(code)

    _append_code(default_code)

    if not ordered_codes:
        logger.warning("[Catalog] No languages available to seed.")
        return

    for index, code in enumerate(ordered_codes):
        display_name = _coerce_string(names_map.get(code, code))
        sort_order = (index + 1) * 10
        _upsert_language(
            code=code,
            display_name=display_name,
            sort_order=sort_order,
            is_active=True,
            is_default=(code == default_code),
        )

    if default_code:
        _set_default_language(default_code)


_ALLOWED_CATALOG_TABLES = {MODELS_TABLE, PROVIDERS_TABLE, LANGUAGES_TABLE}

def _table_has_rows(table_name: str) -> bool:
    if table_name not in _ALLOWED_CATALOG_TABLES:
        raise ValueError(f"Unexpected table: {table_name}")
    cursor = get_cursor()
    try:
        cursor.execute(f"SELECT 1 FROM {table_name} LIMIT 1")
    except MySQLError as err:
        if getattr(err, "errno", None) == 1146:  # Table doesn't exist
            logger.info(f"[Catalog] Table '{table_name}' missing. Re-initializing catalog tables.")
            init_db_command()
            cursor = get_cursor()
            cursor.execute(f"SELECT 1 FROM {table_name} LIMIT 1")
        else:
            raise
    return cursor.fetchone() is not None





def _upsert_language(
    *,
    code: str,
    display_name: str,
    sort_order: int,
    is_active: bool,
    is_default: bool,
) -> None:
    sql = f"""
        INSERT INTO {LANGUAGES_TABLE} (code, display_name, sort_order, is_active, is_default)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            display_name = VALUES(display_name),
            sort_order = VALUES(sort_order),
            is_active = VALUES(is_active),
            is_default = VALUES(is_default)
    """
    cursor = get_cursor()
    cursor.execute(
        sql,
        (
            code,
            _coerce_string(display_name),
            sort_order,
            int(bool(is_active)),
            int(bool(is_default)),
        ),
    )
    get_db().commit()


def _set_default_language(default_code: str) -> None:
    cursor = get_cursor()
    cursor.execute(
        f"UPDATE {LANGUAGES_TABLE} SET is_default = CASE WHEN code = %s THEN TRUE ELSE FALSE END",
        (default_code,),
    )
    get_db().commit()
def _coerce_string(value: Optional[str]) -> Optional[str]:
    """
    Ensures SQL parameters receive plain Python strings (not LazyString instances).
    """
    if value is None:
        return None
    return str(value)
