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
LANGUAGES_TABLE = "transcription_languages_catalog"

# Provider-level metadata for the fixed transcription pipeline providers.
# Models are NOT pre-seeded: rows are registered at runtime when an admin/user
# saves an API key with a model name. The provider list itself comes from
# config (TRANSCRIPTION_PROVIDERS) and is fixed — the admin cannot add providers.
_PROVIDER_METADATA: Dict[str, Dict[str, Optional[str]]] = {
    "assemblyai": {
        "permission_key": "use_api_assemblyai",
        "required_api_key": "assemblyai",
    },
    "openai": {
        "permission_key": "use_api_openai",
        "required_api_key": "openai",
    },
    "openrouter": {
        "permission_key": "use_api_openrouter",
        "required_api_key": "openrouter",
    },
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
        _ensure_models_table(cursor)
        _ensure_languages_table(cursor)
        get_db().commit()
    except MySQLError as err:
        get_db().rollback()
        logger.error(f"{log_prefix} Failed to initialize catalog tables: {err}", exc_info=True)
        raise

    # Seed defaults after the tables are in place.
    try:
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


def get_active_models() -> List[Dict[str, Optional[str]]]:
    """
    Returns active normal transcription models sorted by configured order.

    Models are registered at runtime from saved API keys; nothing is
    pre-seeded (see ``register_model_from_provider``). ``model_purpose``
    distinguishes normal (``transcription``) from ``live`` models.
    """
    if not _table_has_rows(MODELS_TABLE):
        seed_from_config()

    cursor = get_cursor()
    sql = f"""
        SELECT code, display_name, permission_key, required_api_key, is_default, model_purpose
        FROM {MODELS_TABLE}
        WHERE is_active = TRUE
        ORDER BY sort_order ASC, display_name ASC
    """
    cursor.execute(sql)
    rows = cursor.fetchall() or []
    models: List[Dict[str, Optional[str]]] = []
    seen_codes: set[str] = set()
    for row in rows:
        code = row["code"]
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        display_name = row["display_name"]
        purpose = str(row.get("model_purpose") or "transcription").strip().lower()
        if purpose != "transcription":
            continue
        models.append(
            {
                "code": code,
                "display_name": display_name,
                "permission_key": row.get("permission_key"),
                "required_api_key": row.get("required_api_key"),
                "is_default": bool(row.get("is_default", False)),
                "model_purpose": purpose,
            }
        )
    return models


def get_all_active_models(
    model_purpose: Optional[str] = None,
) -> List[Dict[str, Optional[str]]]:
    """Return active catalog rows, optionally filtered by ``model_purpose``.

    Unlike ``get_active_models`` (used for the normal transcription dropdown),
    this keeps both ``transcription`` and ``live`` rows so the admin Models
    page can render every registered model for renaming.
    """
    if not _table_has_rows(MODELS_TABLE):
        seed_from_config()

    cursor = get_cursor()
    sql = f"""
        SELECT code, display_name, permission_key, required_api_key, is_default, model_purpose
        FROM {MODELS_TABLE}
        WHERE is_active = TRUE
    """
    params: List[str] = []
    purpose = str(model_purpose or "").strip().lower()
    if purpose in {'transcription', 'live'}:
        sql += " AND model_purpose = %s"
        params.append(purpose)
    sql += " ORDER BY sort_order ASC, display_name ASC"
    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall() or []
    models: List[Dict[str, Optional[str]]] = []
    seen: set[str] = set()
    for row in rows:
        code = str(row.get("code") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        models.append({
            "code": code,
            "display_name": row.get("display_name"),
            "permission_key": row.get("permission_key"),
            "required_api_key": row.get("required_api_key"),
            "is_default": bool(row.get("is_default", False)),
            "model_purpose": str(row.get("model_purpose") or 'transcription').strip().lower(),
        })
    return models


def get_live_models(key_status: Optional[Dict[str, Any]] = None) -> List[Dict[str, Optional[str]]]:
    """Return the de-duplicated live transcription model catalog for UI consumers.

    Live models are registered at runtime like normal transcription models
    (``model_purpose='live'`` rows created from saved keys), so nothing is
    pre-loaded from config. Each live row's catalog ``display_name`` is used.
    """
    live_models: List[Dict[str, Optional[str]]] = []
    seen_codes: set[str] = set()

    def append_model(code: str, display_name: Optional[str] = None, provider: Optional[str] = None) -> None:
        normalized_code = str(code or "").strip()
        if not normalized_code or normalized_code in seen_codes:
            return
        seen_codes.add(normalized_code)
        live_models.append({
            "code": normalized_code,
            "display_name": display_name or normalized_code,
            "provider": provider or ("openrouter" if "/" in normalized_code else "openai"),
        })

    try:
        cursor = get_cursor()
        cursor.execute(
            f"""
            SELECT code, display_name, required_api_key
            FROM {MODELS_TABLE}
            WHERE is_active = TRUE AND model_purpose = 'live'
            ORDER BY sort_order ASC, display_name ASC
            """
        )
        for row in cursor.fetchall() or []:
            append_model(
                row["code"],
                row["display_name"],
                row.get("required_api_key"),
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
    names = [required_key, provider]
    if provider in {"whisper", "gpt-transcribe", "gpt-4o-transcribe"}:
        names.append("openai")
    if provider == "openrouter":
        names.append("openrouter")
    return [name for name in names if name]


def _key_entries_for_model(
    model: Dict[str, Optional[str]],
    status: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Collect key metadata for a catalog model without duplicating aliases."""
    provider = str(model.get("code") or "").strip().lower()
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
    return entries


def expand_models_for_ui(
    models: List[Dict[str, Optional[str]]],
    key_status: Optional[Dict[str, Any]] = None,
    fallback_openrouter_model: Optional[str] = None,
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
        model_name = str(entry.get("model_name") or entry.get("model_slug") or "").strip()
        identity = (code, model_name if code == "openrouter" else "")
        if code and identity not in seen_entries:
            seen_entries.add(identity)
            expanded.append(entry)

    for model in models:
        provider = str(model.get("code") or "").strip().lower()
        entries = [
            entry
            for entry in _key_entries_for_model(model, status)
            if _is_transcription_key(entry)
        ]

        if provider == "openrouter":
            names: List[str] = []
            for entry in entries:
                if entry.get("provider_wide"):
                    continue
                name = str(entry.get("model_name") or entry.get("model_slug") or "").strip()
                if name and name not in names:
                    names.append(name)
            if not names:
                # No specific OpenRouter model is known. The provider name is
                # not a selectable model, so no option is emitted here.
                continue
            for name in names:
                append_once({
                    **model,
                    "model_name": name,
                    "model_slug": name,
                    "display_name": name,
                })
            continue

        catalog_code = str(model.get("code") or "").strip()
        required_key = str(model.get("required_api_key") or "").strip().lower()
        if not required_key:
            # Models that do not declare a required provider key are available
            # without any saved credential.
            append_once(dict(model))
            continue
        matching_entry = next(
            (
                entry
                for entry in entries
                if not entry.get("provider_wide")
                and str(entry.get("model_name") or entry.get("model_slug") or "").strip() == catalog_code
            ),
            None,
        )
        if matching_entry:
            append_once({
                **model,
                "model_name": catalog_code,
                "model_slug": None,
                "display_name": model.get("display_name") or catalog_code,
            })
        # A provider-wide key or another model's key does not unlock this
        # catalog row. The admin must save an explicit key for this model.

    return expanded


def build_model_options(
    catalog_models: List[Dict[str, Optional[str]]],
    key_status: Optional[Dict[str, Any]] = None,
    fallback_openrouter_model: Optional[str] = None,
) -> List[Dict[str, Optional[str]]]:
    """Return the canonical de-duplicated transcription model option list.

    Every catalog code appears at most once, except OpenRouter entries which
    stay **per slug** so each configured transcription model remains
    selectable. All dropdown consumers (home page, user settings modal, admin
    role form, costs page) must source their options from here so the lists
    cannot drift apart.
    """
    expanded = expand_models_for_ui(catalog_models, key_status, fallback_openrouter_model)

    # Canonical ordering: every catalog code keeps its catalog order; the
    # OpenRouter slugs (already one option per slug) are sorted alphabetically
    # inside the openrouter group so all four dropdowns render identically.
    or_entries = sorted(
        (e for e in expanded if e.get("code") == "openrouter"),
        key=lambda e: str(e.get("model_name") or "").lower(),
    )
    others = [e for e in expanded if e.get("code") != "openrouter"]
    first_or = next(
        (i for i, e in enumerate(expanded) if e.get("code") == "openrouter"),
        len(others),
    )

    options: List[Dict[str, Optional[str]]] = []
    seen: set[tuple[str, str]] = set()
    for entry in others[:first_or] + or_entries + others[first_or:]:
        code = str(entry.get("code") or "").strip()
        identity = (code, entry.get("model_name") or "") if code == "openrouter" else (code, "")
        if identity in seen:
            continue
        seen.add(identity)
        options.append(entry)
    return options


def get_model_by_code(code: str) -> Optional[Dict[str, Optional[str]]]:
    if not code:
        return None
    cursor = get_cursor()
    sql = f"""
        SELECT code, display_name, permission_key, required_api_key, is_default, is_active
        FROM {MODELS_TABLE}
        WHERE code = %s
        LIMIT 1
    """
    cursor.execute(sql, (code,))
    row = cursor.fetchone()
    if not row:
        return None
    display_name = row["display_name"]
    return {
        "code": row["code"],
        "display_name": display_name,
        "permission_key": row.get("permission_key"),
        "required_api_key": row.get("required_api_key"),
        "is_default": bool(row.get("is_default", False)),
        "is_active": bool(row.get("is_active", False)),
    }


def get_default_model_code() -> Optional[str]:
    models = get_active_models()
    for model in models:
        if model.get("is_default"):
            return model["code"]
    # Fallback to the first active model if no explicit default is set.
    if models:
        return models[0]["code"]
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

def _ensure_models_table(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MODELS_TABLE} (
            id INT PRIMARY KEY AUTO_INCREMENT,
            code VARCHAR(80) NOT NULL UNIQUE,
            display_name VARCHAR(120) NOT NULL,
            permission_key VARCHAR(120) DEFAULT NULL,
            required_api_key VARCHAR(80) DEFAULT NULL,
            sort_order INT NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            model_purpose VARCHAR(20) NOT NULL DEFAULT 'transcription',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
    )

    # Migration-safe: add model_purpose to existing tables created before the
    # column existed (models are now key-registered with a transcription/live flag).
    cursor.execute(
        f"SHOW COLUMNS FROM {MODELS_TABLE} LIKE 'model_purpose'"
    )
    if cursor.fetchone() is None:
        logger.info("[DB:Catalog] Adding 'model_purpose' column to '%s'.", MODELS_TABLE)
        cursor.execute(
            f"ALTER TABLE {MODELS_TABLE} ADD COLUMN model_purpose VARCHAR(20) NOT NULL DEFAULT 'transcription' AFTER is_default"
        )


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


def register_model_from_provider(
    provider: str,
    code: str,
    display_name: Optional[str] = None,
    model_purpose: str = 'transcription',
) -> None:
    """Register or refresh a transcription model row from a saved provider key.

    Called when an API key is saved/updated: the model becomes selectable the
    moment its key exists (merged with the admin-added model metadata). The
    row's ``display_name`` defaults to the raw model name and is not
    overwritten on subsequent saves, so admin renames on the Models page stick.
    """
    provider = str(provider or "").strip().lower()
    if provider not in _PROVIDER_METADATA:
        logger.warning("[Catalog] Ignoring model registration for unknown provider '%s'.", provider)
        return
    code = str(code or "").strip()
    if not code:
        logger.warning("[Catalog] Ignoring model registration with empty code (provider '%s').", provider)
        return
    purpose = str(model_purpose or 'transcription').strip().lower()
    if purpose not in {'transcription', 'live'}:
        logger.warning("[Catalog] Ignoring model registration with invalid purpose '%s'.", model_purpose)
        return

    metadata = _PROVIDER_METADATA[provider]
    cursor = get_cursor()
    cursor.execute(
        f"""
        INSERT INTO {MODELS_TABLE} (
            code, display_name, permission_key, required_api_key, sort_order,
            is_active, is_default, model_purpose
        ) VALUES (%s, %s, %s, %s, 0, 1, 0, %s)
        ON DUPLICATE KEY UPDATE
            permission_key = VALUES(permission_key),
            required_api_key = VALUES(required_api_key),
            is_active = 1,
            model_purpose = VALUES(model_purpose)
        """,
        (
            code,
            _coerce_string(display_name) or code,
            metadata.get("permission_key"),
            metadata.get("required_api_key"),
            purpose,
        ),
    )
    get_db().commit()
    logger.info("[Catalog] Registered transcription model '%s' (provider '%s', purpose '%s').", code, provider, purpose)


def rename_model(code: str, display_name: str) -> bool:
    """Rename the display name of a catalog model (Models admin page).

    The catalog ``display_name`` is authoritative everywhere in the app (home,
    user settings, role form, costs), so a rename immediately propagates.
    """
    code = str(code or "").strip()
    cleaned = _coerce_string(display_name)
    if not code or not cleaned:
        return False
    cursor = get_cursor()
    cursor.execute(
        f"UPDATE {MODELS_TABLE} SET display_name = %s WHERE code = %s",
        (cleaned, code),
    )
    get_db().commit()
    return True


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


_ALLOWED_CATALOG_TABLES = {MODELS_TABLE, LANGUAGES_TABLE}

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
