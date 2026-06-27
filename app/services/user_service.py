# app/services/user_service.py
# Handles user-specific business logic, particularly API key management and profile updates.

from app.logging_config import get_logger
import re # For Gemini API key validation
import secrets
import hmac
import hashlib
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List 

from cryptography.fernet import InvalidToken
from flask import current_app

# Import model and User class
from app.models import user as user_model 
from app.models import user_api_key as user_api_key_model
from app.models import public_api_key as public_api_key_model
from app.models import user_prompt as user_prompt_model
from app.models import template_prompt as template_prompt_model
from app.models.user import User 
from app.models.user_prompt import UserPrompt
from app.models.template_prompt import TemplatePrompt
from app.models import transcription as transcription_model

# Import security service for encryption/decryption
from .security_service import get_security_service, SecurityService

# Import MySQL error class for potential specific checks if needed
from mysql.connector import Error as MySQLError

from app.database import get_cursor


# --- Custom Exceptions ---
class UserNotFoundError(Exception):
    """User not found in the database."""
    pass

class ApiKeyManagementError(Exception):
    """General error during API key management."""
    pass

class MissingApiKeyError(ApiKeyManagementError):
    """Required API key is missing or not configured for the user."""
    pass

class KeyNotFoundError(ApiKeyManagementError):
    """API key for a specific service not found for the user."""
    pass

class DatabaseUpdateError(ApiKeyManagementError):
    """Failed to update the database during key management."""
    pass

class ProfileUpdateError(Exception):
    """General error during profile update."""
    pass

class UsernameTakenError(ProfileUpdateError):
    """Username is already taken by another user."""
    pass

class EmailTakenError(ProfileUpdateError):
    """Email is already taken by another user."""
    pass

class PromptManagementError(Exception):
    """General error during prompt management."""
    pass

class PromptNotFoundError(PromptManagementError):
    """Prompt not found."""
    pass

class DuplicatePromptError(PromptManagementError):
    """A prompt with the same title already exists for the user."""
    pass

class DataLengthError(PromptManagementError):
    """Input data exceeds the maximum allowed length for a database field."""
    pass


# --- API Key Management ---

def _hash_public_api_key(raw_key: str) -> str:
    """
    Creates an HMAC-SHA256 hash of the raw API key using the app SECRET_KEY.
    """
    secret = current_app.config.get('SECRET_KEY')
    if not secret:
        raise ValueError("SECRET_KEY is required to generate public API keys.")
    return hmac.new(secret.encode('utf-8'), raw_key.encode('utf-8'), hashlib.sha256).hexdigest()

def _validate_gemini_api_key_format(api_key: str) -> bool:
    """
    Validates the basic format of a Google Gemini API key.
    Checks only for the "AIzaSy" prefix.
    """
    if api_key and api_key.startswith("AIzaSy"):
        return True
    return False

def save_user_api_key(user_id: int, service: str, api_key: str) -> bool:
    """
    Encrypts and saves or updates an API key for a specific user and service.
    Uses MySQL backend via models.
    """
    logger = get_logger(__name__, user_id=user_id, component="UserService")
    if not service or not api_key:
        logger.error("Attempted to save empty service or API key.")
        raise ValueError("Service name and API key cannot be empty.")

    allowed_services = ['openai', 'assemblyai', 'gemini']
    if service not in allowed_services:
        logger.error(f"Attempted to save API key for invalid service: {service}")
        raise ValueError(f"Invalid service specified: {service}. Must be one of {allowed_services}.")
    service = service.lower()

    if service == 'gemini' and not _validate_gemini_api_key_format(api_key):
        logger.warning("Invalid Google Gemini API key format provided.")
        raise ValueError("Invalid Google Gemini API key format. Key should start with 'AIzaSy'.")

    try:
        user = user_model.get_user_by_id(user_id)
        if not user:
            logger.error("User not found when trying to save API key.")
            raise UserNotFoundError(f"User with ID {user_id} not found.")

        security_svc: SecurityService = get_security_service()
        encrypted_key = security_svc.encrypt_data(api_key)
        logger.debug(f"API key for service '{service}' encrypted.")

        success = user_api_key_model.upsert_api_key(user_id, service, encrypted_key)
        if not success:
            logger.error(f"Failed to persist API key for service '{service}'.")
            raise DatabaseUpdateError("Failed to update API keys in the database.")

        logger.debug(f"Successfully saved encrypted API key for service '{service}'.")
        return True

    except (UserNotFoundError, ValueError, DatabaseUpdateError) as e:
         raise e
    except MySQLError as db_err:
        logger.error(f"Database error saving API key for service '{service}': {db_err}", exc_info=True)
        raise ApiKeyManagementError(f"A database error occurred while saving the API key for {service}.") from db_err
    except Exception as e:
        logger.error(f"Unexpected error saving API key for service '{service}': {e}", exc_info=True)
        raise ApiKeyManagementError(f"An unexpected error occurred while saving the API key for {service}.") from e

def get_decrypted_api_key(user_id: int, service: str) -> Optional[str]:
    """
    Retrieves and decrypts a specific API key for a user. Uses MySQL backend via models.
    """
    logger = get_logger(__name__, user_id=user_id, component="UserService")
    if not service:
        logger.error("Attempted to get API key for empty service.")
        return None
    service = service.lower()

    try:
        user = user_model.get_user_by_id(user_id)
        if not user:
            logger.debug("User not found when fetching encrypted API keys.")
            return None

        encrypted_key = user_api_key_model.get_api_key(user_id, service)
        if not encrypted_key:
            logger.debug(f"API key for service '{service}' not found in stored keys.")
            return None

        security_svc: SecurityService = get_security_service()
        try:
            decrypted_key = security_svc.decrypt_data(encrypted_key)
            logger.debug(f"Successfully decrypted API key for service '{service}'.")
            return decrypted_key
        except InvalidToken:
            logger.error(f"Decryption failed for service '{service}': Invalid Token. Key might be corrupted or SECRET_KEY changed.")
            return None
        except ValueError as ve:
            logger.error(f"Decryption error for service '{service}': {ve}", exc_info=True)
            return None

    except MySQLError as db_err:
        logger.error(f"Database error getting API key for service '{service}': {db_err}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Unexpected error getting API key for service '{service}': {e}", exc_info=True)
        return None

def delete_user_api_key(user_id: int, service: str) -> None:
    """
    Deletes a specific API key for a user. Uses MySQL backend via models.
    """
    logger = get_logger(__name__, user_id=user_id, component="UserService")
    if not service:
        logger.error("Attempted to delete API key for empty service.")
        raise ValueError("Service name cannot be empty.")

    allowed_services = ['openai', 'assemblyai', 'gemini']
    if service not in allowed_services:
        logger.error(f"Attempted to delete API key for invalid service: {service}")
        raise ValueError(f"Invalid service specified: {service}. Must be one of {allowed_services}.")
    service = service.lower()

    try:
        user = user_model.get_user_by_id(user_id)
        if not user:
            raise UserNotFoundError(f"User with ID {user_id} not found.")

        removed = user_api_key_model.delete_api_key(user_id, service)
        if not removed:
            logger.warning(f"API key for service '{service}' not found or could not be removed.")
            raise KeyNotFoundError(f"API key for service '{service}' not found.")
        logger.debug(f"Successfully removed API key for service '{service}'.")

    except (UserNotFoundError, KeyNotFoundError, DatabaseUpdateError, ValueError) as specific_error:
        raise specific_error
    except MySQLError as db_err:
        logger.error(f"Database error deleting API key for service '{service}': {db_err}", exc_info=True)
        raise ApiKeyManagementError(f"A database error occurred while deleting the API key for {service}.") from db_err
    except Exception as e:
        logger.error(f"Unexpected error deleting API key for service '{service}': {e}", exc_info=True)
        raise ApiKeyManagementError(f"An unexpected error occurred while deleting the API key for {service}.") from e

def get_user_api_key_status(user_id: int) -> Dict[str, Any]:
    """
    Checks which API keys are configured (present and non-empty) for the user.
    Uses MySQL backend via models.
    """
    logger = get_logger(__name__, user_id=user_id, component="UserService")
    status: Dict[str, Any] = {
        'openai': False,
        'assemblyai': False,
        'gemini': False,
        'public_api': {
            'enabled': False,
            'last_four': None,
            'created_at': None,
            'keys': []
        }
    }
    try:
        user = user_model.get_user_by_id(user_id)
        if not user:
            return status
        allow_public = False
        try:
            allow_public = bool(user.role.allow_public_api_access) if user.role else False
        except Exception:
            allow_public = False

        key_map = user_api_key_model.get_api_keys_by_user(user_id)
        status['openai'] = bool(key_map.get('openai'))
        status['assemblyai'] = bool(key_map.get('assemblyai'))
        status['gemini'] = bool(key_map.get('gemini'))

        status['public_api'] = get_public_api_key_status(user_id) if allow_public else status['public_api']

        logger.debug(f"API Key status checked: {status}")

    except MySQLError as db_err:
        logger.error(f"Database error checking API key status: {db_err}", exc_info=True)
    except Exception as e:
        logger.error(f"Error checking API key status: {e}", exc_info=True)
    return status


def get_public_api_key_status(user_id: int) -> Dict[str, Optional[str]]:
    """
    Returns metadata about the user's public API key used for authenticated API access.
    """
    logger = get_logger(__name__, user_id=user_id, component="UserService")
    try:
        user = user_model.get_user_by_id(user_id)
        if not user:
            raise UserNotFoundError(f"User with ID {user_id} not found.")
        if not (user.role and user.role.has_permission('allow_public_api_access')):
            raise ApiKeyManagementError("Public API access is not permitted for this user.")

        keys = []
        for key in public_api_key_model.get_public_api_keys_by_user(user_id):
            created_at_raw = key.get('created_at')
            if isinstance(created_at_raw, datetime):
                created_at = created_at_raw.replace(tzinfo=timezone.utc).isoformat()
            else:
                created_at = str(created_at_raw) if created_at_raw else None
            keys.append({
                'id': key.get('id'),
                'name': key.get('name'),
                'last_four': key.get('last_four'),
                'created_at': created_at
            })

        legacy_created_raw = getattr(user, 'public_api_key_created_at', None)
        legacy_created = None
        if legacy_created_raw:
            if isinstance(legacy_created_raw, datetime):
                legacy_created = legacy_created_raw.replace(tzinfo=timezone.utc).isoformat()
            else:
                legacy_created = str(legacy_created_raw)

        status = {
            'enabled': bool(keys) or bool(getattr(user, 'public_api_key_hash', None)),
            'last_four': keys[0]['last_four'] if keys else getattr(user, 'public_api_key_last_four', None),
            'created_at': keys[0]['created_at'] if keys else legacy_created,
            'keys': keys
        }
        logger.debug(f"Public API key status for user {user_id}: {status}")
        return status
    except UserNotFoundError:
        raise
    except Exception as e:
        logger.error(f"Error retrieving public API key status for user {user_id}: {e}", exc_info=True)
        raise ApiKeyManagementError("Failed to retrieve public API key status.") from e


def generate_public_api_key(user_id: int, name: Optional[str] = None) -> Dict[str, str]:
    """
    Generates a new public API key for the user, storing only a hashed version.
    Returns the plaintext key once so the caller can display it.
    """
    logger = get_logger(__name__, user_id=user_id, component="UserService")
    try:
        user = user_model.get_user_by_id(user_id)
        if not user:
            raise UserNotFoundError(f"User with ID {user_id} not found.")
        if not (user.role and user.role.has_permission('allow_public_api_access')):
            raise ApiKeyManagementError("Public API access is not permitted for this user.")

        raw_key = f"tk_{secrets.token_urlsafe(32)}"
        key_hash = _hash_public_api_key(raw_key)
        last_four = raw_key[-4:]
        created_at = datetime.now(timezone.utc)
        key_name = (name or "").strip() or "Public API key"
        if len(key_name) > 120:
            raise ValueError("Key name must be 120 characters or fewer.")

        key_id = public_api_key_model.create_public_api_key(user_id, key_name, key_hash, last_four, created_at)
        if not key_id:
            raise ApiKeyManagementError("Failed to persist public API key.")

        logger.info(f"Generated new public API key for user {user_id}.")
        return {
            'id': key_id,
            'name': key_name,
            'api_key': raw_key,
            'last_four': last_four,
            'created_at': created_at.isoformat()
        }
    except (UserNotFoundError, ValueError, ApiKeyManagementError) as e:
        raise e
    except Exception as e:
        logger.error(f"Unexpected error generating public API key for user {user_id}: {e}", exc_info=True)
        raise ApiKeyManagementError("An unexpected error occurred while generating the public API key.") from e


def revoke_public_api_key(user_id: int, key_id: Optional[int] = None) -> None:
    """
    Removes the stored public API key hash/metadata for the user.
    """
    logger = get_logger(__name__, user_id=user_id, component="UserService")
    try:
        user = user_model.get_user_by_id(user_id)
        if not user:
            raise UserNotFoundError(f"User with ID {user_id} not found.")
        if not (user.role and user.role.has_permission('allow_public_api_access')):
            raise ApiKeyManagementError("Public API access is not permitted for this user.")
        if key_id is not None:
            if not public_api_key_model.revoke_public_api_key(user_id, key_id):
                raise KeyNotFoundError("Public API key not found.")
            logger.info(f"Revoked public API key {key_id} for user {user_id}.")
            return

        if not public_api_key_model.revoke_all_public_api_keys(user_id):
            raise ApiKeyManagementError("Failed to revoke the public API key.")
        user_model.clear_public_api_key(user_id)
        logger.info(f"Revoked public API key for user {user_id}.")
    except (UserNotFoundError, KeyNotFoundError, ApiKeyManagementError) as e:
        raise e
    except Exception as e:
        logger.error(f"Unexpected error revoking public API key for user {user_id}: {e}", exc_info=True)
        raise ApiKeyManagementError("An unexpected error occurred while revoking the public API key.") from e


def authenticate_public_api_key(raw_key: str) -> Optional[User]:
    """
    Validates a presented public API key and returns the associated user if valid.
    """
    logger = get_logger(__name__, component="UserService")
    if not raw_key:
        return None
    try:
        key_hash = _hash_public_api_key(raw_key)
        user = public_api_key_model.get_user_by_public_api_key_hash(key_hash)
        if not user:
            user = user_model.get_user_by_public_api_key_hash(key_hash)
        if user and getattr(user, 'public_api_key_hash', None):
            if hmac.compare_digest(user.public_api_key_hash, key_hash):
                return user
        return None
    except Exception as e:
        logger.error(f"Error authenticating public API key: {e}", exc_info=True)
        return None


def hash_public_api_key_for_rate_limit(raw_key: str) -> Optional[str]:
    """
    Utility used by rate-limiters to derive a stable key from the raw API token.
    Returns None if hashing cannot be performed.
    """
    try:
        return _hash_public_api_key(raw_key)
    except Exception:
        return None


# --- Profile Update Service ---
def update_profile(user_id: int, data: Dict[str, Any]) -> None:
    """
    Updates a user's profile information and preferences.
    Performs validation, including uniqueness checks for username and email if changed.

    Args:
        user_id: The ID of the user to update.
        data: A dictionary containing the profile data, typically from a validated form.
              Expected keys: 'username', 'email', 'first_name', 'last_name',
                             'default_content_language', 'default_transcription_model',
                             'enable_auto_title_generation', 'language'.
    """
    logger = get_logger(__name__, user_id=user_id, component="UserService")
    logger.debug(f"Attempting to update profile with data: {data}")

    required_keys = ['username', 'email']
    if not all(key in data for key in required_keys):
        raise ProfileUpdateError("Missing required profile data (username, email).")

    username = data.get('username')
    email = data.get('email')
    first_name = data.get('first_name')
    last_name = data.get('last_name')
    default_language = data.get('default_content_language')
    default_model = data.get('default_transcription_model')
    language = data.get('language')
    enable_auto_title_raw = data.get('enable_auto_title_generation')
    if isinstance(enable_auto_title_raw, bool):
        enable_auto_title = enable_auto_title_raw
    else:
        enable_auto_title = str(enable_auto_title_raw).lower() in ['true', 'on', '1', 'yes']

    default_language = None if default_language == "" else default_language
    default_model = None if default_model == "" else default_model
    language = None if language == "" else language
    logger.debug(f"Processed preferences - Lang: {default_language}, Model: {default_model}, AutoTitle: {enable_auto_title}, UI Lang: {language}")


    if not username or not email:
        raise ProfileUpdateError("Username and Email cannot be empty.")

    try:
        current_user_obj = user_model.get_user_by_id(user_id)
        if not current_user_obj:
            raise UserNotFoundError(f"User with ID {user_id} not found.")

        username_changed = username != current_user_obj.username
        email_changed = email != current_user_obj.email

        if username_changed:
            existing_user = user_model.get_user_by_username(username)
            if existing_user:
                logger.warning(f"Update failed: Username '{username}' is already taken.")
                raise UsernameTakenError(f"Username '{username}' is already taken.")

        if email_changed:
            existing_user = user_model.get_user_by_email(email)
            if existing_user:
                logger.warning(f"Update failed: Email '{email}' is already registered.")
                raise EmailTakenError(f"Email address '{email}' is already registered.")

        core_info_changed = (
            username_changed or
            email_changed or
            first_name != current_user_obj.first_name or
            last_name != current_user_obj.last_name
        )
        prefs_changed = (
            default_language != current_user_obj.default_content_language or
            default_model != current_user_obj.default_transcription_model or
            enable_auto_title != current_user_obj.enable_auto_title_generation or
            language != current_user_obj.language
        )

        if not core_info_changed and not prefs_changed:
            logger.debug("No profile changes were submitted.")
            return

        core_update_performed = False
        if core_info_changed:
            logger.debug("Core profile info changed, attempting update...")
            if user_model.update_user_profile(user_id, username, email, first_name, last_name):
                core_update_performed = True
                logger.debug("Core profile info updated successfully in DB.")
            else:
                logger.debug("Core profile info update: no rows affected (data likely already matched).")
                core_update_performed = True
        else:
            logger.debug("No changes detected in core profile info.")
            core_update_performed = True

        prefs_update_performed = False
        if prefs_changed:
            logger.debug("Preferences changed, attempting update...")
            if user_model.update_user_preferences(user_id, default_language, default_model, enable_auto_title, language):
                prefs_update_performed = True
                logger.debug("Preferences updated successfully in DB.")
            else:
                logger.warning("update_user_preferences model function returned False. Preferences might not have been saved (or were already set to the target values).")
                prefs_update_performed = True
        else:
            logger.debug("No changes detected in preferences.")
            prefs_update_performed = True

        # --- NEW: Trigger template sync if language changed ---
        if language != current_user_obj.language:
            logger.debug(f"User UI language changed from '{current_user_obj.language}' to '{language}'. Triggering template sync.")
            sync_templates_for_user(user_id)
        # --- END NEW ---

        logger.info(f"Profile update process completed. Core processed: {core_update_performed}, Prefs processed: {prefs_update_performed}")

    except (UserNotFoundError, UsernameTakenError, EmailTakenError, DatabaseUpdateError, ProfileUpdateError) as e:
        raise e
    except MySQLError as db_err:
        logger.error(f"Database error updating profile: {db_err}", exc_info=True)
        if db_err.errno == 1062:
            if 'username' in str(db_err).lower():
                raise UsernameTakenError(f"Username '{username}' is already taken (DB constraint).")
            elif 'email' in str(db_err).lower():
                raise EmailTakenError(f"Email address '{email}' is already registered (DB constraint).")
        raise ProfileUpdateError("A database error occurred while updating the profile.") from db_err
    except Exception as e:
        logger.error(f"Unexpected error updating profile: {e}", exc_info=True)
        raise ProfileUpdateError("An unexpected error occurred while updating the profile.") from e


# --- User Prompt Management ---
def save_user_prompt(user_id: int, title: str, prompt_text: str, color: str = '#ffffff') -> Optional[UserPrompt]:
    """Saves a new custom prompt for the user."""
    logger = get_logger(__name__, user_id=user_id, component="UserService")
    logger.debug(f"Service received color: '{color}' (Type: {type(color)})")
    if not title or not prompt_text:
        raise ValueError("Prompt title and text cannot be empty.")
    try:
        new_prompt = user_prompt_model.add_prompt(user_id, title, prompt_text, color)
        if not new_prompt:
            # This case might be redundant if add_prompt always raises on failure, but it's safe to keep.
            raise PromptManagementError("Failed to save prompt for an unknown reason.")
        return new_prompt
    except MySQLError as db_err:
        logger.error(f"Database error saving prompt: {db_err.msg} (Code: {db_err.errno})", exc_info=True)
        if db_err.errno == 1062: # Duplicate entry
            raise DuplicatePromptError(f"A prompt with the title '{title}' already exists.") from db_err
        elif db_err.errno == 1406: # Data too long
            raise DataLengthError("The provided title or prompt text is too long.") from db_err
        raise PromptManagementError("A database error occurred while saving the prompt.") from db_err
    except Exception as e:
        logger.error(f"Unexpected error saving prompt: {e}", exc_info=True)
        raise PromptManagementError("An unexpected error occurred while saving the prompt.") from e

def get_user_prompts(user_id: int) -> List[UserPrompt]:
    """Retrieves all saved prompts for the user."""
    logger = get_logger(__name__, user_id=user_id, component="UserService")
    try:
        return user_prompt_model.get_prompts_by_user(user_id)
    except MySQLError as db_err:
        logger.error(f"Database error getting prompts: {db_err}", exc_info=True)
        raise PromptManagementError("Database error retrieving prompts.") from db_err
    except Exception as e:
        logger.error(f"Unexpected error getting prompts: {e}", exc_info=True)
        raise PromptManagementError("Unexpected error retrieving prompts.") from e

def update_user_prompt(prompt_id: int, user_id: int, title: str, prompt_text: str, color: str = '#ffffff') -> bool:
    """Updates an existing user prompt."""
    logger = get_logger(__name__, user_id=user_id, component="UserService")
    logger.debug(f"Service received color for update: '{color}' (Type: {type(color)})")
    if not title or not prompt_text:
        raise ValueError("Prompt title and text cannot be empty.")
    try:
        success = user_prompt_model.update_prompt(prompt_id, user_id, title, prompt_text, color)
        if not success:
            if not user_prompt_model.get_prompt_by_id(prompt_id):
                 raise PromptNotFoundError(f"Prompt with ID {prompt_id} not found.")
            else:
                 raise PromptManagementError(f"Failed to update prompt {prompt_id} (check ownership or logs).")
        return True
    except MySQLError as db_err:
        logger.error(f"Database error updating prompt {prompt_id}: {db_err}", exc_info=True)
        raise PromptManagementError("Database error updating prompt.") from db_err
    except Exception as e:
        logger.error(f"Unexpected error updating prompt {prompt_id}: {e}", exc_info=True)
        if isinstance(e, (PromptNotFoundError, PromptManagementError)):
            raise e
        else:
            raise PromptManagementError("Unexpected error updating prompt.") from e

def delete_user_prompt(prompt_id: int, user_id: int) -> bool:
    """Deletes a user prompt."""
    logger = get_logger(__name__, user_id=user_id, component="UserService")
    try:
        success = user_prompt_model.delete_prompt(prompt_id, user_id)
        if not success:
            if not user_prompt_model.get_prompt_by_id(prompt_id):
                 raise PromptNotFoundError(f"Prompt with ID {prompt_id} not found.")
            else:
                 raise PromptManagementError(f"Failed to delete prompt {prompt_id} (check ownership).")
        return True
    except MySQLError as db_err:
        logger.error(f"Database error deleting prompt {prompt_id}: {db_err}", exc_info=True)
        raise PromptManagementError("Database error deleting prompt.") from db_err
    except Exception as e:
        logger.error(f"Unexpected error deleting prompt {prompt_id}: {e}", exc_info=True)
        if isinstance(e, (PromptNotFoundError, PromptManagementError)):
            raise e
        else:
            raise PromptManagementError("Unexpected error deleting prompt.") from e

def get_recent_user_prompts(user_id: int, limit: int = 5) -> List[str]:
    """Retrieves the most recently used distinct workflow prompts for a user."""
    logger = get_logger(__name__, user_id=user_id, component="UserService")
    prompts = []
    try:
        cursor = get_cursor()
        sql = """
            SELECT input_text as workflow_prompt
            FROM llm_operations
            WHERE user_id = %s
              AND operation_type = 'workflow'
              AND input_text IS NOT NULL
              AND input_text != ''
              AND completed_at IS NOT NULL
            GROUP BY input_text
            ORDER BY MAX(completed_at) DESC
            LIMIT %s
        """
        cursor.execute(sql, (user_id, limit))
        rows = cursor.fetchall()
        prompts = [row['workflow_prompt'] for row in rows]
        logger.debug(f"Retrieved {len(prompts)} recent prompts from llm_operations.")
    except MySQLError as db_err:
        logger.error(f"Database error getting recent prompts: {db_err}", exc_info=True)
        prompts = []
    except Exception as e:
        logger.error(f"Unexpected error getting recent prompts: {e}", exc_info=True)
        prompts = []
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
    return prompts

def get_prompt_by_id_internal(prompt_id: int) -> Optional[UserPrompt]:
    """Internal helper to get prompt by ID without user check."""
    try:
        return user_prompt_model.get_prompt_by_id(prompt_id)
    except Exception:
        return None

# --- NEW: Template Synchronization Service ---

def sync_templates_for_user(user_id: int) -> None:
    """
    Synchronizes admin-defined templates to a specific user's personal prompt collection.

    - Copies new templates that match the user's language.
    - Updates existing synced prompts if the source template has changed.
    - Deletes user's synced prompts if the source template was deleted.
    """
    logger = get_logger(__name__, user_id=user_id, component="UserService")
    logger.debug("Starting template synchronization.")

    try:
        user = user_model.get_user_by_id(user_id)
        if not user:
            logger.error("User not found, cannot sync templates.")
            return

        # 1. Get all relevant admin templates (matching user lang or 'all')
        user_lang = user.language
        admin_templates = template_prompt_model.get_templates(language=user.language)
        admin_template_map = {t.id: t for t in admin_templates}
        logger.debug(f"Found {len(admin_templates)} applicable admin templates for language '{user_lang}'.")

        # 2. Get user's existing prompts that were synced from a template
        user_synced_prompts_map = user_prompt_model.get_user_synced_prompts_map(user_id)
        logger.debug(f"Found {len(user_synced_prompts_map)} existing synced prompts for user.")

        # 3. Synchronize: Add new, update existing
        for template_id, template in admin_template_map.items():
            existing_user_prompt = user_synced_prompts_map.get(template_id)

            if existing_user_prompt:
                # This template exists in the user's collection, check for updates
                if (existing_user_prompt.title != template.title or
                    existing_user_prompt.prompt_text != template.prompt_text or
                    existing_user_prompt.color != template.color):
                    
                    logger.debug(f"Updating user prompt ID {existing_user_prompt.id} from source template ID {template_id}.")
                    user_prompt_model.update_synced_prompt(
                        prompt_id=existing_user_prompt.id,
                        title=template.title,
                        prompt_text=template.prompt_text,
                        color=template.color
                    )
            else:
                # This is a new template for this user, copy it
                logger.debug(f"Copying new template ID {template_id} ('{template.title}') to user.")
                user_prompt_model.add_prompt(
                    user_id=user_id,
                    title=template.title,
                    prompt_text=template.prompt_text,
                    color=template.color,
                    source_template_id=template.id
                )

        # 4. Synchronize: Remove deleted (Handled by ON DELETE CASCADE)
        # When a template_prompt is deleted, the corresponding user_prompts
        # are automatically removed by the database, so no action is needed here.

        logger.info("Template synchronization complete.")

    except Exception as e:
        logger.error(f"An unexpected error occurred during template sync: {e}", exc_info=True)


def sync_templates_for_all_users() -> None:
    """
    Triggers the template synchronization process for every user in the system.
    This is typically called after an admin modifies the template collection.
    """
    logger = get_logger(__name__, component="UserService")
    logger.info("Starting template synchronization for ALL users.")
    try:
        all_user_ids = user_prompt_model.get_all_user_ids()
        logger.debug(f"Found {len(all_user_ids)} users to sync.")
        for user_id in all_user_ids:
            sync_templates_for_user(user_id)
        logger.info("Finished syncing templates for all users.")
    except Exception as e:
        logger.error(f"An unexpected error occurred during all-user sync: {e}", exc_info=True)

def handle_new_user_template_sync(user_id: int) -> None:
    """
    Handles the initial population of templates for a new user.
    This is just a clear wrapper around the main sync function.
    """
    logger = get_logger(__name__, user_id=user_id, component="UserService")
    logger.debug("Triggering initial template population for new user.")
    sync_templates_for_user(user_id)
