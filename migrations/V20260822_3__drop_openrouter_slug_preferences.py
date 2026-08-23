"""Drop the unused OpenRouter slug preference columns.

The separate per-user ("Default OpenRouter Model" /
"default_openrouter_model") and role-level ("roles.default_openrouter_
model") transcription-slug preferences were removed from the application:
every configured OpenRouter slug is its own selectable catalog model, so a
role or user expresses an OpenRouter default through
``default_transcription_model = openrouter:vendor/model`` exactly like any
other provider. The LLM twin ``default_openrouter_llm_model`` was removed
as well; title generation and workflows resolve their models through the
provider-neutral preferences and catalogs.

Nothing reads or writes these columns any more, so they are dropped to
keep the schema honest.

Fresh installations never create them (they are gone from the CREATE
TABLE statements), so this migration tolerates missing tables/columns and
is safe to run more than once.
"""

from __future__ import annotations


TARGETS = (
    ("users", "default_openrouter_model"),
    ("users", "default_openrouter_llm_model"),
    ("roles", "default_openrouter_model"),
)


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute("SHOW TABLES LIKE %s", (table_name,))
    return cursor.fetchone() is not None


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s", (column_name,))
    return cursor.fetchone() is not None


def upgrade(db) -> None:
    cursor = db.cursor()
    try:
        for table_name, column_name in TARGETS:
            if not _table_exists(cursor, table_name):
                continue
            if not _column_exists(cursor, table_name, column_name):
                continue
            cursor.execute(f"ALTER TABLE {table_name} DROP COLUMN {column_name}")
        db.commit()
    finally:
        cursor.close()
