# app/models/role.py
# Defines the Role model, permissions, and related database functions, including monthly usage tracking.

import logging
import os
import threading
import time
from flask import current_app
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple

from mysql.connector import Error as MySQLError
from app.database import get_db, get_cursor


class UsageReservationError(RuntimeError):
    """Raised when durable quota state cannot be checked or reserved."""


# --- Simple in-process TTL cache for roles (they change very rarely) ---
_ROLE_CACHE_TTL = 300  # seconds
_role_cache: Dict[int, tuple] = {}   # role_id -> (Role, expires_at)
_role_cache_lock = threading.Lock()


def _get_cached_role(role_id: int) -> Optional['Role']:
    with _role_cache_lock:
        entry = _role_cache.get(role_id)
        if entry and time.monotonic() < entry[1]:
            return entry[0]
    return None


def _set_cached_role(role_id: int, role: Optional['Role']) -> None:
    if role is None:
        return
    with _role_cache_lock:
        _role_cache[role_id] = (role, time.monotonic() + _ROLE_CACHE_TTL)


def invalidate_role_cache(role_id: Optional[int] = None) -> None:
    """Call after any role update. Pass role_id to evict one entry, or None to flush all."""
    with _role_cache_lock:
        if role_id is None:
            _role_cache.clear()
        else:
            _role_cache.pop(role_id, None)

# ----- Helper Functions -----

def _column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM {table} LIKE %s", (column,))
    exists = cursor.fetchone() is not None
    cursor.fetchall()  # consume remaining results
    return exists

def _ensure_column(cursor, table: str, old_col: Optional[str], new_col: str, col_def: str, after: Optional[str] = None, log_prefix: str = "") -> None:
    if old_col and _column_exists(cursor, table, old_col):
        logging.info(f"{log_prefix} Found old '{old_col}' column. Renaming to '{new_col}'.")
        cursor.execute(f"ALTER TABLE {table} CHANGE COLUMN {old_col} {new_col} {col_def}")
    elif not _column_exists(cursor, table, new_col):
        extra = ""
        if after and "AFTER" not in col_def:
            extra = f" AFTER {after}"
        logging.info(f"{log_prefix} Adding '{new_col}' column ({col_def}{extra}).")
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {new_col} {col_def}{extra}")

def _convert_role_field(col: str, value: Any) -> Any:
    if isinstance(value, bool):
        return 1 if value else 0
    elif value is None and col.startswith(('max_', 'history_')):
        return 0
    return value

def _prepare_role_fields(data: Dict[str, Any], fields: List[str]) -> Tuple[List[str], List[Any]]:
    columns = []
    values = []
    for col in fields:
        key = col
        # handle renamed limit keys
        if col == 'max_minutes_monthly' and 'max_seconds_monthly' in data:
            key = 'max_seconds_monthly'
        elif col == 'max_minutes_total' and 'max_seconds_total' in data:
            key = 'max_seconds_total'
        if key in data:
            columns.append(col)
            values.append(_convert_role_field(col, data[key]))
    return columns, values

def _normalize_usage_row(row: Dict[str, Any]) -> Dict[str, Any]:
    row['minutes_count'] = float(row.get('minutes_count') or 0.0)
    row['workflow_count'] = int(row.get('workflow_count') or 0)
    return row

def _safe_close(cursor, log_prefix: str = ""):
    if cursor:
        # The cursor is managed by the application context, so we don't close it here.
        pass

_NORMALIZE_ALLOWED_TABLES = {"roles"}
_NORMALIZE_ALLOWED_COLUMNS = {"created_at", "updated_at"}

def _normalize_timestamp_column(table: str, column: str, log_prefix: str) -> None:
    """
    Converts string/ISO timestamps in a column to MySQL-compatible DATETIME before altering to TIMESTAMP.
    This avoids ALTER failures on legacy values such as ISO strings with timezone suffixes.
    """
    if table not in _NORMALIZE_ALLOWED_TABLES or column not in _NORMALIZE_ALLOWED_COLUMNS:
        raise ValueError(f"Unexpected table/column in _normalize_timestamp_column: {table}.{column}")
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")
        rows = cursor.fetchall() or []
        for row in rows:
            raw = row[column]
            # Skip if already a datetime object
            if isinstance(raw, datetime):
                continue
            if raw is None:
                continue
            raw_str = str(raw)
            normalized = None
            try:
                cleaned = raw_str.rstrip('Z')
                # If no timezone info, assume UTC
                if 'T' in cleaned:
                    normalized = datetime.fromisoformat(cleaned)
                else:
                    normalized = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
                if normalized.tzinfo is None:
                    normalized = normalized.replace(tzinfo=timezone.utc)
            except Exception:
                logging.warning(f"{log_prefix} Could not parse timestamp '{raw_str}' in {table}.{column} (id={row.get('id')}). Skipping.")
                continue
            try:
                cursor.execute(f"UPDATE {table} SET {column} = %s WHERE id = %s", (normalized, row["id"]))  # nosec: table/column whitelisted above
            except Exception as update_err:
                logging.warning(f"{log_prefix} Failed to normalize {table}.{column} for id={row.get('id')}: {update_err}")
        conn.commit()
    finally:
        try:
            cursor.close()
        except Exception:
            pass

# ----- Role Model Definition -----

class Role:
    id: int
    name: str
    description: Optional[str]
    default_transcription_model: Optional[str]
    default_title_generation_model: Optional[str]
    default_workflow_model: Optional[str]
    default_openrouter_model: Optional[str]
    default_live_transcription_model: Optional[str]
    # Transcription API Permissions (provider-level; legacy model fields remain for migration compatibility)
    use_api_openai: bool
    use_api_assemblyai: bool
    use_api_google: bool
    use_api_openai_whisper: bool
    use_api_openai_gpt_4o_transcribe: bool
    use_api_openai_live_transcribe: bool
    use_api_google_gemini: bool
    use_api_openrouter: bool
    # Feature Permissions
    access_admin_panel: bool
    allow_large_files: bool
    allow_context_prompt: bool
    allow_api_key_management: bool
    allow_public_api_access: bool
    allow_download_transcript: bool
    allow_workflows: bool
    manage_workflow_templates: bool
    allow_auto_title_generation: bool
    allow_speaker_diarization: bool
    # Usage Limits
    limit_daily_cost: float
    limit_weekly_cost: float
    limit_monthly_cost: float
    limit_daily_minutes: int
    limit_weekly_minutes: int
    limit_monthly_minutes: int
    limit_daily_workflows: int
    limit_weekly_workflows: int
    limit_monthly_workflows: int
    limit_daily_live_minutes: int
    limit_weekly_live_minutes: int
    limit_monthly_live_minutes: int
    # History Limits
    max_history_items: int
    history_retention_days: int
    # Timestamps
    created_at: str
    updated_at: str

    def __init__(self, **kwargs):
        logging.debug(f"[Role Init] Creating Role object with kwargs: {kwargs}")
        self.id = kwargs.get('id')
        self.name = kwargs.get('name')
        self.description = kwargs.get('description')
        self.default_transcription_model = kwargs.get('default_transcription_model') or None
        self.default_title_generation_model = kwargs.get('default_title_generation_model') or None
        self.default_workflow_model = kwargs.get('default_workflow_model') or None
        self.default_openrouter_model = kwargs.get('default_openrouter_model') or None
        self.default_live_transcription_model = kwargs.get('default_live_transcription_model') or None
        # Legacy rows may not contain provider-level fields. Derive those
        # values only when the new columns are absent; once present they are
        # authoritative and can be explicitly disabled by an administrator.
        legacy_provider_aliases = {
            'use_api_openai': (
                'use_api_openai_whisper',
                'use_api_openai_gpt_4o_transcribe',
                'use_api_openai_live_transcribe',
            ),
            'use_api_google': ('use_api_google_gemini',),
        }
        for provider_field, legacy_fields in legacy_provider_aliases.items():
            if provider_field not in kwargs:
                kwargs[provider_field] = any(bool(kwargs.get(field)) for field in legacy_fields)
        # Process boolean fields
        bool_fields = [
            'use_api_openai', 'use_api_assemblyai', 'use_api_google',
            'use_api_openai_whisper', 'use_api_openai_gpt_4o_transcribe',
            'use_api_openai_live_transcribe', 'use_api_google_gemini',
            'use_api_openrouter',
            'access_admin_panel', 'allow_large_files', 'allow_context_prompt',
            'allow_api_key_management', 'allow_public_api_access', 'allow_download_transcript', 'allow_workflows',
            'manage_workflow_templates', 'allow_auto_title_generation', 'allow_speaker_diarization'
        ]
        defaults = {field: (1 if field == 'allow_download_transcript' else 0) for field in bool_fields}
        for field in bool_fields:
            setattr(self, field, bool(kwargs.get(field, defaults[field])))
        # Process integer limit fields
        int_fields = [
            'limit_daily_minutes', 'limit_weekly_minutes', 'limit_monthly_minutes',
            'limit_daily_workflows', 'limit_weekly_workflows', 'limit_monthly_workflows',
            'limit_daily_live_minutes', 'limit_weekly_live_minutes', 'limit_monthly_live_minutes',
            'max_history_items', 'history_retention_days'
        ]
        for field in int_fields:
            setattr(self, field, int(kwargs.get(field, 0)))

        float_fields = [
            'limit_daily_cost', 'limit_weekly_cost', 'limit_monthly_cost'
        ]
        for field in float_fields:
            setattr(self, field, float(kwargs.get(field, 0.0)))
        # Timestamps
        self.created_at = kwargs.get('created_at')
        self.updated_at = kwargs.get('updated_at')

    def __repr__(self):
        return f'<Role {self.name} (ID: {self.id})>'

    def has_permission(self, permission_name: str) -> bool:
        if not permission_name.startswith(('use_', 'allow_', 'access_', 'manage_')):
            logging.warning(f"Attempted to check non-boolean permission '{permission_name}' with has_permission().")
            return False
        provider_permission_aliases = {
            'use_api_openai_whisper': 'use_api_openai',
            'use_api_openai_gpt_4o_transcribe': 'use_api_openai',
            'use_api_openai_live_transcribe': 'use_api_openai',
            'use_api_google_gemini': 'use_api_google',
        }
        effective_permission = provider_permission_aliases.get(permission_name, permission_name)
        return bool(getattr(self, effective_permission, False))

    def get_limit(self, limit_name: str) -> int | float:
        if not limit_name.startswith(('limit_', 'max_', 'history_')):
            logging.warning(f"Attempted to get non-limit permission '{limit_name}' with get_limit().")
            return 0
        return getattr(self, limit_name, 0)

# ----- Database Interaction Functions -----

def _map_row_to_role(row: Dict[str, Any]) -> Optional[Role]:
    if row:
        if 'max_seconds_monthly' in row:
            row['max_minutes_monthly'] = row.pop('max_seconds_monthly')
        if 'max_seconds_total' in row:
            row['max_minutes_total'] = row.pop('max_seconds_total')
        # Keep reads compatible with databases created before provider-level
        # permissions were introduced.
        if 'use_api_openai' not in row:
            row['use_api_openai'] = int(any(
                bool(row.get(field)) for field in (
                    'use_api_openai_whisper',
                    'use_api_openai_gpt_4o_transcribe',
                    'use_api_openai_live_transcribe',
                )
            ))
        if 'use_api_google' not in row:
            row['use_api_google'] = int(bool(row.get('use_api_google_gemini')))
        for field in (
            'use_api_assemblyai', 'use_api_openrouter',
            'use_api_openai_whisper', 'use_api_openai_gpt_4o_transcribe',
            'use_api_openai_live_transcribe', 'use_api_google_gemini',
        ):
            row.setdefault(field, 0)
        if 'default_transcription_model' not in row:
            row['default_transcription_model'] = None
        if 'default_title_generation_model' not in row:
            row['default_title_generation_model'] = None
        if 'default_workflow_model' not in row:
            row['default_workflow_model'] = None
        if 'default_openrouter_model' not in row:
            row['default_openrouter_model'] = None
        if 'default_live_transcription_model' not in row:
            row['default_live_transcription_model'] = None
        if 'allow_auto_title_generation' not in row:
            row['allow_auto_title_generation'] = 0
        if 'allow_speaker_diarization' not in row:
            row['allow_speaker_diarization'] = 0
        if 'allow_public_api_access' not in row:
            row['allow_public_api_access'] = 0
        return Role(**row)
    return None

def init_roles_table() -> None:
    cursor = get_cursor()
    log_prefix = "[DB:Schema:MySQL]"
    logging.info(f"{log_prefix} Checking/Initializing 'roles' table...")
    try:
        # --- MODIFIED: Added use_api_google_gemini ---
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS roles (
                id INT PRIMARY KEY AUTO_INCREMENT,
                name VARCHAR(80) UNIQUE NOT NULL,
                description TEXT,
                default_transcription_model VARCHAR(255) DEFAULT NULL,
                default_title_generation_model VARCHAR(100) DEFAULT NULL,
                default_workflow_model VARCHAR(100) DEFAULT NULL,
                default_openrouter_model VARCHAR(120) DEFAULT NULL,
                default_live_transcription_model VARCHAR(255) DEFAULT NULL,
                use_api_openai BOOLEAN NOT NULL DEFAULT FALSE,
                use_api_assemblyai BOOLEAN NOT NULL DEFAULT FALSE,
                use_api_openai_whisper BOOLEAN NOT NULL DEFAULT FALSE,
                use_api_openai_gpt_4o_transcribe BOOLEAN NOT NULL DEFAULT FALSE,
                use_api_openai_live_transcribe BOOLEAN NOT NULL DEFAULT FALSE,
                use_api_google BOOLEAN NOT NULL DEFAULT FALSE,
                use_api_google_gemini BOOLEAN NOT NULL DEFAULT FALSE,
                use_api_openrouter BOOLEAN NOT NULL DEFAULT FALSE,
                access_admin_panel BOOLEAN NOT NULL DEFAULT FALSE,
                allow_large_files BOOLEAN NOT NULL DEFAULT FALSE,
                allow_context_prompt BOOLEAN NOT NULL DEFAULT FALSE,
                allow_api_key_management BOOLEAN NOT NULL DEFAULT FALSE,
                allow_public_api_access BOOLEAN NOT NULL DEFAULT FALSE,
                allow_download_transcript BOOLEAN NOT NULL DEFAULT TRUE,
                allow_workflows BOOLEAN NOT NULL DEFAULT FALSE,
                manage_workflow_templates BOOLEAN NOT NULL DEFAULT FALSE,
                allow_auto_title_generation BOOLEAN NOT NULL DEFAULT FALSE,
                allow_speaker_diarization BOOLEAN NOT NULL DEFAULT FALSE,
                limit_daily_cost DECIMAL(10, 4) NOT NULL DEFAULT 0.0000,
                limit_weekly_cost DECIMAL(10, 4) NOT NULL DEFAULT 0.0000,
                limit_monthly_cost DECIMAL(10, 4) NOT NULL DEFAULT 0.0000,
                limit_daily_minutes INT NOT NULL DEFAULT 0,
                limit_weekly_minutes INT NOT NULL DEFAULT 0,
                limit_monthly_minutes INT NOT NULL DEFAULT 0,
                limit_daily_workflows INT NOT NULL DEFAULT 0,
                limit_weekly_workflows INT NOT NULL DEFAULT 0,
                limit_monthly_workflows INT NOT NULL DEFAULT 0,
                limit_daily_live_minutes INT NOT NULL DEFAULT 0,
                limit_weekly_live_minutes INT NOT NULL DEFAULT 0,
                limit_monthly_live_minutes INT NOT NULL DEFAULT 0,
                max_history_items INT NOT NULL DEFAULT 0,
                history_retention_days INT NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_role_name (name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            '''
        )
        # --- END MODIFIED ---
        _ensure_column(cursor, "roles", None, "max_transcriptions_monthly", "INT NOT NULL DEFAULT 0", after="limit_monthly_workflows", log_prefix=log_prefix)
        _ensure_column(cursor, "roles", None, "max_transcriptions_total", "INT NOT NULL DEFAULT 0", after="max_transcriptions_monthly", log_prefix=log_prefix)
        _ensure_column(cursor, "roles", None, "limit_daily_live_minutes", "INT NOT NULL DEFAULT 0", after="limit_monthly_workflows", log_prefix=log_prefix)
        _ensure_column(cursor, "roles", None, "limit_weekly_live_minutes", "INT NOT NULL DEFAULT 0", after="limit_daily_live_minutes", log_prefix=log_prefix)
        _ensure_column(cursor, "roles", None, "limit_monthly_live_minutes", "INT NOT NULL DEFAULT 0", after="limit_weekly_live_minutes", log_prefix=log_prefix)
        _ensure_column(cursor, "roles", "max_seconds_monthly", "max_minutes_monthly",
                       "INT NOT NULL DEFAULT 0", after="max_transcriptions_total", log_prefix=log_prefix)
        _ensure_column(cursor, "roles", "max_seconds_total", "max_minutes_total",
                       "INT NOT NULL DEFAULT 0", after="max_minutes_monthly", log_prefix=log_prefix)
        new_workflow_columns = {
            'allow_workflows': "BOOLEAN NOT NULL DEFAULT FALSE AFTER allow_download_transcript",
            'manage_workflow_templates': "BOOLEAN NOT NULL DEFAULT FALSE AFTER allow_workflows",
            'max_workflows_monthly': "INT NOT NULL DEFAULT 0 AFTER max_minutes_total",
            'max_workflows_total': "INT NOT NULL DEFAULT 0 AFTER max_workflows_monthly"
        }
        for col_name, col_def in new_workflow_columns.items():
            _ensure_column(cursor, "roles", None, col_name, col_def, log_prefix=log_prefix)

        _ensure_column(cursor, "roles", None, "allow_public_api_access",
                       "BOOLEAN NOT NULL DEFAULT FALSE", after="allow_api_key_management", log_prefix=log_prefix)
        _ensure_column(cursor, "roles", None, "allow_auto_title_generation",
                       "BOOLEAN NOT NULL DEFAULT FALSE", after="manage_workflow_templates", log_prefix=log_prefix)
        _ensure_column(cursor, "roles", None, "allow_speaker_diarization",
                       "BOOLEAN NOT NULL DEFAULT FALSE", after="allow_auto_title_generation", log_prefix=log_prefix)

        _ensure_column(cursor, "roles", None, "default_transcription_model",
                       "VARCHAR(255) DEFAULT NULL", after="description", log_prefix=log_prefix)
        cursor.execute("SHOW COLUMNS FROM roles LIKE 'default_transcription_model'")
        transcription_model_column = cursor.fetchone()
        cursor.fetchall()
        transcription_model_type = transcription_model_column.get('Type', '') if isinstance(transcription_model_column, dict) else (transcription_model_column[1] if transcription_model_column and len(transcription_model_column) > 1 else '')
        if '255' not in str(transcription_model_type):
            logging.info(f"{log_prefix} Widening 'default_transcription_model' for provider-qualified model keys.")
            cursor.execute("ALTER TABLE roles MODIFY COLUMN default_transcription_model VARCHAR(255) DEFAULT NULL")
        _ensure_column(cursor, "roles", None, "default_title_generation_model",
                       "VARCHAR(100) DEFAULT NULL", after="default_transcription_model", log_prefix=log_prefix)
        _ensure_column(cursor, "roles", None, "default_workflow_model",
                       "VARCHAR(100) DEFAULT NULL", after="default_title_generation_model", log_prefix=log_prefix)
        _ensure_column(cursor, "roles", None, "default_openrouter_model",
                       "VARCHAR(120) DEFAULT NULL", after="default_workflow_model", log_prefix=log_prefix)
        _ensure_column(cursor, "roles", None, "default_live_transcription_model",
                       "VARCHAR(255) DEFAULT NULL", after="default_openrouter_model", log_prefix=log_prefix)
        cursor.execute("SHOW COLUMNS FROM roles LIKE 'default_live_transcription_model'")
        live_model_column = cursor.fetchone()
        cursor.fetchall()
        live_model_type = live_model_column.get('Type', '') if isinstance(live_model_column, dict) else (live_model_column[1] if live_model_column and len(live_model_column) > 1 else '')
        if '255' not in str(live_model_type):
            logging.info(f"{log_prefix} Widening 'default_live_transcription_model' for provider-qualified model keys.")
            cursor.execute("ALTER TABLE roles MODIFY COLUMN default_live_transcription_model VARCHAR(255) DEFAULT NULL")

        openai_permission_exists = _column_exists(cursor, "roles", "use_api_openai")
        _ensure_column(
            cursor, "roles", None, "use_api_openai",
            "BOOLEAN NOT NULL DEFAULT FALSE", after="default_openrouter_model", log_prefix=log_prefix
        )
        if not openai_permission_exists:
            cursor.execute(
                """
                UPDATE roles
                SET use_api_openai = (
                    use_api_openai_whisper
                    OR use_api_openai_gpt_4o_transcribe
                    OR use_api_openai_live_transcribe
                )
                """
            )

        cursor.execute("SHOW COLUMNS FROM roles LIKE 'use_api_openai_live_transcribe'")
        live_permission_exists = cursor.fetchone()
        cursor.fetchall()
        _ensure_column(cursor, "roles", None, "use_api_openai_live_transcribe",
                       "BOOLEAN NOT NULL DEFAULT FALSE", after="use_api_openai_gpt_4o_transcribe", log_prefix=log_prefix)
        if not live_permission_exists:
            cursor.execute(
                "UPDATE roles SET use_api_openai_live_transcribe = TRUE WHERE name = 'admin'"
            )

        # --- MODIFIED: Add use_api_google_gemini column idempotently ---
        _ensure_column(cursor, "roles", None, "use_api_google_gemini",
                       "BOOLEAN NOT NULL DEFAULT FALSE", after="use_api_openai_live_transcribe", log_prefix=log_prefix)
        google_permission_exists = _column_exists(cursor, "roles", "use_api_google")
        _ensure_column(
            cursor, "roles", None, "use_api_google",
            "BOOLEAN NOT NULL DEFAULT FALSE", after="use_api_openai_live_transcribe", log_prefix=log_prefix
        )
        if not google_permission_exists:
            cursor.execute("UPDATE roles SET use_api_google = use_api_google_gemini")
        _ensure_column(
            cursor, "roles", None, "use_api_openrouter",
            "BOOLEAN NOT NULL DEFAULT FALSE",
            after="use_api_google_gemini",
            log_prefix=log_prefix,
        )
        # --- END MODIFIED ---

        # Normalize timestamp columns
        cursor.execute("SHOW COLUMNS FROM roles LIKE 'created_at'")
        created_at_col = cursor.fetchone()
        cursor.fetchall()
        created_at_type = (created_at_col.get('Type') if isinstance(created_at_col, dict) else (created_at_col[1] if created_at_col else "")).lower()
        if created_at_col and 'timestamp' not in created_at_type:
            logging.info(f"{log_prefix} Converting 'created_at' column on 'roles' table to TIMESTAMP.")
            _normalize_timestamp_column("roles", "created_at", log_prefix)
            cursor.execute("ALTER TABLE roles MODIFY COLUMN created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP")

        cursor.execute("SHOW COLUMNS FROM roles LIKE 'updated_at'")
        updated_at_col = cursor.fetchone()
        cursor.fetchall()
        updated_at_type = (updated_at_col.get('Type') if isinstance(updated_at_col, dict) else (updated_at_col[1] if updated_at_col else "")).lower()
        if updated_at_col and 'timestamp' not in updated_at_type:
            logging.info(f"{log_prefix} Converting 'updated_at' column on 'roles' table to TIMESTAMP with auto-update.")
            _normalize_timestamp_column("roles", "updated_at", log_prefix)
            cursor.execute("ALTER TABLE roles MODIFY COLUMN updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")

        get_db().commit()
        logging.info(f"{log_prefix} 'roles' table schema verified/initialized.")
    except MySQLError as err:
        logging.error(f"{log_prefix} Error during 'roles' table initialization: {err}", exc_info=True)
        get_db().rollback()
        raise
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass

# This function is no longer needed as the 'monthly_usage' table has been removed.

def create_role(name: str, description: Optional[str] = None, permissions: Optional[Dict[str, Any]] = None) -> Optional[Role]:
    """
    Creates a new role in the database.
    Handles renamed limit fields and new workflow fields.
    """
    permissions = dict(permissions or {})
    # Keep the public provider-level permission contract compatible with
    # callers that still submit the legacy model-level flags. Do not override
    # an explicitly supplied provider permission.
    if 'use_api_openai' not in permissions:
        permissions['use_api_openai'] = any(
            bool(permissions.get(field))
            for field in (
                'use_api_openai_whisper',
                'use_api_openai_gpt_4o_transcribe',
                'use_api_openai_live_transcribe',
            )
        )
    if 'use_api_google' not in permissions:
        permissions['use_api_google'] = bool(permissions.get('use_api_google_gemini'))
    logging.info(f"[DB:Role] create_role called with permissions: {permissions}")
    valid_permission_columns = [
        'use_api_openai', 'use_api_assemblyai',
        'use_api_openai_whisper', 'use_api_openai_gpt_4o_transcribe',
        'use_api_openai_live_transcribe',
        'use_api_google', 'use_api_google_gemini',
        'use_api_openrouter',
        'access_admin_panel', 'allow_large_files', 'allow_context_prompt',
        'allow_api_key_management', 'allow_public_api_access', 'allow_download_transcript',
        'allow_workflows', 'manage_workflow_templates', 'allow_auto_title_generation', 'allow_speaker_diarization',
        'default_transcription_model', 'default_title_generation_model', 'default_workflow_model', 'default_openrouter_model', 'default_live_transcription_model',
        'limit_daily_cost', 'limit_weekly_cost', 'limit_monthly_cost',
        'limit_daily_minutes', 'limit_weekly_minutes', 'limit_monthly_minutes',
        'limit_daily_workflows', 'limit_weekly_workflows', 'limit_monthly_workflows',
        'limit_daily_live_minutes', 'limit_weekly_live_minutes', 'limit_monthly_live_minutes',
        'max_history_items', 'history_retention_days'
    ]
    # --- END MODIFIED ---
    base_columns = ['name', 'description']
    base_values = [name, description]
    new_columns, new_values = _prepare_role_fields(permissions, valid_permission_columns)
    logging.info(f"[DB:Role] Prepared columns: {new_columns}, values: {new_values}")
    sql_columns = base_columns + new_columns
    sql_values = base_values + new_values
    if not sql_columns:
        return None
    placeholders = ['%s'] * len(sql_values)
    sql = f"INSERT INTO roles ({', '.join(sql_columns)}, created_at, updated_at) VALUES ({', '.join(placeholders)}, NOW(), NOW())"
    cursor = get_cursor()
    try:
        cursor.execute(sql, tuple(sql_values))
        get_db().commit()
        role_id = cursor.lastrowid
        logging.info(f"[DB:Role] Created new role '{name}' with ID {role_id}.")
        return get_role_by_id(role_id)
    except MySQLError as err:
        get_db().rollback()
        if err.errno == 1062:
            logging.warning(f"[DB:Role] Attempted to create role with duplicate name: {name}")
        else:
            logging.error(f"[DB:Role] Error creating role '{name}': {err}", exc_info=True)
        return None
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass

def get_role_by_id(role_id: int) -> Optional[Role]:
    """
    Retrieve a role by ID. Results are cached for _ROLE_CACHE_TTL seconds to
    avoid an extra DB round-trip on every authenticated request.
    Uses a fresh buffered cursor to avoid interference from any previous
    unconsumed result sets on the request-scoped cursor.
    """
    cached = _get_cached_role(role_id)
    if cached is not None:
        return cached

    sql = 'SELECT * FROM roles WHERE id = %s'
    role: Optional[Role] = None
    local_cursor = None
    try:
        conn = get_db()
        # Use a fresh cursor (dictionary=True) to isolate from any prior queries
        local_cursor = conn.cursor(dictionary=True)
        local_cursor.execute(sql, (role_id,))
        row = local_cursor.fetchone()
        if not row:
            try:
                local_cursor.execute('SELECT COUNT(*) AS c FROM roles')
                cnt_row = local_cursor.fetchone()
                total = cnt_row.get('c') if cnt_row else 'unknown'
                logging.warning(f"[DB:Role] get_role_by_id({role_id}) returned no row. roles count={total}.")
            except Exception as diag_err:
                logging.warning(f"[DB:Role] Diagnostic count failed for get_role_by_id({role_id}): {diag_err}")
        role = _map_row_to_role(row)
        _set_cached_role(role_id, role)
    except MySQLError as err:
        logging.error(f"[DB:Role] Error retrieving role by ID '{role_id}': {err}", exc_info=True)
        role = None
    finally:
        try:
            if local_cursor is not None:
                # Consume any remaining results and close this local cursor
                while local_cursor.nextset():
                    pass
                local_cursor.close()
        except Exception:
            pass
    return role

def get_role_by_name(name: str) -> Optional[Role]:
    sql = 'SELECT * FROM roles WHERE name = %s'
    cursor = None
    role = None
    try:
        cursor = get_cursor()
        cursor.execute(sql, (name,))
        row = cursor.fetchone()
        role = _map_row_to_role(row)
    except MySQLError as err:
        logging.error(f"[DB:Role] Error retrieving role by name '{name}': {err}", exc_info=True)
        role = None # Ensure role is None on error
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
    return role

def get_all_roles() -> List[Role]:
    sql = 'SELECT * FROM roles ORDER BY name'
    roles = []
    cursor = get_cursor()
    try:
        cursor.execute(sql)
        rows = cursor.fetchall()
        roles = [_map_row_to_role(row) for row in rows if row]
        logging.debug(f"[DB:Role] Retrieved {len(roles)} roles.")
    except MySQLError as err:
        logging.error(f"[DB:Role] Error retrieving all roles: {err}", exc_info=True)
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
    return roles

# This function is no longer needed as the 'monthly_usage' table has been removed.

def increment_usage(user_id: int, cost: float, minutes_processed: float) -> None:
    """
    Increments usage stats for a user after a transcription.
    """
    now = datetime.now(timezone.utc)
    date_ts = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    log_prefix = f"[DB:Usage:User:{user_id}]"
    
    cursor = get_cursor()
    try:
        sql = """
            INSERT INTO user_usage (user_id, date, cost, minutes, workflows)
            VALUES (%s, %s, %s, %s, 0)
            ON DUPLICATE KEY UPDATE
            cost = cost + VALUES(cost),
            minutes = minutes + VALUES(minutes)
        """
        cursor.execute(sql, (user_id, date_ts, cost, minutes_processed))
        get_db().commit()
        logging.debug(f"{log_prefix} Successfully incremented usage stats.")
    except MySQLError as e:
        logging.error(f"{log_prefix} Error incrementing usage stats: {e}", exc_info=True)
        get_db().rollback()
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass

def increment_workflow_usage(user_id: int) -> None:
    """
    Increments workflow usage stats for a user.
    """
    now = datetime.now(timezone.utc)
    date_ts = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    log_prefix = f"[DB:Usage:Workflow:User:{user_id}]"
    cursor = get_cursor()
    try:
        sql = """
            INSERT INTO user_usage (user_id, date, cost, minutes, workflows)
            VALUES (%s, %s, 0, 0, 1)
            ON DUPLICATE KEY UPDATE
            workflows = workflows + 1
        """
        cursor.execute(sql, (user_id, date_ts))
        get_db().commit()
        logging.debug(f"{log_prefix} Successfully incremented workflow usage stats.")
    except MySQLError as e:
        logging.error(f"{log_prefix} Error incrementing workflow usage stats: {e}", exc_info=True)
        get_db().rollback()
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass

def reserve_usage_if_allowed(
    user_id: int,
    role: Role,
    cost_to_add: float = 0.0,
    minutes_to_add: float = 0.0,
    workflows_to_add: int = 0,
) -> Tuple[bool, str]:
    """Atomically check role quotas and reserve usage under a per-user row lock."""
    now = datetime.now(timezone.utc)
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    week_start = day_start - timedelta(days=day_start.weekday())
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    connection = get_db()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
        if not cursor.fetchone():
            connection.rollback()
            return False, "User not found."

        cursor.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN date >= %s THEN cost ELSE 0 END), 0) AS daily_cost,
                COALESCE(SUM(CASE WHEN date >= %s THEN cost ELSE 0 END), 0) AS weekly_cost,
                COALESCE(SUM(CASE WHEN date >= %s THEN cost ELSE 0 END), 0) AS monthly_cost,
                COALESCE(SUM(CASE WHEN date >= %s THEN minutes ELSE 0 END), 0) AS daily_minutes,
                COALESCE(SUM(CASE WHEN date >= %s THEN minutes ELSE 0 END), 0) AS weekly_minutes,
                COALESCE(SUM(CASE WHEN date >= %s THEN minutes ELSE 0 END), 0) AS monthly_minutes,
                COALESCE(SUM(CASE WHEN date >= %s THEN workflows ELSE 0 END), 0) AS daily_workflows,
                COALESCE(SUM(CASE WHEN date >= %s THEN workflows ELSE 0 END), 0) AS weekly_workflows,
                COALESCE(SUM(CASE WHEN date >= %s THEN workflows ELSE 0 END), 0) AS monthly_workflows
            FROM user_usage WHERE user_id = %s
            """,
            (
                day_start, week_start, month_start,
                day_start, week_start, month_start,
                day_start, week_start, month_start,
                user_id,
            ),
        )
        usage = cursor.fetchone() or {}
        checks = (
            (role.limit_daily_cost, float(usage.get("daily_cost") or 0) + cost_to_add),
            (role.limit_weekly_cost, float(usage.get("weekly_cost") or 0) + cost_to_add),
            (role.limit_monthly_cost, float(usage.get("monthly_cost") or 0) + cost_to_add),
            (role.limit_daily_minutes, float(usage.get("daily_minutes") or 0) + minutes_to_add),
            (role.limit_weekly_minutes, float(usage.get("weekly_minutes") or 0) + minutes_to_add),
            (role.limit_monthly_minutes, float(usage.get("monthly_minutes") or 0) + minutes_to_add),
            (role.limit_daily_workflows, int(usage.get("daily_workflows") or 0) + workflows_to_add),
            (role.limit_weekly_workflows, int(usage.get("weekly_workflows") or 0) + workflows_to_add),
            (role.limit_monthly_workflows, int(usage.get("monthly_workflows") or 0) + workflows_to_add),
        )
        if any(limit > 0 and projected > limit for limit, projected in checks):
            connection.rollback()
            return False, "You have reached your fair use limit."

        cursor.execute(
            """
            INSERT INTO user_usage (user_id, date, cost, minutes, workflows)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                cost = cost + VALUES(cost),
                minutes = minutes + VALUES(minutes),
                workflows = workflows + VALUES(workflows)
            """,
            (user_id, day_start, cost_to_add, minutes_to_add, workflows_to_add),
        )
        connection.commit()
        return True, "Usage reserved."
    except Exception as exc:
        connection.rollback()
        logging.exception("[DB:Usage:Reserve:User:%s] Atomic usage reservation failed.", user_id)
        raise UsageReservationError("Unable to verify usage limits right now.") from exc
    finally:
        cursor.close()


def update_role(role_id: int, role_data: Dict[str, Any]) -> bool:
    """
    Updates an existing role in the database.
    Handles renamed limit fields and new workflow fields.
    """
    log_prefix = f"[DB:Role:Update:{role_id}]"
    role_data = dict(role_data or {})
    if 'use_api_openai' not in role_data:
        role_data['use_api_openai'] = any(
            bool(role_data.get(field))
            for field in (
                'use_api_openai_whisper',
                'use_api_openai_gpt_4o_transcribe',
                'use_api_openai_live_transcribe',
            )
        )
    if 'use_api_google' not in role_data:
        role_data['use_api_google'] = bool(role_data.get('use_api_google_gemini'))
    updatable_columns = [
        'name', 'description',
        'use_api_openai', 'use_api_assemblyai',
        'use_api_openai_whisper', 'use_api_openai_gpt_4o_transcribe',
        'use_api_openai_live_transcribe',
        'use_api_google', 'use_api_google_gemini',
        'use_api_openrouter',
        'access_admin_panel', 'allow_large_files', 'allow_context_prompt',
        'allow_api_key_management', 'allow_public_api_access', 'allow_download_transcript',
        'allow_workflows', 'manage_workflow_templates', 'allow_auto_title_generation', 'allow_speaker_diarization',
        'default_transcription_model', 'default_title_generation_model', 'default_workflow_model', 'default_openrouter_model', 'default_live_transcription_model',
        'limit_daily_cost', 'limit_weekly_cost', 'limit_monthly_cost',
        'limit_daily_minutes', 'limit_weekly_minutes', 'limit_monthly_minutes',
        'limit_daily_workflows', 'limit_weekly_workflows', 'limit_monthly_workflows',
        'limit_daily_live_minutes', 'limit_weekly_live_minutes', 'limit_monthly_live_minutes',
        'max_history_items', 'history_retention_days'
    ]
    # --- END MODIFIED ---
    set_clauses = []
    sql_values = []
    new_columns, new_values = _prepare_role_fields(role_data, updatable_columns)
    for col, value in zip(new_columns, new_values):
        set_clauses.append(f"{col} = %s")
        sql_values.append(value)
    set_clauses.append("updated_at = %s")
    sql_values.append(datetime.now(timezone.utc))
    sql_values.append(role_id)
    if not set_clauses or len(set_clauses) == 1:  # Only updated_at added
        logging.warning(f"{log_prefix} No valid fields provided for update.")
        return False
    sql = f"UPDATE roles SET {', '.join(set_clauses)} WHERE id = %s"
    cursor = get_cursor()
    try:
        cursor.execute(sql, tuple(sql_values))
        get_db().commit()
        if cursor.rowcount > 0:
            invalidate_role_cache(role_id)
            logging.info(f"{log_prefix} Role updated successfully.")
            return True
        else:
            role = get_role_by_id(role_id)
            if role:
                logging.warning(f"{log_prefix} Role update query executed but no rows affected (data might be unchanged).")
                return True
            else:
                logging.warning(f"{log_prefix} Role update failed: Role with ID {role_id} not found.")
                return False
    except MySQLError as err:
        get_db().rollback()
        if err.errno == 1062:
            logging.warning(f"{log_prefix} Role update failed due to duplicate name: {role_data.get('name')}")
        else:
            logging.error(f"{log_prefix} Error updating role: {err}", exc_info=True)
        return False
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass

def delete_role(role_id: int) -> Tuple[bool, str]:
    """
    Deletes a role from the database after performing safety checks.

    Args:
        role_id: The ID of the role to delete.

    Returns:
        A tuple: (success: bool, message: str).
    """
    log_prefix = f"[DB:Role:Delete:{role_id}]"
    cursor = None
    delete_cursor = None
    try:
        cursor = get_cursor()
        cursor.execute("SELECT name FROM roles WHERE id = %s", (role_id,))
        role_row = cursor.fetchone()
        if not role_row:
            logging.warning(f"{log_prefix} Role not found.")
            return False, "Role not found."
        role_name = role_row['name']
        if role_name in ['admin', 'beta-tester']:
            logging.warning(f"{log_prefix} Attempt to delete protected default role '{role_name}'.")
            return False, f"Cannot delete protected default role '{role_name}'."
        cursor.execute("SELECT COUNT(*) as user_count FROM users WHERE role_id = %s", (role_id,))
        user_count_row = cursor.fetchone()
        cursor.fetchall()  # Consume remaining results
        user_count = user_count_row['user_count'] if user_count_row else 0
        if user_count > 0:
            logging.warning(f"{log_prefix} Cannot delete role '{role_name}' as {user_count} user(s) are assigned to it.")
            return False, f"Cannot delete role '{role_name}' as {user_count} user(s) are assigned to it. Reassign users first."
        delete_cursor = get_cursor()
        delete_cursor.execute("DELETE FROM roles WHERE id = %s", (role_id,))
        get_db().commit()
        if delete_cursor.rowcount > 0:
            invalidate_role_cache(role_id)
            logging.info(f"{log_prefix} Role '{role_name}' deleted successfully.")
            return True, f"Role '{role_name}' deleted successfully."
        else:
            logging.error(f"{log_prefix} Delete query executed but no rows affected for role '{role_name}'.")
            return False, "Role deletion failed unexpectedly after checks."
    except MySQLError as err:
        get_db().rollback()
        logging.error(f"{log_prefix} Error deleting role: {err}", exc_info=True)
        return False, "Database error occurred during role deletion."
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
def init_user_usage_table() -> None:
    cursor = get_cursor()
    log_prefix = "[DB:Schema:MySQL]"
    logging.info(f"{log_prefix} Checking/Initializing 'user_usage' table...")
    try:
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS user_usage (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user_id INT NOT NULL,
                date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                cost DECIMAL(10, 4) NOT NULL DEFAULT 0.0000,
                minutes DECIMAL(12, 2) NOT NULL DEFAULT 0.00,
                workflows INT NOT NULL DEFAULT 0,
                live_minutes DECIMAL(12, 2) NOT NULL DEFAULT 0.00,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
                UNIQUE KEY uk_user_date (user_id, date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            '''
        )
        cursor.execute("SHOW COLUMNS FROM user_usage LIKE 'date'")
        date_col = cursor.fetchone()
        cursor.fetchall()
        date_type = (date_col.get('Type') if isinstance(date_col, dict) else (date_col[1] if date_col else "")).lower()
        if date_col and 'timestamp' not in date_type:
            logging.info(f"{log_prefix} Converting 'date' column on 'user_usage' table to TIMESTAMP.")
            cursor.execute("ALTER TABLE user_usage MODIFY COLUMN date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP")
        cursor.execute("SHOW COLUMNS FROM user_usage LIKE 'minutes'")
        minutes_col = cursor.fetchone()
        cursor.fetchall()
        minutes_raw_type = minutes_col.get('Type') if isinstance(minutes_col, dict) else (minutes_col[1] if minutes_col else "")
        minutes_type = str(minutes_raw_type or "").lower()
        if minutes_col and 'decimal' not in minutes_type:
            logging.info(f"{log_prefix} Converting user_usage.minutes to DECIMAL for accurate quota accounting.")
            cursor.execute("ALTER TABLE user_usage MODIFY COLUMN minutes DECIMAL(12, 2) NOT NULL DEFAULT 0.00")
        cursor.execute("SHOW COLUMNS FROM user_usage LIKE 'live_minutes'")
        live_minutes_col = cursor.fetchone()
        cursor.fetchall()
        if not live_minutes_col:
            logging.info(f"{log_prefix} Adding 'live_minutes' column to 'user_usage' table.")
            cursor.execute("ALTER TABLE user_usage ADD COLUMN live_minutes DECIMAL(12, 2) NOT NULL DEFAULT 0.00")
        get_db().commit()
        logging.info(f"{log_prefix} 'user_usage' table schema verified/initialized.")
    except MySQLError as err:
        logging.error(f"{log_prefix} Error during 'user_usage' table initialization:. {err}", exc_info=True)
        get_db().rollback()
        raise
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
