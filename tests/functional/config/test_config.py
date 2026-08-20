# tests/functional/config/test_config.py

import os
import os

class TestConfig:
    """
    Test configuration settings.
    """
    TESTING = True
    
    # Load API keys directly from environment for tests
    ASSEMBLYAI_API_KEY = os.environ.get('ASSEMBLYAI_API_KEY')
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
    GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
    ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
    OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY')
    OPENROUTER_BASE_URL = 'https://openrouter.ai/api/v1'

    # Disable CSRF protection for tests
    WTF_CSRF_ENABLED = False
    
    # Use a less intensive hashing algorithm for tests
    BCRYPT_LOG_ROUNDS = 4
    
    # Disable rate limiting for tests
    RATELIMIT_ENABLED = False
    RATELIMIT_DEFAULT = "1000 per minute"
    
    # Use a dedicated test database
    MYSQL_USER = os.environ.get('MYSQL_TEST_USER', 'test')
    MYSQL_PASSWORD = os.environ.get('MYSQL_TEST_PASSWORD', 'test')
    MYSQL_DB = os.environ.get('MYSQL_TEST_DB', 'test_db')
    # When running in Docker, connect to the mysql-test service
    # When running locally, connect to the published port
    MYSQL_HOST = os.environ.get('MYSQL_TEST_HOST', 'mysql-test')
    MYSQL_PORT = int(os.environ.get('MYSQL_TEST_PORT', '3306'))
    
    # Override the database config dictionary
    MYSQL_CONFIG = {
        'host': MYSQL_HOST,
        'port': MYSQL_PORT,
        'user': MYSQL_USER,
        'password': MYSQL_PASSWORD,
        'database': MYSQL_DB,
        'pool_name': 'transcriber_test_pool',
        'pool_size': 5
    }
    
    # --- Add other necessary overrides from base config to avoid errors ---
    SECRET_KEY = 'a-super-secret-key-for-testing'
    DEPLOYMENT_MODE = 'multi'
    LOG_DIR = '/tmp'
    LOG_FILE = '/tmp/test_app.log'
    RATELIMIT_STORAGE_URI = "memory://"
    BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    RUNTIME_DIR = os.path.join(BASE_DIR, 'runtime_test')
    TEMP_UPLOADS_DIR = os.path.join(RUNTIME_DIR, 'test_uploads')
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    os.makedirs(TEMP_UPLOADS_DIR, exist_ok=True)

    # --- FIX: Explicitly define lock and marker files for tests ---
    TASK_LOCK_FILE = os.path.join(RUNTIME_DIR, 'transcriber_task.lock')
    INIT_MARKER_FILE = os.path.join(RUNTIME_DIR, '.initialized')
    
    # Simplified provider/language config for tests
    TRANSCRIPTION_PROVIDERS = ["assemblyai", "openai", "openrouter"]
    LLM_PROVIDERS = ["GEMINI", "OPENAI", "OPENROUTER"]
    OPENROUTER_MODELS = ['google/gemini-3.7-flash']
    DEFAULT_TRANSCRIPTION_PROVIDER = 'openai'
    LLM_PROVIDER = 'GEMINI'
    SUPPORTED_LANGUAGES = ['en', 'es', 'fr', 'nl']
    SUPPORTED_LANGUAGE_NAMES = {
        'auto': 'Automatic Detection',
        'en': 'English',
        'es': 'Spanish',
        'fr': 'French',
        'nl': 'Dutch',
    }
    DEFAULT_LANGUAGE = 'en'
    MAIL_DEFAULT_SENDER = 'test@example.com'
    GOOGLE_CLIENT_ID = None
    SERVER_NAME = 'localhost'
