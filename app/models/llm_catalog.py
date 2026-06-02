# app/models/llm_catalog.py
# Centralized catalog for LLM models (title generation, workflows, etc.).
# Provides a single source of truth backed by MySQL tables.

import logging
from typing import Dict, List, Optional

from flask import current_app
from mysql.connector import Error as MySQLError

from app.database import get_db, get_cursor

logger = logging.getLogger(__name__)

MODELS_TABLE = "llm_models_catalog"

# Default metadata scoped by provider. Extend as new providers are introduced.
_PROVIDER_METADATA: Dict[str, Dict[str, Optional[str]]] = {
    "GEMINI": {
        "display_name": "Google Gemini",
        "permission_key": "use_api_google_gemini",
        "required_api_key": "gemini",
        "sort_order": 10,
    },
    "OPENAI": {
        "display_name": "OpenAI",
        "permission_key": None,
        "required_api_key": "openai",
        "sort_order": 20,
    },
}

# Default metadata for known models. Extend this mapping as new models are added.
_DEFAULT_MODEL_METADATA: Dict[str, Dict[str, Optional[str]]] = {
    "gemini-2.0-flash": {
        "display_name": "Gemini 2.0 Flash",
        "provider": "GEMINI",
        "sort_order": 10,
    },
    "gemini-3.0-flash": {
        "display_name": "Gemini 3.0 Flash",
        "provider": "GEMINI",
        "sort_order": 20,
    },
    "gpt-4o": {
        "display_name": "OpenAI GPT-4o",
        "provider": "OPENAI",
        "sort_order": 10,
    },
}


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
    Existing rows are upserted to keep display names and defaults in sync.
    """
    _seed_models_from_config()


def _apply_display_name_override(code: Optional[str], db_value: Optional[str]) -> Optional[str]:
    """
    Returns the configured display override for an LLM code while preserving DB values.
    """
    if not code:
        return db_value
    name_map: Dict[str, str] = current_app.config.get("API_PROVIDER_NAME_MAP", {}) or {}
    return name_map.get(code, db_value)


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
        display_name = _apply_display_name_override(row["code"], row["display_name"])
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
    display_name = _apply_display_name_override(row["code"], row["display_name"])
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


def _seed_models_from_config() -> None:
    config = current_app.config
    provider_codes = config.get("LLM_PROVIDERS", [])
    default_general = _sanitize_code(config.get("LLM_MODEL"))
    default_title = _sanitize_code(config.get("TITLE_GENERATION_LLM_MODEL"))
    default_workflow = _sanitize_code(config.get("WORKFLOW_LLM_MODEL"))
    provider_name_map: Dict[str, str] = config.get("API_PROVIDER_NAME_MAP", {}) or {}

    if not provider_codes:
        logger.warning("[LLM Catalog] LLM_PROVIDERS config is empty. No LLM models to seed.")
        return

    seen_codes: List[str] = []

    for provider_index, provider in enumerate(provider_codes):
        provider_upper = _sanitize_provider(provider)
        if not provider_upper:
            continue

        provider_metadata = _PROVIDER_METADATA.get(provider_upper, {})
        provider_display_name = provider_name_map.get(
            provider_upper, provider_metadata.get("display_name") or provider_upper.title()
        )
        provider_permission = provider_metadata.get("permission_key")
        provider_required_key = provider_metadata.get("required_api_key")
        provider_sort_base = provider_metadata.get("sort_order", (provider_index + 1) * 100)

        models_key = f"{provider_upper}_MODELS"
        configured_models = config.get(models_key, [])
        if isinstance(configured_models, str):
            configured_models = configured_models.split(",")

        for model_index, raw_model in enumerate(configured_models):
            code = _sanitize_code(raw_model)
            if not code or code in seen_codes:
                continue

            seen_codes.append(code)

            metadata = _DEFAULT_MODEL_METADATA.get(code, {})
            display_name = provider_name_map.get(code, metadata.get("display_name") or code)
            permission_key = metadata.get("permission_key", provider_permission)
            required_api_key = metadata.get("required_api_key", provider_required_key)
            provider_override = _sanitize_provider(metadata.get("provider") or provider_upper)
            sort_order = metadata.get("sort_order", provider_sort_base + model_index)

            _upsert_model(
                code=code,
                provider=provider_override or provider_upper,
                provider_display_name=provider_display_name,
                display_name=display_name,
                permission_key=permission_key,
                required_api_key=required_api_key,
                sort_order=sort_order,
                is_active=True,
                is_default=(code == default_general),
                is_default_title=(code == default_title),
                is_default_workflow=(code == default_workflow),
            )

    # Ensure explicitly configured default models are seeded even if they were omitted from the provider *__MODELS lists
    _ensure_default_model_seeded(default_general, config.get("LLM_PROVIDER", "GEMINI"), seen_codes, default_general, default_title, default_workflow, provider_name_map)
    _ensure_default_model_seeded(default_title, config.get("TITLE_GENERATION_LLM_PROVIDER", "GEMINI"), seen_codes, default_general, default_title, default_workflow, provider_name_map)
    _ensure_default_model_seeded(default_workflow, config.get("WORKFLOW_LLM_PROVIDER", "GEMINI"), seen_codes, default_general, default_title, default_workflow, provider_name_map)

    _set_default_flag("is_default", _resolve_default_code(default_general, seen_codes))
    _set_default_flag("is_default_title", _resolve_default_code(default_title, seen_codes))
    _set_default_flag("is_default_workflow", _resolve_default_code(default_workflow, seen_codes))


def _ensure_default_model_seeded(
    code: Optional[str],
    provider: Optional[str],
    seen_codes: List[str],
    default_general: Optional[str],
    default_title: Optional[str],
    default_workflow: Optional[str],
    provider_name_map: Dict[str, str],
) -> None:
    if not code or code in seen_codes:
        return

    provider_upper = _sanitize_provider(provider) or "GEMINI"
    seen_codes.append(code)

    metadata = _DEFAULT_MODEL_METADATA.get(code, {})
    provider_metadata = _PROVIDER_METADATA.get(provider_upper, {})

    display_name = provider_name_map.get(code, metadata.get("display_name") or code)
    provider_display_name = provider_name_map.get(
        provider_upper, provider_metadata.get("display_name") or provider_upper.title()
    )

    permission_key = metadata.get("permission_key", provider_metadata.get("permission_key"))
    required_api_key = metadata.get("required_api_key", provider_metadata.get("required_api_key"))
    provider_override = _sanitize_provider(metadata.get("provider") or provider_upper)
    sort_order = metadata.get("sort_order", 999)

    _upsert_model(
        code=code,
        provider=provider_override or provider_upper,
        provider_display_name=provider_display_name,
        display_name=display_name,
        permission_key=permission_key,
        required_api_key=required_api_key,
        sort_order=sort_order,
        is_active=True,
        is_default=(code == default_general),
        is_default_title=(code == default_title),
        is_default_workflow=(code == default_workflow),
    )


_ALLOWED_LLM_TABLES = {MODELS_TABLE}
_ALLOWED_DEFAULT_COLUMNS = {"is_default", "is_default_title", "is_default_workflow"}

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


def _upsert_model(
    *,
    code: str,
    provider: str,
    provider_display_name: Optional[str],
    display_name: str,
    permission_key: Optional[str],
    required_api_key: Optional[str],
    sort_order: int,
    is_active: bool,
    is_default: bool,
    is_default_title: bool,
    is_default_workflow: bool,
) -> None:
    sql = f"""
        INSERT INTO {MODELS_TABLE} (
            code,
            provider,
            provider_display_name,
            display_name,
            permission_key,
            required_api_key,
            sort_order,
            is_active,
            is_default,
            is_default_title,
            is_default_workflow
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            provider = VALUES(provider),
            provider_display_name = VALUES(provider_display_name),
            display_name = VALUES(display_name),
            permission_key = VALUES(permission_key),
            required_api_key = VALUES(required_api_key),
            sort_order = VALUES(sort_order),
            is_active = VALUES(is_active),
            is_default = VALUES(is_default),
            is_default_title = VALUES(is_default_title),
            is_default_workflow = VALUES(is_default_workflow)
    """
    cursor = get_cursor()
    cursor.execute(
        sql,
        (
            code,
            provider,
            _coerce_string(provider_display_name),
            _coerce_string(display_name),
            permission_key,
            required_api_key,
            sort_order,
            int(bool(is_active)),
            int(bool(is_default)),
            int(bool(is_default_title)),
            int(bool(is_default_workflow)),
        ),
    )
    get_db().commit()


def _set_default_flag(column: str, default_code: Optional[str]) -> None:
    if column not in _ALLOWED_DEFAULT_COLUMNS:
        raise ValueError(f"Unexpected column: {column}")
    cursor = get_cursor()
    if default_code:
        cursor.execute(
            f"UPDATE {MODELS_TABLE} SET {column} = CASE WHEN code = %s THEN TRUE ELSE FALSE END",
            (default_code,),
        )
    else:
        cursor.execute(f"UPDATE {MODELS_TABLE} SET {column} = FALSE")
    get_db().commit()


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
