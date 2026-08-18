# app/main/routes.py
# Defines the routes for the main application pages (e.g., the homepage).

import logging
import math
from flask import render_template, current_app, g, abort, request, redirect, session, url_for
from flask_login import current_user, login_required

# Import the main blueprint defined in app/main/__init__.py
from . import main_bp
from app.models import transcription_utils
from app.models import transcription_catalog as transcription_catalog_model
# --- ADDED: Import llm_operation model ---
from app.models import llm_operation as llm_operation_model
# --- END ADDED ---
# Import services for prompt matching
from app.services import user_service, admin_management_service
from app.core.decorators import check_permission

@main_bp.route('/')
@login_required
def index():
    """
    Renders the main index page (index.html).
    Passes necessary configuration and context to the template.
    Determines effective defaults based on user preferences or system config.
    Handles pagination for transcription history.
    Augments transcription data with the latest finished/errored LLM operation info.
    Adds a flag indicating if title polling should occur for each transcription.
    """
    log_prefix = "[ROUTE:Main:Index]"
    user_info = f"User:{current_user.id}" if current_user.is_authenticated else "Anonymous"
    logging.debug(f"{log_prefix} Accessing route '/'. ({user_info})")

    # --- Determine Effective Defaults ---
    catalog_models = transcription_catalog_model.get_active_models()
    logging.debug(f"{log_prefix} Loaded {len(catalog_models)} active transcription models from catalog.")
    active_model_codes = {model['code'] for model in catalog_models}

    supported_languages = transcription_catalog_model.get_language_map()
    effective_default_language = (
        transcription_catalog_model.get_default_language_code()
        or current_app.config.get('DEFAULT_LANGUAGE')
        or 'auto'
    )
    effective_default_api = (
        transcription_catalog_model.get_default_model_code()
        or current_app.config.get('DEFAULT_TRANSCRIPTION_PROVIDER')
    )
    effective_openrouter_model = None

    if current_user.is_authenticated:
        if current_user.default_content_language:
            if current_user.default_content_language in supported_languages:
                effective_default_language = current_user.default_content_language
                logging.debug(f"{log_prefix} Using user's default language: {effective_default_language}")
            else:
                logging.warning(
                    f"{log_prefix} User's preferred language '{current_user.default_content_language}' "
                    f"is not currently available. Using catalog default: {effective_default_language}"
                )
        else:
            logging.debug(f"{log_prefix} User has no language preference, using system default: {effective_default_language}")

        if current_user.default_transcription_model:
            if current_user.default_transcription_model in active_model_codes:
                effective_default_api = current_user.default_transcription_model
                logging.debug(f"{log_prefix} Using user's default model: {effective_default_api}")
            else:
                logging.warning(
                    f"{log_prefix} User's preferred model '{current_user.default_transcription_model}' "
                    f"is not currently allowed. Falling back to catalog default: {effective_default_api}"
                )
        else:
            logging.debug(f"{log_prefix} User has no model preference, using system default: {effective_default_api}")

        effective_openrouter_model = user_service.resolve_effective_openrouter_model(current_user)
    else:
        logging.debug(f"{log_prefix} Using system defaults (Lang: {effective_default_language}, API: {effective_default_api})")
    # --- End Determine Effective Defaults ---

    # --- Pagination Logic ---
    page = request.args.get('page', 1, type=int)
    per_page = 5
    pagination = None
    transcriptions = []
    all_prompts_lookup = {} # Dictionary to store prompts for quick lookup

    try:
        # Fetch user and template prompts for workflow matching
        if current_user.has_permission('allow_workflows'):
            try:
                user_prompts = user_service.get_user_prompts(current_user.id)
                # --- MODIFIED: Fetch all template prompts, regardless of user's current default language ---
                # This ensures that if a workflow was run with a template (e.g., specific language, or "all languages")
                # it can still be found for display even if the user's current default language is different.
                template_prompts = admin_management_service.get_template_prompts(language=None)
                # --- END MODIFIED ---
                for p in user_prompts:
                    all_prompts_lookup[p.id] = {'title': p.title, 'color': p.color} # Use raw ID as key
                for t_template in template_prompts:
                    # Use a prefixed key for templates to avoid collision with user prompt IDs
                    all_prompts_lookup[f"template_{t_template.id}"] = {'title': t_template.title, 'color': t_template.color}
                logging.debug(f"{log_prefix} Loaded {len(user_prompts)} user prompts and {len(template_prompts)} template prompts for matching.")
            except Exception as prompt_err:
                logging.error(f"{log_prefix} Error fetching user/template prompts: {prompt_err}", exc_info=True)

        # Fetch paginated transcriptions (now includes llm_operation_id)
        total_items = transcription_utils.count_visible_user_transcriptions(current_user.id)
        if total_items > 0:
            total_pages = math.ceil(total_items / per_page)
            page = max(1, min(page, total_pages))
            transcriptions = transcription_utils.get_paginated_transcriptions(current_user.id, page, per_page)

            user_can_poll_title = current_user.enable_auto_title_generation and current_user.has_permission('allow_auto_title_generation')
            for t in transcriptions:
                title_status = t.get('title_generation_status', 'pending')
                t['should_poll_title'] = user_can_poll_title and title_status in ['pending', 'processing']

                llm_op_id = t.get('llm_operation_id')
                if llm_op_id:
                    llm_op = llm_operation_model.get_llm_operation_by_id(llm_op_id, current_user.id)
                    if llm_op:
                        t['llm_operation_status'] = llm_op.get('status')
                        t['llm_operation_result'] = llm_op.get('result')
                        t['llm_operation_error'] = llm_op.get('error')
                        t['llm_operation_ran_at'] = llm_op.get('completed_at')
                        t['llm_operation_prompt'] = llm_op.get('input_text')
                        t['llm_operation_prompt_id'] = llm_op.get('prompt_id') # This is the raw ID
                        t['llm_operation_provider'] = llm_op.get('provider')

                        prompt_key_to_lookup = t['llm_operation_prompt_id']
                        matched_prompt = None
                        if prompt_key_to_lookup is not None:
                            # Try matching as a user prompt ID (integer)
                            matched_prompt = all_prompts_lookup.get(prompt_key_to_lookup)
                            if not matched_prompt:
                                # Try matching as a template prompt ID (string "template_ID")
                                matched_prompt = all_prompts_lookup.get(f"template_{prompt_key_to_lookup}")
                        
                        if matched_prompt:
                            t['matched_workflow_title'] = matched_prompt['title']
                            t['matched_workflow_color'] = matched_prompt['color']
                        else:
                            t['matched_workflow_title'] = None
                            t['matched_workflow_color'] = None
                    else:
                        logging.warning(f"{log_prefix} Could not fetch details for LLM operation ID {llm_op_id} linked to transcription {t['id']}.")
                        t['llm_operation_status'] = 'error'
                        t['llm_operation_error'] = 'Could not load workflow details.'
                else: # No llm_operation_id on the transcription record
                    # This handles cases where a workflow was pre-applied but hasn't fully processed
                    # to link the llm_operation_id back to the transcription record yet,
                    # or if the transcription failed before the workflow could run.
                    # We use the 'pending_workflow_...' fields from the transcription table.
                    t['llm_operation_status'] = t.get('llm_operation_status', 'idle') # Use existing status if available, else idle
                    if t.get('pending_workflow_prompt_text') and t['llm_operation_status'] == 'idle':
                        # If there was a pending workflow and status is still idle, it implies it hasn't started or linked yet.
                        # We can infer 'pending' or 'processing' if we had more info, but for display, use pending details.
                        t['llm_operation_status'] = 'pending' # Or 'processing' if more appropriate
                    
                    t['llm_operation_prompt'] = t.get('pending_workflow_prompt_text')
                    t['matched_workflow_title'] = t.get('pending_workflow_prompt_title')
                    t['matched_workflow_color'] = t.get('pending_workflow_prompt_color')
                    
                    # These are null if no pre-applied workflow or if workflow ran and llm_op_id is set
                    t['llm_operation_result'] = None
                    t['llm_operation_error'] = None
                    t['llm_operation_ran_at'] = None
                    t['llm_operation_prompt_id'] = None 
                    t['llm_operation_provider'] = None


            if total_pages > 1:
                pagination = {
                    'current_page': page, 'total_pages': total_pages,
                    'has_prev': page > 1, 'has_next': page < total_pages,
                    'total_items': total_items, 'per_page': per_page
                }
                logging.debug(f"{log_prefix} Pagination active: Page {page}/{total_pages}, Total Items: {total_items}")
            else:
                 logging.debug(f"{log_prefix} Pagination not needed: Total Items: {total_items}")
        else:
             logging.debug(f"{log_prefix} No visible transcriptions found for user.")

    except Exception as e:
        logging.error(f"{log_prefix} Error fetching paginated transcriptions: {e}", exc_info=True)

    return render_template(
        'index.html',
        effective_default_api=effective_default_api,
        effective_openrouter_model=effective_openrouter_model,
        effective_default_language=effective_default_language,
        supported_languages=supported_languages,
        transcriptions=transcriptions,
        pagination=pagination
    )


@main_bp.route('/live')
@login_required
def live():
    """Render the full-screen live transcription workspace."""
    if (
        current_app.config['DEPLOYMENT_MODE'] == 'multi'
        and not (
            check_permission(current_user, 'use_api_openai')
            or check_permission(current_user, 'use_api_openai_live_transcribe')
        )
    ):
        abort(403)
    return render_template(
        'live.html',
        live_languages=transcription_catalog_model.get_language_map(),
        live_transcription_models=[
            {
                'code': model_code,
                'display_name': current_app.config.get('API_PROVIDER_NAME_MAP', {}).get(model_code, model_code),
                'provider': current_app.config.get('LIVE_TRANSCRIPTION_PROVIDERS', {}).get(model_code, 'openai'),
            }
            for model_code in current_app.config.get('LIVE_TRANSCRIPTION_MODELS', [])
        ],
    )

@main_bp.route('/manage-prompts')
@login_required
def manage_prompts():
    """Renders the page for users to manage their saved workflow prompts."""
    log_prefix = f"[ROUTE:Main:ManagePrompts:User:{current_user.id}]"
    logging.debug(f"{log_prefix} Accessing route '/manage-prompts'.")

    if not current_user.has_permission('allow_workflows'):
        logging.warning(f"{log_prefix} Access denied: User lacks 'allow_workflows' permission.")
        abort(403)

    return render_template('manage_prompts.html', title="Manage Workflow Prompts")

@main_bp.route('/set-language/<lang>')
def set_language(lang):
    """Sets the language for the user's session."""
    log_prefix = "[ROUTE:Main:SetLanguage]"
    # Validate the language code against the supported languages
    if lang in current_app.config['SUPPORTED_LANGUAGES']:
        session['language'] = lang
        logging.info(f"{log_prefix} Language set to '{lang}' in session.")
    else:
        logging.warning(f"{log_prefix} Attempted to set unsupported language: '{lang}'.")

    # Redirect back to the page the user came from, or to the index page as a fallback
    referrer = request.referrer
    if referrer:
        return redirect(referrer)
    return redirect(url_for('main.index'))
