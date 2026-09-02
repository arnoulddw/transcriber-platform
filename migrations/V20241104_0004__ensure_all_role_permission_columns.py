# migrations/V20241104_0004__ensure_all_role_permission_columns.py
# Final safety net ensuring every expected permission/limit column exists on the roles table.

import logging

LOG_PREFIX = "[Migration:EnsureRoleColumns]"


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s", (column_name,))
    exists = cursor.fetchone() is not None
    cursor.fetchall()
    return exists


def upgrade(db):
    cursor = db.cursor()
    logging.info(f"{LOG_PREFIX} Starting comprehensive column check.")
    try:
        expected_columns = {
            "use_api_assemblyai": "BOOLEAN NOT NULL DEFAULT FALSE",
            "use_api_openai_whisper": "BOOLEAN NOT NULL DEFAULT FALSE",
            "use_api_openai_gpt_4o_transcribe": "BOOLEAN NOT NULL DEFAULT FALSE",
            "use_api_openai_gpt_4o_transcribe_diarize": "BOOLEAN NOT NULL DEFAULT FALSE",
            "use_api_google_gemini": "BOOLEAN NOT NULL DEFAULT FALSE",
            "access_admin_panel": "BOOLEAN NOT NULL DEFAULT FALSE",
            "allow_large_files": "BOOLEAN NOT NULL DEFAULT FALSE",
            "allow_context_prompt": "BOOLEAN NOT NULL DEFAULT FALSE",
            "allow_api_key_management": "BOOLEAN NOT NULL DEFAULT FALSE",
            "allow_download_transcript": "BOOLEAN NOT NULL DEFAULT TRUE",
            "allow_workflows": "BOOLEAN NOT NULL DEFAULT FALSE",
            "manage_workflow_templates": "BOOLEAN NOT NULL DEFAULT FALSE",
            "allow_auto_title_generation": "BOOLEAN NOT NULL DEFAULT FALSE",
            "limit_daily_cost": "DECIMAL(10, 4) NOT NULL DEFAULT 0.0000",
            "limit_weekly_cost": "DECIMAL(10, 4) NOT NULL DEFAULT 0.0000",
            "limit_monthly_cost": "DECIMAL(10, 4) NOT NULL DEFAULT 0.0000",
            "limit_daily_minutes": "INT NOT NULL DEFAULT 0",
            "limit_weekly_minutes": "INT NOT NULL DEFAULT 0",
            "limit_monthly_minutes": "INT NOT NULL DEFAULT 0",
            "limit_daily_workflows": "INT NOT NULL DEFAULT 0",
            "limit_weekly_workflows": "INT NOT NULL DEFAULT 0",
            "limit_monthly_workflows": "INT NOT NULL DEFAULT 0",
            "max_history_items": "INT NOT NULL DEFAULT 0",
            "history_retention_days": "INT NOT NULL DEFAULT 0",
        }

        added_columns = []
        for column_name, column_def in expected_columns.items():
            if not _column_exists(cursor, "roles", column_name):
                logging.info(f"{LOG_PREFIX} Adding missing column '{column_name}'.")
                cursor.execute(
                    f"""
                    ALTER TABLE roles
                    ADD COLUMN {column_name} {column_def}
                    """
                )
                added_columns.append(column_name)
            else:
                logging.debug(f"{LOG_PREFIX} Column '{column_name}' already exists.")

        if added_columns:
            db.commit()
            logging.info(f"{LOG_PREFIX} Added columns: {', '.join(added_columns)}.")
        else:
            logging.info(f"{LOG_PREFIX} No columns needed to be added.")
    except Exception:
        db.rollback()
        logging.exception(f"{LOG_PREFIX} Upgrade failed; rolled back.")
        raise
