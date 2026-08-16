# app/services/transcription_service.py
# Orchestrates the audio transcription process, including permission checks,
# API client selection, background processing, and database updates.

import os
import logging
from app.logging_config import get_logger
import threading
import json
import time
from typing import Callable, Optional, Any, Tuple
from flask import current_app, Flask
from datetime import datetime, timezone
from app.core.utils import format_currency

# Import DB models
from app.models import transcription as transcription_model
from app.models import user as user_model
from app.models import role as role_model
from app.models import transcription_catalog as transcription_catalog_model

# Import other services
from app.services import file_service, workflow_service # Added workflow_service
from app.services.user_service import get_decrypted_api_key, MissingApiKeyError
from app.services.pricing_service import get_price as get_pricing_service_price, PricingServiceError
from app.services.api_clients import get_transcription_client
from app.services.api_clients.transcription.base_transcription_client import BaseTranscriptionClient
from app.services.api_clients.exceptions import (
    TranscriptionApiError,
    TranscriptionConfigurationError,
    TranscriptionProcessingError,
    TranscriptionAuthenticationError,
    TranscriptionRateLimitError,
    TranscriptionQuotaExceededError
)

# Import permission checking helpers
from app.core.decorators import check_permission

# Import MySQL error class
from mysql.connector import Error as MySQLError

from app.tasks.title_generation import generate_title_task


_API_DISPLAY_NAME_FALLBACKS = {
    'gpt-transcribe': 'OpenAI GPT Transcribe',
    'gpt-4o-transcribe': 'OpenAI GPT-4o Transcribe',
    'whisper': 'OpenAI Whisper',
    'assemblyai': 'AssemblyAI Universal',
    'openrouter': 'OpenRouter'
}


def _get_api_display_name(model_code: str) -> str:
    """
    Resolves a human-friendly display name for a transcription model using the catalog as primary source.
    Falls back to predefined labels or title-cased code.
    """
    try:
        model_metadata = transcription_catalog_model.get_model_by_code(model_code)
        if model_metadata and model_metadata.get('display_name'):
            return model_metadata['display_name']
    except Exception as catalog_err:
        logging.getLogger(__name__).warning(
            "Failed to resolve display name for model '%s' from catalog: %s",
            model_code,
            catalog_err,
        )
    if model_code in _API_DISPLAY_NAME_FALLBACKS:
        return _API_DISPLAY_NAME_FALLBACKS[model_code]
    return model_code.replace('_', ' ').replace('-', ' ').title()

def _update_progress(app: Flask, job_id: str, message: str, is_error: bool = False,
                     log_message: bool = True, **context) -> None:
    """
    Formats, optionally logs, and saves a progress message for a job.
    Requires an active Flask application context to update the database.
    """
    logger = get_logger(__name__, job_id=job_id, **context)
    if log_message:
        log_level = "error" if is_error else "info"
        getattr(logger, log_level)(message)

    try:
        with app.app_context():
            transcription_model.update_job_progress(job_id, message)
    except RuntimeError:
        logger.error("Cannot update DB progress log: No Flask app context.")
    except MySQLError as db_err:
        logger.error(f"Failed to update DB progress log (MySQL Error): {db_err}", exc_info=True)
    except Exception as e:
        logger.error(f"Failed to update DB progress log: {e}", exc_info=True)

def _check_for_cancellation(app: Flask, job_id: str) -> bool:
    """Checks the database if the job status is 'cancelling'."""
    logger = get_logger(__name__, job_id=job_id)
    try:
        with app.app_context():
            job_data = transcription_model.get_transcription_by_id(job_id)
            if job_data and job_data.get('status') == 'cancelling':
                logger.info("Cancellation signal detected (status 'cancelling').")
                return True
    except Exception as e:
        logger.error(f"Error checking for cancellation status: {e}", exc_info=True)
    return False

def process_transcription(app: Flask, job_id: str, user_id: int, temp_filename: str, language_code: str,
                          api_choice: str, original_filename: str, context_prompt: str = "",
                          pending_workflow_prompt_text: Optional[str] = None,
                          pending_workflow_prompt_title: Optional[str] = None,
                          pending_workflow_prompt_color: Optional[str] = None,
                          pending_workflow_origin_prompt_id: Optional[int] = None,
                          speaker_diarization_enabled: bool = False
                          ) -> None:
    """
    Handles the audio transcription process in a background thread.
    Requires Flask app context to interact with database (MySQL) and config.
    Includes checks for cancellation requests. Calculates and stores duration in minutes.
    Uses the appropriate Transcription API client.
    Spawns a title generation task upon successful completion.
    If pending_workflow_prompt_text or pending_workflow_origin_prompt_id is provided,
    starts a workflow after successful transcription.
    """
    logger = get_logger(__name__, job_id=job_id, user_id=user_id, component="TranscriptionService")
    logger.debug(f"Background process started for file '{original_filename}'.")

    cancel_event = threading.Event()
    was_cancelled = False
    user: Optional[user_model.User] = None
    api_client: Optional[BaseTranscriptionClient] = None
    job_finalized_successfully = False

    with app.app_context():
        api_display_name = _get_api_display_name(api_choice)
        last_error_message_from_callback = "Transcription failed via API client."

        try:
            _update_progress(app, job_id, "Processing started. Validating permissions...", user_id=user_id)

            user = user_model.get_user_by_id(user_id)
            logger.debug("User loaded for transcription.", extra={"role_id": getattr(user, 'role_id', None), "has_cached_role": getattr(user, '_role', None) is not None if user else 'n/a'})
            if not user:
                raise PermissionError("User not found. Cannot start transcription.")
            # Conditionally handle/pin role snapshot to avoid cross-test/async drift while preserving test patches
            try:
                cached = getattr(user, '_role', None)
                role_snapshot = cached
                if cached is not None:
                    logger.debug("Preserving pre-cached role snapshot on user; skipping DB reload.")
                else:
                    # In testing mode, we still need to load the role if it's not cached
                    # The user object from get_user_by_id should have the role pinned already
                    # If not, we need to load it to avoid permission check failures
                    logger.debug("No cached role found; loading role from user.role property.")
                    role_snapshot = user.role if user else None
                    if role_snapshot:
                        user._role = role_snapshot
                        logger.debug(f"Pinned role snapshot. role_id={getattr(user, 'role_id', None)}, role_name={getattr(role_snapshot, 'name', None)}")
                    else:
                        logger.warning(f"Failed to load role for user {user_id} with role_id={getattr(user, 'role_id', None)}")
            except Exception as pin_err:
                logger.error(f"Failed during role snapshot handling for user {user_id}: {pin_err}", exc_info=True)
                role_snapshot = getattr(user, '_role', None)
            # Resolve role object for permission checks
            role_obj = role_snapshot
            if not role_obj:
                raise PermissionError("User role not found. Cannot determine permissions.")
            # Ensure user._role aligns with role_obj for subsequent property access
            try:
                user._role = role_obj
            except Exception:
                pass

            if _check_for_cancellation(app, job_id):
                was_cancelled = True; cancel_event.set(); raise InterruptedError("Job cancelled by user before permission checks.")

            api_permission = None
            try:
                model_metadata = transcription_catalog_model.get_model_by_code(api_choice)
                if model_metadata:
                    api_permission = model_metadata.get('permission_key')
            except Exception as catalog_err:
                logger.error(f"Failed to resolve catalog metadata for model '{api_choice}': {catalog_err}", exc_info=True)

            # Diagnostic logging for permission context
            try:
                source = 'cached' if role_snapshot is not None else ('testing-skip' if current_app.testing else 'db')
                logger.debug(
                    "Permission context resolved.",
                    extra={"permission": api_permission, "role": getattr(role_obj, 'name', None), "role_source": source}
                )
            except Exception as diag_err:
                logger.error(f"Failed to log permission diagnostic info: {diag_err}", exc_info=True)

            allowed_perm = bool(api_permission) and check_permission(user, api_permission)
            logger.debug("Permission check result.", extra={"permission": api_permission, "allowed": allowed_perm})
            if not allowed_perm:
                logger.warning("Permission denied.", extra={"permission": api_permission, "role": getattr(role_obj, 'name', None)})
                raise PermissionError(f"Permission denied to use the '{api_display_name}' API.")

            file_size_mb = 0.0
            audio_length_minutes = 0.0
            audio_length_seconds = 0.0
            try:
                if not os.path.exists(temp_filename): raise FileNotFoundError(f"Temporary audio file not found: {temp_filename}")
                file_size_bytes = os.path.getsize(temp_filename)
                file_size_mb = file_size_bytes / (1024 * 1024)

                try:
                    # Use the memory-efficient ffprobe method to get duration
                    audio_length_seconds, audio_length_minutes = file_service.get_audio_duration(temp_filename)
                    if audio_length_seconds == 0.0:
                        logger.warning(f"Could not determine audio duration for '{original_filename}'. Assuming 0 minutes.")
                except Exception as audio_err:
                    logger.error(f"Error getting audio duration for '{original_filename}': {audio_err}", exc_info=True)
                    audio_length_seconds = 0.0
                    audio_length_minutes = 0.0
            except OSError as e:
                logger.error(f"Could not get file size/info: {e}")
                raise PermissionError(f"Could not determine file size for permission check.")

            LARGE_FILE_THRESHOLD_MB = 25
            if file_size_mb > LARGE_FILE_THRESHOLD_MB and not check_permission(user, 'allow_large_files'):
                raise PermissionError(f"File exceeds {LARGE_FILE_THRESHOLD_MB}MB limit. Permission denied.")

            if context_prompt and not check_permission(user, 'allow_context_prompt'):
                logger.warning("Context prompt provided but permission check failed. Ignoring prompt.")
                context_prompt = ""
                _update_progress(app, job_id, "Warning: Context prompt ignored due to lack of permission.", is_error=False, user_id=user_id)

            price = get_pricing_service_price(item_type='transcription', item_key=api_choice)
            cost_to_add = 0.0
            if price is not None:
                cost_to_add = price * (audio_length_minutes if audio_length_minutes >= 1 else audio_length_seconds / 60)

            allowed, reason = role_model.reserve_usage_if_allowed(
                user_id,
                role_obj,
                cost_to_add=cost_to_add,
                minutes_to_add=audio_length_minutes,
            )
            if not allowed:
                raise PermissionError(f"Usage limit exceeded: {reason}")

            logger.debug("Permission checks passed and usage was reserved transactionally.")
            _update_progress(app, job_id, "Permissions validated.", user_id=user_id)

            transcription_model.update_transcription_cost(job_id, cost_to_add)
            logger.debug(f"Successfully calculated and saved cost: {cost_to_add}")
            _update_progress(app, job_id, f"Calculated and recorded cost: {format_currency(cost_to_add)}.", user_id=user_id)
            # --- END MODIFIED ---

            _update_progress(app, job_id, "PHASE_MARKER:UPLOAD_COMPLETE", user_id=user_id, log_message=False)

            transcription_model.update_job_status(job_id, 'processing')
            logger.debug(f"Handing off to API client '{api_choice}'.")

            if _check_for_cancellation(app, job_id):
                was_cancelled = True; cancel_event.set(); raise InterruptedError("Job cancelled by user before API call.")

            api_key: Optional[str] = None
            mode = current_app.config['DEPLOYMENT_MODE']
            try:
                if mode == 'multi':
                    key_service_name = 'openai' if api_choice in ['whisper', 'gpt-4o-transcribe', 'gpt-transcribe'] else api_choice
                    api_key = get_decrypted_api_key(user_id, key_service_name)

                    if api_key:
                        logger.debug(f"Using user-specific API key for '{api_display_name}'.")
                    else:
                        # No user-specific key found, decide fallback strategy
                        if user.has_permission('allow_api_key_management'):
                            # User is allowed to set a key, but hasn't. This is an error.
                            raise MissingApiKeyError(f"ERROR: {api_display_name} API key not configured by user.")
                        else:
                            # User is not allowed to set a key, so fall back to global config.
                            logger.debug(f"User key not found and role does not allow key management. Falling back to global API key for '{api_display_name}'.")
                            key_env_var = None
                            if api_choice == 'assemblyai': key_env_var = 'ASSEMBLYAI_API_KEY'
                            elif api_choice in ['whisper', 'gpt-4o-transcribe', 'gpt-transcribe']: key_env_var = 'OPENAI_API_KEY'
                            elif api_choice == 'openrouter': key_env_var = 'OPENROUTER_API_KEY'
                            
                            if key_env_var:
                                api_key = current_app.config.get(key_env_var)
                            
                            if not api_key:
                                raise MissingApiKeyError(f"ERROR: Global {api_display_name} API key ({key_env_var}) is not configured for role.")
                            logger.debug(f"Using global API key for '{api_display_name}' (user key management disabled).")
                elif mode == 'single':
                    key_env_var = None
                    if api_choice == 'assemblyai': key_env_var = 'ASSEMBLYAI_API_KEY'
                    elif api_choice in ['whisper', 'gpt-4o-transcribe', 'gpt-transcribe']: key_env_var = 'OPENAI_API_KEY'
                    elif api_choice == 'openrouter': key_env_var = 'OPENROUTER_API_KEY'
                    if key_env_var: api_key = current_app.config.get(key_env_var)
                    if not api_key:
                        raise ValueError(f"ERROR: Global {api_display_name} API key ({key_env_var}) is not configured.")
                    logger.debug(f"Using global API key for '{api_display_name}' (single-user mode).")
                else:
                    raise ValueError(f"Invalid DEPLOYMENT_MODE: {mode}")

                api_client = get_transcription_client(api_choice, api_key, app.config)

            except (ValueError, MissingApiKeyError) as key_err:
                logger.error(f"Failed to get API key/client: {key_err}")
                raise PermissionError(str(key_err)) from key_err

            def api_progress_callback(msg: str, is_err: bool = False):
                nonlocal last_error_message_from_callback, was_cancelled
                if not was_cancelled and _check_for_cancellation(app, job_id):
                    was_cancelled = True
                    cancel_event.set()
                    raise InterruptedError("Job cancelled by user (detected via DB status).")
                if is_err: last_error_message_from_callback = msg
                _update_progress(app, job_id, msg, is_error=is_err, user_id=user_id, log_message=False)

            extra_transcription_options = None
            if api_choice == 'assemblyai' and speaker_diarization_enabled:
                if user and check_permission(user, 'allow_speaker_diarization'):
                    extra_transcription_options = {'speaker_diarization_enabled': True}
                    logger.info("Speaker diarization enabled for AssemblyAI job.")
                else:
                    speaker_diarization_enabled = False
                    logger.warning("Speaker diarization flag ignored due to missing permission.")

            transcribe_args = {
                "audio_file_path": temp_filename, "language_code": language_code,
                "progress_callback": api_progress_callback, "original_filename": original_filename,
                "context_prompt": context_prompt, "cancel_event": cancel_event,
                "audio_length_seconds": audio_length_seconds, "extra_options": extra_transcription_options
            }

            transcription_text: Optional[str] = None
            detected_language: Optional[str] = None
            result_tuple: Tuple[Optional[str], Optional[str]] = api_client.transcribe(**transcribe_args)
            transcription_text, detected_language = result_tuple

            logger.debug("API client transcribe method finished successfully.")

            final_language = detected_language or language_code or 'unknown'
            logger.info(f"Transcription successful. Final language: {final_language}.")

            transcription_model.finalize_job_success(job_id, transcription_text, final_language)
            job_finalized_successfully = True
            logger.debug("Job finalized successfully in database.")

        except InterruptedError as ie:
             logger.info(f"Transcription process interrupted: {ie}")
             was_cancelled = True

        except (PermissionError, FileNotFoundError, ValueError) as setup_err:
            error_message = f"ERROR: {str(setup_err)}"
            logger.error(f"Transcription setup failed: {error_message}", exc_info=isinstance(setup_err, ValueError))
            _update_progress(app, job_id, error_message, is_error=True, user_id=user_id)
            try: transcription_model.set_job_error(job_id, error_message)
            except Exception as db_err: logger.error(f"CRITICAL: Failed to record setup error in DB: {db_err}", exc_info=True)

        except TranscriptionQuotaExceededError as quota_err:
            provider_name = quota_err.provider or api_display_name
            error_message = f"ERROR: {provider_name} API quota exceeded. Please check your plan/billing with {provider_name}."
            logger.error(f"Transcription failed due to quota limit: {quota_err}", exc_info=True)
            _update_progress(app, job_id, error_message, is_error=True, user_id=user_id)
            try: transcription_model.set_job_error(job_id, error_message)
            except Exception as db_err: logger.error(f"CRITICAL: Failed to record quota error in DB: {db_err}", exc_info=True)

        except (TranscriptionAuthenticationError, TranscriptionRateLimitError, TranscriptionProcessingError, TranscriptionConfigurationError) as api_err:
            error_message = f"ERROR: {str(api_err)}"
            log_level = "warning" if isinstance(api_err, TranscriptionRateLimitError) else "error"
            getattr(logger, log_level)(f"Transcription API error: {error_message}", exc_info=True)
            _update_progress(app, job_id, error_message, is_error=True, user_id=user_id)
            try: transcription_model.set_job_error(job_id, error_message)
            except Exception as db_err: logger.error(f"CRITICAL: Failed to record API error in DB: {db_err}", exc_info=True)

        except MySQLError as db_err:
            error_message = f"Database Error: {str(db_err)}"
            logger.error(f"Database error during transcription process: {error_message}", exc_info=True)
            try:
                _update_progress(app, job_id, "ERROR: A database error occurred.", is_error=True, user_id=user_id)
                transcription_model.set_job_error(job_id, "Internal database error.")
            except Exception as final_db_err:
                 logger.critical(f"CRITICAL: Failed to record DB error status in DB itself: {final_db_err}", exc_info=True)

        except Exception as e:
            error_message = f"An unexpected error occurred: {str(e)}"
            logger.exception("Unexpected error during transcription process:")
            try:
                _update_progress(app, job_id, "ERROR: An unexpected internal error occurred.", is_error=True, user_id=user_id)
                transcription_model.set_job_error(job_id, "An unexpected internal error occurred.")
            except Exception as final_err:
                 logger.critical(f"CRITICAL: Failed to record unexpected error status in DB: {final_err}", exc_info=True)

        finally:
            if was_cancelled:
                try:
                    with app.app_context():
                        current_status_dict = transcription_model.get_transcription_by_id(job_id)
                        if current_status_dict and current_status_dict.get('status') != 'cancelled':
                            transcription_model.update_job_status(job_id, 'cancelled')
                            _update_progress(app, job_id, "Transcription process cancelled.", user_id=user_id)
                            logger.debug("Job status updated to 'cancelled'.")
                        elif not current_status_dict:
                             logger.warning("Job record not found during cancellation cleanup.")
                        else:
                            logger.debug("Job status already 'cancelled'.")
                except Exception as cancel_db_err:
                    logger.error(f"Failed to update job status to 'cancelled' in DB: {cancel_db_err}", exc_info=True)

            if os.path.exists(temp_filename):
                logger.debug(f"Attempting cleanup of temp file: {temp_filename}")
                removed_count = file_service.remove_files([temp_filename])
                if removed_count > 0:
                    try:
                        with app.app_context():
                            current_status_dict = transcription_model.get_transcription_by_id(job_id)
                            if current_status_dict and current_status_dict.get('status') == 'finished':
                                _update_progress(app, job_id, f"Cleaned up temporary file: {original_filename}", user_id=user_id)
                    except Exception: pass
                    logger.debug("Cleaned up temporary file.")
                else:
                     logger.error(f"Failed to clean up temporary file: {temp_filename}")
                     try:
                         _update_progress(app, job_id, f"Warning: Failed to clean up temporary file {original_filename}.", is_error=False, user_id=user_id)
                     except Exception: pass
            else:
                logger.debug(f"Temp file already removed or never existed: {temp_filename}")

            logger.debug("Background process finished.")

        if job_finalized_successfully:
            if user and user.enable_auto_title_generation and user.has_permission('allow_auto_title_generation'):
                logger.debug(f"Spawning title generation task for job {job_id} (user enabled & permitted).")
                try:
                    title_thread = threading.Thread(
                        target=generate_title_task,
                        args=(app, job_id, user_id),
                        daemon=True
                    )
                    title_thread.start()
                    logger.debug("Title generation thread initiated.")
                except Exception as title_spawn_err:
                    logger.error(f"Failed to spawn title generation thread: {title_spawn_err}", exc_info=True)
            else:
                reason = "user preference disabled" if not (user and user.enable_auto_title_generation) else "permission denied"
                logger.debug(f"Skipping title generation task for job {job_id} ({reason}).")

            if pending_workflow_prompt_text or pending_workflow_origin_prompt_id:
                logger.debug(f"Pending workflow data found. Initiating workflow for job {job_id}. Text set: {bool(pending_workflow_prompt_text)}, Origin ID: {pending_workflow_origin_prompt_id}")
                try:
                    workflow_service.start_workflow(
                        user_id=user_id,
                        transcription_id=job_id,
                        prompt=pending_workflow_prompt_text, # Can be None if only ID is provided
                        prompt_id=pending_workflow_origin_prompt_id
                    )
                    logger.debug(f"Pending workflow successfully initiated for job {job_id}.")
                except Exception as wf_err:
                    logger.error(f"Failed to initiate pending workflow for job {job_id}: {wf_err}", exc_info=True)
                    with app.app_context():
                        transcription_model.update_job_progress(job_id, f"ERROR: Failed to start pre-selected workflow: {str(wf_err)}", is_error=True)
