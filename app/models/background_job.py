"""Durable persistence for work executed by the dedicated background worker."""

import json
import logging
import os
from typing import Any, Dict, Optional

from mysql.connector import Error as MySQLError

from app.database import get_cursor, get_db


TASK_TYPES = frozenset({"transcription", "workflow", "title_generation"})
STALE_JOB_FAILURE_MESSAGE = (
    "WORKER_INTERRUPTED: Background job exhausted its retry attempts after a stale worker claim."
)


def init_db_command() -> None:
    """Create the durable background-job table when the application is initialized."""
    cursor = get_cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS background_jobs (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                task_type VARCHAR(40) NOT NULL,
                payload JSON NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                attempts INT UNSIGNED NOT NULL DEFAULT 0,
                max_attempts INT UNSIGNED NOT NULL DEFAULT 3,
                available_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                claimed_at DATETIME(6) NULL DEFAULT NULL,
                claimed_by VARCHAR(128) NULL DEFAULT NULL,
                last_error TEXT NULL,
                created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                completed_at DATETIME(6) NULL DEFAULT NULL,
                INDEX idx_background_jobs_claim (status, available_at, id),
                INDEX idx_background_jobs_stale (status, claimed_at),
                INDEX idx_background_jobs_type (task_type, status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        get_db().commit()
    except MySQLError:
        get_db().rollback()
        logging.exception("Failed to initialize the background_jobs table.")
        raise


def enqueue_job(
    task_type: str,
    payload: Dict[str, Any],
    max_attempts: int = 3,
    commit: bool = True,
) -> int:
    """Persist one reconstructable task payload for a worker to execute.

    Existing callers own the transaction by default. A caller that needs to
    commit a task together with another state change can pass ``commit=False``
    and commit or roll back the shared application connection.
    """
    if task_type not in TASK_TYPES:
        raise ValueError(f"Unsupported background task type: {task_type}")
    if not isinstance(payload, dict):
        raise TypeError("Background task payload must be a dictionary.")
    if max_attempts < 1:
        raise ValueError("Background task max_attempts must be positive.")

    cursor = get_cursor()
    try:
        cursor.execute(
            """
            INSERT INTO background_jobs (task_type, payload, max_attempts)
            VALUES (%s, %s, %s)
            """,
            (task_type, json.dumps(payload), max_attempts),
        )
        if commit:
            get_db().commit()
        return int(cursor.lastrowid)
    except MySQLError:
        if commit:
            get_db().rollback()
        logging.exception("Failed to enqueue background task type %s.", task_type)
        raise


def _decode_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = row.get("payload")
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValueError(f"Background job {row.get('id')} has an invalid payload.")
    row["payload"] = payload
    return row


def _mark_stale_domain_record_failed(cursor, row: Dict[str, Any]) -> None:
    """Reconcile the domain record when a crashed job cannot be retried.

    The queue row and this update use the same transaction/connection.  Each
    update only targets an active domain state, so a recovery retry is a no-op
    after the first successful reconciliation.
    """
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return
    if not isinstance(payload, dict):
        return

    task_type = row.get("task_type")
    if task_type == "transcription":
        args = payload.get("args")
        if not isinstance(args, list) or not args or not args[0]:
            return
        cursor.execute(
            """
            UPDATE transcriptions
            SET status = 'interrupted',
                completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
                error_message = COALESCE(error_message, %s),
                progress_log = JSON_ARRAY_APPEND(
                    COALESCE(progress_log, JSON_ARRAY()), '$', %s
                )
            WHERE id = %s
              AND status IN ('pending', 'processing', 'cancelling')
            """,
            (STALE_JOB_FAILURE_MESSAGE, STALE_JOB_FAILURE_MESSAGE, args[0]),
        )
    elif task_type == "workflow":
        operation_id = payload.get("operation_id")
        user_id = payload.get("user_id")
        if operation_id is None or user_id is None:
            return
        cursor.execute(
            """
            UPDATE llm_operations
            SET status = 'error',
                completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
                error = COALESCE(error, %s)
            WHERE id = %s
              AND user_id = %s
              AND status IN ('pending', 'processing')
            """,
            (STALE_JOB_FAILURE_MESSAGE, operation_id, user_id),
        )
    elif task_type == "title_generation":
        transcription_id = payload.get("transcription_id")
        if not transcription_id:
            return
        cursor.execute(
            """
            UPDATE transcriptions
            SET title_generation_status = 'failed'
            WHERE id = %s
              AND title_generation_status IN ('pending', 'processing')
            """,
            (transcription_id,),
        )


def claim_next_job(worker_id: str) -> Optional[Dict[str, Any]]:
    """Atomically claim the next available job across all worker processes."""
    connection = get_db()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT id, task_type, payload, attempts, max_attempts
            FROM background_jobs
            WHERE status = 'pending'
              AND available_at <= UTC_TIMESTAMP(6)
            ORDER BY id ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        )
        row = cursor.fetchone()
        if not row:
            connection.rollback()
            return None

        cursor.execute(
            """
            UPDATE background_jobs
            SET status = 'running',
                attempts = attempts + 1,
                claimed_at = UTC_TIMESTAMP(6),
                claimed_by = %s,
                last_error = NULL,
                completed_at = NULL
            WHERE id = %s AND status = 'pending'
            """,
            (worker_id, row["id"]),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return None
        connection.commit()
        row["attempts"] = int(row.get("attempts") or 0) + 1
        return _decode_payload(row)
    except Exception:
        connection.rollback()
        logging.exception("Failed to claim a background job for worker %s.", worker_id)
        raise
    finally:
        cursor.close()


def complete_job(job_id: int, worker_id: str) -> bool:
    """Mark a claimed job successful only when this worker owns its claim."""
    cursor = get_cursor()
    try:
        cursor.execute(
            """
            UPDATE background_jobs
            SET status = 'succeeded',
                claimed_at = NULL,
                claimed_by = NULL,
                completed_at = UTC_TIMESTAMP(6)
            WHERE id = %s AND status = 'running' AND claimed_by = %s
            """,
            (job_id, worker_id),
        )
        get_db().commit()
        return cursor.rowcount == 1
    except MySQLError:
        get_db().rollback()
        logging.exception("Failed to complete background job %s.", job_id)
        return False


def fail_job(
    job_id: int,
    worker_id: str,
    attempts: int,
    max_attempts: int,
    error: str,
    retry_delay_seconds: int,
) -> str:
    """Record a failure and either schedule a retry or terminally fail it."""
    should_retry = attempts < max_attempts
    status = "pending" if should_retry else "failed"
    cursor = get_cursor()
    try:
        if should_retry:
            cursor.execute(
                """
                UPDATE background_jobs
                SET status = 'pending',
                    available_at = DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND),
                    claimed_at = NULL,
                    claimed_by = NULL,
                    last_error = %s
                WHERE id = %s AND status = 'running' AND claimed_by = %s
                """,
                (retry_delay_seconds, error[:4000], job_id, worker_id),
            )
        else:
            cursor.execute(
                """
                UPDATE background_jobs
                SET status = 'failed',
                    available_at = UTC_TIMESTAMP(6),
                    claimed_at = NULL,
                    claimed_by = NULL,
                    last_error = %s,
                    completed_at = UTC_TIMESTAMP(6)
                WHERE id = %s AND status = 'running' AND claimed_by = %s
                """,
                (error[:4000], job_id, worker_id),
            )
        get_db().commit()
        return status if cursor.rowcount == 1 else "lost"
    except MySQLError:
        get_db().rollback()
        logging.exception("Failed to record failure for background job %s.", job_id)
        return "error"


def recover_stale_jobs(stale_seconds: int) -> int:
    """Requeue abandoned claims, or fail them after their final attempt.

    Terminal recovery also reconciles the referenced domain row before the
    queue row is closed, preventing a worker crash from leaving user-visible
    work stuck in ``processing`` forever.
    """
    if stale_seconds <= 0:
        raise ValueError("Background job stale timeout must be positive.")
    cursor = get_cursor()
    try:
        cursor.execute(
            """
            SELECT id, task_type, payload, attempts, max_attempts
            FROM background_jobs
            WHERE status = 'running'
              AND claimed_at < DATE_SUB(UTC_TIMESTAMP(6), INTERVAL %s SECOND)
            ORDER BY id ASC
            FOR UPDATE SKIP LOCKED
            """,
            (stale_seconds,),
        )
        stale_rows = cursor.fetchall() or []
        recovered = 0
        for row in stale_rows:
            terminal = int(row.get("attempts") or 0) >= int(row.get("max_attempts") or 0)
            if terminal:
                _mark_stale_domain_record_failed(cursor, row)
            cursor.execute(
                """
                UPDATE background_jobs
                SET status = %s,
                    available_at = UTC_TIMESTAMP(6),
                    claimed_at = NULL,
                    claimed_by = NULL,
                    last_error = CONCAT(
                        COALESCE(last_error, ''),
                        CASE WHEN COALESCE(last_error, '') = '' THEN '' ELSE ' ' END,
                        'Recovered stale worker claim.'
                    ),
                    completed_at = CASE WHEN %s THEN UTC_TIMESTAMP(6) ELSE NULL END
                WHERE id = %s AND status = 'running'
                """,
                ("failed" if terminal else "pending", terminal, row["id"]),
            )
            recovered += cursor.rowcount
        get_db().commit()
        return recovered
    except MySQLError:
        get_db().rollback()
        logging.exception("Failed to recover stale background jobs.")
        return 0


def heartbeat_jobs(job_ids, worker_id: str) -> int:
    """Refresh claims owned by ``worker_id`` while task calls are in flight."""
    if not job_ids:
        return 0
    placeholders = ", ".join(["%s"] * len(job_ids))
    cursor = get_cursor()
    try:
        cursor.execute(
            f"""
            UPDATE background_jobs
            SET claimed_at = UTC_TIMESTAMP(6)
            WHERE status = 'running'
              AND claimed_by = %s
              AND id IN ({placeholders})
            """,
            (worker_id, *job_ids),
        )
        updated = cursor.rowcount
        get_db().commit()
        return updated
    except MySQLError:
        get_db().rollback()
        logging.exception("Failed to heartbeat background jobs for worker %s.", worker_id)
        return 0


def get_job(job_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve one queue row for diagnostics and focused tests."""
    cursor = get_cursor()
    try:
        cursor.execute("SELECT * FROM background_jobs WHERE id = %s", (job_id,))
        row = cursor.fetchone()
        return _decode_payload(row) if row else None
    except MySQLError:
        logging.exception("Failed to retrieve background job %s.", job_id)
        return None


def get_active_transcription_file_paths() -> set[str]:
    """Return upload paths still needed by queued or running transcriptions."""
    cursor = get_cursor()
    cursor.execute(
        """
        SELECT JSON_UNQUOTE(JSON_EXTRACT(payload, '$.args[2]')) AS file_path
        FROM background_jobs
        WHERE task_type = 'transcription'
          AND status IN ('pending', 'running')
        """
    )
    return {
        os.path.abspath(row["file_path"])
        for row in cursor.fetchall()
        if row.get("file_path")
    }


def purge_terminal_jobs(retention_days: int, batch_size: int = 1000) -> int:
    """Delete terminal queue rows after their diagnostic retention period."""
    if retention_days < 0:
        raise ValueError("Background job retention days cannot be negative.")
    if batch_size <= 0:
        raise ValueError("Background job purge batch size must be positive.")

    cursor = get_cursor()
    try:
        cursor.execute(
            """
            DELETE FROM background_jobs
            WHERE status IN ('succeeded', 'failed')
              AND completed_at IS NOT NULL
              AND completed_at < DATE_SUB(UTC_TIMESTAMP(6), INTERVAL %s DAY)
            ORDER BY completed_at ASC, id ASC
            LIMIT %s
            """,
            (retention_days, batch_size),
        )
        deleted = cursor.rowcount
        get_db().commit()
        return deleted
    except MySQLError:
        get_db().rollback()
        logging.exception("Failed to purge terminal background jobs.")
        return 0
