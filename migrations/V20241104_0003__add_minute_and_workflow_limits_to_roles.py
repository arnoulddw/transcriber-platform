# migrations/V20241104_0003__add_minute_and_workflow_limits_to_roles.py
# Ensures minute and workflow limit columns exist on the roles table.

import logging

LOG_PREFIX = "[Migration:AddMinuteWorkflowLimits]"


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s", (column_name,))
    exists = cursor.fetchone() is not None
    cursor.fetchall()
    return exists


def _add_column(cursor, table_name: str, column_name: str, column_def: str, after: str):
    cursor.execute(
        f"""
        ALTER TABLE {table_name}
        ADD COLUMN {column_name} {column_def}
        AFTER {after}
        """
    )


def upgrade(db):
    cursor = db.cursor()
    logging.info(f"{LOG_PREFIX} Starting upgrade.")
    try:
        minute_columns = [
            ("limit_daily_minutes", "INT NOT NULL DEFAULT 0", "limit_monthly_cost"),
            ("limit_weekly_minutes", "INT NOT NULL DEFAULT 0", "limit_daily_minutes"),
            ("limit_monthly_minutes", "INT NOT NULL DEFAULT 0", "limit_weekly_minutes"),
        ]
        for column_name, column_def, after in minute_columns:
            if not _column_exists(cursor, "roles", column_name):
                logging.info(f"{LOG_PREFIX} Adding '{column_name}' column.")
                _add_column(cursor, "roles", column_name, column_def, after)
            else:
                logging.info(f"{LOG_PREFIX} Column '{column_name}' already present; skipping.")

        workflow_columns = [
            ("limit_daily_workflows", "INT NOT NULL DEFAULT 0", "limit_monthly_minutes"),
            ("limit_weekly_workflows", "INT NOT NULL DEFAULT 0", "limit_daily_workflows"),
            ("limit_monthly_workflows", "INT NOT NULL DEFAULT 0", "limit_weekly_workflows"),
            ("max_history_items", "INT NOT NULL DEFAULT 0", "limit_monthly_workflows"),
            ("history_retention_days", "INT NOT NULL DEFAULT 0", "max_history_items"),
        ]
        for column_name, column_def, after in workflow_columns:
            if not _column_exists(cursor, "roles", column_name):
                logging.info(f"{LOG_PREFIX} Adding '{column_name}' column.")
                _add_column(cursor, "roles", column_name, column_def, after)
            else:
                logging.info(f"{LOG_PREFIX} Column '{column_name}' already present; skipping.")

        db.commit()
        logging.info(f"{LOG_PREFIX} Upgrade completed successfully.")
    except Exception:
        db.rollback()
        logging.exception(f"{LOG_PREFIX} Upgrade failed; rolled back.")
        raise
