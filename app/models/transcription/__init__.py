from app.database import get_db, get_cursor

from .schema import init_db_command
from .serialization import _map_row_to_transcription_dict
from .persistence import (
    create_transcription_job,
    get_transcription_by_id,
    get_active_transcriptions,
    get_all_transcriptions,
    delete_transcription,
    restore_transcription,
    clear_transcriptions,
    mark_transcription_as_downloaded,
    toggle_transcription_pin,
)
from .services import (
    update_job_progress,
    update_job_status,
    set_job_error,
    mark_active_jobs_interrupted,
    finalize_job_success,
    update_title_generation_status,
    update_transcription_cost,
    set_generated_title,
)

__all__ = [
    "get_db",
    "get_cursor",
    "init_db_command",
    "_map_row_to_transcription_dict",
    "create_transcription_job",
    "get_transcription_by_id",
    "get_active_transcriptions",
    "get_all_transcriptions",
    "delete_transcription",
    "restore_transcription",
    "clear_transcriptions",
    "mark_transcription_as_downloaded",
    "toggle_transcription_pin",
    "update_job_progress",
    "update_job_status",
    "set_job_error",
    "mark_active_jobs_interrupted",
    "finalize_job_success",
    "update_title_generation_status",
    "update_transcription_cost",
    "set_generated_title",
]
