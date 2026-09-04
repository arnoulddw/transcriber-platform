"""Add optional per-model audio-format metadata to the transcription catalog.

The metadata is intentionally nullable. A missing value means that the model's
format capabilities are unknown, so existing provider behavior remains
unchanged until a model is explicitly described.
"""

from __future__ import annotations


MODELS_TABLE = "transcription_models_catalog"
FORMATS_COLUMN = "supported_audio_formats"
MAI_MODEL = "microsoft/mai-transcribe-2"
MAI_FORMATS = "mp3,wav,flac"


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute("SHOW TABLES LIKE %s", (table_name,))
    return cursor.fetchone() is not None


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s", (column_name,))
    return cursor.fetchone() is not None


def upgrade(db) -> None:
    cursor = db.cursor()
    try:
        if not _table_exists(cursor, MODELS_TABLE):
            return

        if not _column_exists(cursor, MODELS_TABLE, FORMATS_COLUMN):
            cursor.execute(
                f"""
                ALTER TABLE {MODELS_TABLE}
                ADD COLUMN {FORMATS_COLUMN} VARCHAR(255) DEFAULT NULL
                """
            )

        # Backfill rows already registered before capability metadata existed.
        # Do not overwrite a value an operator has explicitly configured.
        cursor.execute(
            f"""
            UPDATE {MODELS_TABLE}
            SET {FORMATS_COLUMN} = %s
            WHERE provider_code = %s
              AND code = %s
              AND ({FORMATS_COLUMN} IS NULL OR TRIM({FORMATS_COLUMN}) = '')
            """,
            (MAI_FORMATS, "openrouter", MAI_MODEL),
        )
        db.commit()
    finally:
        cursor.close()
