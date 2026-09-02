"""Drop redundant llm_operation_* columns from transcriptions.

The llm_operations table is the primary source of truth for AI workflows
and post-processing tasks. Denormalizing operation status, result, error,
and ID onto the transcriptions table caused duplication and concurrency drift.

This migration ensures any legacy completed workflows are preserved in
llm_operations before dropping the columns from transcriptions.
"""

from __future__ import annotations


COLUMNS_TO_DROP = (
    "llm_operation_id",
    "llm_operation_status",
    "llm_operation_result",
    "llm_operation_error",
    "llm_operation_ran_at",
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
        if not _table_exists(cursor, "transcriptions"):
            return

        # Backfill any orphaned transcription workflow results into llm_operations if needed
        if (
            _table_exists(cursor, "llm_operations")
            and _column_exists(cursor, "transcriptions", "llm_operation_result")
            and _column_exists(cursor, "transcriptions", "llm_operation_id")
        ):
            cursor.execute(
                """
                INSERT INTO llm_operations (
                    user_id, provider, operation_type, input_text, result,
                    transcription_id, created_at, completed_at, status, error
                )
                SELECT
                    t.user_id,
                    'UNKNOWN',
                    'workflow',
                    COALESCE(t.pending_workflow_prompt_text, ''),
                    t.llm_operation_result,
                    t.id,
                    COALESCE(t.llm_operation_ran_at, t.created_at),
                    t.llm_operation_ran_at,
                    COALESCE(t.llm_operation_status, 'finished'),
                    t.llm_operation_error
                FROM transcriptions t
                LEFT JOIN llm_operations lo ON lo.transcription_id = t.id AND lo.operation_type = 'workflow'
                WHERE t.llm_operation_result IS NOT NULL
                  AND lo.id IS NULL
                """
            )

        for column_name in COLUMNS_TO_DROP:
            if _column_exists(cursor, "transcriptions", column_name):
                cursor.execute(f"ALTER TABLE transcriptions DROP COLUMN {column_name}")

        db.commit()
    finally:
        cursor.close()
