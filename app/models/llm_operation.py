# app/models/llm_operation.py
# Defines the LLMOperation model and database interaction functions for MySQL.

import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

# Import MySQL specific error class
from mysql.connector import Error as MySQLError

# Import centralized DB functions
from app.database import get_db, get_cursor

# Import config for provider validation
from app.config import Config
from app.core.utils import format_currency

# --- LLMOperation Class Definition (Optional but good practice) ---
class LLMOperation:
    id: int
    user_id: int
    provider: str
    model: Optional[str]
    operation_type: str
    input_text: Optional[str]
    result: Optional[str]
    transcription_id: Optional[str]  # Nullable foreign key to Transcription ID (VARCHAR 36)
    prompt_id: Optional[int]  # Nullable foreign key to UserPrompt or TemplatePrompt (or other prompt types)
    created_at: str
    completed_at: Optional[str]
    status: str  # e.g., 'pending', 'processing', 'finished', 'error'
    error: Optional[str]
    # Add other fields as needed, e.g., token counts, cost, model used

    def __init__(self, **kwargs):
        self.id = kwargs.get('id')
        self.user_id = kwargs.get('user_id')
        self.provider = kwargs.get('provider')
        self.model = kwargs.get('model')
        self.operation_type = kwargs.get('operation_type')
        self.input_text = kwargs.get('input_text')
        self.result = kwargs.get('result')
        self.transcription_id = kwargs.get('transcription_id')
        self.prompt_id = kwargs.get('prompt_id')
        self.created_at = kwargs.get('created_at')
        self.completed_at = kwargs.get('completed_at')
        self.status = kwargs.get('status', 'pending')
        self.error = kwargs.get('error')

    def __repr__(self):
        transcription_info = f", TranscriptionID:{self.transcription_id[:8]}..." if self.transcription_id else ""
        prompt_info = f", PromptID:{self.prompt_id}" if self.prompt_id else ""
        return f'<LLMOperation {self.id} (User:{self.user_id}, Provider:{self.provider}, Type:{self.operation_type}, Status:{self.status}{transcription_info}{prompt_info})>'

# --- Database Schema Initialization ---

def init_db_command() -> None:
    """Initializes the 'llm_operations' table schema."""
    cursor = get_cursor()
    log_prefix = "[DB:Schema:MySQL]"
    logging.info(f"{log_prefix} Checking/Initializing 'llm_operations' table...")
    try:
        # Dependency Checks
        cursor.execute("SHOW TABLES LIKE 'users'")
        if not cursor.fetchone():
            raise RuntimeError("User table must exist before llm_operations table can be initialized.")
        cursor.fetchall()
        cursor.execute("SHOW TABLES LIKE 'transcriptions'")
        if not cursor.fetchone():
            raise RuntimeError("Transcriptions table must exist before llm_operations table can be initialized.")
        cursor.fetchall()
        # Note: We don't strictly depend on prompt tables, as prompt_id is nullable

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS llm_operations (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user_id INT NOT NULL,
                provider VARCHAR(50) NOT NULL,
                model VARCHAR(120) DEFAULT NULL,
                operation_type VARCHAR(50) NOT NULL,
                input_text MEDIUMTEXT,
                result MEDIUMTEXT,
                transcription_id VARCHAR(36) DEFAULT NULL,
                prompt_id INT DEFAULT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP NULL DEFAULT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                error TEXT DEFAULT NULL,
                cost DECIMAL(10, 5) DEFAULT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
                FOREIGN KEY (transcription_id) REFERENCES transcriptions (id) ON DELETE SET NULL,
                INDEX idx_llm_op_user (user_id),
                INDEX idx_llm_op_provider (provider),
                INDEX idx_llm_op_model (model),
                INDEX idx_llm_op_type (operation_type),
                INDEX idx_llm_op_status (status),
                INDEX idx_llm_op_transcription (transcription_id),
                INDEX idx_llm_op_prompt (prompt_id),
                INDEX idx_llm_op_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            '''
        )
        get_db().commit()
        logging.info(f"{log_prefix} 'llm_operations' table schema verified/initialized.")

        # Check and add cost
        cursor.execute("SHOW COLUMNS FROM llm_operations LIKE 'cost'")
        cost_col_exists = cursor.fetchone()
        cursor.fetchall()  # Consume remaining results
        if not cost_col_exists:
            logging.info(f"{log_prefix} Adding 'cost' column (DECIMAL(10, 5)) to 'llm_operations' table.")
            cursor.execute("ALTER TABLE llm_operations ADD COLUMN cost DECIMAL(10, 5) DEFAULT NULL AFTER error")

        cursor.execute("SHOW COLUMNS FROM llm_operations LIKE 'model'")
        model_col_exists = cursor.fetchone()
        cursor.fetchall()
        if not model_col_exists:
            logging.info(f"{log_prefix} Adding 'model' column to 'llm_operations' table.")
            cursor.execute("ALTER TABLE llm_operations ADD COLUMN model VARCHAR(120) DEFAULT NULL AFTER provider")
        cursor.execute("SHOW INDEX FROM llm_operations WHERE Key_name = 'idx_llm_op_model'")
        model_index_exists = cursor.fetchone()
        cursor.fetchall()
        if not model_index_exists:
            cursor.execute("ALTER TABLE llm_operations ADD INDEX idx_llm_op_model (model)")

        cursor.execute("SHOW COLUMNS FROM llm_operations LIKE 'transcription_id'")
        transcription_id_col = cursor.fetchone()
        cursor.fetchall()
        transcription_id_type = (transcription_id_col.get('Type') if isinstance(transcription_id_col, dict) else (transcription_id_col[1] if transcription_id_col else "")).lower()
        if transcription_id_col and 'varchar' not in transcription_id_type:
            logging.info(f"{log_prefix} Normalizing 'transcription_id' column to VARCHAR(36).")
            cursor.execute("ALTER TABLE llm_operations MODIFY COLUMN transcription_id VARCHAR(36) DEFAULT NULL")

        cursor.execute("SHOW COLUMNS FROM llm_operations LIKE 'created_at'")
        created_at_col = cursor.fetchone()
        cursor.fetchall()
        created_at_type = (created_at_col.get('Type') if isinstance(created_at_col, dict) else (created_at_col[1] if created_at_col else "")).lower()
        if created_at_col and 'timestamp' not in created_at_type:
            logging.info(f"{log_prefix} Converting 'created_at' column on 'llm_operations' table to TIMESTAMP.")
            cursor.execute("ALTER TABLE llm_operations MODIFY COLUMN created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP")

        cursor.execute("SHOW COLUMNS FROM llm_operations LIKE 'completed_at'")
        completed_at_col = cursor.fetchone()
        cursor.fetchall()
        completed_at_type = (completed_at_col.get('Type') if isinstance(completed_at_col, dict) else (completed_at_col[1] if completed_at_col else "")).lower()
        if completed_at_col and 'timestamp' not in completed_at_type:
            logging.info(f"{log_prefix} Converting 'completed_at' column on 'llm_operations' table to TIMESTAMP.")
            cursor.execute("ALTER TABLE llm_operations MODIFY COLUMN completed_at TIMESTAMP NULL DEFAULT NULL")
        get_db().commit()
    except MySQLError as err:
        logging.error(f"{log_prefix} Error during 'llm_operations' table initialization: {err}", exc_info=True)
        get_db().rollback()
        raise
    except RuntimeError as e:
        logging.error(f"{log_prefix} Initialization dependency error: {e}")
        get_db().rollback()
        raise
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass

# --- Helper Function ---

def _map_row_to_llm_operation_dict(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Maps a database row (dictionary) to a dictionary representing an LLMOperation."""
    if not row:
        return None
    return row

# --- CRUD Operations ---

def create_llm_operation(
    user_id: int,
    provider: str,
    operation_type: str,
    input_text: Optional[str] = None,
    transcription_id: Optional[str] = None,
    prompt_id: Optional[int] = None,
    status: str = 'pending',
    model: Optional[str] = None,
) -> Optional[int]:
    """
    Creates an initial record for an LLM operation.

    Returns:
        The ID of the newly created operation, or None on failure.
    """
    log_prefix = f"[DB:LLMOperation:User:{user_id}]"

    if not any(provider.startswith(p) for p in Config.LLM_PROVIDERS):
        logging.error(f"{log_prefix} Invalid LLM provider specified: '{provider}'. Valid providers are: {Config.LLM_PROVIDERS}")
        return None

    sql = """
        INSERT INTO llm_operations (
            user_id, provider, model, operation_type, input_text, transcription_id,
            prompt_id, created_at, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), %s)
    """
    cursor = get_cursor()
    operation_id = None
    try:
        cursor.execute(sql, (
            user_id, provider, model, operation_type, input_text, transcription_id,
            prompt_id, status
        ))
        get_db().commit()
        operation_id = cursor.lastrowid
        logging.info(f"{log_prefix} Created LLM operation record ID {operation_id} (Type: {operation_type}, Provider: {provider}, Status: {status}).")
    except MySQLError as err:
        get_db().rollback()
        logging.error(f"{log_prefix} Error creating LLM operation record: {err}", exc_info=True)
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
    return operation_id

def update_llm_operation_status(
    operation_id: int,
    status: str,
    result: Optional[str] = None,
    error: Optional[str] = None
) -> bool:
    """Updates the status, result, error, and completed_at timestamp for an LLM operation."""
    log_prefix = f"[DB:LLMOperation:{operation_id}]"
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)

    valid_statuses = ['pending', 'processing', 'finished', 'error']
    if status not in valid_statuses:
        logging.error(f"{log_prefix} Attempted to set invalid status: '{status}'")
        return False

    update_fields = {'status': status}
    params = [status]

    if status in ['finished', 'error']:
        update_fields['completed_at'] = now_utc
        params.append(now_utc)

    if status == 'finished':
        update_fields['result'] = result
        update_fields['error'] = None
        params.append(result)
        params.append(None)
    elif status == 'error':
        update_fields['error'] = error
        params.append(error)
    elif status == 'processing':
        update_fields['completed_at'] = None
        params.append(None)

    set_clauses = ", ".join([f"{field} = %s" for field in update_fields])
    sql = f"UPDATE llm_operations SET {set_clauses} WHERE id = %s"
    params.append(operation_id)

    cursor = get_cursor()
    success = False
    try:
        cursor.execute(sql, tuple(params))
        get_db().commit()
        if cursor.rowcount > 0:
            logging.info(f"{log_prefix} Updated LLM operation status to '{status}'.")
            success = True
        else:
            logging.warning(f"{log_prefix} Update failed: LLM operation ID {operation_id} not found or no changes made.")
    except MySQLError as err:
        get_db().rollback()
        logging.error(f"{log_prefix} Error updating LLM operation status: {err}", exc_info=True)
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
    return success

def update_llm_operation_result(operation_id: int, user_id: int, new_result: str) -> bool:
    """
    Updates the result field of a specific LLM operation, verifying ownership.

    Args:
        operation_id: The ID of the LLM operation to update.
        user_id: The ID of the user attempting the update.
        new_result: The new result text.

    Returns:
        True if the update was successful (record found and owned) or if the new result matches the current value, False otherwise.
    """
    log_prefix = f"[DB:LLMOperation:{operation_id}:User:{user_id}]"
    sql = "UPDATE llm_operations SET result = %s WHERE id = %s AND user_id = %s"
    cursor = get_cursor()
    success = False
    try:
        cursor.execute(sql, (new_result, operation_id, user_id))
        get_db().commit()
        if cursor.rowcount > 0:
            logging.info(f"{log_prefix} Updated LLM operation result.")
            success = True
        else:
            # Check if the record exists and if its result already equals the new value.
            cursor.execute("SELECT result, user_id FROM llm_operations WHERE id = %s", (operation_id,))
            op_info = cursor.fetchone()
            if not op_info:
                logging.warning(f"{log_prefix} Update result failed: LLM operation not found.")
            elif op_info['user_id'] != user_id:
                logging.warning(f"{log_prefix} Update result failed: Ownership mismatch.")
            elif op_info['result'] == new_result:
                logging.info(f"{log_prefix} Update result not needed: new result matches existing result.")
                success = True
            else:
                logging.warning(f"{log_prefix} Update result failed: No changes made (rowcount 0).")
    except MySQLError as err:
        get_db().rollback()
        logging.error(f"{log_prefix} Error updating LLM operation result: {err}", exc_info=True)
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
    return success

def mark_stale_operations_interrupted(stale_seconds: Optional[int] = None) -> int:
    """Fail pending/processing operations abandoned by a restart or crash.

    Background LLM work runs in daemon threads, so rows can stay
    'pending'/'processing' forever after the app dies mid-run — which then
    blocks new workflows for that transcription. Called at bootstrap with
    ``stale_seconds=None`` (sweep everything: no threads can survive a
    restart) or opportunistically with an age threshold to also catch stalls.

    Returns the number of operations marked as interrupted.
    """
    log_prefix = "[DB:LLMOperation:Recovery]"
    if stale_seconds is not None and stale_seconds <= 0:
        return 0

    base_sql = """
        UPDATE llm_operations
        SET status = 'error',
            error = 'Interrupted: application restarted or operation stalled.',
            completed_at = NOW()
        WHERE status IN ('pending', 'processing')
    """
    sql = base_sql + " AND created_at < (NOW() - INTERVAL %s SECOND)" if stale_seconds is not None else base_sql
    params = (stale_seconds,) if stale_seconds is not None else ()

    cursor = get_cursor()
    interrupted = 0
    try:
        cursor.execute(sql, params)
        interrupted = cursor.rowcount
        get_db().commit()
        if interrupted > 0:
            logging.warning(f"{log_prefix} Marked {interrupted} stuck LLM operation(s) as interrupted.")
        else:
            logging.debug(f"{log_prefix} No stuck LLM operations found.")
    except MySQLError as err:
        get_db().rollback()
        logging.error(f"{log_prefix} Error marking stale LLM operations interrupted: {err}", exc_info=True)
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
    return interrupted

def delete_orphaned_llm_operations(retention_period_days: int) -> int:
    """Physically deletes LLM operations whose transcription is gone.

    Transcription rows are physically deleted by the retention pipeline and
    their llm_operations links are severed (ON DELETE SET NULL), leaving
    orphaned rows — including stored prompt/result text — behind forever.
    This removes orphans older than the retention period, mirroring the
    transcription deletion policy. Returns the number of rows deleted.
    """
    log_prefix = "[DB:LLMOperation:OrphanCleanup]"
    if retention_period_days <= 0:
        logging.warning(
            "%s Orphan retention period is zero or negative (%s days). Skipping.",
            log_prefix,
            retention_period_days,
        )
        return 0

    cursor = get_cursor()
    deleted_count = 0
    try:
        sql = """
            DELETE FROM llm_operations
            WHERE transcription_id IS NULL
              AND created_at < (NOW() - INTERVAL %s DAY)
        """
        cursor.execute(sql, (retention_period_days,))
        deleted_count = cursor.rowcount
        get_db().commit()
        if deleted_count > 0:
            logging.info(f"{log_prefix} Physically deleted {deleted_count} orphaned LLM operation(s).")
        else:
            logging.debug(f"{log_prefix} No orphaned LLM operations older than {retention_period_days} day(s).")
    except MySQLError as err:
        get_db().rollback()
        logging.error(f"{log_prefix} Error deleting orphaned LLM operations: {err}", exc_info=True)
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
    return deleted_count

def get_llm_operation_by_id(operation_id: int, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Retrieves a specific LLM operation by its ID.
    Optionally verifies ownership if user_id is provided.
    """
    log_prefix = f"[DB:LLMOperation:{operation_id}]"
    sql = "SELECT * FROM llm_operations WHERE id = %s"
    params: List[Any] = [operation_id]

    if user_id is not None:
        sql += " AND user_id = %s"
        params.append(user_id)
        log_prefix += f":User:{user_id}"

    operation_dict = None
    cursor = get_cursor()
    try:
        cursor.execute(sql, tuple(params))
        row = cursor.fetchone()
        operation_dict = _map_row_to_llm_operation_dict(row)
        if operation_dict:
            logging.debug(f"{log_prefix} Retrieved LLM operation record.")
        else:
            logging.debug(f"{log_prefix} LLM operation record not found or ownership mismatch.")
    except MySQLError as err:
        logging.error(f"{log_prefix} Error retrieving LLM operation: {err}", exc_info=True)
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
    return operation_dict
 
def update_llm_operation_cost(operation_id: int, cost: float) -> bool:
    """Updates the cost for a specific LLM operation."""
    log_prefix = f"[DB:Cost:LLMOp:{operation_id}]"
    sql = "UPDATE llm_operations SET cost = %s WHERE id = %s"
    cursor = get_cursor()
    success = False
    try:
        cursor.execute(sql, (cost, operation_id))
        get_db().commit()
        if cursor.rowcount > 0:
            logging.info(f"{log_prefix} Updated LLM operation cost to: {format_currency(cost)}")
            success = True
        else:
            logging.warning(f"{log_prefix} Attempted to update cost for non-existent LLM operation.")
    except MySQLError as err:
        logging.error(f"{log_prefix} Error updating LLM operation cost to '{cost}': {err}", exc_info=True)
        get_db().rollback()
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
    return success


def get_latest_workflow_operation_for_transcription(transcription_id: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Gets the latest workflow LLM operation record for a given transcription."""
    sql = "SELECT * FROM llm_operations WHERE transcription_id = %s AND operation_type = 'workflow'"
    params: List[Any] = [transcription_id]
    if user_id is not None:
        sql += " AND user_id = %s"
        params.append(user_id)
    sql += " ORDER BY completed_at DESC, created_at DESC LIMIT 1"
    cursor = get_cursor()
    try:
        cursor.execute(sql, tuple(params))
        row = cursor.fetchone()
        cursor.fetchall()
        return _map_row_to_llm_operation_dict(row)
    except MySQLError as err:
        logging.error(f"[DB:LLMOperation] Error fetching latest workflow for transcription {transcription_id}: {err}", exc_info=True)
        return None
