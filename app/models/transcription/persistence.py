import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from mysql.connector import Error as MySQLError

from app.database import get_cursor, get_db
from app.logging_config import get_logger

from .serialization import _map_row_to_transcription_dict


def create_transcription_job(job_id: str, user_id: int, filename: str, api_used: str,
                             file_size_mb: float, audio_length_minutes: float,
                             context_prompt_used: bool,
                             pending_workflow_prompt_text: Optional[str] = None,
                             pending_workflow_prompt_title: Optional[str] = None,
                             pending_workflow_prompt_color: Optional[str] = None,
                             pending_workflow_origin_prompt_id: Optional[int] = None,
                             public_api_invocation: bool = False,
                             api_model: Optional[str] = None
                             ) -> None:
# --- END MODIFIED ---
    """
    Creates an initial record for a transcription job in the database.
    Sets the status to 'pending'.
    Raises MySQLError on failure.
    """
    logger = get_logger(__name__, job_id=job_id, user_id=user_id, component="DB:Job")
    sql = '''
        INSERT INTO transcriptions (
            id, user_id, filename, generated_title, title_generation_status,
            file_size_mb, audio_length_minutes, api_used, api_model,
            created_at, status, progress_log, error_message, context_prompt_used, downloaded,
            is_hidden_from_user, hidden_date, hidden_reason,
            pending_workflow_prompt_text, pending_workflow_prompt_title, pending_workflow_prompt_color,
            pending_workflow_origin_prompt_id, public_api_invocation, cost
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        '''
    initial_log_entry = f"Job created at {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}."
    initial_log_json = json.dumps([initial_log_entry])

    cursor = get_cursor()
    try:
        cursor.execute(sql, (
            job_id, user_id, filename, None, 'pending',
            file_size_mb, audio_length_minutes, api_used, api_model,
            datetime.now(timezone.utc), 'pending', initial_log_json, None,
            context_prompt_used, False,
            False, None, None,
            pending_workflow_prompt_text, pending_workflow_prompt_title, pending_workflow_prompt_color,
            pending_workflow_origin_prompt_id,
            public_api_invocation,
            None # cost
        ))
        # --- END MODIFIED ---
        get_db().commit()
        # --- MODIFIED: Updated log message ---
        pending_wf_log = "No"
        if pending_workflow_prompt_text or pending_workflow_origin_prompt_id:
            pending_wf_log = f"Yes (Text: {'Set' if pending_workflow_prompt_text else 'Not Set'}, ID: {pending_workflow_origin_prompt_id if pending_workflow_origin_prompt_id else 'Not Set'})"
        logger.debug(f"Created initial job record for '{filename}' (Size: {file_size_mb:.2f} MB, Length: {audio_length_minutes:.2f} min, Context: {context_prompt_used}, Pending WF: {pending_wf_log}).")
        # --- END MODIFIED ---
    except MySQLError as err:
        logger.error(f"Error creating job record: {err}", exc_info=True)
        get_db().rollback()
        raise  # Re-raise the exception
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass


def get_transcription_by_id(transcription_id: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Retrieves a specific transcription job record by its ID.
    If user_id is provided, it ensures the job belongs to that user.
    Returns the job data as a dictionary or None if not found or not owned.
    """
    logger = get_logger(__name__, job_id=transcription_id, user_id=user_id, component="DB:Job")
    sql = 'SELECT * FROM transcriptions WHERE id = %s'
    params: List[Any] = [transcription_id]

    if user_id is not None:
        sql += ' AND user_id = %s'
        params.append(user_id)

    cursor = get_cursor()
    transcription_dict = None
    try:
        cursor.execute(sql, tuple(params))
        row = cursor.fetchone()
        cursor.fetchall()
        transcription_dict = _map_row_to_transcription_dict(row)

        if transcription_dict:
            log_msg = "Retrieved job record by ID."
            if user_id:
                log_msg += f" (Ownership verified for user {user_id})"
        else:
            log_msg = "Job record not found"
            if user_id:
                log_msg += f" or not owned by user {user_id}"
            logger.debug(log_msg)

    except MySQLError as err:
        log_msg = "Error retrieving transcription by ID"
        if user_id:
            log_msg += f" for user {user_id}"
        logger.error(f"{log_msg}: {err}", exc_info=True)
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
    return transcription_dict


def get_active_transcriptions(user_id: int) -> List[Dict[str, Any]]:
    """Return unfinished jobs for progress-panel reconnection."""
    cursor = get_cursor()
    cursor.execute(
        """
        SELECT * FROM transcriptions
        WHERE user_id = %s
          AND status IN ('pending', 'processing', 'cancelling')
          AND is_hidden_from_user = FALSE
        ORDER BY FIELD(status, 'processing', 'cancelling', 'pending'), created_at ASC
        """,
        (user_id,),
    )
    rows = cursor.fetchall()
    active_jobs = []
    for row in rows:
        mapped = _map_row_to_transcription_dict(row)
        if mapped is not None:
            active_jobs.append(mapped)
    return active_jobs


def get_all_transcriptions(user_id: int, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Retrieves transcription records for a specific user, ordered by creation date DESC.
    Only retrieves records that are NOT hidden from the user.
    Optionally limits the number of records returned via the SQL query.
    """
    logger = get_logger(__name__, user_id=user_id, component="DB:History")
    sql = 'SELECT * FROM transcriptions WHERE user_id = %s AND is_hidden_from_user = FALSE ORDER BY created_at DESC'
    params: List[Any] = [user_id]

    if limit is not None and limit > 0:
        sql += ' LIMIT %s'
        params.append(limit)
        limit_msg = f" with limit {limit}"
    else:
        limit_msg = ""

    transcriptions = []
    cursor = get_cursor()
    try:
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        transcriptions = [_map_row_to_transcription_dict(row) for row in rows if row]
        logger.debug(f"Retrieved {len(transcriptions)} visible transcription records{limit_msg}.")
    except MySQLError as err:
        logger.error(f"Error retrieving transcriptions: {err}", exc_info=True)
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
    return transcriptions


def delete_transcription(transcription_id: str, user_id: int) -> bool:
    """
    Soft-deletes a specific transcription record by ID, ensuring it belongs to the specified user.
    Sets the 'is_hidden_from_user' flag to TRUE and records the hidden date/reason.
    Returns True if the update was successful, False otherwise.
    """
    logger = get_logger(__name__, job_id=transcription_id, user_id=user_id, component="DB:Delete")
    sql = """
        UPDATE transcriptions
        SET is_hidden_from_user = TRUE,
            hidden_date = NOW(),
            hidden_reason = 'USER_DELETED'
        WHERE id = %s AND user_id = %s AND is_hidden_from_user = FALSE
        """
    cursor = get_cursor()
    try:
        cursor.execute(sql, (transcription_id, user_id))
        get_db().commit()
        if cursor.rowcount > 0:
            logger.info("Soft-deleted transcription record owned by user.")
            return True
        else:
            cursor.execute('SELECT user_id, is_hidden_from_user FROM transcriptions WHERE id = %s', (transcription_id,))
            job_info = cursor.fetchone()
            cursor.fetchall()
            if not job_info:
                logger.warning("Soft delete failed: Transcription not found.")
            elif job_info['user_id'] != user_id:
                logger.warning("Soft delete failed due to ownership mismatch.")
            elif job_info['is_hidden_from_user']:
                logger.warning("Soft delete failed: Transcription already hidden.")
            else:
                logger.warning("Soft delete failed for an unknown reason (rowcount was 0).")
            return False
    except MySQLError as err:
        logger.error(f"Error soft-deleting transcription: {err}", exc_info=True)
        get_db().rollback()
        return False
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass


def restore_transcription(transcription_id: str, user_id: int) -> bool:
    """
    Restores a previously soft-deleted transcription by resetting the hidden flags.
    Returns True when the record was restored, False otherwise.
    """
    logger = get_logger(__name__, job_id=transcription_id, user_id=user_id, component="DB:Restore")
    sql = """
        UPDATE transcriptions
        SET is_hidden_from_user = FALSE,
            hidden_date = NULL,
            hidden_reason = NULL
        WHERE id = %s AND user_id = %s AND is_hidden_from_user = TRUE
        """
    cursor = get_cursor()
    try:
        cursor.execute(sql, (transcription_id, user_id))
        get_db().commit()
        if cursor.rowcount > 0:
            logger.info("Restored soft-deleted transcription record for user.")
            return True
        logger.warning("Restore failed: transcription not found, not owned, or already visible.")
        return False
    except MySQLError as err:
        logger.error(f"Error restoring transcription: {err}", exc_info=True)
        get_db().rollback()
        return False
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass


def clear_transcriptions(user_id: int) -> int:
    """
    Soft-deletes ALL visible transcription records for a specific user.
    Sets the 'is_hidden_from_user' flag to TRUE and records the hidden date/reason.
    Returns the number of records hidden.
    """
    logger = get_logger(__name__, user_id=user_id, component="DB:Clear")
    sql = """
        UPDATE transcriptions
        SET is_hidden_from_user = TRUE,
            hidden_date = NOW(),
            hidden_reason = 'USER_DELETED'
        WHERE user_id = %s AND is_hidden_from_user = FALSE
        """
    hidden_count = 0
    cursor = get_cursor()
    try:
        cursor.execute(sql, (user_id,))
        hidden_count = cursor.rowcount
        get_db().commit()
        logger.info(f"Soft-deleted {hidden_count} transcription records.")
    except MySQLError as err:
        logger.error(f"Error clearing transcriptions: {err}", exc_info=True)
        get_db().rollback()
        hidden_count = 0
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
    return hidden_count


def toggle_transcription_pin(transcription_id: str, user_id: int) -> tuple[bool, bool]:
    """Toggles the is_pinned flag for a transcription owned by the user.

    Returns (success, new_is_pinned).
    """
    logger = get_logger(__name__, job_id=transcription_id, user_id=user_id, component="DB:Pin")
    sql = "UPDATE transcriptions SET is_pinned = NOT is_pinned WHERE id = %s AND user_id = %s AND is_hidden_from_user = FALSE"
    cursor = get_cursor()
    try:
        cursor.execute(sql, (transcription_id, user_id))
        get_db().commit()
        if cursor.rowcount > 0:
            cursor.execute("SELECT is_pinned FROM transcriptions WHERE id = %s", (transcription_id,))
            row = cursor.fetchone()
            cursor.fetchall()
            new_state = bool(row['is_pinned']) if row else False
            logger.info(f"Toggled is_pinned to {new_state}.")
            return True, new_state
        logger.warning("Pin toggle failed: transcription not found, not owned by user, or already hidden.")
        return False, False
    except MySQLError as err:
        logger.error(f"Error toggling pin: {err}", exc_info=True)
        get_db().rollback()
        return False, False
    finally:
        pass


def mark_transcription_as_downloaded(transcription_id: str, user_id: int) -> bool:
    """Sets the 'downloaded' flag to TRUE for a specific job owned by the user."""
    logger = get_logger(__name__, job_id=transcription_id, user_id=user_id, component="DB:DownloadLog")
    sql = "UPDATE transcriptions SET downloaded = TRUE WHERE id = %s AND user_id = %s AND status = 'finished'"
    cursor = get_cursor()
    try:
        cursor.execute(sql, (transcription_id, user_id))
        get_db().commit()
        if cursor.rowcount > 0:
            logger.info("Marked transcription as downloaded.")
            return True
        else:
            cursor.execute("SELECT status FROM transcriptions WHERE id = %s AND user_id = %s", (transcription_id, user_id))
            job_status = cursor.fetchone()
            cursor.fetchall()
            if job_status:
                logger.warning(f"Could not mark as downloaded. Job status is '{job_status['status']}'.")
            else:
                logger.warning("Could not mark as downloaded. Job not found or not owned by user.")
            return False
    except MySQLError as err:
        logger.error(f"Error marking transcription as downloaded: {err}", exc_info=True)
        get_db().rollback()
        return False
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
