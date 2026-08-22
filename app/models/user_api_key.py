import logging
from typing import Any, Dict, List, Optional

from mysql.connector import Error as MySQLError

from app.database import get_cursor, get_db


def init_db_command() -> None:
    """Initializes the 'user_api_keys' table schema."""
    cursor = get_cursor()
    log_prefix = "[DB:Schema:MySQL]"
    logging.info(f"{log_prefix} Checking/Initializing 'user_api_keys' table...")
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_api_keys (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user_id INT NOT NULL,
                provider_code VARCHAR(80) NOT NULL,
                model_slug VARCHAR(120) NOT NULL DEFAULT '',
                model_purposes VARCHAR(64) NOT NULL DEFAULT 'transcription',
                encrypted_key MEDIUMTEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP NULL DEFAULT NULL,
                UNIQUE KEY uq_user_provider_model (user_id, provider_code, model_slug),
                INDEX idx_user_api_key_provider (provider_code),
                INDEX idx_user_api_key_last_used (user_id, provider_code, last_used_at),
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """
        )
        # Ensure columns are correctly typed if table already exists
        for col_name, col_def in (
            ("created_at", "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"),
            ("updated_at", "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ):
            cursor.execute("SHOW COLUMNS FROM user_api_keys LIKE %s", (col_name,))
            col_info = cursor.fetchone()
            cursor.fetchall()
            col_type = (col_info.get('Type') if isinstance(col_info, dict) else (col_info[1] if col_info else "")).lower()
            if col_info and 'timestamp' not in col_type:
                logging.info(f"{log_prefix} Converting '{col_name}' column on 'user_api_keys' table to TIMESTAMP.")
                cursor.execute(f"ALTER TABLE user_api_keys MODIFY COLUMN {col_name} {col_def}")

        # Add columns introduced after the original provider-wide schema.
        for col_name, col_def, position in (
            ("model_slug", "VARCHAR(120) NOT NULL DEFAULT ''", "AFTER provider_code"),
            ("model_purposes", "VARCHAR(64) NOT NULL DEFAULT 'transcription'", "AFTER model_slug"),
            ("last_used_at", "TIMESTAMP NULL DEFAULT NULL", "AFTER updated_at"),
        ):
            cursor.execute("SHOW COLUMNS FROM user_api_keys LIKE %s", (col_name,))
            col_info = cursor.fetchone()
            cursor.fetchall()
            if not col_info:
                logging.info(f"{log_prefix} Adding '{col_name}' column to 'user_api_keys'.")
                cursor.execute(
                    f"ALTER TABLE user_api_keys ADD COLUMN {col_name} {col_def} {position}"
                )

        # Create the replacement index before dropping the legacy one. The
        # legacy index may be the only index currently supporting the foreign
        # key on user_id, so MySQL rejects dropping it until another suitable
        # index exists.
        cursor.execute("SHOW INDEX FROM user_api_keys WHERE Key_name = 'uq_user_provider_model'")
        scoped_unique_exists = cursor.fetchone()
        cursor.fetchall()
        if not scoped_unique_exists:
            logging.info(f"{log_prefix} Adding uq_user_provider_model index.")
            cursor.execute(
                "ALTER TABLE user_api_keys ADD UNIQUE INDEX "
                "uq_user_provider_model (user_id, provider_code, model_slug)"
            )

        cursor.execute("SHOW INDEX FROM user_api_keys WHERE Key_name = 'uq_user_provider'")
        legacy_unique_exists = cursor.fetchone()
        cursor.fetchall()
        if legacy_unique_exists:
            logging.info(f"{log_prefix} Replacing legacy uq_user_provider index.")
            cursor.execute("ALTER TABLE user_api_keys DROP INDEX uq_user_provider")

        cursor.execute("SHOW INDEX FROM user_api_keys WHERE Key_name = 'idx_user_api_key_last_used'")
        last_used_index_exists = cursor.fetchone()
        cursor.fetchall()
        if not last_used_index_exists:
            cursor.execute(
                "ALTER TABLE user_api_keys ADD INDEX idx_user_api_key_last_used "
                "(user_id, provider_code, last_used_at)"
            )

        get_db().commit()
        logging.info(f"{log_prefix} 'user_api_keys' table schema verified/initialized.")
    except MySQLError as err:
        logging.error(f"{log_prefix} Error during 'user_api_keys' table initialization: {err}", exc_info=True)
        get_db().rollback()
        raise


def _stored_model_slug(provider_code: str, model_slug: Optional[str]) -> str:
    """Return the normalized storage key for any provider/model pair.

    Empty model names remain valid for legacy provider-wide keys. New UI writes
    a model name, which lets one provider own multiple independently managed
    keys without changing the table shape.
    """
    return str(model_slug or "").strip()


def upsert_api_key(
    user_id: int,
    provider_code: str,
    encrypted_key: str,
    model_slug: Optional[str] = None,
    model_purpose: Optional[str] = None,
) -> bool:
    provider = provider_code.lower()
    stored_model_slug = _stored_model_slug(provider, model_slug)
    if model_purpose is None:
        # Preserve the legacy write shape for callers that predate purpose
        # metadata. New rows receive the database default.
        sql = """
            INSERT INTO user_api_keys (user_id, provider_code, model_slug, encrypted_key, last_used_at)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON DUPLICATE KEY UPDATE
                encrypted_key = VALUES(encrypted_key),
                updated_at = CURRENT_TIMESTAMP,
                last_used_at = CURRENT_TIMESTAMP
        """
        params = (user_id, provider, stored_model_slug, encrypted_key)
    else:
        purpose = str(model_purpose).strip().lower()
        if purpose not in {"transcription", "llm", "live"}:
            raise ValueError(f"Invalid model purpose: {purpose}")
        sql = """
            INSERT INTO user_api_keys (
                user_id, provider_code, model_slug, model_purposes, encrypted_key, last_used_at
            )
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON DUPLICATE KEY UPDATE
                encrypted_key = VALUES(encrypted_key),
                model_purposes = IF(
                    FIND_IN_SET(VALUES(model_purposes), model_purposes),
                    model_purposes,
                    CONCAT_WS(',', NULLIF(model_purposes, ''), VALUES(model_purposes))
                ),
                updated_at = CURRENT_TIMESTAMP,
                last_used_at = CURRENT_TIMESTAMP
        """
        params = (user_id, provider, stored_model_slug, purpose, encrypted_key)
    cursor = get_cursor()
    try:
        cursor.execute(sql, params)
        get_db().commit()
        return True
    except MySQLError as err:
        logging.error(
            f"[DB:UserApiKey] Error upserting API key for user {user_id}, "
            f"provider {provider_code}, model {stored_model_slug}: {err}",
            exc_info=True,
        )
        get_db().rollback()
        return False


def get_api_key_record(
    user_id: int,
    provider_code: str,
    model_slug: Optional[str] = None,
    *,
    allow_model_fallback: bool = False,
) -> Optional[Dict[str, Any]]:
    provider = provider_code.lower()
    stored_model_slug = _stored_model_slug(provider, model_slug)
    if stored_model_slug and allow_model_fallback:
        # The save flow uses this opt-in lookup only when the user submits a
        # masked key for a new model. Prefer that exact model, otherwise reuse
        # the most recently used key for the same provider. Runtime lookups
        # keep the default exact-model behavior below.
        sql = """
            SELECT id, provider_code, model_slug, model_purposes, encrypted_key, created_at, updated_at, last_used_at
            FROM user_api_keys
            WHERE user_id = %s
              AND provider_code = %s
            ORDER BY CASE WHEN model_slug = %s THEN 0 ELSE 1 END,
                     COALESCE(last_used_at, updated_at, created_at) DESC,
                     id DESC
            LIMIT 1
        """
        params = (user_id, provider, stored_model_slug)
    elif provider == "openrouter" and not stored_model_slug:
        sql = """
            SELECT id, provider_code, model_slug, model_purposes, encrypted_key, created_at, updated_at, last_used_at
            FROM user_api_keys
            WHERE user_id = %s
              AND provider_code = %s
              AND model_slug = %s
            ORDER BY COALESCE(last_used_at, updated_at, created_at) DESC,
                     id DESC
            LIMIT 1
        """
        params = (user_id, provider, stored_model_slug)
    elif stored_model_slug:
        sql = """
            SELECT id, provider_code, model_slug, model_purposes, encrypted_key, created_at, updated_at, last_used_at
            FROM user_api_keys
            WHERE user_id = %s
              AND provider_code = %s
              AND model_slug = %s
            ORDER BY COALESCE(last_used_at, updated_at, created_at) DESC,
                     id DESC
            LIMIT 1
        """
        params = (user_id, provider, stored_model_slug)
    else:
        sql = """
            SELECT id, provider_code, model_slug, model_purposes, encrypted_key, created_at, updated_at, last_used_at
            FROM user_api_keys
            WHERE user_id = %s
              AND provider_code = %s
              AND model_slug = %s
            ORDER BY COALESCE(last_used_at, updated_at, created_at) DESC,
                     id DESC
            LIMIT 1
        """
        params = (user_id, provider, stored_model_slug)

    cursor = get_cursor()
    try:
        cursor.execute(sql, params)
        return cursor.fetchone()
    except MySQLError as err:
        logging.error(
            f"[DB:UserApiKey] Error retrieving API key for user {user_id}, "
            f"provider {provider_code}, model {stored_model_slug}: {err}",
            exc_info=True,
        )
        return None


def get_api_key(
    user_id: int,
    provider_code: str,
    model_slug: Optional[str] = None,
) -> Optional[str]:
    record = get_api_key_record(user_id, provider_code, model_slug)
    if not record:
        return None
    encrypted_key = record.get("encrypted_key")
    return encrypted_key if isinstance(encrypted_key, str) else None


def mark_api_key_used(user_id: int, key_id: int) -> bool:
    cursor = get_cursor()
    try:
        cursor.execute(
            "UPDATE user_api_keys SET last_used_at = CURRENT_TIMESTAMP "
            "WHERE id = %s AND user_id = %s",
            (key_id, user_id),
        )
        get_db().commit()
        return cursor.rowcount > 0
    except MySQLError as err:
        logging.error(
            f"[DB:UserApiKey] Error marking key {key_id} as used for user {user_id}: {err}",
            exc_info=True,
        )
        get_db().rollback()
        return False


def delete_api_key(
    user_id: int,
    provider_code: str,
    model_slug: Optional[str] = None,
) -> bool:
    provider = provider_code.lower()
    stored_model_slug = _stored_model_slug(provider, model_slug)
    if model_slug is not None and stored_model_slug:
        sql = (
            "DELETE FROM user_api_keys WHERE user_id = %s AND provider_code = %s "
            "AND model_slug = %s"
        )
        params = (user_id, provider, stored_model_slug)
    else:
        sql = "DELETE FROM user_api_keys WHERE user_id = %s AND provider_code = %s"
        params = (user_id, provider)

    cursor = get_cursor()
    try:
        cursor.execute(sql, params)
        get_db().commit()
        return cursor.rowcount > 0
    except MySQLError as err:
        logging.error(
            f"[DB:UserApiKey] Error deleting API key for user {user_id}, "
            f"provider {provider_code}, model {stored_model_slug}: {err}",
            exc_info=True,
        )
        get_db().rollback()
        return False


def get_api_key_record_by_id(
    user_id: int,
    key_id: int,
) -> Optional[Dict[str, Any]]:
    """Return one owned key row (provider/model/purposes) before deletion."""
    cursor = get_cursor()
    try:
        cursor.execute(
            """
            SELECT id, provider_code, model_slug, model_purposes
            FROM user_api_keys
            WHERE id = %s AND user_id = %s
            LIMIT 1
            """,
            (key_id, user_id),
        )
        return cursor.fetchone()
    except MySQLError as err:
        logging.error(
            "[DB:UserApiKey] Error retrieving key %s for user %s: %s",
            key_id,
            user_id,
            err,
            exc_info=True,
        )
        return None


def delete_api_key_by_id(user_id: int, key_id: int) -> bool:
    """Delete exactly one stored key owned by the user."""
    cursor = get_cursor()
    try:
        cursor.execute(
            "DELETE FROM user_api_keys WHERE id = %s AND user_id = %s",
            (key_id, user_id),
        )
        get_db().commit()
        return cursor.rowcount > 0
    except MySQLError as err:
        logging.error(
            "[DB:UserApiKey] Error deleting key %s for user %s: %s",
            key_id,
            user_id,
            err,
            exc_info=True,
        )
        get_db().rollback()
        return False


def delete_all_api_keys_for_user(user_id: int) -> None:
    cursor = get_cursor()
    try:
        cursor.execute("DELETE FROM user_api_keys WHERE user_id = %s", (user_id,))
        get_db().commit()
    except MySQLError as err:
        logging.error(f"[DB:UserApiKey] Error deleting all API keys for user {user_id}: {err}", exc_info=True)
        get_db().rollback()


def get_api_key_records_by_user(
    user_id: int,
    provider_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    sql = """
        SELECT id, provider_code, model_slug, model_purposes, encrypted_key, created_at, updated_at, last_used_at
        FROM user_api_keys
        WHERE user_id = %s
    """
    params: List[Any] = [user_id]
    if provider_code:
        sql += " AND provider_code = %s"
        params.append(provider_code.lower())
    sql += " ORDER BY COALESCE(last_used_at, updated_at, created_at) DESC, id DESC"

    cursor = get_cursor()
    try:
        cursor.execute(sql, tuple(params))
        return cursor.fetchall()
    except MySQLError as err:
        logging.error(
            f"[DB:UserApiKey] Error fetching API key records for user {user_id}: {err}",
            exc_info=True,
        )
        return []


def get_all_api_key_records() -> List[Dict[str, Any]]:
    """Return API key records for every user (used for admin model aggregation)."""
    sql = """
        SELECT id, user_id, provider_code, model_slug, model_purposes, encrypted_key, created_at, updated_at, last_used_at
        FROM user_api_keys
        ORDER BY COALESCE(last_used_at, updated_at, created_at) DESC, id DESC
    """
    cursor = get_cursor()
    try:
        cursor.execute(sql)
        return cursor.fetchall()
    except MySQLError as err:
        logging.error(
            "[DB:UserApiKey] Error fetching all API key records: %s",
            err,
            exc_info=True,
        )
        return []


def get_admin_api_key_records(
    provider_code: Optional[str] = None,
    model_slug: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return keys owned by users whose role can access the admin panel.

    Admin-managed model keys are the shared credentials that users without
    key-management permission may use. When ``model_slug`` is supplied, only
    that exact model row is returned; a provider-wide row is never a match for
    a named model.
    """
    sql = """
        SELECT k.id, k.user_id, k.provider_code, k.model_slug,
               k.model_purposes, k.encrypted_key, k.created_at,
               k.updated_at, k.last_used_at
        FROM user_api_keys AS k
        INNER JOIN users AS u ON u.id = k.user_id
        INNER JOIN roles AS r ON r.id = u.role_id
        WHERE r.access_admin_panel = TRUE
    """
    params: List[Any] = []
    if provider_code:
        provider = provider_code.lower()
        sql += " AND k.provider_code = %s"
        params.append(provider)
    if model_slug is not None:
        stored_model_slug = _stored_model_slug(provider_code or "", model_slug)
        sql += " AND k.model_slug = %s"
        params.append(stored_model_slug)
    sql += " ORDER BY COALESCE(k.last_used_at, k.updated_at, k.created_at) DESC, k.id DESC"

    cursor = get_cursor()
    try:
        cursor.execute(sql, tuple(params))
        return cursor.fetchall() or []
    except MySQLError as err:
        logging.error(
            "[DB:UserApiKey] Error fetching admin API key records: %s",
            err,
            exc_info=True,
        )
        return []


def get_distinct_model_names() -> List[str]:
    """Return the non-empty model names configured in user-managed API keys."""
    cursor = get_cursor()
    try:
        cursor.execute(
            """
            SELECT DISTINCT model_slug
            FROM user_api_keys
            WHERE model_slug IS NOT NULL AND TRIM(model_slug) <> ''
            ORDER BY model_slug
            """
        )
        rows = cursor.fetchall() or []
    except MySQLError as err:
        logging.error("[DB:UserApiKey] Error fetching distinct configured model names: %s", err, exc_info=True)
        return []

    model_names = []
    for row in rows:
        raw_name = row.get("model_slug") if isinstance(row, dict) else row[0]
        model_name = str(raw_name or "").strip()
        if model_name and model_name not in model_names:
            model_names.append(model_name)
    return model_names


def get_api_keys_by_user(user_id: int) -> Dict[str, str]:
    keys: Dict[str, str] = {}
    for row in get_api_key_records_by_user(user_id):
        provider = row.get("provider_code")
        encrypted_key = row.get("encrypted_key")
        if provider and provider not in keys and isinstance(encrypted_key, str):
            keys[provider] = encrypted_key
    return keys
