import logging
import json
from typing import Optional, List, Dict
from datetime import datetime

from flask import current_app
from mysql.connector import Error as MySQLError

from app.database import get_db, get_cursor
from app.models import transcription_catalog as transcription_catalog_model
from app.models import user_api_key as user_api_key_model

from .model import User, _map_row_to_user

logger = logging.getLogger(__name__)
_DEFAULT_OPENROUTER_MODEL_UNSET = object()
_DEFAULT_LIVE_MODEL_UNSET = object()

try:
    from app.models.role import Role, get_role_by_id, get_role_by_name
except ImportError as e:
    logger.critical(f"[DB:Models:User] Failed to import Role model dependencies: {e}. This may cause runtime errors.")
    Role = None  # type: ignore
    get_role_by_id = None  # type: ignore
    get_role_by_name = None  # type: ignore


def _get_default_transcription_model_for_new_user(role: Role) -> Optional[str]:
    """
    Determines the default transcription model for a new user based on their role and system config.
    """
    if not role:
        return None

    try:
        catalog_models = transcription_catalog_model.get_active_models()
    except Exception as catalog_err:
        logger.error(f"[DB:User] Failed to load transcription model catalog: {catalog_err}", exc_info=True)
        catalog_models = []

    if not catalog_models:
        logger.warning(f"[DB:User] No active transcription models available while creating user with role '{role.name}'.")
        return current_app.config.get('DEFAULT_TRANSCRIPTION_PROVIDER')

    permitted_model_keys: List[str] = []
    default_model_key: Optional[str] = None
    role_preferred_model = (getattr(role, 'default_transcription_model', None) or '').strip()
    models_by_code: Dict[str, List[str]] = {}

    for model in catalog_models:
        permission_key = model.get('permission_key')
        model_code = str(model.get('code') or '').strip()
        model_key = str(model.get('model_key') or model_code).strip()
        if not model_code or not model_key:
            continue
        models_by_code.setdefault(model_code, []).append(model_key)
        if not permission_key or role.has_permission(permission_key):
            permitted_model_keys.append(model_key)
            if model.get('is_default'):
                default_model_key = model_key

    if not permitted_model_keys:
        logger.warning(f"[DB:User] New user with role '{role.name}' has no transcription models permitted.")
        return None

    preferred_key = role_preferred_model
    if preferred_key not in permitted_model_keys:
        legacy_matches = models_by_code.get(role_preferred_model, [])
        if len(legacy_matches) == 1:
            preferred_key = legacy_matches[0]
    if preferred_key in permitted_model_keys:
        logger.debug(
            f"[DB:User] Using role-level default transcription model '{preferred_key}' for new user with role '{role.name}'."
        )
        return preferred_key
    if role_preferred_model:
        logger.warning(
            f"[DB:User] Role '{role.name}' default transcription model '{role_preferred_model}' is not permitted. Falling back to catalog defaults."
        )

    if default_model_key and default_model_key in permitted_model_keys:
        logger.debug(f"[DB:User] Setting default model for new user to catalog default: '{default_model_key}'")
        return default_model_key

    fallback_model_key = permitted_model_keys[0]
    logger.debug(
        f"[DB:User] Catalog default not permitted for role '{role.name}'. Falling back to first available: '{fallback_model_key}'"
    )
    return fallback_model_key


def _get_default_openrouter_model_for_new_user(role: Role) -> Optional[str]:
    """Returns a valid role-level OpenRouter transcription slug, if configured."""
    raw_model = (getattr(role, 'default_openrouter_model', None) or '').strip() if role else ''
    if not raw_model:
        return None
    if '/' not in raw_model or ' ' in raw_model or len(raw_model) > 120:
        logger.warning("[DB:User] Ignoring invalid role OpenRouter model default '%s'.", raw_model)
        return None
    return raw_model


def add_user(username: str, email: str, password_hash: str, role_name: str = 'beta-tester', language: Optional[str] = None) -> Optional[User]:
    if get_role_by_name is None:
        return None
    logger.info(f"[DB:User] Adding user with role_name: {role_name}")
    role = get_role_by_name(role_name)
    if not role:
        logger.error(f"[DB:User] Cannot add user '{username}': Role '{role_name}' not found.")
        if role_name != 'beta-tester':
            logger.warning(f"[DB:User] Falling back to default role 'beta-tester' for user '{username}'.")
            role = get_role_by_name('beta-tester')
        if not role:
            logger.critical(f"[DB:User] Default role 'beta-tester' also not found. Cannot create user '{username}'.")
            return None
    role_id = role.id
    logger.info(f"[DB:User] Role ID to be inserted: {role_id}")

    default_model = _get_default_transcription_model_for_new_user(role)
    default_openrouter_model = _get_default_openrouter_model_for_new_user(role)
    default_language = transcription_catalog_model.get_default_language_code() or current_app.config.get('DEFAULT_LANGUAGE', 'auto')

    default_auto_title_enabled = False
    if role:
        has_perm = role.has_permission('allow_auto_title_generation')
        logger.info(
            f"[DB:User] Role '{role.name}' allow_auto_title_generation permission: {getattr(role, 'allow_auto_title_generation', 'ATTR_MISSING')}, has_permission()={has_perm}"
        )
        if has_perm:
            default_auto_title_enabled = True
    else:
        logger.warning(f"[DB:User] No role provided for default auto-title check")

    sql = '''
        INSERT INTO users (
            username, email, password_hash, role_id, created_at,
            enable_auto_title_generation, language,
            default_content_language, default_transcription_model,
            default_openrouter_model
        )
        VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s)
        '''
    cursor = get_cursor()
    try:
        cursor.execute(
            sql,
            (
                username,
                email,
                password_hash,
                role.id,
                default_auto_title_enabled,
                language,
                default_language,
                default_model,
                default_openrouter_model,
            ),
        )
        user_id = cursor.lastrowid
        logger.info(
            f"[DB:User] Added new user '{username}' (Email: {email}) with ID {user_id}, role '{role_name}' (ID: {role_id}), AutoTitle: {default_auto_title_enabled}, Language: {language}, DefaultModel: {default_model}."
        )
        user = get_user_by_id(user_id)
        if user:
            user._role = get_role_by_name(role_name)
        return user
    except MySQLError as err:
        get_db().rollback()
        if err.errno == 1062:
            if 'users.username' in err.msg or 'idx_username' in err.msg:
                logger.warning(f"[DB:User] Attempted to add user with duplicate username: {username}")
            elif 'users.email' in err.msg or 'idx_email' in err.msg:
                logger.warning(f"[DB:User] Attempted to add user with duplicate email: {email}")
            else:
                logger.warning(f"[DB:User] Duplicate entry error adding user '{username}'/'{email}': {err}")
        else:
            logger.error(f"[DB:User] Error adding user '{username}': {err}", exc_info=True)
        return None
    finally:
        pass


def add_oauth_user(
    email: str,
    first_name: Optional[str],
    last_name: Optional[str],
    oauth_provider: str,
    oauth_provider_id: str,
    role_name: str = 'beta-tester',
    language: Optional[str] = None,
) -> Optional[User]:
    if get_role_by_name is None:
        return None
    role = get_role_by_name(role_name)
    if not role:
        logger.error(f"[DB:User] Cannot add OAuth user '{email}': Role '{role_name}' not found.")
        if role_name != 'beta-tester':
            logger.warning(f"[DB:User] Falling back to default role 'beta-tester' for OAuth user '{email}'.")
            role = get_role_by_name('beta-tester')
        if not role:
            logger.critical(f"[DB:User] Default role 'beta-tester' also not found. Cannot create OAuth user '{email}'.")
            return None
    role_id = role.id

    default_model = _get_default_transcription_model_for_new_user(role)
    default_openrouter_model = _get_default_openrouter_model_for_new_user(role)
    default_language = transcription_catalog_model.get_default_language_code() or current_app.config.get('DEFAULT_LANGUAGE', 'auto')

    default_auto_title_enabled = False
    if role and role.has_permission('allow_auto_title_generation'):
        default_auto_title_enabled = True

    username_base = email.split('@')[0].lower().replace('.', '').replace('+', '')
    username = username_base
    suffix = 1
    while get_user_by_username(username):
        username = f"{username_base}{suffix}"
        suffix += 1
        if suffix > 100:
            logger.error(f"[DB:User] Could not generate unique username for OAuth user '{email}' after {suffix-1} attempts.")
            return None

    sql = '''
        INSERT INTO users (
            username, email, password_hash, role_id, created_at,
            first_name, last_name, oauth_provider, oauth_provider_id,
            enable_auto_title_generation, language,
            default_content_language, default_transcription_model,
            default_openrouter_model
        )
        VALUES (%s, %s, NULL, %s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
        '''
    cursor = get_cursor()
    try:
        cursor.execute(
            sql,
            (
                username,
                email,
                role_id,
                first_name,
                last_name,
                oauth_provider,
                oauth_provider_id,
                default_auto_title_enabled,
                language,
                default_language,
                default_model,
                default_openrouter_model,
            ),
        )
        get_db().commit()
        user_id = cursor.lastrowid
        logger.info(
            f"[DB:User] Added new OAuth user '{username}' (Email: {email}, Provider: {oauth_provider}) with ID {user_id}, role '{role_name}' (ID: {role_id}), AutoTitle: {default_auto_title_enabled}, Language: {language}, DefaultModel: {default_model}."
        )
        return get_user_by_id(user_id)
    except MySQLError as err:
        get_db().rollback()
        if err.errno == 1062:
            if 'users.email' in err.msg or 'idx_email' in err.msg:
                logger.warning(f"[DB:User] Attempted to add OAuth user with duplicate email: {email}")
            elif 'uk_oauth' in err.msg:
                logger.warning(f"[DB:User] Attempted to add OAuth user with duplicate provider/id: {oauth_provider}/{oauth_provider_id}")
            else:
                logger.warning(f"[DB:User] Duplicate entry error adding OAuth user '{email}': {err}")
        else:
            logger.error(f"[DB:User] Error adding OAuth user '{email}': {err}", exc_info=True)
        return None
    finally:
        pass


def get_user_by_username(username: str) -> Optional[User]:
    sql = 'SELECT u.*, r.name as role_name FROM users u LEFT JOIN roles r ON u.role_id = r.id WHERE u.username = %s'
    cursor = None
    user = None
    try:
        cursor = get_cursor()
        cursor.execute(sql, (username,))
        row = cursor.fetchone()
        user = _map_row_to_user(row)
    except MySQLError as err:
        logger.error(f"[DB:User] Error retrieving user by username '{username}': {err}", exc_info=True)
        user = None
    finally:
        pass
    return user


def get_user_by_email(email: str) -> Optional[User]:
    sql = 'SELECT u.*, r.name as role_name FROM users u LEFT JOIN roles r ON u.role_id = r.id WHERE u.email = %s'
    cursor = None
    user = None
    try:
        cursor = get_cursor()
        cursor.execute(sql, (email,))
        row = cursor.fetchone()
        user = _map_row_to_user(row)
    except MySQLError as err:
        logger.error(f"[DB:User] Error retrieving user by email '{email}': {err}", exc_info=True)
        user = None
    finally:
        pass
    return user


def get_user_by_id(user_id: int) -> Optional[User]:
    sql = 'SELECT u.*, r.name as role_name FROM users u LEFT JOIN roles r ON u.role_id = r.id WHERE u.id = %s'
    cursor = None
    user = None
    try:
        cursor = get_cursor()
        cursor.execute(sql, (user_id,))
        row = cursor.fetchone()
        if row:
            logger.info(
                f"[DB:User] get_user_by_id({user_id}) - DB row: username={row.get('username')}, role_id={row.get('role_id')}, role_name={row.get('role_name')}"
            )
            user = _map_row_to_user(row)
            if user and user.role_id is not None:
                try:
                    role_snapshot = get_role_by_id(user.role_id) if get_role_by_id else None
                    user._role = role_snapshot
                    if role_snapshot:
                        logger.info(
                            f"[DB:User] get_user_by_id({user_id}) pinned role snapshot: role_id={user.role_id}, role_name={role_snapshot.name}, use_api_openai_whisper={role_snapshot.use_api_openai_whisper}"
                        )
                    else:
                        logger.warning(f"[DB:User] get_user_by_id({user_id}) failed to load role snapshot for role_id={user.role_id}")
                except Exception as pin_err:
                    logger.error(f"[DB:User] Failed to pin role snapshot for user {user_id}: {pin_err}", exc_info=True)
        else:
            logger.warning(f"[DB:User] get_user_by_id({user_id}) - No row found in database")
    except MySQLError as err:
        logger.error(f"[DB:User] Error retrieving user by ID '{user_id}': {err}", exc_info=True)
        user = None
    finally:
        pass
    return user


def get_user_by_oauth(provider: str, provider_id: str) -> Optional[User]:
    sql = 'SELECT * FROM users WHERE oauth_provider = %s AND oauth_provider_id = %s'
    cursor = None
    user = None
    try:
        cursor = get_cursor()
        cursor.execute(sql, (provider, provider_id))
        row = cursor.fetchone()
        user = _map_row_to_user(row)
        logger.debug(f"[DB:User] Searched for user by OAuth '{provider}/{provider_id}'. Found: {'Yes' if user else 'No'}")
    except MySQLError as err:
        logger.error(f"[DB:User] Error retrieving user by OAuth '{provider}/{provider_id}': {err}", exc_info=True)
        user = None
    finally:
        pass
    return user


def get_user_by_public_api_key_hash(key_hash: str) -> Optional[User]:
    sql = 'SELECT * FROM users WHERE public_api_key_hash = %s LIMIT 1'
    cursor = None
    user = None
    try:
        cursor = get_cursor()
        cursor.execute(sql, (key_hash,))
        row = cursor.fetchone()
        user = _map_row_to_user(row)
        logger.debug(f"[DB:User] Lookup by public API key hash found user: {'Yes' if user else 'No'}.")
    except MySQLError as err:
        logger.error(f"[DB:User] Error retrieving user by public API key hash: {err}", exc_info=True)
        user = None
    finally:
        pass
    return user


def link_oauth_to_user(user_id: int, oauth_provider: str, oauth_provider_id: str) -> bool:
    sql = 'UPDATE users SET oauth_provider = %s, oauth_provider_id = %s WHERE id = %s'
    cursor = get_cursor()
    try:
        cursor.execute(sql, (oauth_provider, oauth_provider_id, user_id))
        get_db().commit()
        logger.info(f"[DB:User] Linked OAuth provider '{oauth_provider}' to user ID {user_id}.")
        return True
    except MySQLError as err:
        logger.error(f"[DB:User] Error linking OAuth provider '{oauth_provider}' to user ID {user_id}: {err}", exc_info=True)
        get_db().rollback()
        return False
    finally:
        pass


def update_user_api_keys(user_id: int, encrypted_keys_json: Optional[str]) -> bool:
    """
    Backwards-compatible helper to sync API key payloads into the normalized user_api_keys table.
    Accepts the legacy JSON blob format mapping provider -> encrypted key.
    """
    try:
        if not encrypted_keys_json:
            user_api_key_model.delete_all_api_keys_for_user(user_id)
            logger.info(f"[DB:User] Cleared all API keys for user ID {user_id}.")
            return True

        keys_dict = json.loads(encrypted_keys_json)
        if not isinstance(keys_dict, dict):
            logger.error(f"[DB:User] update_user_api_keys expected a dict, got {type(keys_dict)}")
            return False

        existing_keys = user_api_key_model.get_api_keys_by_user(user_id)
        success = True

        # Remove keys that are no longer present
        for provider in set(existing_keys.keys()) - set(keys_dict.keys()):
            success = user_api_key_model.delete_api_key(user_id, provider) and success

        # Upsert provided keys
        for provider, encrypted_key in keys_dict.items():
            success = user_api_key_model.upsert_api_key(user_id, provider, encrypted_key) and success

        if success:
            logger.info(f"[DB:User] Updated API keys for user ID {user_id} using normalized storage.")
        else:
            logger.warning(f"[DB:User] One or more API keys failed to persist for user ID {user_id}.")
        return success
    except (json.JSONDecodeError, TypeError) as parse_err:
        logger.error(f"[DB:User] Failed to parse API key JSON for user {user_id}: {parse_err}", exc_info=True)
        return False
    except MySQLError as err:
        logger.error(f"[DB:User] Database error updating API keys for user ID {user_id}: {err}", exc_info=True)
        get_db().rollback()
        return False
    except Exception as err:
        logger.error(f"[DB:User] Unexpected error updating API keys for user ID {user_id}: {err}", exc_info=True)
        get_db().rollback()
        return False


def update_public_api_key(user_id: int, key_hash: str, last_four: str, created_at: datetime) -> bool:
    sql = '''
        UPDATE users
        SET public_api_key_hash = %s,
            public_api_key_last_four = %s,
            public_api_key_created_at = %s
        WHERE id = %s
    '''
    cursor = get_cursor()
    try:
        cursor.execute(sql, (key_hash, last_four, created_at, user_id))
        get_db().commit()
        logger.info(f"[DB:User] Updated public API key for user ID {user_id}.")
        return True
    except MySQLError as err:
        logger.error(f"[DB:User] Error updating public API key for user ID {user_id}: {err}", exc_info=True)
        get_db().rollback()
        return False
    finally:
        pass


def clear_public_api_key(user_id: int) -> bool:
    sql = '''
        UPDATE users
        SET public_api_key_hash = NULL,
            public_api_key_last_four = NULL,
            public_api_key_created_at = NULL
        WHERE id = %s
    '''
    cursor = get_cursor()
    try:
        cursor.execute(sql, (user_id,))
        get_db().commit()
        logger.info(f"[DB:User] Cleared public API key for user ID {user_id}.")
        return True
    except MySQLError as err:
        logger.error(f"[DB:User] Error clearing public API key for user ID {user_id}: {err}", exc_info=True)
        get_db().rollback()
        return False
    finally:
        pass


def get_all_users() -> List[User]:
    # JOIN roles so callers (e.g. cleanup task) never need extra per-user role queries.
    sql = 'SELECT u.*, r.name as role_name FROM users u LEFT JOIN roles r ON u.role_id = r.id ORDER BY u.username'
    users = []
    cursor = get_cursor()
    try:
        cursor.execute(sql)
        rows = cursor.fetchall()
        for row in rows:
            user = _map_row_to_user(row)
            if user and user.role_id is not None:
                try:
                    role_snapshot = get_role_by_id(user.role_id) if get_role_by_id else None
                    user._role = role_snapshot
                except Exception:
                    pass
            if user:
                users.append(user)
        logger.debug(f"[DB:User] Retrieved {len(users)} users.")
    except MySQLError as err:
        logger.error(f"[DB:User] Error retrieving all users: {err}", exc_info=True)
    finally:
        pass
    return users


def delete_user_by_id(user_id: int) -> bool:
    sql = 'DELETE FROM users WHERE id = %s'
    cursor = get_cursor()
    try:
        cursor.execute(sql, (user_id,))
        get_db().commit()
        if cursor.rowcount > 0:
            logger.info(f"[DB:User] Deleted user with ID {user_id}.")
            return True
        else:
            logger.warning(f"[DB:User] Attempted to delete non-existent user with ID {user_id}.")
            return False
    except MySQLError as err:
        logger.error(f"[DB:User] Error deleting user with ID {user_id}: {err}", exc_info=True)
        get_db().rollback()
        return False
    finally:
        pass


def update_user_password_hash(user_id: int, new_password_hash: str) -> bool:
    sql = 'UPDATE users SET password_hash = %s WHERE id = %s'
    cursor = get_cursor()
    try:
        cursor.execute(sql, (new_password_hash, user_id))
        get_db().commit()
        logger.info(f"[DB:User] Updated password hash for user ID {user_id}.")
        return True
    except MySQLError as err:
        logger.error(f"[DB:User] Error updating password hash for user ID {user_id}: {err}", exc_info=True)
        get_db().rollback()
        return False
    finally:
        pass


def update_user_role(user_id: int, new_role_id: int) -> bool:
    sql = 'UPDATE users SET role_id = %s WHERE id = %s'
    cursor = get_cursor()

    prev_role_id = None
    prev_role_name = None
    try:
        prev_user = get_user_by_id(user_id)
        if prev_user:
            prev_role_id = prev_user.role_id
            try:
                prev_role_name = getattr(prev_user.role, 'name', None)
            except Exception:
                prev_role_name = None
    except Exception as diag_err:
        logger.debug(f"[DB:User] update_user_role pre-fetch failed for user {user_id}: {diag_err}", exc_info=True)

    logger.info(f"[DB:User] ROLE_UPDATE: Updating user {user_id} from role_id={prev_role_id}:{prev_role_name} to role_id={new_role_id}")

    try:
        cursor.execute(sql, (new_role_id, user_id))
        get_db().commit()

        new_role_name = None
        new_role_permissions = {}
        try:
            new_role = get_role_by_id(new_role_id) if new_role_id is not None else None
            if new_role:
                new_role_name = new_role.name
                new_role_permissions = {
                    'use_api_openai_whisper': new_role.use_api_openai_whisper,
                    'allow_workflows': new_role.allow_workflows,
                    'allow_auto_title_generation': new_role.allow_auto_title_generation,
                    'allow_speaker_diarization': new_role.allow_speaker_diarization,
                }
        except Exception as name_err:
            logger.debug(f"[DB:User] update_user_role post-fetch failed to resolve role name for role_id {new_role_id}: {name_err}", exc_info=True)

        logger.info(
            f"[DB:User] ROLE_UPDATE: Updated user {user_id}: {prev_role_id}:{prev_role_name} -> {new_role_id}:{new_role_name}, permissions={new_role_permissions}"
        )

        verify_user = get_user_by_id(user_id)
        if verify_user:
            logger.info(f"[DB:User] ROLE_UPDATE: Verification - user {user_id} now has role_id={verify_user.role_id}")

        return True
    except MySQLError as err:
        logger.error(f"[DB:User] Error updating role ID for user ID {user_id}: {err}", exc_info=True)
        get_db().rollback()
        return False
    finally:
        pass


def update_user_profile(user_id: int, username: str, email: str, first_name: Optional[str], last_name: Optional[str]) -> bool:
    """
    Updates the core profile information (username, email, names) for a user.
    Does NOT handle uniqueness checks here; that should be done in the service layer before calling this.
    """
    sql = '''
        UPDATE users
        SET username = %s, email = %s, first_name = %s, last_name = %s
        WHERE id = %s
    '''
    cursor = get_cursor()
    try:
        cursor.execute(sql, (username, email, first_name, last_name, user_id))
        get_db().commit()
        if cursor.rowcount > 0:
            logger.info(f"[DB:User:{user_id}] Updated core profile information (username, email, names).")
            return True
        else:
            logger.warning(f"[DB:User:{user_id}] Attempted to update profile for non-existent user or no changes made.")
            return False
    except MySQLError as err:
        get_db().rollback()
        if err.errno == 1062:
            if 'users.username' in err.msg or 'idx_username' in err.msg:
                logger.warning(f"[DB:User:{user_id}] Profile update failed: Username '{username}' is already taken.")
            elif 'users.email' in err.msg or 'idx_email' in err.msg:
                logger.warning(f"[DB:User:{user_id}] Profile update failed: Email '{email}' is already taken.")
            else:
                logger.warning(f"[DB:User:{user_id}] Profile update failed due to duplicate entry: {err}")
            return False
        else:
            logger.error(f"[DB:User:{user_id}] Error updating profile: {err}", exc_info=True)
            return False
    finally:
        pass


def update_user_preferences(
    user_id: int,
    default_language: Optional[str],
    default_model: Optional[str],
    enable_auto_title_generation: Optional[bool] = None,
    language: Optional[str] = None,
    default_openrouter_model: Optional[str] = _DEFAULT_OPENROUTER_MODEL_UNSET,  # type: ignore[assignment]
    default_openrouter_llm_model: Optional[str] = _DEFAULT_OPENROUTER_MODEL_UNSET,  # type: ignore[assignment]
    default_live_transcription_model: Optional[str] = _DEFAULT_LIVE_MODEL_UNSET,  # type: ignore[assignment]
    default_title_generation_model: Optional[str] = _DEFAULT_OPENROUTER_MODEL_UNSET,  # type: ignore[assignment]
    default_workflow_model: Optional[str] = _DEFAULT_OPENROUTER_MODEL_UNSET,  # type: ignore[assignment]
) -> bool:
    """
    Updates the user's language, transcription model, LLM model preferences,
    OpenRouter model preferences, and auto-title setting.
    """
    log_prefix = f"[DB:User:{user_id}]"
    set_clauses = []
    params = []

    if default_language is not None:
        set_clauses.append("default_content_language = %s")
        params.append(default_language if default_language else None)

    if default_model is not None:
        set_clauses.append("default_transcription_model = %s")
        params.append(default_model if default_model else None)

    if enable_auto_title_generation is not None:
        set_clauses.append("enable_auto_title_generation = %s")
        params.append(bool(enable_auto_title_generation))

    if language is not None:
        set_clauses.append("language = %s")
        params.append(language if language else None)

    if default_openrouter_model is not _DEFAULT_OPENROUTER_MODEL_UNSET:
        set_clauses.append("default_openrouter_model = %s")
        params.append(default_openrouter_model if default_openrouter_model else None)

    if default_openrouter_llm_model is not _DEFAULT_OPENROUTER_MODEL_UNSET:
        set_clauses.append("default_openrouter_llm_model = %s")
        params.append(default_openrouter_llm_model if default_openrouter_llm_model else None)

    if default_live_transcription_model is not _DEFAULT_LIVE_MODEL_UNSET:
        set_clauses.append("default_live_transcription_model = %s")
        params.append(default_live_transcription_model if default_live_transcription_model else None)

    if default_title_generation_model is not _DEFAULT_OPENROUTER_MODEL_UNSET:
        set_clauses.append("default_title_generation_model = %s")
        params.append(default_title_generation_model if default_title_generation_model else None)

    if default_workflow_model is not _DEFAULT_OPENROUTER_MODEL_UNSET:
        set_clauses.append("default_workflow_model = %s")
        params.append(default_workflow_model if default_workflow_model else None)

    if not set_clauses:
        logger.debug(f"{log_prefix} No preference fields provided for update.")
        return False

    sql = f"UPDATE users SET {', '.join(set_clauses)} WHERE id = %s"
    params.append(user_id)

    cursor = get_cursor()
    try:
        cursor.execute(sql, tuple(params))
        get_db().commit()
        if cursor.rowcount > 0:
            logger.info(f"{log_prefix} Updated preferences. Clauses: {set_clauses}")
            return True
        else:
            logger.warning(f"{log_prefix} Attempted to update preferences for non-existent user or no changes made.")
            return False
    except MySQLError as err:
        get_db().rollback()
        logger.error(f"{log_prefix} Error updating preferences: {err}", exc_info=True)
        return False
    finally:
        pass


def count_users_by_role_id(role_id: int) -> int:
    """Counts the number of users assigned to a specific role ID."""
    sql = "SELECT COUNT(*) as count FROM users WHERE role_id = %s"
    cursor = get_cursor()
    count = 0
    try:
        cursor.execute(sql, (role_id,))
        result = cursor.fetchone()
        if result:
            count = result['count']
        logger.debug(f"[DB:User] Counted {count} users for role_id {role_id}.")
    except MySQLError as err:
        logger.error(f"[DB:User] Error counting users by role_id {role_id}: {err}", exc_info=True)
    finally:
        pass
    return count
