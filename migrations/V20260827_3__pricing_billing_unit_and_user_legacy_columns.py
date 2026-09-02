"""Add billing_unit to pricing and drop deprecated public_api_key_* columns from users.

1. pricing table: Add explicit billing_unit column (e.g. per_minute for transcription,
   per_1k_tokens for workflows and title generation).
2. users table: Drop deprecated public_api_key_hash, public_api_key_last_four,
   and public_api_key_created_at columns since public_api_keys is now the
   sole authority.
"""

from __future__ import annotations


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute("SHOW TABLES LIKE %s", (table_name,))
    return cursor.fetchone() is not None


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s", (column_name,))
    return cursor.fetchone() is not None


def upgrade(db) -> None:
    cursor = db.cursor()
    try:
        # 1. Update pricing table
        if _table_exists(cursor, "pricing"):
            if not _column_exists(cursor, "pricing", "billing_unit"):
                cursor.execute(
                    """
                    ALTER TABLE pricing
                    ADD COLUMN billing_unit ENUM('per_minute', 'per_1k_tokens', 'per_execution')
                    NOT NULL DEFAULT 'per_minute' AFTER price
                    """
                )
                cursor.execute(
                    """
                    UPDATE pricing
                    SET billing_unit = 'per_1k_tokens'
                    WHERE item_type IN ('workflow', 'title_generation')
                    """
                )

        # 2. Update users table
        if _table_exists(cursor, "users"):
            # Ensure any lingering public API key in users is migrated to public_api_keys
            if _table_exists(cursor, "public_api_keys") and _column_exists(cursor, "users", "public_api_key_hash"):
                cursor.execute(
                    """
                    INSERT IGNORE INTO public_api_keys (user_id, name, key_hash, last_four, created_at)
                    SELECT id, 'Default key', public_api_key_hash, COALESCE(public_api_key_last_four, '****'),
                           COALESCE(public_api_key_created_at, created_at)
                    FROM users
                    WHERE public_api_key_hash IS NOT NULL
                    """
                )

            for col in ("public_api_key_hash", "public_api_key_last_four", "public_api_key_created_at"):
                if _column_exists(cursor, "users", col):
                    cursor.execute(f"ALTER TABLE users DROP COLUMN {col}")

        db.commit()
    finally:
        cursor.close()
