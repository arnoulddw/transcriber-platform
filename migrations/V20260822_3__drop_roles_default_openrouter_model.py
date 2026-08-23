"""Drop the unused roles.default_openrouter_model column.

The separate role-level OpenRouter slug preference was removed from the
application (commit "refactor(roles): drop the role-level Default
OpenRouter Model preference"): a role expresses an OpenRouter default by
setting ``default_transcription_model`` to an ``openrouter:vendor/model``
key, exactly like any other provider. Nothing reads or writes the column
any more, so it is dropped to keep the schema honest.

Fresh installations never create the column (it is gone from the CREATE
TABLE statement), so this migration tolerates a missing table or column
and is safe to run more than once.
"""

from __future__ import annotations


ROLES_TABLE = "roles"
COLUMN_NAME = "default_openrouter_model"


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute("SHOW TABLES LIKE %s", (table_name,))
    return cursor.fetchone() is not None


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s", (column_name,))
    return cursor.fetchone() is not None


def upgrade(db) -> None:
    cursor = db.cursor()
    try:
        if not _table_exists(cursor, ROLES_TABLE):
            return
        if not _column_exists(cursor, ROLES_TABLE, COLUMN_NAME):
            return
        cursor.execute(f"ALTER TABLE {ROLES_TABLE} DROP COLUMN {COLUMN_NAME}")
        db.commit()
    finally:
        cursor.close()
