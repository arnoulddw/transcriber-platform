import logging

from mysql.connector import Error as MySQLError

from app.database import get_db, get_cursor

logger = logging.getLogger(__name__)


def _get_sql_mode(cursor):
    cursor.execute("SELECT @@SESSION.sql_mode")
    row = cursor.fetchone()
    if not row:
        return None
    if isinstance(row, dict):
        return row.get('@@SESSION.sql_mode') or row.get('sql_mode')
    if isinstance(row, (list, tuple)):
        return row[0]
    return str(row)


def _temporarily_allow_zero_dates(cursor):
    """
    Removes NO_ZERO_DATE/NO_ZERO_IN_DATE for the session so we can clean bad values.
    Returns the original sql_mode so it can be restored.
    """
    original_mode = _get_sql_mode(cursor)
    if not original_mode:
        return None
    modes = [m for m in original_mode.split(',') if m not in ('NO_ZERO_DATE', 'NO_ZERO_IN_DATE')]
    new_mode = ",".join(modes)
    if new_mode != original_mode:
        cursor.execute("SET SESSION sql_mode = %s", (new_mode,))
    return original_mode


def _restore_sql_mode(cursor, original_mode):
    if original_mode is None:
        return
    try:
        cursor.execute("SET SESSION sql_mode = %s", (original_mode,))
    except Exception:
        pass


def init_db_command() -> None:
    cursor = get_cursor()
    conn = get_db()
    log_prefix = "[DB:Schema:MySQL]"
    logger.info(f"{log_prefix} Checking/Initializing 'users' table schema...")
    try:
        cursor.execute("SHOW TABLES LIKE 'roles'")
        if not cursor.fetchone():
            logger.error(f"{log_prefix} Cannot initialize 'users' table: 'roles' table does not exist yet.")
            raise RuntimeError("Roles table must exist before users table can be initialized.")
        cursor.fetchall()

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS users (
                id INT PRIMARY KEY AUTO_INCREMENT,
                username VARCHAR(80) UNIQUE NOT NULL,
                email VARCHAR(120) UNIQUE NOT NULL,
                password_hash VARCHAR(255),
                role_id INT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                first_name VARCHAR(100),
                last_name VARCHAR(100),
                oauth_provider VARCHAR(50),
                oauth_provider_id VARCHAR(255),
                default_content_language VARCHAR(10),
                default_transcription_model VARCHAR(255),
                default_title_generation_model VARCHAR(100) DEFAULT NULL,
                default_workflow_model VARCHAR(100) DEFAULT NULL,
                default_live_transcription_model VARCHAR(255) DEFAULT NULL,
                enable_auto_title_generation BOOLEAN NOT NULL DEFAULT FALSE,
                public_api_key_hash VARCHAR(128),
                public_api_key_last_four VARCHAR(12),
                public_api_key_created_at TIMESTAMP NULL DEFAULT NULL,
                FOREIGN KEY (role_id) REFERENCES roles (id) ON DELETE SET NULL,
                UNIQUE KEY uk_oauth (oauth_provider, oauth_provider_id),
                INDEX idx_username (username),
                INDEX idx_email (email),
                INDEX idx_oauth (oauth_provider, oauth_provider_id),
                INDEX idx_user_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            '''
        )
        logger.debug(f"{log_prefix} CREATE TABLE IF NOT EXISTS users executed.")

        cursor.execute("SHOW COLUMNS FROM users LIKE 'last_name'")
        last_name_exists = cursor.fetchone()
        cursor.fetchall()
        if not last_name_exists:
            logger.info(f"{log_prefix} Adding 'last_name' column to 'users' table.")
            cursor.execute("ALTER TABLE users ADD COLUMN last_name VARCHAR(100) AFTER first_name")

        cursor.execute("SHOW COLUMNS FROM users LIKE 'oauth_provider'")
        oauth_provider_exists = cursor.fetchone()
        cursor.fetchall()
        if not oauth_provider_exists:
            logger.info(f"{log_prefix} Adding 'oauth_provider' column to 'users' table.")
            cursor.execute("ALTER TABLE users ADD COLUMN oauth_provider VARCHAR(50) AFTER last_name")

        cursor.execute("SHOW COLUMNS FROM users LIKE 'oauth_provider_id'")
        oauth_provider_id_exists = cursor.fetchone()
        cursor.fetchall()
        if not oauth_provider_id_exists:
            logger.info(f"{log_prefix} Adding 'oauth_provider_id' column to 'users' table.")
            cursor.execute("ALTER TABLE users ADD COLUMN oauth_provider_id VARCHAR(255) AFTER oauth_provider")

        cursor.execute("SHOW COLUMNS FROM users LIKE 'default_content_language'")
        default_lang_exists = cursor.fetchone()
        cursor.fetchall()
        if not default_lang_exists:
            logger.info(f"{log_prefix} Adding 'default_content_language' column to 'users' table.")
            cursor.execute("ALTER TABLE users ADD COLUMN default_content_language VARCHAR(10) AFTER oauth_provider_id")

        cursor.execute("SHOW COLUMNS FROM users LIKE 'default_transcription_model'")
        default_model_exists = cursor.fetchone()
        cursor.fetchall()
        if not default_model_exists:
            logger.info(f"{log_prefix} Adding 'default_transcription_model' column to 'users' table.")
            cursor.execute("ALTER TABLE users ADD COLUMN default_transcription_model VARCHAR(255) AFTER default_content_language")
        else:
            model_type = default_model_exists.get('Type', '') if isinstance(default_model_exists, dict) else (default_model_exists[1] if len(default_model_exists) > 1 else '')
            if '255' not in str(model_type):
                logger.info(f"{log_prefix} Widening 'default_transcription_model' for provider-qualified model keys.")
                cursor.execute("ALTER TABLE users MODIFY COLUMN default_transcription_model VARCHAR(255)")

        cursor.execute("SHOW COLUMNS FROM users LIKE 'default_title_generation_model'")
        default_title_model_exists = cursor.fetchone()
        cursor.fetchall()
        if not default_title_model_exists:
            logger.info(f"{log_prefix} Adding 'default_title_generation_model' column to 'users' table.")
            cursor.execute("ALTER TABLE users ADD COLUMN default_title_generation_model VARCHAR(100) DEFAULT NULL AFTER default_transcription_model")

        cursor.execute("SHOW COLUMNS FROM users LIKE 'default_workflow_model'")
        default_workflow_model_exists = cursor.fetchone()
        cursor.fetchall()
        if not default_workflow_model_exists:
            logger.info(f"{log_prefix} Adding 'default_workflow_model' column to 'users' table.")
            cursor.execute("ALTER TABLE users ADD COLUMN default_workflow_model VARCHAR(100) DEFAULT NULL AFTER default_title_generation_model")

        cursor.execute("SHOW COLUMNS FROM users LIKE 'default_live_transcription_model'")
        default_live_model_exists = cursor.fetchone()
        cursor.fetchall()
        if not default_live_model_exists:
            logger.info(f"{log_prefix} Adding 'default_live_transcription_model' column to 'users' table.")
            cursor.execute("ALTER TABLE users ADD COLUMN default_live_transcription_model VARCHAR(255) DEFAULT NULL AFTER default_workflow_model")
        else:
            live_model_type = default_live_model_exists.get('Type', '') if isinstance(default_live_model_exists, dict) else (default_live_model_exists[1] if len(default_live_model_exists) > 1 else '')
            if '255' not in str(live_model_type):
                logger.info(f"{log_prefix} Widening 'default_live_transcription_model' for provider-qualified model keys.")
                cursor.execute("ALTER TABLE users MODIFY COLUMN default_live_transcription_model VARCHAR(255)")

        cursor.execute("SHOW COLUMNS FROM users LIKE 'enable_auto_title_generation'")
        auto_title_exists = cursor.fetchone()
        cursor.fetchall()
        if not auto_title_exists:
            logger.info(f"{log_prefix} Adding 'enable_auto_title_generation' column (BOOLEAN) to 'users' table.")
            cursor.execute("ALTER TABLE users ADD COLUMN enable_auto_title_generation BOOLEAN NOT NULL DEFAULT FALSE AFTER default_transcription_model")

        cursor.execute("SHOW COLUMNS FROM users LIKE 'language'")
        language_col_exists = cursor.fetchone()
        cursor.fetchall()
        if not language_col_exists:
            logger.info(f"{log_prefix} Adding 'language' column to 'users' table.")
            cursor.execute("ALTER TABLE users ADD COLUMN language VARCHAR(10) DEFAULT NULL AFTER default_transcription_model")

        cursor.execute("SHOW COLUMNS FROM users LIKE 'public_api_key_hash'")
        public_api_key_hash_exists = cursor.fetchone()
        cursor.fetchall()
        if not public_api_key_hash_exists:
            logger.info(f"{log_prefix} Adding 'public_api_key_hash' column to 'users' table.")
            cursor.execute("ALTER TABLE users ADD COLUMN public_api_key_hash VARCHAR(128) DEFAULT NULL AFTER created_at")

        cursor.execute("SHOW COLUMNS FROM users LIKE 'public_api_key_last_four'")
        public_api_key_last_four_exists = cursor.fetchone()
        cursor.fetchall()
        if not public_api_key_last_four_exists:
            logger.info(f"{log_prefix} Adding 'public_api_key_last_four' column to 'users' table.")
            cursor.execute("ALTER TABLE users ADD COLUMN public_api_key_last_four VARCHAR(12) DEFAULT NULL AFTER public_api_key_hash")

        cursor.execute("SHOW COLUMNS FROM users LIKE 'public_api_key_created_at'")
        public_api_key_created_at_exists = cursor.fetchone()
        cursor.fetchall()
        if not public_api_key_created_at_exists:
            logger.info(f"{log_prefix} Adding 'public_api_key_created_at' column to 'users' table.")
            cursor.execute("ALTER TABLE users ADD COLUMN public_api_key_created_at TIMESTAMP NULL DEFAULT NULL AFTER public_api_key_last_four")

        cursor.execute("SHOW COLUMNS FROM users LIKE 'api_keys_encrypted'")
        api_keys_encrypted_exists = cursor.fetchone()
        cursor.fetchall()
        if api_keys_encrypted_exists:
            logger.info(f"{log_prefix} Dropping deprecated 'api_keys_encrypted' column from 'users' table.")
            cursor.execute("ALTER TABLE users DROP COLUMN api_keys_encrypted")

        # Normalize timestamp-like columns to avoid invalid zero dates while preserving existing data
        timestamp_columns = {
            'created_at': "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
            'public_api_key_created_at': "TIMESTAMP NULL DEFAULT NULL",
            'plan_start_at': "TIMESTAMP NULL DEFAULT NULL",
            'plan_end_at': "TIMESTAMP NULL DEFAULT NULL",
        }
        for col_name, col_def in timestamp_columns.items():
            cursor.execute(f"SHOW COLUMNS FROM users LIKE '{col_name}'")
            col_info = cursor.fetchone()
            cursor.fetchall()
            if not col_info:
                # Column missing; add fresh
                logger.info(f"{log_prefix} Adding missing '{col_name}' column to 'users' table.")
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
                continue

            original_mode = _temporarily_allow_zero_dates(cursor)
            try:
                replacement_expr = "NOW()" if col_name == 'created_at' else "NULL"
                cursor.execute(
                    f"UPDATE users SET {col_name} = {replacement_expr} WHERE {col_name} IN ('0000-00-00 00:00:00', '0000-00-00')"
                )
            finally:
                _restore_sql_mode(cursor, original_mode)

            col_type = ""
            if isinstance(col_info, dict):
                col_type = str(col_info.get('Type', '')).lower()
            elif isinstance(col_info, (list, tuple)) and len(col_info) > 1:
                col_type = str(col_info[1]).lower()

            if 'timestamp' not in col_type:
                logger.info(f"{log_prefix} Converting '{col_name}' column on 'users' table to TIMESTAMP.")
                cursor.execute(f"ALTER TABLE users MODIFY COLUMN {col_name} {col_def}")

        cursor.execute("SHOW INDEX FROM users WHERE Key_name = 'uk_oauth'")
        uk_oauth_exists = cursor.fetchone()
        cursor.fetchall()
        if not uk_oauth_exists:
            logger.info(f"{log_prefix} Adding unique constraint 'uk_oauth' to 'users' table.")
            cursor.execute("SHOW INDEX FROM users WHERE Key_name = 'idx_oauth'")
            idx_oauth_exists = cursor.fetchone()
            cursor.fetchall()
            if not idx_oauth_exists:
                cursor.execute("ALTER TABLE users ADD INDEX idx_oauth (oauth_provider, oauth_provider_id)")
            cursor.execute("ALTER TABLE users ADD CONSTRAINT uk_oauth UNIQUE (oauth_provider, oauth_provider_id)")

        cursor.execute("SHOW INDEX FROM users WHERE Key_name = 'idx_user_created_at'")
        idx_created_at_exists = cursor.fetchone()
        cursor.fetchall()
        if not idx_created_at_exists:
            logger.info(f"{log_prefix} Adding index 'idx_user_created_at' to 'users' table.")
            cursor.execute("ALTER TABLE users ADD INDEX idx_user_created_at (created_at)")

        cursor.execute("SHOW INDEX FROM users WHERE Key_name = 'idx_user_public_api_hash'")
        idx_public_api_hash_exists = cursor.fetchone()
        cursor.fetchall()
        if not idx_public_api_hash_exists:
            logger.info(f"{log_prefix} Adding index 'idx_user_public_api_hash' to 'users' table.")
            cursor.execute("ALTER TABLE users ADD INDEX idx_user_public_api_hash (public_api_key_hash)")

        get_db().commit()
        logger.info(f"{log_prefix} 'users' table schema verified/initialized successfully.")
    except MySQLError as err:
        logger.error(f"{log_prefix} MySQL error during 'users' table initialization: {err}", exc_info=True)
        get_db().rollback()
        raise
    except RuntimeError as e:
        logger.error(f"{log_prefix} Initialization dependency error: {e}")
        get_db().rollback()
        raise
    finally:
        pass
