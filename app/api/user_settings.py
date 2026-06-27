# app/api/user_settings.py
# Defines the Blueprint for user-specific settings API endpoints (e.g., API keys, readiness, profile).

import logging
from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user
from flask_babel import gettext as _

# Import forms and services
from app.forms import ApiKeyForm, UserProfileForm, ChangePasswordForm
from app.services import user_service, auth_service, admin_management_service 
from app.services.user_service import (
    KeyNotFoundError, DatabaseUpdateError, UserNotFoundError, ApiKeyManagementError,
    ProfileUpdateError, UsernameTakenError, EmailTakenError,
    PromptManagementError, PromptNotFoundError, DuplicatePromptError, DataLengthError
)
from app.services.auth_service import InvalidCredentialsError, AuthServiceError
from app.services.admin_management_service import AdminServiceError
from app.models.user import User 
from app.models import user, user_utils, user_prompt as user_prompt_model
from app.models.user_prompt import UserPrompt
from app.models import transcription as transcription_model 
from app.core.decorators import check_permission
from app.extensions import limiter

# Define the Blueprint
user_settings_bp = Blueprint('user_settings', __name__, url_prefix='/api/user') 

# --- User Readiness Endpoint ---
@user_settings_bp.route('/readiness', methods=['GET'])
@login_required
@limiter.exempt
def get_user_readiness():
    """
    API endpoint to get the user's readiness status for transcription.
    Includes API key status, relevant permissions, usage limits, and current usage stats.
    Crucial for the frontend to determine available actions and display limits.
    """
    user_obj: User = current_user 
    user_id = user_obj.id
    log_prefix = f"[API:Readiness:User:{user_id}]"
    logging.debug(f"{log_prefix} Request received.")

    try:
        # This is a good place to ensure the user's templates are up-to-date
        # as it's called frequently when the user is active on the main page.
        user_service.sync_templates_for_user(user_id)

        key_status = user_service.get_user_api_key_status(user_id)

        permissions = {}
        limits = {}
        if user_obj.role: 
            role = user_obj.role 
            permissions = {
                'use_api_assemblyai': role.has_permission('use_api_assemblyai'),
                'use_api_openai_whisper': role.has_permission('use_api_openai_whisper'),
                'use_api_openai_gpt_4o_transcribe': role.has_permission('use_api_openai_gpt_4o_transcribe'),
                'use_api_google_gemini': role.has_permission('use_api_google_gemini'),
                'allow_large_files': role.has_permission('allow_large_files'),
                'allow_context_prompt': role.has_permission('allow_context_prompt'),
                'allow_download_transcript': role.has_permission('allow_download_transcript'),
                'allow_api_key_management': role.has_permission('allow_api_key_management'), 
                'allow_public_api_access': role.has_permission('allow_public_api_access'),
                'access_admin_panel': role.has_permission('access_admin_panel'), 
                'allow_workflows': role.has_permission('allow_workflows'), 
                'manage_workflow_templates': role.has_permission('manage_workflow_templates'), 
                'allow_auto_title_generation': role.has_permission('allow_auto_title_generation'),
                'allow_speaker_diarization': role.has_permission('allow_speaker_diarization')
            }
            limits = {
                'max_transcriptions_monthly': role.get_limit('max_transcriptions_monthly'),
                'max_minutes_monthly': role.get_limit('max_minutes_monthly'),
                'max_transcriptions_total': role.get_limit('max_transcriptions_total'),
                'max_minutes_total': role.get_limit('max_minutes_total'),
                'max_workflows_monthly': role.get_limit('max_workflows_monthly'), 
                'max_workflows_total': role.get_limit('max_workflows_total') 
            }
        else:
            logging.warning(f"{log_prefix} User has no role assigned. Permissions/limits will be empty.")

        usage_stats = user_utils.get_user_usage_stats(user_id) 

        readiness_data = {
            'api_keys': key_status,
            'permissions': permissions,
            'limits': limits,
            'usage': usage_stats
        }
        logging.debug(f"{log_prefix} Returning readiness data: {readiness_data}")
        logging.debug(f"User readiness data for user {user_id}: {readiness_data}")
        return jsonify(readiness_data), 200

    except Exception as e:
        logging.error(f"{log_prefix} Error getting user readiness: {e}", exc_info=True)
        return jsonify({'error': _('Failed to retrieve user readiness status.')}), 500


# --- API Key Management Endpoints ---
@user_settings_bp.route('/keys', methods=['GET'])
@login_required
def get_api_key_status():
    """
    API endpoint to return the configuration status (set/not set) of the user's API keys.
    Used by the 'Manage API Keys' modal.
    """
    user_id = current_user.id
    log_prefix = f"[API:UserKeys:{user_id}:GET]"
    logging.debug(f"{log_prefix} Request received for API key status.")
    try:
        status = user_service.get_user_api_key_status(user_id)
        logging.info(f"{log_prefix} Returning API key status: {status}")
        return jsonify(status), 200
    except Exception as e:
        logging.error(f"{log_prefix} Error getting API key status: {e}", exc_info=True)
        return jsonify({'error': _('Failed to retrieve API key status.')}), 500


@user_settings_bp.route('/public-api-key', methods=['GET'])
@login_required
def get_public_api_key_status():
    """
    Returns the status/metadata for the user's public API key (used for authenticated API access).
    """
    user_id = current_user.id
    log_prefix = f"[API:UserPublicKey:{user_id}:GET]"
    try:
        if not check_permission(current_user, 'allow_public_api_access'):
            logging.warning(f"{log_prefix} Permission denied for public API key status.")
            return jsonify({'error': _('You do not have permission to use the public API.')}), 403
        status = user_service.get_public_api_key_status(user_id)
        logging.info(f"{log_prefix} Returning public API key status.")
        return jsonify(status), 200
    except UserNotFoundError as e:
        logging.warning(f"{log_prefix} User not found: {e}")
        return jsonify({'error': str(e)}), 404
    except Exception as e:
        logging.error(f"{log_prefix} Error getting public API key status: {e}", exc_info=True)
        return jsonify({'error': _('Failed to retrieve API key status.')}), 500


@user_settings_bp.route('/public-api-key', methods=['POST'])
@login_required
def generate_public_api_key():
    """
    Generates (or regenerates) a public API key for the authenticated user.
    Returns the plaintext key once so it can be copied.
    """
    user_id = current_user.id
    log_prefix = f"[API:UserPublicKey:{user_id}:POST]"
    try:
        if not check_permission(current_user, 'allow_public_api_access'):
            logging.warning(f"{log_prefix} Permission denied for public API key generation.")
            return jsonify({'error': _('You do not have permission to use the public API.')}), 403
        name = (request.form.get('name') or (request.get_json(silent=True) or {}).get('name') or '').strip()
        key_data = user_service.generate_public_api_key(user_id, name=name)
        logging.info(f"{log_prefix} Public API key generated.")
        return jsonify(key_data), 201
    except UserNotFoundError as e:
        logging.warning(f"{log_prefix} User not found: {e}")
        return jsonify({'error': str(e)}), 404
    except (ValueError, ApiKeyManagementError) as e:
        logging.error(f"{log_prefix} Failed to generate public API key: {e}")
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error generating public API key: {e}", exc_info=True)
        return jsonify({'error': _('An unexpected error occurred while generating the API key.')}), 500


@user_settings_bp.route('/public-api-key', methods=['DELETE'])
@login_required
def delete_public_api_key():
    """
    Revokes the stored public API key for the authenticated user.
    """
    user_id = current_user.id
    log_prefix = f"[API:UserPublicKey:{user_id}:DELETE]"
    try:
        if not check_permission(current_user, 'allow_public_api_access'):
            logging.warning(f"{log_prefix} Permission denied for public API key revoke.")
            return jsonify({'error': _('You do not have permission to use the public API.')}), 403
        user_service.revoke_public_api_key(user_id)
        logging.info(f"{log_prefix} Public API key revoked.")
        return jsonify({'message': _('Public API key revoked.')}), 200
    except UserNotFoundError as e:
        logging.warning(f"{log_prefix} User not found: {e}")
        return jsonify({'error': str(e)}), 404
    except ApiKeyManagementError as e:
        logging.error(f"{log_prefix} Failed to revoke public API key: {e}")
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error revoking public API key: {e}", exc_info=True)
        return jsonify({'error': _('An unexpected error occurred while revoking the API key.')}), 500


@user_settings_bp.route('/public-api-key/<int:key_id>', methods=['DELETE'])
@login_required
def delete_named_public_api_key(key_id):
    """
    Revokes one stored public API key for the authenticated user.
    """
    user_id = current_user.id
    log_prefix = f"[API:UserPublicKey:{user_id}:DELETE:{key_id}]"
    try:
        if not check_permission(current_user, 'allow_public_api_access'):
            logging.warning(f"{log_prefix} Permission denied for public API key revoke.")
            return jsonify({'error': _('You do not have permission to use the public API.')}), 403
        user_service.revoke_public_api_key(user_id, key_id=key_id)
        logging.info(f"{log_prefix} Public API key revoked.")
        return jsonify({'message': _('Public API key revoked.')}), 200
    except KeyNotFoundError as e:
        logging.warning(f"{log_prefix} Public API key not found: {e}")
        return jsonify({'error': str(e)}), 404
    except UserNotFoundError as e:
        logging.warning(f"{log_prefix} User not found: {e}")
        return jsonify({'error': str(e)}), 404
    except ApiKeyManagementError as e:
        logging.error(f"{log_prefix} Failed to revoke public API key: {e}")
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error revoking public API key: {e}", exc_info=True)
        return jsonify({'error': _('An unexpected error occurred while revoking the API key.')}), 500

@user_settings_bp.route('/keys', methods=['POST'])
@login_required
def save_api_key():
    """
    API endpoint to save or update an API key for a specific service for the logged-in user.
    Uses WTForms (ApiKeyForm) for validation, including CSRF protection.
    """
    user_id = current_user.id
    log_prefix = f"[API:UserKeys:{user_id}:POST]"
    form = ApiKeyForm()

    if form.validate_on_submit():
        service = form.service.data
        api_key = form.api_key.data
        logging.info(f"{log_prefix} Attempting to save API key for service '{service}'.")
        try:
            user_service.save_user_api_key(user_id, service, api_key)
            logging.info(f"{log_prefix} API key for service '{service}' saved successfully.")
            return jsonify({'message': _('API key for %(service)s saved successfully.', service=service)}), 200
        except (UserNotFoundError, ValueError, DatabaseUpdateError, ApiKeyManagementError) as e:
             logging.error(f"{log_prefix} Failed to save API key for service '{service}': {e}")
             status_code = 404 if isinstance(e, UserNotFoundError) else 400 if isinstance(e, ValueError) else 500
             return jsonify({'error': str(e)}), status_code
        except Exception as e:
             logging.error(f"{log_prefix} Unexpected error saving API key for '{service}': {e}", exc_info=True)
             return jsonify({'error': _('An unexpected error occurred while saving the API key.')}), 500
    else:
        errors = form.errors
        logging.warning(f"{log_prefix} API key save failed validation: {errors}")
        return jsonify({'error': _('Validation failed'), 'errors': errors}), 400

@user_settings_bp.route('/keys/<service>', methods=['DELETE'])
@login_required
def delete_api_key(service):
    """
    API endpoint to delete a specific API key for the logged-in user.
    Handles 'openai', 'assemblyai', and 'gemini'.
    """
    user_id = current_user.id
    log_prefix = f"[API:UserKeys:{user_id}:DELETE:{service}]"

    allowed_services = ['openai', 'assemblyai', 'gemini']
    if service not in allowed_services:
        logging.warning(f"{log_prefix} Attempt to delete key for invalid service in URL: {service}")
        return jsonify({'error': _('Invalid service specified in URL.')}), 400

    logging.info(f"{log_prefix} Attempting to delete API key.")
    try:
        user_service.delete_user_api_key(user_id, service)
        logging.info(f"{log_prefix} API key deleted successfully.")
        return jsonify({'message': _('API key for %(service)s deleted successfully.', service=service)}), 200
    except KeyNotFoundError as e:
        logging.warning(f"{log_prefix} Failed to delete API key: {e}")
        return jsonify({'error': str(e)}), 404 
    except (UserNotFoundError, DatabaseUpdateError, ValueError, ApiKeyManagementError) as e:
        logging.error(f"{log_prefix} Failed to delete API key: {e}")
        status_code = 404 if isinstance(e, UserNotFoundError) else 400 if isinstance(e, ValueError) else 500
        return jsonify({'error': str(e)}), status_code
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error deleting API key: {e}", exc_info=True)
        return jsonify({'error': _('An unexpected error occurred while deleting the API key.')}), 500


# --- User Profile Endpoints ---

@user_settings_bp.route('/profile', methods=['GET'])
@login_required
def get_profile():
    """API endpoint to get the current user's profile data."""
    user_obj: User = current_user 
    log_prefix = f"[API:UserProfile:{user_obj.id}:GET]"
    logging.debug(f"{log_prefix} Request received.")
    try:
        profile_data = {
            'username': user_obj.username,
            'email': user_obj.email,
            'first_name': user_obj.first_name,
            'last_name': user_obj.last_name,
            'default_content_language': user_obj.default_content_language,
            'default_transcription_model': user_obj.default_transcription_model,
            'oauth_provider': user_obj.oauth_provider,
            'enable_auto_title_generation': user_obj.enable_auto_title_generation,
            'language': user_obj.language
        }
        return jsonify(profile_data), 200
    except Exception as e:
        logging.error(f"{log_prefix} Error retrieving profile data: {e}", exc_info=True)
        return jsonify({'error': _('Failed to retrieve profile data.')}), 500


@user_settings_bp.route('/profile', methods=['PUT'])
@login_required
def update_profile():
    """API endpoint to update the user's profile information and preferences."""
    user_id = current_user.id
    log_prefix = f"[API:UserProfile:{user_id}:PUT]"
    form_data = request.get_json() 

    logging.debug(f"{log_prefix} Received profile update data: {form_data}")
    form = UserProfileForm(data=form_data)
    logging.debug(f"{log_prefix} Form data after instantiation: Lang='{form.default_content_language.data}', Model='{form.default_transcription_model.data}', AutoTitle='{form.enable_auto_title_generation.data}'")

    if form.validate():
        logging.info(f"{log_prefix} Profile update request validated.")
        try:
            user_service.update_profile(user_id, form.data)
            logging.info(f"{log_prefix} Profile updated successfully.")
            return jsonify({'message': _('Profile updated successfully.')}), 200
        except UsernameTakenError as e:
            logging.warning(f"{log_prefix} Profile update failed: {e}")
            return jsonify({'error': str(e), 'field': 'username'}), 409
        except EmailTakenError as e:
            logging.warning(f"{log_prefix} Profile update failed: {e}")
            return jsonify({'error': str(e), 'field': 'email'}), 409
        except (UserNotFoundError, ProfileUpdateError, DatabaseUpdateError) as e:
            logging.error(f"{log_prefix} Profile update failed: {e}")
            status_code = 404 if isinstance(e, UserNotFoundError) else 500
            return jsonify({'error': str(e)}), status_code
        except Exception as e:
            logging.error(f"{log_prefix} Unexpected error updating profile: {e}", exc_info=True)
            return jsonify({'error': _('An unexpected error occurred while updating the profile.')}), 500
    else:
        errors = form.errors
        logging.warning(f"{log_prefix} Profile update failed validation: {errors}")
        return jsonify({'error': _('Validation failed'), 'errors': errors}), 400


@user_settings_bp.route('/change-password', methods=['POST'])
@login_required
def change_password():
    """API endpoint for the user to change their own password."""
    user_id = current_user.id
    log_prefix = f"[API:UserPassword:{user_id}:POST]"
    form = ChangePasswordForm(data=request.get_json())

    if current_user.oauth_provider and not current_user.password_hash:
         logging.warning(f"{log_prefix} Password change attempt failed: User logged in via OAuth and has no password set.")
         return jsonify({'error': _('Password change is not available for accounts signed in via Google/OAuth.')}), 400

    if form.validate():
        logging.info(f"{log_prefix} Change password request validated.")
        try:
            auth_service.change_password(
                user_id,
                form.current_password.data,
                form.new_password.data
            )
            logging.info(f"{log_prefix} Password changed successfully.")
            return jsonify({'message': _('Password changed successfully.')}), 200
        except InvalidCredentialsError as e:
            logging.warning(f"{log_prefix} Password change failed: {e}")
            return jsonify({'error': str(e), 'field': 'current_password'}), 400
        except (ValueError, AuthServiceError) as e:
            logging.error(f"{log_prefix} Password change failed: {e}")
            status_code = 400 if isinstance(e, ValueError) else 500
            return jsonify({'error': str(e)}), status_code
        except Exception as e:
            logging.error(f"{log_prefix} Unexpected error changing password: {e}", exc_info=True)
            return jsonify({'error': _('An unexpected error occurred while changing the password.')}), 500
    else:
        errors = form.errors
        logging.warning(f"{log_prefix} Change password failed validation: {errors}")
        return jsonify({'error': _('Validation failed'), 'errors': errors}), 400


# --- User Prompt Management Endpoints ---
@user_settings_bp.route('/prompts', methods=['GET']) 
@login_required
def get_user_prompts_api():
    """
    API endpoint to get user's saved prompts.
    Now returns a single list of all prompts for the user.
    """
    user_id = current_user.id
    log_prefix = f"[API:UserPrompts:{user_id}:GET]"
    try:
        user_prompts: List[UserPrompt] = user_service.get_user_prompts(user_id)
        
        # --- MODIFIED: Include source_template_id in the response ---
        prompt_dicts = [
            {
                'id': p.id,
                'title': p.title,
                'prompt_text': p.prompt_text,
                'color': p.color,
                'source_template_id': p.source_template_id
            } for p in user_prompts
        ]

        return jsonify(prompt_dicts), 200
        # --- END MODIFIED ---
    except Exception as e:
        logging.error(f"{log_prefix} Error fetching user prompts: {e}", exc_info=True)
        return jsonify({'error': _('Failed to retrieve user prompts.')}), 500

@user_settings_bp.route('/prompts', methods=['POST']) 
@login_required
def save_user_prompt_api():
    """API endpoint to save a new custom prompt."""
    user_id = current_user.id
    log_prefix = f"[API:UserPrompts:{user_id}:POST]"
    data = request.get_json()
    logging.debug(f"{log_prefix} Received raw data: {data}")
    if not data or not data.get('title') or not data.get('prompt_text'):
        return jsonify({'error': _('Missing title or prompt_text in request body.')}), 400

    title = data['title']
    prompt_text = data['prompt_text']
    color = data.get('color', '#ffffff') 
    logging.debug(f"{log_prefix} Extracted color: '{color}' (Type: {type(color)})")

    try:
        new_prompt = user_service.save_user_prompt(user_id, title, prompt_text, color)
        if new_prompt:
            return jsonify({
                'message': _('Prompt saved successfully.'),
                'prompt': {'id': new_prompt.id, 'title': new_prompt.title, 'prompt_text': new_prompt.prompt_text, 'color': new_prompt.color, 'source_template_id': new_prompt.source_template_id}
            }), 201
        else:
            raise PromptManagementError(_("Failed to save prompt (service returned None)."))
    except DuplicatePromptError as e:
        logging.warning(f"{log_prefix} Failed to save prompt: {e}")
        return jsonify({'error': str(e)}), 409
    except DataLengthError as e:
        logging.warning(f"{log_prefix} Failed to save prompt due to invalid data length: {e}")
        return jsonify({'error': str(e)}), 400
    except PromptManagementError as e:
        logging.error(f"{log_prefix} Error saving prompt: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error saving prompt: {e}", exc_info=True)
        return jsonify({'error': _('An unexpected error occurred while saving the prompt.')}), 500

@user_settings_bp.route('/prompts/<int:prompt_id>', methods=['PUT']) 
@login_required
def update_user_prompt_api(prompt_id: int):
    """API endpoint to update an existing custom prompt."""
    user_id = current_user.id
    log_prefix = f"[API:UserPrompts:{user_id}:PUT:{prompt_id}]"
    data = request.get_json()
    logging.debug(f"{log_prefix} Received raw data for update: {data}")
    if not data or not data.get('title') or not data.get('prompt_text'):
        return jsonify({'error': _('Missing title or prompt_text in request body.')}), 400

    title = data['title']
    prompt_text = data['prompt_text']
    color = data.get('color', '#ffffff') 
    logging.debug(f"{log_prefix} Extracted color for update: '{color}' (Type: {type(color)})")

    try:
        success = user_service.update_user_prompt(prompt_id, user_id, title, prompt_text, color)
        if success:
            return jsonify({'message': _('Prompt updated successfully.')}), 200
        else:
            return jsonify({'error': _('Failed to update prompt (check ownership or logs).')}), 500
    except PromptNotFoundError as e:
        logging.warning(f"{log_prefix} Update failed: {e}")
        return jsonify({'error': str(e)}), 404
    except PromptManagementError as e: 
        logging.error(f"{log_prefix} Error updating prompt: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error updating prompt: {e}", exc_info=True)
        return jsonify({'error': _('An unexpected error occurred while updating the prompt.')}), 500

@user_settings_bp.route('/prompts/<int:prompt_id>', methods=['DELETE']) 
@login_required
def delete_user_prompt_api(prompt_id: int):
    """API endpoint to delete a custom prompt."""
    user_id = current_user.id
    log_prefix = f"[API:UserPrompts:{user_id}:DELETE:{prompt_id}]"
    try:
        success = user_service.delete_user_prompt(prompt_id, user_id)
        if success:
            return jsonify({'message': _('Prompt deleted successfully.')}), 200
        else:
            return jsonify({'error': _('Failed to delete prompt (check ownership or logs).')}), 500
    except PromptNotFoundError as e:
        logging.warning(f"{log_prefix} Delete failed: {e}")
        return jsonify({'error': str(e)}), 404
    except PromptManagementError as e: 
        logging.error(f"{log_prefix} Error deleting prompt: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error deleting prompt: {e}", exc_info=True)
        return jsonify({'error': _('An unexpected error occurred while deleting the prompt.')}), 500
