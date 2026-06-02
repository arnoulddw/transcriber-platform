# app/config.py
# Configuration settings loaded from environment variables.

import os
from dotenv import load_dotenv
from flask_babel import lazy_gettext

# Determine the absolute path of the project root directory
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# Load environment variables from .env file in the project root
dotenv_path = os.path.join(BASE_DIR, '.env')
load_dotenv(dotenv_path=dotenv_path)

class Config:
    """Base configuration class. Loads settings from environment variables."""

    BASE_DIR = BASE_DIR

    # --- Core App Settings ---
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY:
        raise ValueError("No SECRET_KEY set for Flask application. Please set it in .env")

    # --- Deployment Mode ---
    DEPLOYMENT_MODE = os.environ.get('DEPLOYMENT_MODE', 'multi').lower()
    if DEPLOYMENT_MODE not in ['single', 'multi']:
        raise ValueError(f"Invalid DEPLOYMENT_MODE: '{DEPLOYMENT_MODE}'. Must be 'single' or 'multi'.")

    # --- Timezone Setting ---
    TZ = os.environ.get('TZ', 'UTC')

    # --- API Keys (Used ONLY in Single-User mode for global access, or by services) ---
    ASSEMBLYAI_API_KEY = os.environ.get('ASSEMBLYAI_API_KEY')
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY') # Used for Whisper, GPT-4o Transcribe, and potentially OpenAI LLMs
    GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY') # Used for Gemini LLM
    ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY') # Placeholder for future LLM
    OPENAI_HTTP_TIMEOUT = 120

    # --- Provider Configuration (NEW) ---
    TRANSCRIPTION_PROVIDERS = os.environ.get('TRANSCRIPTION_PROVIDERS', "assemblyai,whisper,gpt-4o-transcribe").split(',')
    LLM_PROVIDERS = ["GEMINI", "OPENAI"]
    # Default providers
    DEFAULT_TRANSCRIPTION_PROVIDER = os.environ.get('DEFAULT_TRANSCRIPTION_PROVIDER', 'gpt-4o-transcribe')
    LLM_PROVIDER = os.environ.get('LLM_PROVIDER', 'GEMINI').upper()
    LLM_MODEL = os.environ.get('LLM_MODEL')
    TITLE_GENERATION_LLM_PROVIDER = os.environ.get('TITLE_GENERATION_LLM_PROVIDER', 'GEMINI').upper()
    WORKFLOW_LLM_PROVIDER = os.environ.get('WORKFLOW_LLM_PROVIDER', LLM_PROVIDER).upper()

    # --- MODIFIED: Add specific model configs and API provider map ---
    TITLE_GENERATION_LLM_MODEL = os.environ.get('TITLE_GENERATION_LLM_MODEL', 'gemma-4-26b-a4b-it')
    WORKFLOW_LLM_MODEL = os.environ.get('WORKFLOW_LLM_MODEL', LLM_MODEL)

    # --- NEW: Centralized model lists ---
    GEMINI_MODELS = os.environ.get('GEMINI_MODELS', 'gemini-2.0-flash,gemini-3.0-flash,gemma-4-26b-a4b-it').split(',')
    OPENAI_MODELS = os.environ.get('OPENAI_MODELS', 'gpt-4o').split(',')
    # --- END NEW ---

    API_PROVIDER_NAME_MAP = {
        # Transcription Providers
        "assemblyai": "AssemblyAI Universal",
        "whisper": "OpenAI Whisper",
        "gpt-4o-transcribe": "OpenAI GPT-4o Transcribe",
        # LLM Providers
        "GEMINI": "Google Gemini",
        "OPENAI": "OpenAI",
        # Specific Models (add more as needed for display)
        "gemini-2.0-flash": "Gemini 2.0 Flash",
        "gemini-3.0-flash": "Gemini 3.0 Flash",
        "gemma-4-26b-a4b-it": "Gemma 4 26B A4B",
        "gpt-4o": "OpenAI GPT-4o",
    }
    # --- END MODIFIED ---

    # --- Transcription Language Settings ---
    DEFAULT_LANGUAGE = os.environ.get('DEFAULT_LANGUAGE', 'auto')
    SUPPORTED_LANGUAGE_CODES = os.environ.get('SUPPORTED_LANGUAGE_CODES', 'en,nl,fr,es').split(',')
    SUPPORTED_LANGUAGE_NAMES = {
        'auto': lazy_gettext('Automatic Detection'),
        'en': lazy_gettext('English'),
        'nl': lazy_gettext('Dutch'),
        'fr': lazy_gettext('French'),
        'es': lazy_gettext('Spanish'),
        # Add more if SUPPORTED_LANGUAGE_CODES is extended
    }
    for code in SUPPORTED_LANGUAGE_CODES:
        if code != 'auto' and code not in SUPPORTED_LANGUAGE_NAMES:
             print(f"WARNING: Language code '{code}' in SUPPORTED_LANGUAGE_CODES is missing from SUPPORTED_LANGUAGE_NAMES in config.py.")

    # --- For Flask-Babel ---
    SUPPORTED_LANGUAGES = ['en', 'es', 'fr', 'nl'] # Example languages
    BABEL_DEFAULT_LOCALE = 'en'

    # --- Database (MySQL Configuration) ---
    MYSQL_HOST = os.environ.get('MYSQL_HOST', 'localhost')
    MYSQL_PORT = int(os.environ.get('MYSQL_PORT', 3306))
    MYSQL_USER = os.environ.get('MYSQL_USER')
    MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD')
    MYSQL_DB = os.environ.get('MYSQL_DB')

    if not all([MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB]):
        raise ValueError("Missing required MySQL configuration (MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB) in environment.")

    DEFAULT_POOL_SIZE = 10
    MYSQL_CONFIG = {
        'host': MYSQL_HOST,
        'port': MYSQL_PORT,
        'user': MYSQL_USER,
        'password': MYSQL_PASSWORD,
        'database': MYSQL_DB,
        'pool_name': 'transcriber_pool',
        'pool_size': int(os.environ.get('MYSQL_POOL_SIZE', DEFAULT_POOL_SIZE))
    }

    # --- File Storage ---
    TEMP_UPLOADS_DIR = os.path.join(BASE_DIR, 'uploads')
    DELETE_THRESHOLD = int(os.environ.get('DELETE_THRESHOLD', 24 * 60 * 60)) # 24 hours

    # --- Logging ---
    LOG_DIR = os.path.join(BASE_DIR, 'logs')
    LOG_FILE = os.path.join(LOG_DIR, 'app.log')
    # Default to DEBUG if FLASK_ENV is 'development', else default to INFO
    flask_env = os.environ.get('FLASK_ENV', 'production').lower()
    if flask_env == 'development':
        LOG_LEVEL = 'DEBUG'
    else:
        LOG_LEVEL = 'INFO'
    if LOG_LEVEL not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        raise ValueError(f"Invalid LOG_LEVEL: '{LOG_LEVEL}'. Must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL.")

    # --- Runtime State / Markers ---
    RUNTIME_DIR = os.path.join(BASE_DIR, 'runtime')
    TASK_LOCK_FILE = os.environ.get('TASK_LOCK_FILE', os.path.join(RUNTIME_DIR, 'transcriber_task.lock'))
    INIT_MARKER_FILE = os.path.join(RUNTIME_DIR, '.initialized')

    # --- Security Settings ---
    BCRYPT_LOG_ROUNDS = int(os.environ.get('BCRYPT_LOG_ROUNDS', 12))
    RATELIMIT_DEFAULT = os.environ.get('RATELIMIT_DEFAULT', "600 per minute;10000 per day")
    RATELIMIT_STORAGE_URI = "memory://" # Consider redis for production
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = DEPLOYMENT_MODE == 'multi'
    PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS = int(os.environ.get('PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS', 3600))


    # --- Admin User Credentials ---
    ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
    ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL')

    # --- File Upload Limits ---
    # Server-level limit (in bytes). Flask will reject requests larger than this with a 413 error.
    MAX_CONTENT_LENGTH = 200 * 1024 * 1024 # 200 MB
    # Application-level limit (in MB). Used for checks within the application logic.
    MAX_FILE_SIZE_MB = 200

    # --- Email Configuration ---
    MAIL_SERVER = os.environ.get('MAIL_SERVER')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'true').lower() in ['true', '1', 't']
    MAIL_USE_SSL = os.environ.get('MAIL_USE_SSL', 'false').lower() in ['true', '1', 't']
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER', 'noreply@example.com')
    MAIL_DEBUG = os.environ.get('MAIL_DEBUG', 'false').lower() in ['true', '1', 't']

    # --- OAuth Configuration ---
    GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
    if DEPLOYMENT_MODE == 'multi' and not GOOGLE_CLIENT_ID:
        print("WARNING: DEPLOYMENT_MODE is 'multi' but GOOGLE_CLIENT_ID is not set in .env. Google Sign-In will not work.")

    # --- Data Retention Configuration ---
    PHYSICAL_DELETION_DAYS = int(os.environ.get('PHYSICAL_DELETION_DAYS', 120)) # Days after soft-delete

    # --- Workflow Configuration ---
    WORKFLOW_MAX_OUTPUT_TOKENS = int(os.environ.get('WORKFLOW_MAX_OUTPUT_TOKENS', 1024))
    WORKFLOW_RATE_LIMIT = os.environ.get('WORKFLOW_RATE_LIMIT', '10 per hour')

    # --- NEW: Centralized API Limits ---
    API_LIMITS = {
        'gpt-4o-transcribe': {
            # OpenAI enforces a hard 1400 second cap on GPT-4o Transcribe uploads.
            # Stay slightly under the limit so borderline files are chunked automatically.
            'duration_s': 420,
            'size_mb': 25,
            'rate_limit_rpm': 500  # requests per minute
        },
        'whisper': {
            'duration_s': None,
            'size_mb': 25,
            'rate_limit_rpm': 50
        },
        'assemblyai': {
            'duration_s': None,
            'size_mb': None,
            'rate_limit_rpm': None  # Handled by SDK
        }
    }

    # --- Transcription Workers ---
    TRANSCRIPTION_WORKERS = int(os.environ.get('TRANSCRIPTION_WORKERS', 4))
    if TRANSCRIPTION_WORKERS <= 0:
        raise ValueError("TRANSCRIPTION_WORKERS must be a positive integer.")
    TRANSCRIPTION_SINGLE_FILE_MAX_RETRIES = 0


# --- Validation for new defaults (Moved outside the class) ---
# Access attributes via Config.AttributeName
if Config.DEFAULT_TRANSCRIPTION_PROVIDER not in Config.TRANSCRIPTION_PROVIDERS:
    raise ValueError(f"Invalid DEFAULT_TRANSCRIPTION_PROVIDER: '{Config.DEFAULT_TRANSCRIPTION_PROVIDER}'. Must be one of {Config.TRANSCRIPTION_PROVIDERS}.")
# Basic check for LLM provider prefix
if Config.LLM_PROVIDER not in Config.LLM_PROVIDERS:
    raise ValueError(f"Invalid LLM_PROVIDER: '{Config.LLM_PROVIDER}'. Must be one of {Config.LLM_PROVIDERS}.")
