# app/api/llm.py
# Defines the Blueprint for LLM operation endpoints.

import logging
from flask import Blueprint, jsonify
from flask_login import login_required, current_user
from flask_babel import gettext as _

from app.models import llm_operation as llm_operation_model

# Define the Blueprint
llm_bp = Blueprint('llm', __name__, url_prefix='/api/llm')

# --- LLM Operation Status Endpoint ---
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
        # Fetch the operation, verifying ownership
        operation_data = llm_operation_model.get_llm_operation_by_id(operation_id, user_id)

        if not operation_data:
            # Check if it exists at all to differentiate 404 from 403
            unowned_op = llm_operation_model.get_llm_operation_by_id(operation_id)
            if unowned_op:
                logging.warning(f"{log_prefix} Access denied: Operation exists but is not owned by user.")
                return jsonify({'error': _('You do not have access to this AI operation.')}), 403
            else:
                logging.warning(f"{log_prefix} LLM operation not found.")
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
