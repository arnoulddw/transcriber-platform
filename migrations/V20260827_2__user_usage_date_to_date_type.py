"""Convert user_usage.date from TIMESTAMP to DATE type.

user_usage tracks daily aggregated usage (cost, minutes, workflows,
live_minutes). Storing this as a TIMESTAMP introduced timestamp precision
and timezone conversion issues where duplicate rows could be created for the
same day if timestamps were not normalized.

This migration aggregates any existing multi-timestamp records per (user_id, date)
and modifies the column to DATE NOT NULL DEFAULT (CURRENT_DATE).
"""

from __future__ import annotations


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute("SHOW TABLES LIKE %s", (table_name,))
    return cursor.fetchone() is not None


def _column_type(cursor, table_name: str, column_name: str) -> str:
    cursor.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s", (column_name,))
    row = cursor.fetchone()
    if not row:
        return ""
    if isinstance(row, dict):
        return str(row.get("Type") or "").lower()
    return str(row[1] or "").lower()


def upgrade(db) -> None:
    cursor = db.cursor()
    try:
        if not _table_exists(cursor, "user_usage"):
            return

        col_type = _column_type(cursor, "user_usage", "date")
        if "date" in col_type and "timestamp" not in col_type and "datetime" not in col_type:
            # Already DATE type
            return

        # Deduplicate/aggregate rows by (user_id, CAST(date AS DATE))
        cursor.execute(
            """
            CREATE TEMPORARY TABLE temp_aggregated_user_usage AS
            SELECT
                user_id,
                CAST(date AS DATE) AS usage_date,
                SUM(cost) AS total_cost,
                SUM(minutes) AS total_minutes,
                SUM(workflows) AS total_workflows,
                SUM(live_minutes) AS total_live_minutes
            FROM user_usage
            GROUP BY user_id, CAST(date AS DATE)
            """
        )

        cursor.execute("TRUNCATE TABLE user_usage")

        cursor.execute(
            """
            ALTER TABLE user_usage
            MODIFY COLUMN date DATE NOT NULL DEFAULT (CURRENT_DATE)
            """
        )

        cursor.execute(
            """
            INSERT INTO user_usage (user_id, date, cost, minutes, workflows, live_minutes)
            SELECT user_id, usage_date, total_cost, total_minutes, total_workflows, total_live_minutes
            FROM temp_aggregated_user_usage
            """
        )

        cursor.execute("DROP TEMPORARY TABLE IF EXISTS temp_aggregated_user_usage")
        db.commit()
    finally:
        cursor.close()
