# app/forms.py
# Defines WTForms classes for user input validation and CSRF protection.

import logging

from flask_babel import lazy_gettext as _
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField, SelectField, ValidationError, EmailField, TextAreaField, IntegerField, HiddenField, FloatField
# --- MODIFIED: Removed Optional validator ---
from wtforms.validators import DataRequired, Length, EqualTo, Email, NumberRange
# --- END MODIFIED ---
from flask_login import current_user
from flask import current_app

from app.models import transcription_catalog as transcription_catalog_model
from app.models import llm_catalog as llm_catalog_model
try:
    from .models.user import get_user_by_username, get_user_by_email
    from .models.role import get_role_by_name
except ImportError:
    logging.warning("[FORMS] Could not import user/role model functions. Validation might fail.")
    get_user_by_username = None
    get_user_by_email = None
    get_role_by_name = None

# --- RegistrationForm, LoginForm, ForgotPasswordForm, ResetPasswordForm remain unchanged ---
class RegistrationForm(FlaskForm):
    username = StringField(
        _('Username'),
        validators=[
            DataRequired(message="Username is required."),
            Length(min=4, max=25, message="Username must be between 4 and 25 characters.")
        ]
    )
    email = EmailField(
        _('Email'),
        validators=[
            DataRequired(message="Email is required."),
            Email(message="Invalid email address.")
        ]
    )
    password = PasswordField(
        _('Password'),
        validators=[
            DataRequired(message="Password is required."),
            Length(min=8, message="Password must be at least 8 characters long.")
        ]
    )
    confirm_password = PasswordField(
        _('Confirm Password'),
        validators=[
            DataRequired(message="Please confirm your password."),
            EqualTo('password', message='Passwords must match.')
        ]
    )
    submit = SubmitField(_('Register'))



class LoginForm(FlaskForm):
    username = StringField(
        _('Username'),
        validators=[DataRequired(message="Username is required.")]
    )
    password = PasswordField(
        _('Password'),
        validators=[DataRequired(message="Password is required.")]
    )
    remember_me = BooleanField(_('Remember Me'))

    submit = SubmitField(_('Login'))


class ApiKeyForm(FlaskForm):
    service = SelectField(
        _('API Service'),
        choices=[
            ('', '-- Select Service --'),
            ('openai', 'OpenAI (Whisper/GPT-4o)'),
            ('assemblyai', 'AssemblyAI (Universal)'),
            ('gemini', 'Google (Gemini)'), # Added Gemini
            ('openrouter', 'OpenRouter')
        ],
        validators=[DataRequired(message="Please select an API service.")]
    )
    api_key = StringField(
        _('API Key'),
        validators=[
            DataRequired(message="API Key is required."),
            Length(min=10, message="API Key seems too short.") # Basic length check
        ]
    )
    submit = SubmitField(_('Save API Key'))


class ForgotPasswordForm(FlaskForm):
    email = EmailField(
        _('Email'),
        validators=[
            DataRequired(message="Please enter your registered email address."),
            Email(message="Invalid email address.")
        ]
    )
    submit = SubmitField(_('Send Reset Link'))

class ResetPasswordForm(FlaskForm):
    password = PasswordField(
        _('New Password'),
        validators=[
            DataRequired(message="Password is required."),
            Length(min=8, message="Password must be at least 8 characters long.")
        ]
    )
    confirm_password = PasswordField(
        _('Confirm New Password'),
        validators=[
            DataRequired(message="Please confirm your new password."),
            EqualTo('password', message='Passwords must match.')
        ]
    )
    submit = SubmitField(_('Reset Password'))


class UserProfileForm(FlaskForm):
    username = StringField(
        _('Username'),
        validators=[
            DataRequired(message="Username is required."),
            Length(min=4, max=25, message="Username must be between 4 and 25 characters.")
        ]
    )
    email = EmailField(
        _('Email Address'),
        validators=[
            DataRequired(message="Email is required."),
            Email(message="Invalid email address.")
        ]
    )
    first_name = StringField(_('First Name'), validators=[Length(max=100)]) # Optional handled by form processing
    last_name = StringField(_('Last Name'), validators=[Length(max=100)]) # Optional handled by form processing

    default_content_language = SelectField(
        _('Default Transcription Language'),
        validators=[] # Optional handled by form processing
    )
    default_transcription_model = SelectField(
        _('Default Transcription Model'),
        validators=[] # Optional handled by form processing
    )
    # --- NEW: Add UI language field ---
    language = SelectField(_('Interface Language'), validators=[])
    # --- END NEW ---
    enable_auto_title_generation = BooleanField(_('Automatically Generate Titles for Transcriptions'))

    def __init__(self, *args, **kwargs):
        super(UserProfileForm, self).__init__(*args, **kwargs)
        # --- MODIFICATION START: Task 1 ---
        lang_choices = [] # Removed ('', '-- Use System Default --')
        # --- MODIFICATION END ---
        try:
            catalog_languages = transcription_catalog_model.get_active_languages()
        except Exception as catalog_err:
            logging.warning(f"[FORMS] Failed to load languages from catalog: {catalog_err}", exc_info=True)
            catalog_languages = []
        if catalog_languages:
            auto_entry = next((lang for lang in catalog_languages if lang['code'] == 'auto'), None)
            if auto_entry:
                lang_choices.append((auto_entry['code'], auto_entry['display_name']))
            for lang in sorted(catalog_languages, key=lambda item: item['display_name']):
                if lang['code'] != 'auto':
                    lang_choices.append((lang['code'], lang['display_name']))
        else:
            supported_langs = current_app.config.get('SUPPORTED_LANGUAGE_NAMES', {})
            sorted_langs = sorted(supported_langs.items(), key=lambda item: item[1])
            if 'auto' in supported_langs:
                lang_choices.append(('auto', supported_langs['auto']))
                sorted_langs = [(code, name) for code, name in sorted_langs if code != 'auto']
            lang_choices.extend(sorted_langs)
        self.default_content_language.choices = lang_choices

        # --- MODIFICATION START: Task 1 ---
        model_choices = []
        try:
            catalog_models = transcription_catalog_model.get_active_models()
        except Exception as catalog_err:
            logging.warning(f"[FORMS] Failed to load transcription models from catalog: {catalog_err}", exc_info=True)
            catalog_models = []
        for model in catalog_models:
            permission_key = model.get('permission_key')
            if not permission_key or (current_user.is_authenticated and current_user.has_permission(permission_key)):
                model_choices.append((model['code'], model['display_name']))
        # --- MODIFICATION END ---
        self.default_transcription_model.choices = model_choices

        # --- NEW: Populate UI language choices ---
        ui_lang_choices = []
        supported_ui_langs = current_app.config.get('SUPPORTED_LANGUAGES', [])
        ui_lang_names = {'en': 'English', 'es': 'Español', 'fr': 'Français', 'nl': 'Nederlands'}
        for lang_code in supported_ui_langs:
            ui_lang_choices.append((lang_code, ui_lang_names.get(lang_code, lang_code)))
        self.language.choices = ui_lang_choices
        # --- END NEW ---

    def validate_username(self, username_field):
        if current_user and username_field.data != current_user.username:
            if get_user_by_username:
                user = get_user_by_username(username_field.data)
                if user:
                    raise ValidationError('That username is already taken. Please choose a different one.')
            else:
                import logging
                logging.error("[FORMS] Cannot validate username uniqueness because get_user_by_username failed to import.")

    def validate_email(self, email_field):
        if current_user and email_field.data != current_user.email:
            if get_user_by_email:
                user = get_user_by_email(email_field.data)
                if user:
                    raise ValidationError('That email address is already registered. Please use a different one.')
            else:
                import logging
                logging.error("[FORMS] Cannot validate email uniqueness because get_user_by_email failed to import.")


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField(
        _('Current Password'),
        validators=[DataRequired(message="Current password is required.")]
    )
    new_password = PasswordField(
        _('New Password'),
        validators=[
            DataRequired(message="New password is required."),
            Length(min=8, message="Password must be at least 8 characters long.")
        ]
    )
    confirm_new_password = PasswordField(
        _('Confirm New Password'),
        validators=[
            DataRequired(message="Please confirm your new password."),
            EqualTo('new_password', message='New passwords must match.')
        ]
    )

# --- Admin Role Form ---
class AdminRoleForm(FlaskForm):
    """Form for creating or editing roles in the admin panel."""
    name = StringField(
        _('Role Name'),
        validators=[
            DataRequired(message="Role name is required."),
            Length(min=3, max=80, message="Role name must be between 3 and 80 characters.")
        ]
    )
    description = TextAreaField(
        _('Description'),
        validators=[Length(max=500)] # Optional handled by form processing
    )

    default_transcription_model = SelectField(
        _('Default Transcription Model'),
        validators=[],
        choices=[],
        validate_choice=False
    )
    default_title_generation_model = SelectField(
        _('Default Title Generation Model'),
        validators=[],
        choices=[],
        validate_choice=False
    )
    default_workflow_model = SelectField(
        _('Default Workflow Model'),
        validators=[],
        choices=[],
        validate_choice=False
    )

    # Permissions (Boolean Fields)
    use_api_assemblyai = BooleanField(_('Use AssemblyAI API'))
    use_api_openai_whisper = BooleanField(_('Use OpenAI Whisper API'))
    use_api_openai_gpt_4o_transcribe = BooleanField(_('Use OpenAI GPT-4o Transcribe API'))
    use_api_openai_live_transcribe = BooleanField(_('Use OpenAI Live Transcription'))
    # --- MODIFIED: Add use_api_google_gemini field ---
    use_api_google_gemini = BooleanField(_('Use Google Gemini API'))
    use_api_openrouter = BooleanField(_('Use OpenRouter API'))
    # --- END MODIFIED ---
    access_admin_panel = BooleanField(_('Access Admin Panel'))
    allow_large_files = BooleanField(_('Allow Large Files (>25MB)'))
    allow_context_prompt = BooleanField(_('Allow Context Prompt'))
    allow_api_key_management = BooleanField(_('Allow User API Key Management'))
    allow_public_api_access = BooleanField(_('Allow Public API Access'))
    allow_download_transcript = BooleanField(_('Allow Transcript Download'))
    allow_workflows = BooleanField(_('Allow Workflows'))
    manage_workflow_templates = BooleanField(_('Manage Workflow Templates (Admin)'))
    allow_auto_title_generation = BooleanField(_('Allow Automatic Title Generation'))
    allow_speaker_diarization = BooleanField(_('Allow Speaker Diarization (AssemblyAI)'))

    # Limits
    limit_daily_cost = FloatField(_('Daily Quota'), validators=[NumberRange(min=0)], default=0.0)
    limit_weekly_cost = FloatField(_('Weekly Quota'), validators=[NumberRange(min=0)], default=0.0)
    limit_monthly_cost = FloatField(_('Monthly Quota'), validators=[NumberRange(min=0)], default=0.0)
    limit_daily_minutes = IntegerField(_('Daily Quota'), validators=[NumberRange(min=0)], default=0)
    limit_weekly_minutes = IntegerField(_('Weekly Quota'), validators=[NumberRange(min=0)], default=0)
    limit_monthly_minutes = IntegerField(_('Monthly Quota'), validators=[NumberRange(min=0)], default=0)
    limit_daily_workflows = IntegerField(_('Daily Quota'), validators=[NumberRange(min=0)], default=0)
    limit_weekly_workflows = IntegerField(_('Weekly Quota'), validators=[NumberRange(min=0)], default=0)
    limit_monthly_workflows = IntegerField(_('Monthly Quota'), validators=[NumberRange(min=0)], default=0)
    max_history_items = IntegerField(
        _('Max History Items'),
        validators=[NumberRange(min=0)], default=0
    )
    history_retention_days = IntegerField(
        _('History Retention Days'),
        validators=[NumberRange(min=0)], default=0
    )

    submit = SubmitField(_('Save Role'))

    def __init__(self, original_name=None, *args, **kwargs):
        super(AdminRoleForm, self).__init__(*args, **kwargs)
        self.original_name = original_name

        placeholder_choice = [('', '-- Use System Default --')]

        # Populate transcription model choices
        transcription_choices = list(placeholder_choice)
        try:
            catalog_models = transcription_catalog_model.get_active_models()
        except Exception as catalog_err:
            logging.warning(f"[FORMS] Failed to load transcription models from catalog for admin role form: {catalog_err}", exc_info=True)
            catalog_models = []

        seen_transcription_codes = set()
        for model in catalog_models:
            model_code = (model.get('code') or '').strip()
            if not model_code or model_code in seen_transcription_codes:
                continue
            display_name = model.get('display_name') or model_code
            transcription_choices.append((model_code, display_name))
            seen_transcription_codes.add(model_code)

        self._assign_choices(self.default_transcription_model, transcription_choices)

        # Populate LLM model choices (shared for title generation and workflows)
        llm_choices = list(placeholder_choice)
        seen_llm_models = set()
        try:
            catalog_llm_models = llm_catalog_model.get_active_models()
        except Exception as catalog_err:
            logging.warning(f"[FORMS] Failed to load LLM models from catalog for admin role form: {catalog_err}", exc_info=True)
            catalog_llm_models = []

        for model in catalog_llm_models:
            model_code = (model.get('code') or '').strip()
            if not model_code or model_code in seen_llm_models:
                continue
            permission_key = model.get('permission_key')
            if permission_key and current_user.is_authenticated and not current_user.has_permission(permission_key):
                continue
            display_name = model.get('display_name') or model_code
            llm_choices.append((model_code, display_name))
            seen_llm_models.add(model_code)

        # Assign choices to both LLM-related fields separately to avoid shared list mutations
        self._assign_choices(self.default_title_generation_model, list(llm_choices))
        self._assign_choices(self.default_workflow_model, list(llm_choices))

    @staticmethod
    def _assign_choices(field, choices):
        current_value = field.data or ''
        if current_value and not any(value == current_value for value, _ in choices):
            choices.append((current_value, current_value))
        field.choices = choices
        if current_value == '':
            field.data = ''

    def validate_name(self, name_field):
        if get_role_by_name:
            if name_field.data != self.original_name:
                role = get_role_by_name(name_field.data)
                if role:
                    raise ValidationError('That role name already exists. Please choose a different one.')
        else:
            import logging
            logging.error("[FORMS] Cannot validate role name uniqueness because get_role_by_name failed to import.")


class AdminTemplateWorkflowForm(FlaskForm):
    """Form for creating or editing template workflows in the admin panel."""
    title = StringField(
        _('Workflow Label'),
        validators=[
            DataRequired(message="Label is required."),
            Length(min=3, max=100, message="Label must be between 3 and 100 characters.")
        ]
    )
    prompt_text = TextAreaField(
        _('Workflow Prompt'),
        validators=[
            DataRequired(message="Prompt text is required."),
            Length(max=1000)
        ]
    )
    language = SelectField(
        _('Workflow Language'),
        validators=[]
    )
    color = HiddenField(_('Label Color'))
    submit = SubmitField(_('Save Workflow Template'))

    def __init__(self, *args, **kwargs):
        super(AdminTemplateWorkflowForm, self).__init__(*args, **kwargs)
        lang_choices = [('', 'All Languages')]
        try:
            catalog_languages = transcription_catalog_model.get_active_languages()
        except Exception as catalog_err:
            logging.warning(f"[FORMS] Failed to load languages from catalog for admin template workflow form: {catalog_err}", exc_info=True)
            catalog_languages = []
        if catalog_languages:
            sorted_langs = sorted(
                [(lang['code'], lang['display_name']) for lang in catalog_languages if lang['code'] != 'auto'],
                key=lambda item: item[1]
            )
            lang_choices.extend(sorted_langs)
        else:
            supported_langs = current_app.config.get('SUPPORTED_LANGUAGE_NAMES', {})
            sorted_langs = sorted(
                [(code, name) for code, name in supported_langs.items() if code != 'auto'],
                key=lambda item: item[1]
            )
            lang_choices.extend(sorted_langs)
        self.language.choices = lang_choices

    def validate_color(self, color_field):
        value = color_field.data
        if value and not value.startswith('#'):
            raise ValidationError('Invalid color format. Must start with #.')
        if value and len(value) != 7:
             raise ValidationError('Invalid color format. Must be # followed by 6 hex digits.')
        if not value:
            pass
