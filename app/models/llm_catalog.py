# app/models/llm_catalog.py
# Centralized catalog for LLM models (title generation, workflows, etc.).
# Provides a single source of truth backed by MySQL tables.

import logging
from typing import Any, Dict, List, Optional

from flask import current_app
from mysql.connector import Error as MySQLError

from app.database import get_db, get_cursor

logger = logging.getLogger(__name__)

MODELS_TABLE = "llm_models_catalog"

# Default metadata scoped by provider. Extend as new providers are introduced.
# Display labels are intentionally absent: model display names come from the
# catalog's ``display_name`` column (admin-renamable on the Models page), and
# provider-level labels would otherwise leak in as hardcoded branding.
_PROVIDER_METADATA: Dict[str, Dict[str, Optional[str]]] = {
    "GEMINI": {
        "permission_key": "use_api_google_gemini",
        "required_api_key": "gemini",
        "sort_order": 10,
    },
    "OPENAI": {
        "permission_key": None,
        "required_api_key": "openai",
        "sort_order": 20,
    },
    "OPENROUTER": {
        "permission_key": "use_api_openrouter",
        "required_api_key": "openrouter",
        "sort_order": 30,
    },
}

# Columns that may carry the "default model" flag; used by _get_default_code
# to guard against SQL injection via a caller-supplied column name.
_ALLOWED_DEFAULT_COLUMNS = {"is_default", "is_default_title", "is_default_workflow"}

# Default metadata for known models. Extend this mapping as new models are added.
def init_db_command() -> None:
    """
    Ensures the LLM models catalog table exists and is seeded with defaults derived from config.
    Safe to call multiple times.
    """
    cursor = get_cursor()
    log_prefix = "[DB:Catalog:LLM]"
    logger.info(f"{log_prefix} Ensuring LLM catalog table exists.")

    try:
        _ensure_models_table(cursor)
        get_db().commit()
    except MySQLError as err:
        get_db().rollback()
        logger.error(f"{log_prefix} Failed to initialize LLM catalog table: {err}", exc_info=True)
        raise

    try:
        seed_from_config()
    except Exception as seed_err:
        logger.error(f"{log_prefix} Failed to seed LLM catalog: {seed_err}", exc_info=True)
        raise


def seed_from_config() -> None:
    """
    Seeds LLM models based on the current Flask config.

    LLM models are intentionally NOT pre-seeded: like transcription models,
    they are registered at runtime when an API key with an LLM-purpose model
    is saved (see ``register_model_from_provider``). Providers themselves are
    fixed by config (``LLM_PROVIDERS``) and cannot be extended by admins.
    """
    # No model rows are pre-seeded; kept as a no-op hook for future config
    # driven defaults (e.g. provider metadata refresh).


def get_active_models() -> List[Dict[str, Optional[str]]]:
    """
    Returns active LLM models sorted by configured order.
    """
    if not _table_has_rows(MODELS_TABLE):
        seed_from_config()

    cursor = get_cursor()
    sql = f"""
        SELECT
            code,
            provider,
            provider_display_name,
            display_name,
            permission_key,
            required_api_key,
            is_default,
            is_default_title,
            is_default_workflow
        FROM {MODELS_TABLE}
        WHERE is_active = TRUE
        ORDER BY sort_order ASC, display_name ASC
    """
    cursor.execute(sql)
    rows = cursor.fetchall() or []
    models: List[Dict[str, Optional[str]]] = []
    for row in rows:
        display_name = row["display_name"]
        models.append(
            {
                "code": row["code"],
                "provider": row.get("provider"),
                "provider_display_name": row.get("provider_display_name"),
                "display_name": display_name,
                "permission_key": row.get("permission_key"),
                "required_api_key": row.get("required_api_key"),
                "is_default": bool(row.get("is_default", False)),
                "is_default_title": bool(row.get("is_default_title", False)),
                "is_default_workflow": bool(row.get("is_default_workflow", False)),
            }
        )
    return models


def filter_models_by_api_key_status(
    models: List[Dict[str, Optional[str]]],
    api_key_status: Dict[str, bool],
) -> List[Dict[str, Optional[str]]]:
    """Return only models whose required provider key is available."""
    return [
        model
        for model in models
        if not model.get("required_api_key")
        or bool(api_key_status.get(str(model["required_api_key"]).lower()))
    ]


def get_model_by_code(code: str) -> Optional[Dict[str, Optional[str]]]:
    if not code:
        return None
    cursor = get_cursor()
    sql = f"""
        SELECT
            code,
            provider,
            provider_display_name,
            display_name,
            permission_key,
            required_api_key,
            is_default,
            is_default_title,
            is_default_workflow,
            is_active
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
        "provider": row.get("provider"),
        "provider_display_name": row.get("provider_display_name"),
        "display_name": display_name,
        "permission_key": row.get("permission_key"),
        "required_api_key": row.get("required_api_key"),
        "is_default": bool(row.get("is_default", False)),
        "is_default_title": bool(row.get("is_default_title", False)),
        "is_default_workflow": bool(row.get("is_default_workflow", False)),
        "is_active": bool(row.get("is_active", False)),
    }


def get_default_model_code() -> Optional[str]:
    return _get_default_code("is_default")


def get_default_title_generation_model_code() -> Optional[str]:
    return _get_default_code("is_default_title") or get_default_model_code()


def get_default_workflow_model_code() -> Optional[str]:
    return _get_default_code("is_default_workflow") or get_default_model_code()


def get_models_grouped_by_provider() -> Dict[str, Dict[str, str]]:
    """
    Returns active models grouped by provider display name: { provider_name: {code: display_name} }
    Useful for rendering nested selections.
    """
    grouped: Dict[str, Dict[str, str]] = {}
    for model in get_active_models():
        provider_display = model.get("provider_display_name") or model.get("provider") or "LLM"
        grouped.setdefault(provider_display, {})
        grouped[provider_display][model["code"]] = model["display_name"]
    return grouped


def get_llm_model_options(
    key_status: Optional[Dict[str, Any]] = None,
    include_user_models: bool = True,
) -> List[Dict[str, Optional[str]]]:
    """Return the full LLM model option list shared by every LLM dropdown.

    Combines the active catalog with model slugs saved on user API keys
    (purpose ``llm``). Catalog entries win on code collisions so display
    names stay stable. When ``key_status`` aggregates several users, this is
    the list admins see; with a single user's status it matches that user's
    own options exactly.
    """
    options: List[Dict[str, Optional[str]]] = list(get_active_models())
    if not include_user_models or not key_status:
        return options

    seen_codes = {str(model.get("code") or "").strip() for model in options}
    status = key_status or {}
    provider_keys = status.get("provider_keys") or {}
    for provider, entries in provider_keys.items():
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            purposes = entry.get("model_purposes") or []
            if isinstance(purposes, str):
                purposes = purposes.split(",")
            if "llm" not in {str(p).strip().lower() for p in purposes}:
                continue
            if entry.get("provider_wide"):
                # Provider-wide rows grant access to the provider, not to a model.
                continue
            slug = str(entry.get("model_name") or entry.get("model_slug") or "").strip()
            if not slug or slug in seen_codes:
                continue
            seen_codes.add(slug)
            options.append({
                "code": slug,
                "display_name": slug,
                "required_api_key": provider,
                "provider": str(provider).upper(),
                "permission_key": None,
            })
    return options


# ----- Internal Helpers -----

def _ensure_models_table(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MODELS_TABLE} (
            id INT PRIMARY KEY AUTO_INCREMENT,
            code VARCHAR(120) NOT NULL UNIQUE,
            provider VARCHAR(40) NOT NULL,
            provider_display_name VARCHAR(120) DEFAULT NULL,
            display_name VARCHAR(120) NOT NULL,
            permission_key VARCHAR(120) DEFAULT NULL,
            required_api_key VARCHAR(80) DEFAULT NULL,
            sort_order INT NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            is_default_title BOOLEAN NOT NULL DEFAULT FALSE,
            is_default_workflow BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
    )


def register_model_from_provider(
    provider: str,
    code: str,
    display_name: Optional[str] = None,
) -> None:
    """Register or refresh an LLM model row from a saved provider key.

    LLM models follow the same key-driven rule as transcription models: they
    become selectable the moment their API key exists. ``display_name``
    defaults to the raw model name and is preserved on re-saves so admin
    renames (Models page) stick.
    """
    provider_upper = _sanitize_provider(provider) or ""
    if provider_upper not in _PROVIDER_METADATA:
        logger.warning("[LLM Catalog] Ignoring LLM model registration for unknown provider '%s'.", provider)
        return
    code = str(code or "").strip()
    if not code:
        logger.warning("[LLM Catalog] Ignoring LLM model registration with empty code (provider '%s').", provider)
        return

    provider_metadata = _PROVIDER_METADATA[provider_upper]
    cursor = get_cursor()
    cursor.execute(
        f"""
        INSERT INTO {MODELS_TABLE} (
            code, provider, provider_display_name, display_name,
            permission_key, required_api_key, sort_order,
            is_active, is_default, is_default_title, is_default_workflow
        ) VALUES (%s, %s, %s, %s, %s, %s, 0, 1, 0, 0, 0)
        ON DUPLICATE KEY UPDATE
            provider = VALUES(provider),
            provider_display_name = VALUES(provider_display_name),
            permission_key = VALUES(permission_key),
            required_api_key = VALUES(required_api_key),
            is_active = 1
        """,
        (
            code,
            provider_upper,
            _coerce_string(provider_metadata.get("display_name")),
            _coerce_string(display_name) or code,
            provider_metadata.get("permission_key"),
            provider_metadata.get("required_api_key"),
        ),
    )
    get_db().commit()
    logger.info("[LLM Catalog] Registered LLM model '%s' (provider '%s').", code, provider_upper)


def rename_model(code: str, display_name: str) -> bool:
    """Rename the display name of an LLM catalog model (Models admin page)."""
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


def _table_has_rows(table_name: str) -> bool:
    if table_name not in _ALLOWED_LLM_TABLES:
        raise ValueError(f"Unexpected table: {table_name}")
    cursor = get_cursor()
    try:
        cursor.execute(f"SELECT 1 FROM {table_name} LIMIT 1")
    except MySQLError as err:
        if getattr(err, "errno", None) == 1146:  # Table doesn't exist
            logger.info(f"[LLM Catalog] Table '{table_name}' missing. Re-initializing.")
            init_db_command()
            cursor = get_cursor()
            cursor.execute(f"SELECT 1 FROM {table_name} LIMIT 1")
        else:
            raise
    return cursor.fetchone() is not None


def _get_default_code(column: str) -> Optional[str]:
    if column not in _ALLOWED_DEFAULT_COLUMNS:
        raise ValueError(f"Unexpected column: {column}")
    cursor = get_cursor()
    sql = f"""
        SELECT code
        FROM {MODELS_TABLE}
        WHERE {column} = TRUE AND is_active = TRUE
        ORDER BY sort_order ASC, display_name ASC
        LIMIT 1
    """
    cursor.execute(sql)
    row = cursor.fetchone()
    if row:
        return row["code"]

    # Fall back to the first active model if a specific default is not designated.
    models = get_active_models()
    if models:
        return models[0]["code"]
    return None


def _resolve_default_code(preferred: Optional[str], available: List[str]) -> Optional[str]:
    if preferred and preferred in available:
        return preferred
    return available[0] if available else None


def _sanitize_code(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    candidate = value.strip()
    return candidate or None


def _sanitize_provider(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    candidate = str(value).strip().upper()
    return candidate or None


def _coerce_string(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return str(value)
