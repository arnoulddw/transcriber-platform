import logging
from datetime import datetime
from typing import Dict, List, Optional

from mysql.connector import Error as MySQLError

from app.database import get_cursor, get_db
from app.models.user.model import User, _map_row_to_user

logger = logging.getLogger(__name__)


def init_db_command() -> None:
    """Initializes the public API keys table."""
    cursor = get_cursor()
    log_prefix = "[DB:Schema:MySQL]"
    logger.info(f"{log_prefix} Checking/Initializing 'public_api_keys' table...")
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS public_api_keys (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user_id INT NOT NULL,
                name VARCHAR(120) NOT NULL,
                key_hash VARCHAR(128) NOT NULL,
                last_four VARCHAR(12) NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                revoked_at TIMESTAMP NULL DEFAULT NULL,
                UNIQUE KEY uq_public_api_key_hash (key_hash),
                INDEX idx_public_api_keys_user_active (user_id, revoked_at),
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """
        )

        cursor.execute("SHOW INDEX FROM public_api_keys WHERE Key_name = 'uq_public_api_key_hash'")
        unique_exists = cursor.fetchone()
        cursor.fetchall()
        if not unique_exists:
            logger.info(f"{log_prefix} Adding unique index uq_public_api_key_hash to 'public_api_keys'.")
            cursor.execute("ALTER TABLE public_api_keys ADD UNIQUE INDEX uq_public_api_key_hash (key_hash)")

        cursor.execute(
            """
            INSERT IGNORE INTO public_api_keys (user_id, name, key_hash, last_four, created_at)
            SELECT id, 'Default key', public_api_key_hash, public_api_key_last_four,
                   COALESCE(public_api_key_created_at, created_at)
            FROM users
            WHERE public_api_key_hash IS NOT NULL
            """
        )

        get_db().commit()
        logger.info(f"{log_prefix} 'public_api_keys' table schema verified/initialized.")
    except MySQLError as err:
        logger.error(f"{log_prefix} Error during 'public_api_keys' table initialization: {err}", exc_info=True)
        get_db().rollback()
        raise


def create_public_api_key(user_id: int, name: str, key_hash: str, last_four: str, created_at: datetime) -> Optional[int]:
    sql = """
        INSERT INTO public_api_keys (user_id, name, key_hash, last_four, created_at)
        VALUES (%s, %s, %s, %s, %s)
    """
    cursor = get_cursor()
    try:
        cursor.execute(sql, (user_id, name, key_hash, last_four, created_at))
        get_db().commit()
        return cursor.lastrowid
    except MySQLError as err:
        logger.error(f"[DB:PublicApiKey] Error creating public API key for user {user_id}: {err}", exc_info=True)
        get_db().rollback()
        return None


def get_public_api_keys_by_user(user_id: int) -> List[Dict]:
    sql = """
        SELECT id, name, last_four, created_at
        FROM public_api_keys
        WHERE user_id = %s AND revoked_at IS NULL
        ORDER BY created_at DESC, id DESC
    """
    cursor = get_cursor()
    try:
        cursor.execute(sql, (user_id,))
        return cursor.fetchall()
    except MySQLError as err:
        logger.error(f"[DB:PublicApiKey] Error listing public API keys for user {user_id}: {err}", exc_info=True)
        return []


def get_user_by_public_api_key_hash(key_hash: str) -> Optional[User]:
    sql = """
        SELECT u.*, p.key_hash AS public_api_key_hash, p.last_four AS public_api_key_last_four,
               p.created_at AS public_api_key_created_at
        FROM public_api_keys p
        JOIN users u ON u.id = p.user_id
        WHERE p.key_hash = %s AND p.revoked_at IS NULL
        LIMIT 1
    """
    cursor = get_cursor()
    try:
        cursor.execute(sql, (key_hash,))
        return _map_row_to_user(cursor.fetchone())
    except MySQLError as err:
        logger.error(f"[DB:PublicApiKey] Error looking up public API key hash: {err}", exc_info=True)
        return None


def revoke_public_api_key(user_id: int, key_id: int) -> bool:
    sql = """
        UPDATE public_api_keys
        SET revoked_at = CURRENT_TIMESTAMP
        WHERE id = %s AND user_id = %s AND revoked_at IS NULL
    """
    cursor = get_cursor()
    try:
        cursor.execute(sql, (key_id, user_id))
        get_db().commit()
        return cursor.rowcount > 0
    except MySQLError as err:
        logger.error(f"[DB:PublicApiKey] Error revoking public API key {key_id} for user {user_id}: {err}", exc_info=True)
        get_db().rollback()
        return False


def revoke_all_public_api_keys(user_id: int) -> bool:
    sql = """
        UPDATE public_api_keys
        SET revoked_at = CURRENT_TIMESTAMP
        WHERE user_id = %s AND revoked_at IS NULL
    """
    cursor = get_cursor()
    try:
        cursor.execute(sql, (user_id,))
        get_db().commit()
        return True
    except MySQLError as err:
        logger.error(f"[DB:PublicApiKey] Error revoking all public API keys for user {user_id}: {err}", exc_info=True)
        get_db().rollback()
        return False
