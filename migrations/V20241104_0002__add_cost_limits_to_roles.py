# migrations/V20241104_0002__add_cost_limits_to_roles.py
# Ensures cost limit columns exist on the roles table.

import logging

LOG_PREFIX = "[Migration:AddCostLimits]"


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s", (column_name,))
    exists = cursor.fetchone() is not None
    cursor.fetchall()
    return exists


def upgrade(db):
    cursor = db.cursor()
    logging.info(f"{LOG_PREFIX} Starting upgrade.")
    try:
        desired_columns = [
            ("limit_daily_cost", "DECIMAL(10, 4) NOT NULL DEFAULT 0.0000", "allow_auto_title_generation"),
            ("limit_weekly_cost", "DECIMAL(10, 4) NOT NULL DEFAULT 0.0000", "limit_daily_cost"),
            ("limit_monthly_cost", "DECIMAL(10, 4) NOT NULL DEFAULT 0.0000", "limit_weekly_cost"),
        ]
        for column_name, column_def, after in desired_columns:
            if not _column_exists(cursor, "roles", column_name):
                logging.info(f"{LOG_PREFIX} Adding '{column_name}' column.")
                cursor.execute(
                    f"""
                    ALTER TABLE roles
                    ADD COLUMN {column_name} {column_def}
                    AFTER {after}
                    """
                )
            else:
                logging.info(f"{LOG_PREFIX} Column '{column_name}' already present; skipping.")

        db.commit()
        logging.info(f"{LOG_PREFIX} Upgrade completed successfully.")
    except Exception:
        db.rollback()
        logging.exception(f"{LOG_PREFIX} Upgrade failed; rolled back.")
        raise
