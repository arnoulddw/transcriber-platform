import json
import logging

log = logging.getLogger(__name__)


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute("SHOW TABLES LIKE %s", (table_name,))
    exists = cursor.fetchone() is not None
    cursor.fetchall()
    return exists


def _column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM {table} LIKE %s", (column,))
    exists = cursor.fetchone() is not None
    cursor.fetchall()
    return exists


def upgrade(db):
    """
    Migrate legacy users.api_keys_encrypted JSON blob into user_api_keys rows,
    then drop the old column.
    """
    cursor = db.cursor(dictionary=True)
    log_prefix = "[Migration:V20251122_0005]"

    try:
        log.info(f"{log_prefix} Starting migration of user API keys.")

        # Ensure destination table exists
        if not _table_exists(cursor, "user_api_keys"):
            log.info(f"{log_prefix} Creating missing user_api_keys table.")
            cursor.execute(
                """
                CREATE TABLE user_api_keys (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    user_id INT NOT NULL,
                    provider_code VARCHAR(80) NOT NULL,
                    encrypted_key MEDIUMTEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_user_provider (user_id, provider_code),
                    INDEX idx_user_api_key_provider (provider_code),
                    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
                """
            )
            db.commit()

        if not _column_exists(cursor, "users", "api_keys_encrypted"):
            log.info(f"{log_prefix} Column 'api_keys_encrypted' already removed. Nothing to migrate.")
            return

        # Fetch all legacy blobs
        cursor.execute("SELECT id, api_keys_encrypted FROM users WHERE api_keys_encrypted IS NOT NULL")
        rows = cursor.fetchall() or []
        migrated_count = 0

        for row in rows:
            user_id = row["id"]
            raw_json = row["api_keys_encrypted"]
            try:
                keys_dict = json.loads(raw_json) if raw_json else {}
            except (json.JSONDecodeError, TypeError):
                log.warning(f"{log_prefix} Skipping user {user_id}: invalid JSON payload.")
                continue

            if not isinstance(keys_dict, dict):
                log.warning(f"{log_prefix} Skipping user {user_id}: payload is not a dict.")
                continue

            for provider_code, encrypted_key in keys_dict.items():
                if not encrypted_key:
                    continue
                cursor.execute(
                    """
                    INSERT INTO user_api_keys (user_id, provider_code, encrypted_key)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE encrypted_key = VALUES(encrypted_key), updated_at = CURRENT_TIMESTAMP
                    """,
                    (user_id, provider_code.lower(), encrypted_key),
                )
                migrated_count += 1

        db.commit()
        log.info(f"{log_prefix} Migrated {migrated_count} API key entries to user_api_keys.")

        # Drop legacy column after migration
        log.info(f"{log_prefix} Dropping legacy column users.api_keys_encrypted.")
        cursor.execute("ALTER TABLE users DROP COLUMN api_keys_encrypted")
        db.commit()
        log.info(f"{log_prefix} Migration completed successfully.")
    except Exception as err:
        db.rollback()
        log.error(f"{log_prefix} Migration failed: {err}", exc_info=True)
        raise
