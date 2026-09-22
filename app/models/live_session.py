"""Durable state for signed live transcription sessions."""

import time
from typing import Any, Dict, Optional

from app.database import get_cursor, get_db


LIVE_SESSION_STATUSES = frozenset({
    "active",
    "closing",
    "finalizing",
    "finalized",
    "revoked",
})
LIVE_SESSION_RETENTION_SECONDS = 7 * 24 * 60 * 60
LIVE_SESSION_MAX_DURATION_SECONDS = 120 * 60


def init_db_command() -> None:
    """Create the live-session state table for new installations."""
    cursor = get_cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS live_transcription_sessions (
            session_id VARCHAR(64) PRIMARY KEY,
            user_id INT NOT NULL,
            transcription_id VARCHAR(36) NOT NULL UNIQUE,
            provider VARCHAR(32) NOT NULL,
            model VARCHAR(255) NOT NULL,
            transport VARCHAR(32) NOT NULL,
            started_at DOUBLE NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'active',
            version INT UNSIGNED NOT NULL DEFAULT 1,
            last_sequence BIGINT NOT NULL DEFAULT -1,
            last_transcript MEDIUMTEXT NULL,
            created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            hangup_at DATETIME(6) NULL DEFAULT NULL,
            hangup_completed_at DATETIME(6) NULL DEFAULT NULL,
            finalized_at DATETIME(6) NULL DEFAULT NULL,
            revoked_at DATETIME(6) NULL DEFAULT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            INDEX idx_live_sessions_user_status (user_id, status),
            INDEX idx_live_sessions_status_created (status, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    get_db().commit()


def create_session(
    session_id: str,
    user_id: int,
    transcription_id: str,
    provider: str,
    model: str,
    transport: str,
    started_at: float,
) -> None:
    """Persist one newly issued session before returning its signed token."""
    cursor = get_cursor()
    cursor.execute(
        """
        INSERT INTO live_transcription_sessions (
            session_id, user_id, transcription_id, provider, model,
            transport, started_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            session_id,
            user_id,
            transcription_id,
            provider,
            model,
            transport,
            started_at,
        ),
    )
    get_db().commit()


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Return one session row, or ``None`` when it no longer exists."""
    if not isinstance(session_id, str) or not session_id:
        return None
    cursor = get_cursor()
    cursor.execute(
        """
        SELECT session_id, user_id, transcription_id, provider, model,
               transport, started_at, status, version, last_sequence,
               last_transcript, created_at, hangup_at, hangup_completed_at,
               finalized_at, revoked_at
        FROM live_transcription_sessions
        WHERE session_id = %s
        """,
        (session_id,),
    )
    return cursor.fetchone()


def claim_chunk_sequence(session_id: str, sequence: int) -> Dict[str, Any]:
    """Atomically claim the next OpenRouter chunk sequence.

    A duplicate sequence is reported without claiming it again. The last
    completed transcript is retained so an immediate client retry can receive
    the same result without another provider call.
    """
    cursor = get_cursor()
    cursor.execute(
        """
        UPDATE live_transcription_sessions
        SET last_sequence = %s,
            last_transcript = NULL,
            version = version + 1
        WHERE session_id = %s
          AND status = 'active'
          AND last_sequence = %s
          AND (last_sequence = -1 OR last_transcript IS NOT NULL)
        """,
        (sequence, session_id, sequence - 1),
    )
    if cursor.rowcount == 1:
        get_db().commit()
        return {"claimed": True, "duplicate": False}

    row = get_session(session_id)
    if row is None:
        get_db().rollback()
        return {"claimed": False, "missing": True}
    if row.get("status") != "active":
        get_db().rollback()
        return {
            "claimed": False,
            "duplicate": False,
            "status": row.get("status"),
        }
    if int(row.get("last_sequence", -1)) == sequence:
        get_db().rollback()
        transcript = row.get("last_transcript")
        return {
            "claimed": False,
            "duplicate": True,
            "in_progress": transcript is None,
            "transcript": transcript if transcript is not None else "",
        }
    get_db().rollback()
    return {
        "claimed": False,
        "duplicate": False,
        "out_of_order": True,
        "last_sequence": int(row.get("last_sequence", -1)),
    }


def record_chunk_result(session_id: str, sequence: int, transcript: str) -> bool:
    """Store a claimed result, including when hangup has begun concurrently."""
    cursor = get_cursor()
    cursor.execute(
        """
        UPDATE live_transcription_sessions
        SET last_transcript = %s,
            version = version + 1
        WHERE session_id = %s
          AND status IN ('active', 'closing')
          AND last_sequence = %s
        """,
        (transcript, session_id, sequence),
    )
    get_db().commit()
    return cursor.rowcount == 1


def release_chunk_sequence(session_id: str, sequence: int) -> bool:
    """Release a failed claim so the client can retry that sequence."""
    cursor = get_cursor()
    cursor.execute(
        """
        UPDATE live_transcription_sessions
        SET last_sequence = %s,
            last_transcript = NULL,
            version = version + 1
        WHERE session_id = %s
          AND status IN ('active', 'closing')
          AND last_sequence = %s
        """,
        (sequence - 1, session_id, sequence),
    )
    get_db().commit()
    return cursor.rowcount == 1


def begin_finalize(session_id: str) -> str:
    """Claim finalization, returning an idempotency state."""
    cursor = get_cursor()
    cursor.execute(
        """
        UPDATE live_transcription_sessions
        SET status = 'finalizing',
            version = version + 1
        WHERE session_id = %s
          AND status IN ('active', 'closing')
          AND (last_sequence = -1 OR last_transcript IS NOT NULL)
        """,
        (session_id,),
    )
    if cursor.rowcount == 1:
        get_db().commit()
        return "claimed"

    row = get_session(session_id)
    get_db().rollback()
    if row is None:
        return "missing"
    status = str(row.get("status") or "")
    if status == "finalized":
        return "finalized"
    if status == "finalizing":
        return "in_progress"
    if status == "revoked":
        return "revoked"
    return status or "unknown"


def complete_finalize(session_id: str) -> bool:
    """Mark a claimed finalization complete."""
    cursor = get_cursor()
    cursor.execute(
        """
        UPDATE live_transcription_sessions
        SET status = 'finalized',
            finalized_at = CURRENT_TIMESTAMP(6),
            version = version + 1
        WHERE session_id = %s AND status = 'finalizing'
        """,
        (session_id,),
    )
    get_db().commit()
    return cursor.rowcount == 1


def release_finalize(session_id: str) -> bool:
    """Return a failed finalization to its pre-finalize state."""
    cursor = get_cursor()
    cursor.execute(
        """
        UPDATE live_transcription_sessions
        SET status = CASE WHEN hangup_at IS NULL THEN 'active' ELSE 'closing' END,
            version = version + 1
        WHERE session_id = %s AND status = 'finalizing'
        """,
        (session_id,),
    )
    get_db().commit()
    return cursor.rowcount == 1


def mark_hangup(session_id: str) -> Dict[str, Any]:
    """Stop accepting live input while keeping finalization possible."""
    cursor = get_cursor()
    cursor.execute(
        """
        UPDATE live_transcription_sessions
        SET status = 'closing',
            hangup_at = COALESCE(hangup_at, CURRENT_TIMESTAMP(6)),
            version = version + 1
        WHERE session_id = %s AND status = 'active'
        """,
        (session_id,),
    )
    if cursor.rowcount == 1:
        get_db().commit()
        return {
            "found": True,
            "transitioned": True,
            "status": "closing",
            "hangup_completed_at": None,
        }

    row = get_session(session_id)
    get_db().rollback()
    if row is None:
        return {"found": False, "transitioned": False}
    return {
        "found": True,
        "transitioned": False,
        "status": row.get("status"),
        "hangup_completed_at": row.get("hangup_completed_at"),
    }


def mark_hangup_complete(session_id: str) -> bool:
    """Record that the provider stop has completed successfully."""
    cursor = get_cursor()
    cursor.execute(
        """
        UPDATE live_transcription_sessions
        SET hangup_completed_at = CURRENT_TIMESTAMP(6),
            version = version + 1
        WHERE session_id = %s
          AND status IN ('closing', 'finalizing', 'finalized')
          AND hangup_completed_at IS NULL
        """,
        (session_id,),
    )
    get_db().commit()
    return cursor.rowcount == 1


def revoke_session(session_id: str) -> bool:
    """Revoke a session that has not already been finalized."""
    cursor = get_cursor()
    cursor.execute(
        """
        UPDATE live_transcription_sessions
        SET status = 'revoked',
            revoked_at = CURRENT_TIMESTAMP(6),
            version = version + 1
        WHERE session_id = %s
          AND status IN ('active', 'closing')
        """,
        (session_id,),
    )
    get_db().commit()
    return cursor.rowcount == 1


def purge_expired_sessions(
    *,
    retention_seconds: int = LIVE_SESSION_RETENTION_SECONDS,
    max_duration_seconds: int = LIVE_SESSION_MAX_DURATION_SECONDS,
    now: Optional[float] = None,
    batch_size: int = 1000,
) -> int:
    """Delete terminal rows and abandoned sessions beyond bounded retention.

    Terminal rows remain available for replay/idempotency during the retention
    window. Active or transitional rows are also removed once they are older
    than the maximum session duration plus that window, which bounds claims
    left behind by a crashed process.
    """
    retention_seconds = max(0, int(retention_seconds))
    max_duration_seconds = max(0, int(max_duration_seconds))
    batch_size = max(1, int(batch_size))
    current_time = time.time() if now is None else float(now)
    terminal_cutoff = current_time - retention_seconds
    expired_cutoff = current_time - max_duration_seconds - retention_seconds

    cursor = get_cursor()
    cursor.execute(
        """
        DELETE FROM live_transcription_sessions
        WHERE (
            status IN ('finalized', 'revoked')
            AND COALESCE(finalized_at, revoked_at, created_at)
                < FROM_UNIXTIME(%s)
        ) OR (
            status IN ('active', 'closing', 'finalizing')
            AND started_at < %s
        )
        ORDER BY created_at ASC
        LIMIT %s
        """,
        (terminal_cutoff, expired_cutoff, batch_size),
    )
    get_db().commit()
    return int(cursor.rowcount)
