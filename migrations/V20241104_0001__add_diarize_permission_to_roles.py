# migrations/V20241104_0001__add_diarize_permission_to_roles.py
# Adds the diarize permission column (and related Gemini column for completeness)
# to the roles table. Idempotent so it can run safely multiple times.

import logging

LOG_PREFIX = "[Migration:AddDiarizePerm]"


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s", (column_name,))
    exists = cursor.fetchone() is not None
    cursor.fetchall()
    return exists


def upgrade(db):
    cursor = db.cursor()
    logging.info(f"{LOG_PREFIX} Starting upgrade.")
    try:
        if not _column_exists(cursor, "roles", "use_api_openai_gpt_4o_transcribe_diarize"):
            logging.info(f"{LOG_PREFIX} Adding 'use_api_openai_gpt_4o_transcribe_diarize' column.")
            cursor.execute(
                """
                ALTER TABLE roles
                ADD COLUMN use_api_openai_gpt_4o_transcribe_diarize BOOLEAN NOT NULL DEFAULT FALSE
                AFTER use_api_openai_gpt_4o_transcribe
                """
            )
        else:
            logging.info(f"{LOG_PREFIX} Column 'use_api_openai_gpt_4o_transcribe_diarize' already present; skipping.")

        if not _column_exists(cursor, "roles", "use_api_google_gemini"):
            logging.info(f"{LOG_PREFIX} Adding 'use_api_google_gemini' column.")
            cursor.execute(
                """
                ALTER TABLE roles
                ADD COLUMN use_api_google_gemini BOOLEAN NOT NULL DEFAULT FALSE
                AFTER use_api_openai_gpt_4o_transcribe_diarize
                """
            )
        else:
            logging.info(f"{LOG_PREFIX} Column 'use_api_google_gemini' already present; skipping.")

        db.commit()
        logging.info(f"{LOG_PREFIX} Upgrade completed successfully.")
    except Exception:
        db.rollback()
        logging.exception(f"{LOG_PREFIX} Upgrade failed; rolled back.")
        raise
