# app/tasks/title_generation.py
# Defines the background task for generating transcription titles using an LLM.

import logging
import threading
import time
from flask import Flask, current_app # Added current_app
from typing import Optional

# Import models
from app.models import user as user_model
from app.models import transcription as transcription_model
from app.models import llm_operation as llm_operation_model # Keep for potential future logging
from app.models import llm_catalog as llm_catalog_model
from app.models.user import User # For type hinting

# Import services
from app.services import llm_service
# from app.services.user_service import get_decrypted_api_key # No longer needed here

# Import exceptions
from app.services.api_clients.exceptions import (
    LlmApiError, LlmConfigurationError, LlmGenerationError, LlmSafetyError, LlmRateLimitError
)
from app.services.user_service import MissingApiKeyError

# Import permission checking and rate limiter
from app.core.decorators import check_permission
from app.extensions import limiter
from limits import parse # Import the parse function from the limits library

# --- Constants ---
TITLE_GENERATION_RATE_LIMIT = "10 per minute" # Example rate limit
TITLE_GENERATION_TIMEOUT_SECONDS = 30 # Timeout for the LLM call

# Utils
from app.utils.title_utils import (
    TITLE_MAX_WORDS,
    clean_generated_title,
    enforce_word_limit,
)

# --- Helper Function for LLM Call ---
# --- MODIFIED: Add provider/model parameters and rename internally ---
def _call_gemini_for_title(app: Flask, user_id: int, prompt: str, operation_id: int, operation_type: str, provider: str, model_name: Optional[str]) -> str:
# --- END MODIFIED ---
    """
    Calls the LLM service to generate text using Gemini.
    Handles potential errors from the LLM service.
    Requires app context to be pushed by the caller if run in a separate thread.
    """
    try:
        effective_provider = provider or app.config.get('TITLE_GENERATION_LLM_PROVIDER', app.config.get('LLM_PROVIDER', 'GEMINI'))
        provider_name = effective_provider.lower()
        effective_model = model_name or app.config.get('TITLE_GENERATION_LLM_MODEL') or app.config.get('LLM_MODEL')
        # --- MODIFIED: Pass user_id to llm_service, remove api_key and config ---
        result = llm_service.generate_text_via_llm(
            provider_name=provider_name,
            model=effective_model,
            user_id=user_id, # Pass user_id
            prompt=prompt,
            max_tokens=20, # Limit output tokens for a title
            disable_thinking=True,
            operation_id=operation_id,
            operation_type=operation_type,
        )
        # --- END MODIFIED ---
        return result.strip() if result else ""
    except (LlmConfigurationError, LlmApiError, LlmGenerationError, LlmSafetyError, LlmRateLimitError, ValueError) as e:
        raise e
    except Exception as e:
        raise LlmGenerationError(f"Unexpected error calling LLM for title: {e}") from e


# --- Background Task ---
def generate_title_task(app: Flask, transcription_id: str, user_id: int) -> None:
    """
    Background task to generate a title for a completed transcription.

    Args:
        app: The Flask application instance.
        transcription_id: The ID of the transcription record.
        user_id: The ID of the user who owns the transcription.
    """
    logger = logging.getLogger(__name__)
    log_extra = {"transcription_id": transcription_id, "user_id": user_id}
    log_prefix = f"[TASK:TitleGen:TID:{transcription_id[:8]}:UID:{user_id}]"

    logger.debug(f"{log_prefix} Starting title generation task.", extra=log_extra)

    with app.app_context():
        if not transcription_model.update_title_generation_status(transcription_id, 'processing'):
            logger.error(f"{log_prefix} Failed to update initial status to 'processing'. Aborting.", extra=log_extra)
            return

    generated_title: Optional[str] = None
    final_status: str = 'failed'
    start_time = time.time()
    duration = 0.0
    error_reason = "unknown_error"
    error_message = ""

    try:
        with app.app_context():
            user: Optional[User] = user_model.get_user_by_id(user_id)
            if not user:
                error_reason = "user_not_found"
                logger.error(f"{log_prefix} User not found. Aborting.", extra=log_extra)
                transcription_model.update_title_generation_status(transcription_id, 'failed')
                return

            if not user.enable_auto_title_generation:
                logger.debug(f"{log_prefix} Skipping title generation as user preference is disabled.", extra=log_extra)
                transcription_model.update_title_generation_status(transcription_id, 'disabled') # MODIFIED: Mark as disabled if skipped
                error_reason = "user_preference_disabled"
                final_status = 'disabled' # MODIFIED: Set final_status for logging
                return

            if not check_permission(user, 'allow_auto_title_generation'):
                error_reason = "permission_denied"
                logger.warning(f"{log_prefix} Permission denied for title generation.", extra=log_extra)
                transcription_model.update_title_generation_status(transcription_id, 'failed')
                return

            transcription = transcription_model.get_transcription_by_id(transcription_id, user_id)
            if not transcription or not transcription.get('transcription_text'):
                error_reason = "transcription_missing_or_empty"
                logger.error(f"{log_prefix} Transcription record or text not found/empty.", extra=log_extra)
                transcription_model.update_title_generation_status(transcription_id, 'failed')
                return

            transcript_text = transcription['transcription_text']
            max_prompt_chars = 10000
            if len(transcript_text) > max_prompt_chars:
                 transcript_text = transcript_text[:max_prompt_chars] + "..."
                 logger.debug(f"{log_prefix} Truncated transcript text for title generation prompt.", extra=log_extra)

            role_obj = user.role
            role_provider_override: Optional[str] = None
            role_model_override: Optional[str] = None
            if role_obj:
                role_model_candidate = getattr(role_obj, 'default_title_generation_model', None)
                if role_model_candidate:
                    candidate_clean = role_model_candidate.strip()
                    if candidate_clean:
                        resolved_provider = llm_service.get_provider_for_model_code(candidate_clean)
                        if resolved_provider:
                            role_model_override = candidate_clean
                            role_provider_override = resolved_provider
                        else:
                            logger.warning(f"{log_prefix} Role default title model '{candidate_clean}' is not recognized. Falling back to configured provider.", extra=log_extra)

            catalog_default_model: Optional[str] = None
            try:
                catalog_default_model = llm_catalog_model.get_default_title_generation_model_code()
            except Exception as catalog_err:
                logger.warning(f"{log_prefix} Failed to resolve default title generation model from catalog: {catalog_err}", exc_info=True, extra=log_extra)

            fallback_model = current_app.config.get('TITLE_GENERATION_LLM_MODEL') or current_app.config.get('LLM_MODEL')
            model_name = role_model_override or catalog_default_model or fallback_model

            provider_config = role_provider_override
            if not provider_config and model_name:
                provider_config = llm_service.get_provider_for_model_code(model_name)
            if not provider_config:
                provider_config = current_app.config.get('TITLE_GENERATION_LLM_PROVIDER', current_app.config.get('LLM_PROVIDER', 'GEMINI'))
            if role_provider_override:
                logger.debug(f"{log_prefix} Using role-level title generation model override '{model_name}' with provider '{provider_config}'.", extra=log_extra)
            else:
                logger.debug(f"{log_prefix} Using configured title generation model '{model_name}' with provider '{provider_config}'.", extra=log_extra)

            provider_suffix = (provider_config or 'default').lower()
            try:
                rate_limit_item = parse(TITLE_GENERATION_RATE_LIMIT)
                limit_key = f"title_gen:user:{user_id}:provider:{provider_suffix}"
                allowed = limiter.limiter.test(rate_limit_item, limit_key)
                if not allowed:
                    error_reason = "rate_limit_exceeded"
                    logger.warning(f"{log_prefix} Rate limit check failed (would exceed limit).", extra=log_extra)
                    transcription_model.update_title_generation_status(transcription_id, 'failed')
                    return
                limiter.limiter.hit(rate_limit_item, limit_key)
                logger.debug(f"{log_prefix} Rate limit check passed and hit.", extra=log_extra)
            except Exception as rl_err:
                error_reason = "rate_limit_error"
                logger.error(f"{log_prefix} Error checking/hitting rate limit: {rl_err}", exc_info=True, extra=log_extra)
                transcription_model.update_title_generation_status(transcription_id, 'failed')
                return

            # --- REMOVED: Direct API key fetching from config ---
            # gemini_api_key = app.config.get('GEMINI_API_KEY')
            # if not gemini_api_key:
            #     raise LlmConfigurationError("Gemini API key not configured in application settings.")
            # --- END REMOVED ---

            prompt = f"""
You are a specialized title generation system for audio transcriptions.
Your task is to analyze transcription content and create concise, relevant titles.

Guidelines:
- Titles must be 5 words or fewer
- Match the exact language of the transcription (e.g., English, Spanish, etc.)
- Capture the main topic or theme
- Maintain the style/tone of the original content
- Avoid generic titles like "Meeting Discussion" or "Audio Recording"
- Do not include metadata or file information in the title

Only respond with the title in the language of the transcription. No explanations or additional text.

Transcription Content:
---
{transcript_text}
---

Generated Title:"""

            operation_id = llm_operation_model.create_llm_operation(
                user_id=user_id,
                provider=provider_config,
                operation_type='title_generation',
                input_text=prompt,
                transcription_id=transcription_id,
                status='processing'
            )
            if not operation_id:
                error_reason = "db_create_failed"
                logger.error(f"{log_prefix} Failed to create LLM Operation record for title generation.", extra=log_extra)
                final_status = 'failed'
                transcription_model.update_title_generation_status(transcription_id, 'failed')
                return

            result_container = {}
            exception_container = {}

            def llm_call_wrapper(flask_app: Flask, current_user_id: int, op_id: int, op_type: str, provider_override: str, model_override: Optional[str]):
                with flask_app.app_context():
                    try:
                        result_container['title'] = _call_gemini_for_title(flask_app, current_user_id, prompt, op_id, op_type, provider_override, model_override)
                    except Exception as e:
                        exception_container['error'] = e

            llm_thread = threading.Thread(target=llm_call_wrapper, args=(app, user_id, operation_id, 'title_generation', provider_config, model_name))
            # --- END MODIFIED ---
            llm_thread.start()
            llm_thread.join(timeout=TITLE_GENERATION_TIMEOUT_SECONDS)

            if llm_thread.is_alive():
                error_reason = "timeout"
                logger.error(f"{log_prefix} Title generation timed out after {TITLE_GENERATION_TIMEOUT_SECONDS} seconds.", extra=log_extra)
            elif 'error' in exception_container:
                llm_operation_model.update_llm_operation_status(operation_id, 'error', error=str(exception_container['error']))
                raise exception_container['error']
            elif 'title' in result_container:
                generated_title = result_container['title']
                logger.debug(f"{log_prefix} Received title from LLM: '{generated_title}'", extra=log_extra)
            else:
                error_reason = "unknown_llm_issue"
                logger.error(f"{log_prefix} LLM thread finished but no result or exception captured.", extra=log_extra)

            if generated_title is not None:
                duration = time.time() - start_time
                cleaned_title = clean_generated_title(generated_title)
                if cleaned_title:
                    final_title = enforce_word_limit(cleaned_title)
                    if final_title != cleaned_title:
                        logger.info(
                            f"{log_prefix} Adjusted generated title to comply with word limit.",
                            extra={**log_extra, "duration_ms": int(duration * 1000), "original_title": cleaned_title, "adjusted_title": final_title}
                        )
                else:
                    final_title = cleaned_title

                final_word_count = len(final_title.split())

                if final_title and final_word_count <= TITLE_MAX_WORDS:
                    if transcription_model.set_generated_title(transcription_id, final_title):
                        final_status = 'success'
                        llm_operation_model.update_llm_operation_status(operation_id, 'finished', result=final_title)
                        logger.info(f"{log_prefix} Title generation successful. Title: '{final_title}'", extra={**log_extra, "duration_ms": int(duration * 1000), "success": True})
                    else:
                        error_reason = "db_update_failed"
                        logger.error(f"{log_prefix} Generated title was valid, but failed to save to DB.", extra={**log_extra, "duration_ms": int(duration * 1000), "success": False, "reason": error_reason})
                        final_status = 'failed'
                else:
                    error_reason = "invalid_format"
                    logger.warning(
                        f"{log_prefix} Generated title failed validation (empty or exceeds word limit).",
                        extra={
                            **log_extra,
                            "duration_ms": int(duration * 1000),
                            "success": False,
                            "reason": error_reason,
                            "raw_title": generated_title,
                            "cleaned_title": cleaned_title,
                            "final_title": final_title
                        }
                    )
                    final_status = 'failed'
                    llm_operation_model.update_llm_operation_status(operation_id, 'error', error="Generated title failed validation")
                    transcription_model.update_title_generation_status(transcription_id, 'failed')

    except (LlmRateLimitError, LlmSafetyError, LlmConfigurationError, LlmGenerationError, LlmApiError) as llm_err:
        duration = time.time() - start_time
        if isinstance(llm_err, LlmRateLimitError): error_reason = "llm_rate_limit"
        elif isinstance(llm_err, LlmSafetyError): error_reason = "llm_safety"
        elif isinstance(llm_err, LlmConfigurationError): error_reason = "llm_config"
        else: error_reason = "llm_api_error"
        error_message = str(llm_err)
        logger.error(f"{log_prefix} Title generation failed due to LLM error ({error_reason}): {error_message}", extra={**log_extra, "duration_ms": int(duration * 1000), "success": False, "reason": error_reason, "error_message": error_message})
        final_status = 'failed'
        with app.app_context():
            transcription_model.update_title_generation_status(transcription_id, 'failed')
    except Exception as e:
        duration = time.time() - start_time
        error_reason = "unexpected_error"
        error_message = str(e)
        logger.error(f"{log_prefix} Unexpected error during title generation: {error_message}", exc_info=True, extra={**log_extra, "duration_ms": int(duration * 1000), "success": False, "reason": error_reason, "error_message": error_message})
        final_status = 'failed'
        try:
            with app.app_context():
                transcription_model.update_title_generation_status(transcription_id, 'failed')
        except Exception as db_err:
             logging.error(f"{log_prefix} CRITICAL: Failed to update status to 'failed' after unexpected error: {db_err}")

    logger.debug(f"{log_prefix} Title generation task finished with status: {final_status}", extra=log_extra)
