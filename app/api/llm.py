# app/api/llm.py
# Defines the Blueprint for direct LLM interaction endpoints.

import logging
from typing import Optional
from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user
from flask_babel import gettext as _

# Import services and exceptions
from app.services import llm_service
from app.services.llm_service import LlmServiceError
from app.services.api_clients.exceptions import LlmApiError, LlmConfigurationError, LlmGenerationError, LlmSafetyError, LlmRateLimitError
from app.models import role as role_model
# --- ADDED: Import llm_operation model ---
from app.models import llm_operation as llm_operation_model
# --- END ADDED ---

# Import decorators
from app.core.decorators import permission_required
from app.extensions import limiter, build_user_limit_key

# Define the Blueprint
llm_bp = Blueprint('llm', __name__, url_prefix='/api/llm')


def _compose_error_message(base_message: str, details: Optional[str] = None) -> str:
    """Return a translated error message with optional diagnostic details."""
    details_text = str(details or "").strip()
    if details_text:
        return f"{base_message} {_('Details')}: {details_text}"
    return base_message

# --- Direct LLM Interaction Endpoints (Example) ---

@llm_bp.route('/generate', methods=['POST'])
@login_required
@permission_required('allow_workflows')
@limiter.limit(
    lambda: current_app.config.get('DIRECT_LLM_RATE_LIMIT', '5 per hour'),
    key_func=lambda: build_user_limit_key('llm-generate'),
)
def generate_llm_text():
    """
    API endpoint for direct text generation using a configured LLM.
    Expects JSON: {"prompt": "...", "provider": "optional_provider_name", ...}
    """
    user_id = current_user.id
    log_prefix = f"[API:LLM:Generate:User:{user_id}]"
    data = request.get_json()

    if not data or 'prompt' not in data:
        logging.warning(f"{log_prefix} Invalid request: Missing 'prompt' in JSON payload.")
        return jsonify({'error': _('Please include a prompt before requesting AI text generation.')}), 400

    prompt = str(data['prompt']).strip()
    if not prompt or len(prompt) > 8000 or len(prompt.split()) > 120:
        return jsonify({'error': _('The prompt must contain at most 120 words and 8000 characters.')}), 400
    # Use user's default or system default LLM provider
    provider = data.get('provider', current_app.config.get('DEFAULT_LLM_PROVIDER'))
    kwargs = {k: data[k] for k in ('model', 'temperature', 'max_tokens') if k in data}
    if 'max_tokens' in kwargs:
        try:
            kwargs['max_tokens'] = min(max(1, int(kwargs['max_tokens'])), 1024)
        except (TypeError, ValueError):
            return jsonify({'error': _('max_tokens must be a number.')}), 400

    logging.info(f"{log_prefix} Received request for text generation using provider '{provider}'.")

    try:
        role = current_user.role
        if role is None:
            return jsonify({'error': _('You do not have permission to run workflows.')}), 403
        allowed, reason = role_model.reserve_usage_if_allowed(
            user_id, role, workflows_to_add=1
        )
        if not allowed:
            return jsonify({'error': reason}), 429

        # Keep API-key selection in the central LLM service so this endpoint
        # follows the same user, admin, and environment fallback rules.
        result_text = llm_service.generate_text_via_llm(
            provider_name=provider,
            prompt=prompt,
            user_id=user_id,
            **kwargs
        )

        # TODO: Consider logging this operation in the llm_operations table

        return jsonify({'result': result_text}), 200

    except role_model.UsageReservationError:
        logging.exception(f"{log_prefix} Durable quota reservation failed.")
        return jsonify({'error': _('Usage limits could not be verified. Please try again.')}), 503
    except (LlmConfigurationError, ValueError) as e: # Config/Input errors
        logging.warning(f"{log_prefix} Configuration or Value error: {e}")
        return jsonify({'error': _compose_error_message(_('We could not start the AI request because the configuration or input is invalid.'), str(e))}), 400
    except LlmRateLimitError as e:
        logging.warning(f"{log_prefix} LLM Rate Limit error: {e}")
        return jsonify({'error': _compose_error_message(_('The AI provider temporarily rate-limited this request. Please wait a moment and try again.'), str(e))}), 429
    except LlmSafetyError as e:
        logging.warning(f"{log_prefix} LLM Safety error: {e}")
        return jsonify({'error': _compose_error_message(_('The AI provider blocked this request because of its safety filters. Please adjust your prompt and try again.'), str(e))}), 400 # Bad Request for safety
    except (LlmApiError, LlmServiceError) as e: # API/Service errors
        logging.error(f"{log_prefix} LLM generation failed: {e}", exc_info=True)
        return jsonify({'error': _compose_error_message(_('The AI provider could not complete this request. Please try again later.'), str(e))}), 500 # Internal Server Error or specific code from error
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error during LLM generation: {e}", exc_info=True)
        return jsonify({'error': _('We encountered an unexpected error while generating text. Please try again.')}), 500

# --- ADDED: LLM Operation Status Endpoint ---
@llm_bp.route('/operations/<int:operation_id>/status', methods=['GET'])
@login_required
def get_llm_operation_status(operation_id: int):
    """
    API endpoint to get the status and result of a specific LLM operation.
    Ensures the requesting user owns the operation.
    """
    user_id = current_user.id
    log_prefix = f"[API:LLM:Status:Op:{operation_id}:User:{user_id}]"
    logging.debug(f"{log_prefix} Request received.")

    try:
        # Fetch the operation, verifying ownership. A missing or foreign
        # operation returns the same 404 so the endpoint cannot be used to
        # enumerate which operation IDs exist.
        operation_data = llm_operation_model.get_llm_operation_by_id(operation_id, user_id)

        if not operation_data:
            logging.warning(f"{log_prefix} LLM operation not found or not owned by user.")
            return jsonify({'error': _('We could not find that AI operation.')}), 404

        # Prepare response
        response_data = {
            'operation_id': operation_id,
            'status': operation_data.get('status', 'unknown'),
            'result': operation_data.get('result') if operation_data.get('status') == 'finished' else None,
            'error': operation_data.get('error') if operation_data.get('status') == 'error' else None,
            'provider': operation_data.get('provider'),
            'operation_type': operation_data.get('operation_type'),
            'created_at': operation_data.get('created_at'),
            'completed_at': operation_data.get('completed_at'),
            'transcription_id': operation_data.get('transcription_id'),
            'prompt_id': operation_data.get('prompt_id')
        }
        logging.debug(f"{log_prefix} Returning status: {response_data['status']}")
        return jsonify(response_data), 200

    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error fetching LLM operation status: {e}", exc_info=True)
        return jsonify({'error': _('We encountered an internal error while fetching the AI operation status. Please try again.')}), 500
# --- END ADDED ---

# Add other endpoints like /chat, /embedding as needed
