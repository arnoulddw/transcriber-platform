# app/api/workflows.py
# Defines the Blueprint for workflow management API endpoints.

import logging
from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user
from flask_babel import gettext as _
# --- ADDED: Import Optional ---
from typing import Optional
# --- END ADDED ---

# Import services and exceptions
# --- MODIFIED: Import only workflow_service and its specific exceptions ---
from app.services import workflow_service
from app.models import llm_operation as llm_operation_model
from app.services.workflow_service import (
    WorkflowError, PermissionDeniedError, UsageLimitExceededError,
    TranscriptionNotFoundError, InvalidPromptError, WorkflowInProgressError,
    # --- ADDED: Import OperationNotFoundError ---
    OperationNotFoundError
    # --- END ADDED ---
)
# --- END MODIFIED ---
# Note: LLM exceptions are not caught here — start_workflow only validates
# and spawns the background thread; the LLM call happens asynchronously.

# Import decorators
from app.core.decorators import permission_required
from app.extensions import limiter

# Define the Blueprint
workflows_bp = Blueprint('workflows', __name__, url_prefix='/api')


def _compose_error_message(base_message: str, details: Optional[str] = None) -> str:
    """Return a translated error message with optional diagnostic details."""
    details_text = str(details or "").strip()
    if details_text:
        return f"{base_message} {_('Details')}: {details_text}"
    return base_message

# --- Workflow Execution Endpoints ---

@workflows_bp.route('/transcriptions/<transcription_id>/workflow', methods=['POST'])
@login_required
@permission_required('allow_workflows')
@limiter.limit(lambda: current_app.config.get('WORKFLOW_RATE_LIMIT', '10 per hour'), key_func=lambda: str(current_user.id))
def run_workflow(transcription_id: str):
    """
    API endpoint to initiate a workflow analysis on a transcription.
    Expects JSON: {"prompt": "...", "prompt_id": optional_int}
    Returns the operation_id on success.
    """
    user_id = current_user.id
    log_prefix = f"[API:Workflow:Run:{transcription_id[:8]}:User:{user_id}]"

    # Opportunistically fail LLM operations stalled beyond the configured age
    # so a crashed background run cannot block this transcription forever.
    try:
        stale_seconds = current_app.config.get('LLM_OPERATION_STALE_SECONDS', 1800)
        llm_operation_model.mark_stale_operations_interrupted(stale_seconds)
    except Exception as sweep_err:
        logging.warning(f"{log_prefix} Stale LLM operation sweep failed: {sweep_err}")

    data = request.get_json()

    if not data or 'prompt' not in data:
        logging.warning(f"{log_prefix} Invalid request: Missing 'prompt' in JSON payload.")
        return jsonify({'error': _('Please include a workflow prompt before submitting.')}), 400

    prompt = data['prompt']
    # --- ADDED: Get optional prompt_id ---
    prompt_id_str = data.get('prompt_id')
    prompt_id: Optional[int] = None
    if prompt_id_str:
        try:
            prompt_id = int(prompt_id_str)
        except (ValueError, TypeError):
            logging.warning(f"{log_prefix} Invalid prompt_id received: {prompt_id_str}. Ignoring.")
    # --- END ADDED ---

    try:
        logging.debug(f"{log_prefix} Received request to start workflow (PromptID: {prompt_id}).")
        # --- MODIFIED: Capture returned operation_id ---
        operation_id = workflow_service.start_workflow(user_id, transcription_id, prompt, prompt_id)
        # --- END MODIFIED ---
        logging.info(f"{log_prefix} Workflow initiation successful.")
        # --- MODIFIED: Return operation_id in response ---
        return jsonify({
            'message': _('Workflow started successfully.'),
            'operation_id': operation_id
        }), 202 # Accepted
        # --- END MODIFIED ---
    except PermissionDeniedError as e:
        logging.warning(f"{log_prefix} Workflow start failed: {e}")
        return jsonify({'error': _compose_error_message(_('You do not have permission to run workflows.'), str(e))}), 403
    except UsageLimitExceededError as e:
        logging.warning(f"{log_prefix} Workflow start failed: {e}")
        return jsonify({'error': _compose_error_message(_('You have reached your workflow usage limit. Please try again later.'), str(e)), 'code': 'WORKFLOW_LIMIT_EXCEEDED'}), 403
    except TranscriptionNotFoundError as e:
        logging.warning(f"{log_prefix} Workflow start failed: {e}")
        return jsonify({'error': _compose_error_message(_('We could not find that transcription or you no longer have access to it.'), str(e))}), 404
    except InvalidPromptError as e:
        logging.warning(f"{log_prefix} Workflow start failed: {e}")
        return jsonify({'error': _compose_error_message(_('The workflow prompt needs attention before it can run. Please review it and try again.'), str(e))}), 400
    except WorkflowInProgressError as e:
        logging.warning(f"{log_prefix} Workflow start failed: {e}")
        return jsonify({'error': _compose_error_message(_('A workflow is already running for this transcription. Please wait for it to finish.'), str(e))}), 400
    # Note: LLM errors (rate limit, safety, API) cannot occur here — the LLM
    # is only called asynchronously in the background thread after this
    # endpoint returns. Those failures surface via operation polling instead.
    except WorkflowError as e: # Catch remaining workflow setup errors
        logging.error(f"{log_prefix} Workflow start failed: {e}", exc_info=True)
        return jsonify({'error': _compose_error_message(_('We were unable to run this workflow. Please try again.'), str(e))}), 500
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error starting workflow: {e}", exc_info=True)
        return jsonify({'error': _('We hit an unexpected error while starting the workflow. Please try again.')}), 500

# --- MODIFIED: Changed route to target operation_id ---
@workflows_bp.route('/workflows/operations/<int:operation_id>', methods=['PUT'])
@login_required
@permission_required('allow_workflows')
def edit_workflow(operation_id: int):
# --- END MODIFIED ---
    """
    API endpoint to edit the result of a completed workflow (LLMOperation).
    Expects JSON: {"result": "..."}
    """
    user_id = current_user.id
    # --- MODIFIED: Updated log prefix ---
    log_prefix = f"[API:Workflow:Edit:Op:{operation_id}:User:{user_id}]"
    # --- END MODIFIED ---
    data = request.get_json()

    if not data or 'result' not in data:
        logging.warning(f"{log_prefix} Invalid request: Missing 'result' in JSON payload.")
        return jsonify({'error': _('Please include the updated workflow result in your request.')}), 400

    new_result = data['result']

    try:
        logging.debug(f"{log_prefix} Received request to edit workflow result.")
        # --- MODIFIED: Pass operation_id instead of transcription_id ---
        workflow_service.edit_workflow_result(user_id, operation_id, new_result)
        # --- END MODIFIED ---
        logging.info(f"{log_prefix} Workflow result edit successful.")
        return jsonify({'message': _('Workflow result updated successfully.')}), 200
    # --- MODIFIED: Catch OperationNotFoundError ---
    except OperationNotFoundError as e:
        logging.warning(f"{log_prefix} Workflow edit failed: {e}")
        return jsonify({'error': _compose_error_message(_('We could not find that workflow operation.'), str(e))}), 404
    # --- END MODIFIED ---
    except WorkflowError as e: # Catches DB errors or other issues like permission
        logging.error(f"{log_prefix} Workflow edit failed: {e}", exc_info=True)
        # Determine status code based on error message content
        status_code = 403 if "permission issue" in str(e).lower() else 500
        return jsonify({'error': _compose_error_message(_('We could not update this workflow result. Please try again.'), str(e))}), status_code
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error editing workflow result: {e}", exc_info=True)
        return jsonify({'error': _('We hit an unexpected error while editing the workflow result. Please try again.')}), 500

# --- delete_workflow remains unchanged, still targets transcription_id ---
@workflows_bp.route('/transcriptions/<transcription_id>/workflow', methods=['DELETE'])
@login_required
@permission_required('allow_workflows')
def delete_workflow(transcription_id: str):
    """
    API endpoint to delete the workflow result (LLMOperation) associated with a transcription.
    """
    user_id = current_user.id
    log_prefix = f"[API:Workflow:Delete:{transcription_id[:8]}:User:{user_id}]"

    try:
        logging.debug(f"{log_prefix} Received request to delete workflow result(s).")
        workflow_service.delete_workflow_result(user_id, transcription_id)
        logging.info(f"{log_prefix} Workflow result delete successful.")
        return jsonify({'message': _('Workflow result deleted successfully.')}), 200
    except TranscriptionNotFoundError as e: # Service raises this if transcription not found/owned
        logging.warning(f"{log_prefix} Workflow delete failed: {e}")
        return jsonify({'error': _compose_error_message(_('We could not find that transcription or you no longer have access to it.'), str(e))}), 404
    except WorkflowError as e: # Catches DB errors
        logging.error(f"{log_prefix} Workflow delete failed: {e}", exc_info=True)
        return jsonify({'error': _compose_error_message(_('We were unable to delete the workflow result. Please try again.'), str(e))}), 500
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error deleting workflow result: {e}", exc_info=True)
        return jsonify({'error': _('We hit an unexpected error while deleting the workflow result. Please try again.')}), 500
