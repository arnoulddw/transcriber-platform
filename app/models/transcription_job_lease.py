"""Persistence for process ownership of in-flight transcription jobs."""

from typing import Any, Dict, List, Optional

from app.database import get_cursor, get_db


def init_db_command() -> None:
    cursor = get_cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS transcription_job_leases (
            job_id VARCHAR(36) PRIMARY KEY,
            worker_id VARCHAR(96) NOT NULL,
            slot_number INT NULL,
            heartbeat_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            FOREIGN KEY (job_id) REFERENCES transcriptions (id) ON DELETE CASCADE,
            INDEX idx_transcription_lease_heartbeat (heartbeat_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    get_db().commit()


def register_waiting(job_id: str, worker_id: str) -> None:
    cursor = get_cursor()
    cursor.execute(
        """
        INSERT INTO transcription_job_leases (job_id, worker_id, slot_number, heartbeat_at)
        VALUES (%s, %s, NULL, CURRENT_TIMESTAMP(6))
        ON DUPLICATE KEY UPDATE worker_id=VALUES(worker_id), slot_number=NULL,
                                heartbeat_at=CURRENT_TIMESTAMP(6)
        """,
        (job_id, worker_id),
    )
    get_db().commit()


def mark_running(job_id: str, worker_id: str, slot_number: int) -> None:
    cursor = get_cursor()
    cursor.execute(
        """
        UPDATE transcription_job_leases
        SET worker_id=%s, slot_number=%s, heartbeat_at=CURRENT_TIMESTAMP(6)
        WHERE job_id=%s
        """,
        (worker_id, slot_number, job_id),
    )
    get_db().commit()


def heartbeat(job_id: str, worker_id: str) -> None:
    cursor = get_cursor()
    cursor.execute(
        """
        UPDATE transcription_job_leases SET heartbeat_at=CURRENT_TIMESTAMP(6)
        WHERE job_id=%s AND worker_id=%s
        """,
        (job_id, worker_id),
    )
    get_db().commit()


def heartbeat_worker(worker_id: str) -> None:
    """Refresh every queued/running lease owned by one live web worker."""
    cursor = get_cursor()
    cursor.execute(
        "UPDATE transcription_job_leases SET heartbeat_at=CURRENT_TIMESTAMP(6) WHERE worker_id=%s",
        (worker_id,),
    )
    get_db().commit()


def release(job_id: str, worker_id: Optional[str] = None) -> None:
    cursor = get_cursor()
    if worker_id:
        cursor.execute(
            "DELETE FROM transcription_job_leases WHERE job_id=%s AND worker_id=%s",
            (job_id, worker_id),
        )
    else:
        cursor.execute("DELETE FROM transcription_job_leases WHERE job_id=%s", (job_id,))
    get_db().commit()


def get_active_job_ownership() -> List[Dict[str, Any]]:
    cursor = get_cursor()
    cursor.execute(
        """
        SELECT t.id AS job_id, t.status, t.created_at,
               l.worker_id, l.slot_number, l.heartbeat_at,
               TIMESTAMPDIFF(
                   SECOND,
                   COALESCE(l.heartbeat_at, t.created_at),
                   CURRENT_TIMESTAMP
               ) AS lease_age_seconds
        FROM transcriptions t
        LEFT JOIN transcription_job_leases l ON l.job_id=t.id
        WHERE t.status IN ('pending', 'processing', 'cancelling')
        """
    )
    return list(cursor.fetchall())
