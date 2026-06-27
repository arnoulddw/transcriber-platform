# app/initialization.py
# Contains functions for one-time application initialization (DB schema, roles, admin).

import logging
logger = logging.getLogger(__name__)
import os
from flask import current_app, Flask
from typing import Mapping, Any

# Import necessary models and services
from app.models import role as role_model
from app.models import user as user_model
from app.models import user_api_key as user_api_key_model
from app.models import public_api_key as public_api_key_model
from app.models import transcription as transcription_model
from app.models import user_prompt as user_prompt_model
from app.models import template_prompt as template_prompt_model
# --- ADDED: Import llm_operation model ---
from app.models import llm_operation as llm_operation_model
from app.models import pricing as pricing_model
from app.models import transcription_catalog as transcription_catalog_model
from app.models import llm_catalog as llm_catalog_model
# --- END ADDED ---
from app.services import auth_service
from app.services.auth_service import AuthServiceError # Import specific exception

# Removed MARKER_FILENAME definition here, now defined in Config

def get_marker_path(config: Mapping[str, Any]) -> str:
    """Gets the absolute path for the initialization marker file from config."""
    marker_path = config['INIT_MARKER_FILE']
    marker_base_dir = os.path.dirname(marker_path)
    try:
        os.makedirs(marker_base_dir, exist_ok=True)
    except OSError as e:
        logger.error(f"[INIT] Failed to ensure marker directory exists '{marker_base_dir}': {e}")
    return marker_path

def check_initialization_marker(config: Mapping[str, Any] = None) -> bool:
    """Checks if the initialization marker file exists."""
    if config is None:
        config = current_app.config
    
    try:
        marker_path = get_marker_path(config)
        exists = os.path.exists(marker_path)
        logger.info(f"[INIT] Checking for initialization marker '{marker_path}': {'Found' if exists else 'Not Found'}")
        return exists
    except Exception as e:
        logger.error(f"Exception in check_initialization_marker: {e}", exc_info=True)
        # Log available keys to see what config we actually have
        if isinstance(config, dict) or hasattr(config, 'keys'):
            logger.error(f"Available config keys: {list(config.keys())}")
        raise

def create_initialization_marker(config: Mapping[str, Any]) -> None:
    """Creates the initialization marker file."""
    marker_path = get_marker_path(config)
    try:
        with open(marker_path, 'w') as f:
            f.write(f"Initialized at {logging.Formatter().formatTime(logging.LogRecord(None,None,None,None,None,None,None))}\n") # Write timestamp
        logger.info(f"[INIT] Created initialization marker file: {marker_path}")
    except Exception as e:
        logger.error(f"[INIT] Failed to create initialization marker file '{marker_path}': {e}", exc_info=True)

def initialize_database_schema(create_roles: bool = True) -> None:
    """Initializes all database tables in the correct order."""
    log_prefix = "[INIT:Schema]"
    logger.info(f"{log_prefix} Starting database schema initialization...")
    try:
        logger.debug(f"{log_prefix} Initializing 'roles' table...")
        role_model.init_roles_table()
        logger.debug(f"{log_prefix} Initializing 'users' table...")
        user_model.init_db_command()
        logger.debug(f"{log_prefix} Initializing 'user_api_keys' table...")
        user_api_key_model.init_db_command()
        logger.debug(f"{log_prefix} Initializing 'public_api_keys' table...")
        public_api_key_model.init_db_command()
        # monthly_usage deprecated; using user_usage aggregations instead
        logger.debug(f"{log_prefix} Initializing 'transcriptions' table...")
        transcription_model.init_db_command()
        logger.debug(f"{log_prefix} Initializing 'template_prompts' table...")
        template_prompt_model.init_db_command()
        logger.debug(f"{log_prefix} Initializing 'user_prompts' table...")
        user_prompt_model.init_db_command()
        # --- ADDED: Initialize llm_operations table ---
        logger.debug(f"{log_prefix} Initializing 'llm_operations' table...")
        llm_operation_model.init_db_command()
        # --- END ADDED ---
        logger.debug(f"{log_prefix} Initializing 'pricing' table...")
        pricing_model.init_db_command()
        logger.debug(f"{log_prefix} Initializing transcription catalog tables...")
        transcription_catalog_model.init_db_command()
        logger.debug(f"{log_prefix} Initializing LLM catalog table...")
        llm_catalog_model.init_db_command()
        logger.debug(f"{log_prefix} Initializing 'user_usage' table...")
        role_model.init_user_usage_table()
        logger.info(f"{log_prefix} Database schema initialization complete.")

        if create_roles:
            create_default_roles()
            create_initial_admin()

    except RuntimeError as e:
         logger.error(f"{log_prefix} Initialization failed due to dependency error: {e}", exc_info=True)
         raise
    except Exception as e:
        logger.error(f"{log_prefix} Initialization failed: {e}", exc_info=True)
        raise

def create_default_roles() -> None:
    """Creates the default 'admin' and 'beta-tester' roles if they don't exist."""
    log_prefix = "[INIT:Roles]"
    logger.debug(f"{log_prefix} Checking/Creating initial roles...")
    roles_created_count = 0
    roles_existed_count = 0
    default_roles = {
        'admin': {
            'description': 'Administrator role with all permissions',
            'permissions': {
                'use_api_assemblyai': True, 'use_api_openai_whisper': True, 'use_api_openai_gpt_4o_transcribe': True,
                'use_api_google_gemini': True, 'access_admin_panel': True, 'allow_large_files': True, 'allow_context_prompt': True,
                'allow_api_key_management': True, 'allow_download_transcript': True,
                'allow_workflows': True, 'manage_workflow_templates': True,
                'allow_auto_title_generation': True, 'allow_speaker_diarization': True,
                'limit_daily_cost': 0, 'limit_weekly_cost': 0, 'limit_monthly_cost': 0,
                'limit_daily_minutes': 0, 'limit_weekly_minutes': 0, 'limit_monthly_minutes': 0,
                'limit_daily_workflows': 0, 'limit_weekly_workflows': 0, 'limit_monthly_workflows': 0,
                'max_history_items': 0, 'history_retention_days': 0,
            }
        },
        'beta-tester': {
            'description': 'Beta tester role with standard permissions',
            'permissions': {
                'use_api_assemblyai': True, 'use_api_openai_whisper': True, 'use_api_openai_gpt_4o_transcribe': True,
                'access_admin_panel': False,
                'allow_large_files': True, 'allow_context_prompt': True,
                'allow_api_key_management': True, 'allow_download_transcript': True,
                'allow_workflows': True, 'manage_workflow_templates': False,
                'allow_auto_title_generation': True, 'allow_speaker_diarization': False,
                'limit_daily_cost': 0, 'limit_weekly_cost': 0, 'limit_monthly_cost': 0,
                'limit_daily_minutes': 0, 'limit_weekly_minutes': 0, 'limit_monthly_minutes': 0,
                'limit_daily_workflows': 0, 'limit_weekly_workflows': 0, 'limit_monthly_workflows': 50,
                'max_history_items': 0, 'history_retention_days': 0,
            }
        }
    }
    try:
        for name, config in default_roles.items():
            existing_role = role_model.get_role_by_name(name)
            if not existing_role:
                logger.debug(f"{log_prefix} Creating '{name}' role...")
                # Pass permissions dict directly, model handles mapping old/new limit names
                created_role = role_model.create_role(name, config['description'], config['permissions'])
                if created_role:
                    logger.debug(f"[INIT] Role '{name}' created.")
                    roles_created_count += 1
                else:
                    logger.error(f"{log_prefix} Failed to create role '{name}'.")
            else:
                logger.debug(f"{log_prefix} Role '{name}' already exists.")
                roles_existed_count += 1
        
        summary = f"Role creation check complete. Created: {roles_created_count}, Existed: {roles_existed_count}."
        logger.info(f"{log_prefix} {summary}")
    except Exception as e:
        logger.error(f"{log_prefix} Failed during role creation/check: {e}", exc_info=True)
        raise

def create_initial_admin() -> None:
    """Creates the initial admin user based on .env variables if in multi mode and user doesn't exist."""
    log_prefix = "[INIT:Admin]"
    admin_username = current_app.config.get('ADMIN_USERNAME')
    admin_password = current_app.config.get('ADMIN_PASSWORD')
    admin_email = current_app.config.get('ADMIN_EMAIL', f"{admin_username}@example.com")
    if current_app.config['DEPLOYMENT_MODE'] != 'multi':
         logger.info(f"{log_prefix} Skipping admin creation: Not in 'multi' deployment mode.")
         return
    if not admin_username or not admin_password:
        logger.error(f"{log_prefix} ADMIN_USERNAME or ADMIN_PASSWORD not set. Cannot create admin.")
        return
    logger.debug(f"{log_prefix} Checking/Creating initial admin user '{admin_username}'...")
    try:
        existing_admin = auth_service.get_user_by_username(admin_username)
        if not existing_admin:
            logger.debug(f"{log_prefix} Admin user '{admin_username}' not found. Creating...")
            admin_role = role_model.get_role_by_name('admin')
            if not admin_role:
                 logger.error(f"{log_prefix} Cannot create admin user: 'admin' role not found. Run role creation first.")
                 raise RuntimeError("Admin role 'admin' not found during initial admin creation.")
            created_user = auth_service.create_user(admin_username, password=admin_password, email=admin_email, role_name='admin')
            if created_user:
                logger.info(f"{log_prefix} Admin user '{admin_username}' created successfully.")
            else:
                logger.error(f"{log_prefix} Failed to create admin user '{admin_username}' (auth_service returned None).")
                raise RuntimeError(f"Failed to create admin user '{admin_username}'.")
        else:
            logger.debug(f"{log_prefix} Admin user '{admin_username}' already exists.")
            admin_role = role_model.get_role_by_name('admin')
            if admin_role and existing_admin.role_id != admin_role.id:
                 logger.warning(f"{log_prefix} Existing admin user '{admin_username}' has incorrect role ID ({existing_admin.role_id}). Consider updating manually.")
            if not existing_admin.email:
                 logger.warning(f"{log_prefix} Existing admin user '{admin_username}' is missing an email address. Consider updating manually.")
    except AuthServiceError as ase:
         logger.error(f"{log_prefix} Failed to create admin user: {ase}")
         raise RuntimeError(f"Failed to create admin user: {ase}") from ase
    except Exception as e:
        logger.error(f"{log_prefix} Unexpected error creating admin user: {e}", exc_info=True)
        raise RuntimeError(f"Unexpected error creating admin user: {e}") from e

def run_initialization_sequence(app: Flask) -> None:
    """
    Runs the full initialization sequence: schema, roles, admin user.
    Requires app context. Should only be run by one process.
    """
    log_prefix = "[INIT:Sequence]"
    logger.info(f"{log_prefix} Starting one-time initialization sequence...")
    try:
        with app.app_context():
            initialize_database_schema()
            create_default_roles()
            create_initial_admin()
        logger.info(f"{log_prefix} One-time initialization sequence completed successfully.")
    except Exception as e:
        logger.critical(f"{log_prefix} One-time initialization sequence FAILED: {e}", exc_info=True)
        raise
