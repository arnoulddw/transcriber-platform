# app/api/transcriptions.py
# Defines the Blueprint for transcription-related API endpoints.

import os
import uuid
import logging
import json
import math
from flask import Blueprint, request, jsonify, current_app
from flask_babel import gettext as _
from werkzeug.utils import secure_filename

# Import Flask-Login decorators and current_user proxy
from flask_login import login_required, current_user

# Import application components
from app.config import Config
from app.services import transcription_service, file_service, user_service, pricing_service
from app.models import transcription as transcription_model
from app.models import transcription_utils
from app.models import transcription_catalog as transcription_catalog_model
from app.models import user as user_model
from app.models.user import User # For type hinting
from app.services.user_service import MissingApiKeyError
from app.services.api_clients.exceptions import TranscriptionApiError
from app.core.decorators import check_permission, check_usage_limits
from app.extensions import limiter, build_user_limit_key, csrf
from app.tasks.transcription_queue import submit_transcription_job
from mysql.connector import Error as MySQLError
# --- ADDED: Import Optional ---
from typing import Optional
# --- END ADDED ---


# Define the Blueprint
transcriptions_bp = Blueprint('transcriptions', __name__, url_prefix='/api')


def transcribe_rate_limit_key() -> str:
    """
    Rate limit key that scopes uploads per authenticated user and provider.
    This keeps limits aligned with billing/plan usage while still falling
    back to IP-based limits for anonymous users (should not happen here,
    but the fallback avoids crashes if a session expires mid-request).
    """
    provider = request.headers.get('X-Transcription-Provider')
    if not provider and request.is_json:
        data = request.get_json(silent=True) or {}
        provider = data.get('api_choice')
    if not provider:
        provider = request.args.get('api_choice')
    provider = (provider or current_app.config.get('DEFAULT_TRANSCRIPTION_PROVIDER', 'default')).strip().lower()
    return build_user_limit_key(f"transcribe:{provider}")


def _compose_error_message(base_message: str, details: Optional[str] = None) -> str:
    """Return a translated error message with optional diagnostic details."""
    details_text = str(details or "").strip()
    if details_text:
        return f"{base_message} {_('Details')}: {details_text}"
    return base_message


def public_transcribe_rate_limit_key() -> str:
    """
    Rate limit key for public API requests. Uses the hashed API token when present,
    otherwise falls back to the requestor IP.
    """
    auth_header = request.headers.get('Authorization', '')
    token = None
    if auth_header and auth_header.lower().startswith('bearer '):
        token = auth_header.split(' ', 1)[1].strip()
    if token:
        hashed = user_service.hash_public_api_key_for_rate_limit(token)
        if hashed:
            return build_user_limit_key(f"public-transcribe:{hashed}")
    return request.remote_addr or "public-api"


@transcriptions_bp.route('/v1/transcribe', methods=['POST'])
@csrf.exempt
@limiter.limit("10 per hour", key_func=public_transcribe_rate_limit_key)
def transcribe_audio_public():
    """
    Public API endpoint to upload an audio file and start a transcription using the
    authenticated user's default settings. Authentication is provided via a
    Bearer token (user-generated API key).
    """
    auth_header = request.headers.get('Authorization', '')
    if not auth_header or not auth_header.lower().startswith('bearer '):
        return jsonify({'error': _('A valid API key is required. Provide it in the Authorization header.')}), 401
    token = auth_header.split(' ', 1)[1].strip()
    if not token:
        return jsonify({'error': _('A valid API key is required.')}), 401

    user = user_service.authenticate_public_api_key(token)
    if not user:
        return jsonify({'error': _('Authentication failed. Please check your API key.')}), 401
    if not check_permission(user, 'allow_public_api_access'):
        return jsonify({'error': _('You do not have permission to use the public API.')}), 403

    user_id = user.id
    log_prefix = f"[API:PublicTranscribe:User:{user_id}]"
    logging.debug(f"{log_prefix} /api/v1/transcribe request received.")

    try:
        catalog_models = transcription_catalog_model.get_active_models()
    except Exception as catalog_err:
        logging.error(f"{log_prefix} Failed to load transcription models from catalog: {catalog_err}", exc_info=True)
        catalog_models = []
    model_lookup = {model['code']: model for model in catalog_models}
    active_model_codes = set(model_lookup.keys())
    default_model_code = next((model['code'] for model in catalog_models if model.get('is_default')), None)
    if not default_model_code and catalog_models:
        default_model_code = catalog_models[0]['code']
    if not default_model_code:
        default_model_code = current_app.config.get('DEFAULT_TRANSCRIPTION_PROVIDER')

    try:
        language_rows = transcription_catalog_model.get_active_languages()
    except Exception as lang_err:
        logging.error(f"{log_prefix} Failed to load transcription languages from catalog: {lang_err}", exc_info=True)
        language_rows = []
    active_language_codes = {lang['code'] for lang in language_rows}
    default_language_code = next((lang['code'] for lang in language_rows if lang.get('is_default')), None)
    if not default_language_code and language_rows:
        default_language_code = language_rows[0]['code']
    if not default_language_code:
        default_language_code = current_app.config.get('DEFAULT_LANGUAGE', 'auto')

    if 'audio_file' not in request.files:
        logging.error(f"{log_prefix} No 'audio_file' part in the request.")
        return jsonify({'error': _('We did not receive an audio file in your request.')}), 400
    file = request.files['audio_file']
    if file.filename == '':
        logging.error(f"{log_prefix} No file selected for upload.")
        return jsonify({'error': _('Please choose a file before starting the transcription.')}), 400
    if not file_service.allowed_file(file.filename):
        logging.error(f"{log_prefix} File type not allowed: {file.filename}")
        return jsonify({'error': _('This file type is not supported for transcription.')}), 400

    api_choice = user.default_transcription_model or default_model_code
    if api_choice not in active_model_codes:
        logging.warning(f"{log_prefix} User default model '{api_choice}' not available. Falling back to '{default_model_code}'.")
        api_choice = default_model_code
    if api_choice not in active_model_codes:
        logging.error(f"{log_prefix} No valid transcription provider available.")
        return jsonify({'error': _('No transcription provider is available for your account.')}), 400

    permission_key = model_lookup.get(api_choice, {}).get('permission_key')
    if permission_key and not check_permission(user, permission_key):
        logging.warning(f"{log_prefix} Permission check failed for provider '{api_choice}'.")
        return jsonify({'error': _('You do not have permission to use this transcription provider.')}), 403

    language_code = user.default_content_language or default_language_code
    if language_code not in active_language_codes:
        logging.info(f"{log_prefix} Default language '{language_code}' not available. Falling back to '{default_language_code}'.")
        language_code = default_language_code

    original_filename = secure_filename(file.filename)
    job_id = str(uuid.uuid4())
    short_job_id = job_id[:8]
    job_log_prefix = f"[JOB:{short_job_id}:User:{user_id}:Public]"

    upload_dir = current_app.config['TEMP_UPLOADS_DIR']
    temp_filename = os.path.join(upload_dir, f"{job_id}_{original_filename}")

    file_size_mb = 0.0
    audio_length_minutes = 0.0

    try:
        os.makedirs(upload_dir, exist_ok=True)
        if not file_service.validate_file_path(temp_filename, upload_dir):
            logging.error(f"{job_log_prefix} Invalid temporary file path generated: {temp_filename}")
            raise PermissionError("Invalid file path.")

        file.save(temp_filename)
        file_size_bytes = os.path.getsize(temp_filename)
        file_size_mb = round(file_size_bytes / (1024 * 1024), 2)
        logging.info(f"{job_log_prefix} Saved temp upload: {os.path.basename(temp_filename)} (Size: {file_size_mb:.2f} MB)")

        max_size_mb = current_app.config.get('MAX_FILE_SIZE_MB', 1024)
        if file_size_mb > max_size_mb:
            logging.warning(f"{job_log_prefix} File size {file_size_mb:.2f}MB exceeds limit {max_size_mb}MB.")
            file_service.remove_files([temp_filename])
            return jsonify({'error': _('The file exceeds the size limit of %(size)sMB.', size=max_size_mb), 'code': 'SIZE_LIMIT_EXCEEDED'}), 413

        try:
            audio_length_seconds, audio_length_minutes = file_service.get_audio_duration(temp_filename)
            if audio_length_seconds == 0.0:
                logging.warning(f"{job_log_prefix} Could not determine audio duration for '{os.path.basename(temp_filename)}'. Assuming 0 minutes.")
        except Exception as audio_err:
            logging.error(f"{job_log_prefix} Error getting audio duration for '{os.path.basename(temp_filename)}': {audio_err}", exc_info=True)
            audio_length_seconds = 0.0
            audio_length_minutes = 0.0

    except Exception as e:
        logging.exception(f"{job_log_prefix} Failed during file save or metadata extraction: {e}")
        if os.path.exists(temp_filename):
            file_service.remove_files([temp_filename])
        return jsonify({'error': _('We could not save or process the uploaded file. Please try again.')}), 500

    try:
        price = pricing_service.get_price(item_type='transcription', item_key=api_choice)
        cost_to_add = 0.0
        if price is not None:
            cost_to_add = price * (audio_length_minutes if audio_length_minutes >= 1 else audio_length_seconds / 60)

        allowed, reason = check_usage_limits(user, cost_to_add=cost_to_add, minutes_to_add=audio_length_minutes)
        if not allowed:
            logging.warning(f"{job_log_prefix} Usage limit check failed: {reason}")
            file_service.remove_files([temp_filename])
            return jsonify({'error': reason, 'code': 'USAGE_LIMIT_EXCEEDED'}), 403

        transcription_model.create_transcription_job(
            job_id=job_id,
            user_id=user_id,
            filename=original_filename,
            api_used=api_choice,
            file_size_mb=file_size_mb,
            audio_length_minutes=audio_length_minutes,
            context_prompt_used=False,
            pending_workflow_prompt_text=None,
            pending_workflow_prompt_title=None,
            pending_workflow_prompt_color=None,
            pending_workflow_origin_prompt_id=None,
            public_api_invocation=True
        )
        logging.info(f"{job_log_prefix} Created initial job record in database.")
    except MySQLError as db_create_err:
        logging.error(f"{job_log_prefix} Failed to create initial job record in DB: {db_create_err}", exc_info=True)
        file_service.remove_files([temp_filename])
        return jsonify({'error': _('We could not initialize the transcription job. Please try again.')}), 500
    except Exception as db_create_err:
        logging.error(f"{job_log_prefix} Unexpected error creating initial job record in DB: {db_create_err}", exc_info=True)
        file_service.remove_files([temp_filename])
        return jsonify({'error': _('We could not initialize the transcription job. Please try again.')}), 500

    try:
        app_instance = current_app._get_current_object()
        submit_transcription_job(
            current_app.config,
            transcription_service.process_transcription,
            app_instance,
            job_id,
            user_id,
            temp_filename,
            language_code,
            api_choice,
            original_filename,
            "",
            None,
            None,
            None,
            None,
            False,
        )
        logging.info(f"{job_log_prefix} Background transcription job queued.")

        return jsonify({
            'job_id': job_id,
            'message': _('Transcription job started successfully.'),
            'audio_length_minutes': audio_length_minutes
        }), 202
    except Exception as e:
        logging.exception(f"{job_log_prefix} Error initiating transcription job: {e}")
        file_service.remove_files([temp_filename])
        try:
            with current_app.app_context():
                transcription_model.set_job_error(job_id, f"Initialization failed: {str(e)}")
        except Exception as db_err:
            logging.error(f"{job_log_prefix} Failed to set error status after initialization failure: {db_err}")
        return jsonify({'error': _('We could not start the transcription job due to an internal error. Please try again.')}), 500



def public_transcribe_rate_limit_key() -> str:
    """
    Rate limit key for public API requests. Uses the hashed API token when present,
    otherwise falls back to the requestor IP.
    """
    auth_header = request.headers.get('Authorization', '')
    token = None
    if auth_header and auth_header.lower().startswith('bearer '):
        token = auth_header.split(' ', 1)[1].strip()
    if token:
        hashed = user_service.hash_public_api_key_for_rate_limit(token)
        if hashed:
            return build_user_limit_key(f"public-transcribe:{hashed}")
    return request.remote_addr or "public-api"

# --- Transcription Job Endpoints ---

@transcriptions_bp.route('/transcribe', methods=['POST'])
@login_required
@limiter.limit("10 per hour", key_func=transcribe_rate_limit_key)
def transcribe_audio():
    """
    API endpoint to upload an audio file and initiate a transcription job.
    Handles file validation (size, usage limits), saving, metadata extraction,
    creating the initial DB record, and starting the background task.
    Calculates duration in minutes.
    Accepts pending workflow information including the original prompt ID.
    """
    user: User = current_user
    user_id = user.id
    log_prefix = f"[API:Transcribe:User:{user_id}]"
    logging.debug(f"{log_prefix} /transcribe request received.")

    try:
        catalog_models = transcription_catalog_model.get_active_models()
    except Exception as catalog_err:
        logging.error(f"{log_prefix} Failed to load transcription models from catalog: {catalog_err}", exc_info=True)
        catalog_models = []
    model_lookup = {model['code']: model for model in catalog_models}
    active_model_codes = set(model_lookup.keys())
    default_model_code = next((model['code'] for model in catalog_models if model.get('is_default')), None)
    if not default_model_code and catalog_models:
        default_model_code = catalog_models[0]['code']
    if not default_model_code:
        default_model_code = current_app.config.get('DEFAULT_TRANSCRIPTION_PROVIDER')

    try:
        language_rows = transcription_catalog_model.get_active_languages()
    except Exception as lang_err:
        logging.error(f"{log_prefix} Failed to load transcription languages from catalog: {lang_err}", exc_info=True)
        language_rows = []
    active_language_codes = {lang['code'] for lang in language_rows}
    default_language_code = next((lang['code'] for lang in language_rows if lang.get('is_default')), None)
    if not default_language_code and language_rows:
        default_language_code = language_rows[0]['code']
    if not default_language_code:
        default_language_code = current_app.config.get('DEFAULT_LANGUAGE', 'auto')

    if 'audio_file' not in request.files:
        logging.error(f"{log_prefix} No 'audio_file' part in the request.")
        return jsonify({'error': _('We did not receive an audio file in your request.')}), 400
    file = request.files['audio_file']
    if file.filename == '':
        logging.error(f"{log_prefix} No file selected for upload.")
        return jsonify({'error': _('Please choose a file before starting the transcription.')}), 400
    if not file_service.allowed_file(file.filename):
        logging.error(f"{log_prefix} File type not allowed: {file.filename}")
        return jsonify({'error': _('This file type is not supported for transcription.')}), 400

    original_filename = secure_filename(file.filename)
    job_id = str(uuid.uuid4())
    short_job_id = job_id[:8]
    job_log_prefix = f"[JOB:{short_job_id}:User:{user_id}]"

    upload_dir = current_app.config['TEMP_UPLOADS_DIR']
    temp_filename = os.path.join(upload_dir, f"{job_id}_{original_filename}")

    file_size_mb = 0.0
    audio_length_minutes = 0.0

    try:
        os.makedirs(upload_dir, exist_ok=True)
        if not file_service.validate_file_path(temp_filename, upload_dir):
             logging.error(f"{job_log_prefix} Invalid temporary file path generated: {temp_filename}")
             raise PermissionError("Invalid file path.")

        file.save(temp_filename)
        file_size_bytes = os.path.getsize(temp_filename)
        file_size_mb = round(file_size_bytes / (1024 * 1024), 2)
        logging.info(f"{job_log_prefix} Saved temp upload: {os.path.basename(temp_filename)} (Size: {file_size_mb:.2f} MB)")

        max_size_mb = current_app.config.get('MAX_FILE_SIZE_MB', 1024)
        if file_size_mb > max_size_mb:
             logging.warning(f"{job_log_prefix} File size {file_size_mb:.2f}MB exceeds limit {max_size_mb}MB.")
             file_service.remove_files([temp_filename])
             return jsonify({'error': _('The file exceeds the size limit of %(size)sMB.', size=max_size_mb), 'code': 'SIZE_LIMIT_EXCEEDED'}), 413

        try:
            # Use the memory-efficient ffprobe method to get duration
            audio_length_seconds, audio_length_minutes = file_service.get_audio_duration(temp_filename)
            if audio_length_seconds == 0.0:
                logging.warning(f"{job_log_prefix} Could not determine audio duration for '{os.path.basename(temp_filename)}'. Assuming 0 minutes.")
        except Exception as audio_err:
            logging.error(f"{job_log_prefix} Error getting audio duration for '{os.path.basename(temp_filename)}': {audio_err}", exc_info=True)
            audio_length_seconds = 0.0
            audio_length_minutes = 0.0

    except Exception as e:
        logging.exception(f"{job_log_prefix} Failed during file save or metadata extraction: {e}")
        if os.path.exists(temp_filename):
            file_service.remove_files([temp_filename])
        return jsonify({'error': _('We could not save or process the uploaded file. Please try again.')}), 500

    try:
        language_code = request.form.get('language_code', default_language_code)
        if language_code not in active_language_codes:
            logging.warning(f"{job_log_prefix} Received unsupported language '{language_code}'. Falling back to '{default_language_code}'.")
            language_code = default_language_code

        api_choice = request.form.get('api_choice', default_model_code)
        context_prompt = request.form.get('context_prompt', '')
        pending_workflow_prompt_text = request.form.get('pending_workflow_prompt_text')
        pending_workflow_prompt_title = request.form.get('pending_workflow_prompt_title')
        pending_workflow_prompt_color = request.form.get('pending_workflow_prompt_color')
        pending_workflow_origin_prompt_id_str = request.form.get('pending_workflow_origin_prompt_id')
        parsed_pending_workflow_origin_id: Optional[int] = None
        if pending_workflow_origin_prompt_id_str:
            try:
                parsed_pending_workflow_origin_id = int(pending_workflow_origin_prompt_id_str)
            except (ValueError, TypeError): # Added TypeError
                logging.warning(f"{job_log_prefix} Invalid pending_workflow_origin_prompt_id received: '{pending_workflow_origin_prompt_id_str}'. Ignoring.")
        diarization_flag_raw = request.form.get('speaker_diarization', '')
        speaker_diarization_enabled = str(diarization_flag_raw).strip().lower() in ('1', 'true', 'yes', 'on')
        if api_choice != 'assemblyai':
            if speaker_diarization_enabled:
                logging.info(f"{job_log_prefix} Speaker diarization requested but API '{api_choice}' does not support it. Ignoring flag.")
            speaker_diarization_enabled = False
        elif speaker_diarization_enabled and not check_permission(user, 'allow_speaker_diarization'):
            logging.warning(f"{job_log_prefix} User lacks permission to enable speaker diarization. Blocking request.")
            return jsonify({'error': _('You do not have permission to identify speakers for this model.')}), 403
        
        logging.debug(f"{job_log_prefix} Params - API: {api_choice}, Lang: {language_code}, Context: {'Yes' if context_prompt else 'No'}, Pending WF Text: {'Set' if pending_workflow_prompt_text else 'Not Set'}, Pending WF Origin ID: {parsed_pending_workflow_origin_id}, Speaker Diarization: {speaker_diarization_enabled}")


        if api_choice not in active_model_codes:
            logging.error(f"{job_log_prefix} Invalid API choice '{api_choice}'. Allowed: {sorted(active_model_codes)}")
            raise ValueError(f"Invalid transcription provider selected: {api_choice}")

        price = pricing_service.get_price(item_type='transcription', item_key=api_choice)
        cost_to_add = 0.0
        if price is not None:
            cost_to_add = price * (audio_length_minutes if audio_length_minutes >= 1 else audio_length_seconds / 60)

        allowed, reason = check_usage_limits(user, cost_to_add=cost_to_add, minutes_to_add=audio_length_minutes)
        if not allowed:
            logging.warning(f"{job_log_prefix} Usage limit check failed: {reason}")
            file_service.remove_files([temp_filename])
            return jsonify({'error': reason, 'code': 'USAGE_LIMIT_EXCEEDED'}), 403
        logging.debug(f"{job_log_prefix} Usage limit check passed.")

        context_prompt_used_flag = False
        if context_prompt:
            if check_permission(user, 'allow_context_prompt'):
                context_prompt_used_flag = True
            else:
                logging.warning(f"{job_log_prefix} User provided context prompt but lacks permission. Prompt will be ignored.")
                context_prompt = ""

        try:
            # --- MODIFIED: Pass parsed_pending_workflow_origin_id to create_transcription_job ---
            transcription_model.create_transcription_job(
                job_id=job_id,
                user_id=user_id,
                filename=original_filename,
                api_used=api_choice,
                file_size_mb=file_size_mb,
                audio_length_minutes=audio_length_minutes,
                context_prompt_used=context_prompt_used_flag,
                pending_workflow_prompt_text=pending_workflow_prompt_text if pending_workflow_prompt_text else None,
                pending_workflow_prompt_title=pending_workflow_prompt_title if pending_workflow_prompt_title else None,
                pending_workflow_prompt_color=pending_workflow_prompt_color if pending_workflow_prompt_color else None,
                pending_workflow_origin_prompt_id=parsed_pending_workflow_origin_id # Pass the ID
            )
            # --- END MODIFIED ---
            logging.info(f"{job_log_prefix} Created initial job record in database (Context Used: {context_prompt_used_flag}).")
        except MySQLError as db_create_err:
            logging.error(f"{job_log_prefix} Failed to create initial job record in DB: {db_create_err}", exc_info=True)
            file_service.remove_files([temp_filename])
            return jsonify({'error': _('We could not initialize the transcription job. Please try again.')}), 500
        except Exception as db_create_err:
            logging.error(f"{job_log_prefix} Unexpected error creating initial job record in DB: {db_create_err}", exc_info=True)
            file_service.remove_files([temp_filename])
            return jsonify({'error': _('We could not initialize the transcription job. Please try again.')}), 500

        app_instance = current_app._get_current_object()

        submit_transcription_job(
            current_app.config,
            transcription_service.process_transcription,
            app_instance,
            job_id,
            user_id,
            temp_filename,
            language_code,
            api_choice,
            original_filename,
            context_prompt,
            pending_workflow_prompt_text,
            pending_workflow_prompt_title,
            pending_workflow_prompt_color,
            parsed_pending_workflow_origin_id,
            speaker_diarization_enabled,
        )
        logging.info(f"{job_log_prefix} Background transcription job queued.")

        return jsonify({
            'job_id': job_id,
            'message': _('Transcription job started successfully.'),
            'audio_length_minutes': audio_length_minutes
        }), 202

    except (PermissionError, MissingApiKeyError, ValueError) as e:
         logging.error(f"{job_log_prefix} Failed to initiate transcription due to pre-check failure: {e}")
         file_service.remove_files([temp_filename])
         try:
             with current_app.app_context():
                 transcription_model.set_job_error(job_id, f"Initialization failed: {str(e)}")
         except Exception as db_err:
             logging.error(f"{job_log_prefix} Failed to set error status after initialization failure: {db_err}")
         status_code = 403 if isinstance(e, (PermissionError, MissingApiKeyError)) else 400
         if isinstance(e, ValueError):
             base_message = _('We could not start the transcription because one of the inputs was invalid.')
         else:
             base_message = _('We could not start the transcription because of a configuration or permission issue.')
         return jsonify({'error': _compose_error_message(base_message, str(e))}), status_code
    except Exception as e:
        logging.exception(f"{job_log_prefix} Error initiating transcription job: {e}")
        file_service.remove_files([temp_filename])
        try:
             with current_app.app_context():
                 transcription_model.set_job_error(job_id, f"Initialization failed: {str(e)}")
        except Exception as db_err:
             logging.error(f"{job_log_prefix} Failed to set error status after initialization failure: {db_err}")
        return jsonify({'error': _('We could not start the transcription job due to an internal error. Please try again.')}), 500


@transcriptions_bp.route('/progress/<job_id>', methods=['GET'])
@login_required
@limiter.exempt
def get_progress(job_id):
    """
    API endpoint to poll for transcription job progress and results.
    Ensures the requesting user owns the job.
    NOTE: This endpoint now ONLY returns transcription status.
          LLM/Workflow status must be polled separately if needed.
    Includes a flag indicating if title polling should occur.
    Includes pending workflow details if the job is finished.
    """
    user_id = current_user.id
    short_job_id = job_id[:8] if job_id else 'invalid'
    log_prefix = f"[API:Progress:JOB:{short_job_id}:User:{user_id}]"

    try:
        job_data = transcription_model.get_transcription_by_id(job_id, user_id)

        if not job_data:
            unowned_job = transcription_model.get_transcription_by_id(job_id)
            if unowned_job:
                logging.warning(f"{log_prefix} Access denied: Job exists but is not owned by user.")
                return jsonify({'error': _('You do not have access to this transcription job.')}), 403
            else:
                logging.warning(f"{log_prefix} Job not found.")
                return jsonify({'error': _('We could not find that transcription job.')}), 404

        status = job_data.get('status', 'unknown')
        is_finished = status in ('finished', 'error', 'cancelled')
        is_error = status == 'error'
        is_cancelled = status == 'cancelled'

        progress_log = []
        raw_log = job_data.get('progress_log')
        if isinstance(raw_log, list):
            progress_log = raw_log
        elif raw_log:
            logging.warning(f"{log_prefix} Progress log from DB is not a list. Type: {type(raw_log)}. Content: {raw_log}")
            progress_log = [str(_('Error: Invalid progress log format.'))]

        should_poll_title = False
        if status == 'finished':
            user = user_model.get_user_by_id(user_id)
            if user and user.enable_auto_title_generation and user.has_permission('allow_auto_title_generation'):
                title_status = job_data.get('title_generation_status', 'pending')
                if title_status in ['pending', 'processing']:
                    should_poll_title = True

        response_data = {
            'job_id': job_id,
            'status': status,
            'progress': progress_log,
            'finished': is_finished,
            'error_message': job_data.get('error_message') if is_error else None,
            'result': None, # This will be populated below if finished successfully
            'file_size_mb': job_data.get('file_size_mb', 0.0),
            'audio_length_minutes': job_data.get('audio_length_minutes', 0.0),
            'api_used': job_data.get('api_used', 'unknown'),
            'filename': job_data.get('filename', 'unknown'),
            'should_poll_title': should_poll_title,
            '_llm_status_note': str(_('LLM/Workflow status must be polled separately.'))
        }

        if is_finished and not is_error and not is_cancelled:
            response_data['result'] = {
                'id': job_data['id'],
                'filename': job_data.get('filename'),
                'detected_language': job_data.get('detected_language'),
                'transcription_text': job_data.get('transcription_text'),
                'api_used': job_data.get('api_used'),
                'created_at': job_data.get('created_at'),
                'status': status,
                'audio_length_minutes': job_data.get('audio_length_minutes', 0.0),
                'generated_title': job_data.get('generated_title'),
                'title_generation_status': job_data.get('title_generation_status', 'pending'),
                'pending_workflow_prompt_text': job_data.get('pending_workflow_prompt_text'),
                'pending_workflow_prompt_title': job_data.get('pending_workflow_prompt_title'),
                'pending_workflow_prompt_color': job_data.get('pending_workflow_prompt_color'),
                # --- MODIFIED: Include pending_workflow_origin_prompt_id in response ---
                'pending_workflow_origin_prompt_id': job_data.get('pending_workflow_origin_prompt_id')
                # --- END MODIFIED ---
            }
            logging.debug(f"{log_prefix} Job finished successfully, returning result. Should poll title: {should_poll_title}")
        elif is_error:
            logging.debug(f"{log_prefix} Job finished with error.")
        elif is_cancelled:
            logging.debug(f"{log_prefix} Job was cancelled.")

        return jsonify(response_data), 200

    except Exception as e:
        logging.exception(f"{log_prefix} Unexpected error fetching progress:")
        return jsonify({'error': _('We encountered an internal error while fetching job progress. Please try again.')}), 500

@transcriptions_bp.route('/transcribe/<job_id>', methods=['DELETE'])
@login_required
def cancel_transcription(job_id):
    """
    API endpoint to request cancellation of an ongoing transcription job.
    Updates the job status to 'cancelling' to signal the background thread.
    """
    user_id = current_user.id
    short_job_id = job_id[:8] if job_id else 'invalid'
    log_prefix = f"[API:Cancel:JOB:{short_job_id}:User:{user_id}]"
    logging.debug(f"{log_prefix} Cancellation request received.")

    try:
        job_data = transcription_model.get_transcription_by_id(job_id, user_id)

        if not job_data:
            unowned_job = transcription_model.get_transcription_by_id(job_id)
            if unowned_job:
                logging.warning(f"{log_prefix} Access denied: Job exists but is not owned by user.")
                return jsonify({'error': _('You do not have access to this transcription job.')}), 403
            else:
                logging.warning(f"{log_prefix} Job not found.")
                return jsonify({'error': _('We could not find that transcription job.')}), 404

        current_status = job_data.get('status')
        if current_status not in ['pending', 'processing']:
            logging.warning(f"{log_prefix} Cannot cancel job with status '{current_status}'.")
            return jsonify({'error': _('This transcription cannot be cancelled because it is in %(status)s status.', status=current_status)}), 400

        transcription_model.update_job_status(job_id, 'cancelling')
        transcription_model.update_job_progress(job_id, str(_('Cancellation requested by user.')))

        logging.info(f"{log_prefix} Job status updated to 'cancelling'. Background thread will terminate.")
        return jsonify({'message': _('Transcription cancellation requested.')}), 200

    except Exception as e:
        logging.exception(f"{log_prefix} Unexpected error requesting cancellation:")
        return jsonify({'error': _('We encountered an internal error while requesting cancellation. Please try again.')}), 500

@transcriptions_bp.route('/transcriptions', methods=['GET'])
@login_required
def get_transcriptions():
    """
    API endpoint to get the list of the logged-in user's transcription history.
    Respects history limits defined by the user's role.
    NOTE: Returns only transcription data. Associated LLM data must be fetched separately if needed.
    """
    user: User = current_user
    user_id = user.id
    log_prefix = f"[API:History:User:{user_id}]"
    logging.debug(f"{log_prefix} /transcriptions GET request received.")

    try:
        limit = user.get_limit('max_history_items') if user.role else 0
        logging.debug(f"{log_prefix} Applying history limit: {'Unlimited' if limit <= 0 else limit}")

        transcriptions = transcription_model.get_all_transcriptions(user_id, limit=limit)

        logging.info(f"{log_prefix} Retrieved {len(transcriptions)} transcription records.")
        return jsonify(transcriptions), 200
    except Exception as e:
        logging.exception(f"{log_prefix} Error fetching transcription history:")
        return jsonify({'error': _('We could not retrieve your transcription history. Please try again.')}), 500

@transcriptions_bp.route('/transcriptions/search', methods=['GET'])
@login_required
def search_transcriptions():
    """Search user's transcription history. Returns paginated JSON results."""
    user_id = current_user.id
    q = request.args.get('q', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 20
    log_prefix = f"[API:Search:User:{user_id}]"

    try:
        total = transcription_utils.count_visible_user_transcriptions(
            user_id, search_query=q or None
        )
        items = []
        total_pages = 0
        if total > 0:
            total_pages = math.ceil(total / per_page)
            page = max(1, min(page, total_pages))
            items = transcription_utils.get_paginated_transcriptions(
                user_id, page, per_page, search_query=q or None
            )
        logging.debug("%s Search q=%r returned %s/%s results (page %s).", log_prefix, q, len(items), total, page)
        return jsonify({
            'items': items,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'query': q,
        }), 200
    except Exception:
        logging.exception("%s Error searching transcriptions:", log_prefix)
        return jsonify({'error': _('Search failed. Please try again.')}), 500


@transcriptions_bp.route('/transcriptions/<transcription_id>', methods=['DELETE'])
@login_required
def delete_transcription(transcription_id):
    """
    API endpoint to delete a specific transcription record owned by the user.
    The service layer handles deletion of associated workflow results (LLM operations).
    """
    user_id = current_user.id
    short_job_id = transcription_id[:8] if transcription_id else 'invalid'
    log_prefix = f"[API:Delete:JOB:{short_job_id}:User:{user_id}]"
    logging.debug(f"{log_prefix} /transcriptions DELETE request received.")

    try:
        from app.services import workflow_service
        try:
            workflow_service.delete_workflow_result(user_id, transcription_id)
            logging.info(f"{log_prefix} Associated workflow LLM operation(s) cleared (if existed).")
        except workflow_service.TranscriptionNotFoundError:
            pass
        except Exception as wf_del_err:
            logging.error(f"{log_prefix} Error clearing workflow result during transcription delete: {wf_del_err}", exc_info=True)

        success = transcription_model.delete_transcription(transcription_id, user_id)
        if success:
            logging.info(f"{log_prefix} Transcription soft-deleted successfully.")
            return jsonify({'message': _('Transcription deleted successfully.')}), 200
        else:
            exists_check = transcription_model.get_transcription_by_id(transcription_id)
            if exists_check:
                logging.warning(f"{log_prefix} Delete failed due to ownership mismatch.")
                return jsonify({'error': _('You do not have permission to delete this transcription.')}), 403
            else:
                logging.warning(f"{log_prefix} Delete failed: Transcription not found.")
                return jsonify({'error': _('We could not find that transcription.')}), 404
    except Exception as e:
        logging.exception(f"{log_prefix} Error deleting transcription:")
        return jsonify({'error': _('We could not delete the transcription because of an internal error. Please try again.')}), 500

@transcriptions_bp.route('/transcriptions/<transcription_id>/restore', methods=['POST'])
@login_required
def restore_transcription(transcription_id):
    """
    API endpoint to restore a previously deleted transcription owned by the user.
    """
    user_id = current_user.id
    short_job_id = transcription_id[:8] if transcription_id else 'invalid'
    log_prefix = f"[API:Restore:JOB:{short_job_id}:User:{user_id}]"
    logging.debug(f"{log_prefix} /transcriptions/restore request received.")

    try:
        restored = transcription_model.restore_transcription(transcription_id, user_id)
        if restored:
            logging.info(f"{log_prefix} Transcription restored successfully.")
            return jsonify({'message': _('Transcription restored.')}), 200

        existing_job = transcription_model.get_transcription_by_id(transcription_id, user_id)
        if existing_job and not existing_job.get('is_hidden_from_user'):
            logging.info(f"{log_prefix} Restore skipped: transcription already visible.")
            return jsonify({'message': _('Transcription already active.')}), 200

        if existing_job is None:
            logging.warning(f"{log_prefix} Restore failed: transcription not found or not owned.")
            return jsonify({'error': _('We could not find that transcription.')}), 404

        logging.warning(f"{log_prefix} Restore failed: transcription not eligible for restoration.")
        return jsonify({'error': _('This transcription cannot be restored.')}), 409
    except Exception as e:
        logging.exception(f"{log_prefix} Error restoring transcription:")
        return jsonify({'error': _('We could not restore the transcription because of an internal error. Please try again.')}), 500

@transcriptions_bp.route('/transcriptions/clear', methods=['DELETE'])
@login_required
def clear_transcriptions():
    """
    API endpoint to delete all transcription records for the logged-in user.
    The service layer handles deletion of associated workflow results (LLM operations).
    """
    user_id = current_user.id
    log_prefix = f"[API:Clear:User:{user_id}]"
    logging.warning(f"{log_prefix} /transcriptions/clear DELETE request received.")

    try:
        from app.services import workflow_service
        # Bulk-delete all workflow LLM operations for this user in 2 SQL statements
        # instead of fetching every transcription row (with MEDIUMTEXT) and looping.
        try:
            cleared_workflows = workflow_service.delete_all_workflow_results_for_user(user_id)
            logging.info(f"{log_prefix} Bulk-cleared {cleared_workflows} workflow LLM operation(s).")
        except Exception as wf_clear_err:
            logging.error(f"{log_prefix} Error bulk-clearing workflow ops during clear all: {wf_clear_err}")

        deleted_count = transcription_model.clear_transcriptions(user_id)
        logging.info(f"{log_prefix} {deleted_count} transcriptions soft-deleted successfully.")
        return jsonify({'message': _('All %(count)s transcriptions were cleared successfully.', count=deleted_count)}), 200
    except Exception as e:
        logging.exception(f"{log_prefix} Error clearing all transcriptions:")
        return jsonify({'error': _('We could not clear your transcriptions because of an internal error. Please try again.')}), 500

@transcriptions_bp.route('/transcriptions/<transcription_id>/log_download', methods=['POST'])
@login_required
def log_download(transcription_id):
    """
    API endpoint to mark a transcription as downloaded.
    """
    user_id = current_user.id
    short_job_id = transcription_id[:8] if transcription_id else 'invalid'
    log_prefix = f"[API:LogDownload:JOB:{short_job_id}:User:{user_id}]"
    logging.debug(f"{log_prefix} Request received to log download.")

    if not check_permission(current_user, 'allow_download_transcript'):
        logging.warning(f"{log_prefix} Download log failed: User lacks 'allow_download_transcript' permission.")
        return jsonify({'error': _('You do not have permission to download transcripts.')}), 403

    try:
        success = transcription_model.mark_transcription_as_downloaded(transcription_id, user_id)
        if success:
            logging.info(f"{log_prefix} Download logged successfully.")
            return jsonify({'message': _('Download logged successfully.')}), 200
        else:
            job_data = transcription_model.get_transcription_by_id(transcription_id)
            if not job_data:
                logging.warning(f"{log_prefix} Download log failed: Job not found.")
                return jsonify({'error': _('We could not find that transcription.')}), 404
            elif job_data.get('user_id') != user_id:
                logging.warning(f"{log_prefix} Download log failed: Ownership mismatch.")
                return jsonify({'error': _('You do not have permission to access this transcription.')}), 403
            elif job_data.get('status') != 'finished':
                logging.warning(f"{log_prefix} Download log failed: Job status is '{job_data.get('status')}'.")
                return jsonify({'error': _('You can only download completed transcriptions.')}), 400
            else:
                logging.warning(f"{log_prefix} Download log failed for unknown reason (model returned False).")
                return jsonify({'error': _('We could not log this download. Please try again.')}), 500
    except Exception as e:
        logging.exception(f"{log_prefix} Error logging download:")
        return jsonify({'error': _('We could not log the download because of an internal error. Please try again.')}), 500

@transcriptions_bp.route('/transcriptions/<transcription_id>/toggle_pin', methods=['POST'])
@login_required
def toggle_pin(transcription_id):
    """API endpoint to toggle the is_pinned flag for a transcription."""
    user_id = current_user.id
    short_job_id = transcription_id[:8] if transcription_id else 'invalid'
    log_prefix = f"[API:TogglePin:JOB:{short_job_id}:User:{user_id}]"
    logging.debug(f"{log_prefix} Request received.")

    try:
        success, new_pinned = transcription_model.toggle_transcription_pin(transcription_id, user_id)
        if success:
            logging.info(f"{log_prefix} Pin toggled. New state: {new_pinned}.")
            return jsonify({'is_pinned': new_pinned}), 200
        job_data = transcription_model.get_transcription_by_id(transcription_id)
        if not job_data:
            logging.warning(f"{log_prefix} Toggle pin failed: job not found.")
            return jsonify({'error': _('We could not find that transcription.')}), 404
        elif job_data.get('user_id') != user_id:
            logging.warning(f"{log_prefix} Toggle pin failed: ownership mismatch.")
            return jsonify({'error': _('You do not have permission to modify this transcription.')}), 403
        else:
            logging.warning(f"{log_prefix} Toggle pin failed for unknown reason.")
            return jsonify({'error': _('Could not toggle pin. Please try again.')}), 500
    except Exception as e:
        logging.exception(f"{log_prefix} Error toggling pin:")
        return jsonify({'error': _('An internal error occurred. Please try again.')}), 500


@transcriptions_bp.route('/transcriptions/<transcription_id>/title', methods=['GET'])
@login_required
def get_title_status(transcription_id):
    """
    API endpoint to get the status and generated title for a transcription.
    Used by the frontend to poll for title updates.
    """
    user_id = current_user.id
    short_job_id = transcription_id[:8] if transcription_id else 'invalid'
    log_prefix = f"[API:TitleStatus:JOB:{short_job_id}:User:{user_id}]"

    try:
        job_data = transcription_model.get_transcription_by_id(transcription_id, user_id)

        if not job_data:
            unowned_job = transcription_model.get_transcription_by_id(transcription_id)
            if unowned_job:
                logging.warning(f"{log_prefix} Access denied: Job exists but is not owned by user.")
                return jsonify({'error': _('You do not have access to this transcription job.')}), 403
            else:
                logging.warning(f"{log_prefix} Job not found.")
                return jsonify({'error': _('We could not find that transcription job.')}), 404

        title_status = job_data.get('title_generation_status', 'pending')
        generated_title = job_data.get('generated_title')
        filename = job_data.get('filename', 'Unknown Filename')

        response_data = {}
        if title_status == 'success' and generated_title:
            response_data = {'title': generated_title, 'status': 'generated'}
        elif title_status == 'failed':
            response_data = {'title': filename, 'status': 'failed'}
        elif title_status == 'processing':
            response_data = {'title': filename, 'status': 'processing'}
        elif title_status == 'pending':
            response_data = {'title': filename, 'status': 'pending'}
        # --- MODIFIED: Add case for 'disabled' status ---
        elif title_status == 'disabled':
            response_data = {'title': filename, 'status': 'disabled'}
        # --- END MODIFIED ---
        else:
            logging.error(f"{log_prefix} Unknown title generation status found: {title_status}")
            response_data = {'title': filename, 'status': 'unknown'}

        return jsonify(response_data), 200

    except Exception as e:
        logging.exception(f"{log_prefix} Unexpected error fetching title status:")
        return jsonify({'error': _('We encountered an internal error while fetching the title status. Please try again.')}), 500

@transcriptions_bp.route('/transcriptions/<transcription_id>/workflow-details', methods=['GET'])
@login_required
def get_workflow_details_for_transcription(transcription_id: str):
    """
    API endpoint to get the LLM operation details linked to a transcription.
    Used by the frontend to initiate workflow polling for pre-applied workflows.
    """
    user_id = current_user.id
    short_job_id = transcription_id[:8] if transcription_id else 'invalid'
    log_prefix = f"[API:WFDetails:JOB:{short_job_id}:User:{user_id}]"
    logging.debug(f"{log_prefix} Request received for workflow details.")

    try:
        job_data = transcription_model.get_transcription_by_id(transcription_id, user_id)

        if not job_data:
            unowned_job = transcription_model.get_transcription_by_id(transcription_id)
            if unowned_job:
                logging.warning(f"{log_prefix} Access denied: Job exists but is not owned by user.")
                return jsonify({'error': _('You do not have access to this transcription job.')}), 403
            else:
                logging.warning(f"{log_prefix} Job not found.")
                return jsonify({'error': _('We could not find that transcription job.')}), 404

        response_data = {
            'transcription_id': transcription_id,
            'llm_operation_id': job_data.get('llm_operation_id'),
            'llm_operation_status': job_data.get('llm_operation_status'),
            'llm_operation_result': job_data.get('llm_operation_result'),
            'llm_operation_error': job_data.get('llm_operation_error'),
            'llm_operation_ran_at': job_data.get('llm_operation_ran_at'),
            'pending_workflow_prompt_text': job_data.get('pending_workflow_prompt_text'),
            'pending_workflow_prompt_title': job_data.get('pending_workflow_prompt_title'),
            'pending_workflow_prompt_color': job_data.get('pending_workflow_prompt_color'),
            # --- MODIFIED: Include pending_workflow_origin_prompt_id in response ---
            'pending_workflow_origin_prompt_id': job_data.get('pending_workflow_origin_prompt_id')
            # --- END MODIFIED ---
        }
        logging.debug(f"{log_prefix} Returning workflow details: OpID {response_data['llm_operation_id']}, Status {response_data['llm_operation_status']}")
        return jsonify(response_data), 200

    except Exception as e:
        logging.exception(f"{log_prefix} Unexpected error fetching workflow details:")
        return jsonify({'error': _('We encountered an internal error while fetching workflow details. Please try again.')}), 500
