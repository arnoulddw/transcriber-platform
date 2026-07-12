# tests/conftest.py

import pytest
import os
import sys
from dotenv import load_dotenv

# Ensure project root (one level up from tests/) is on sys.path so 'app' package imports work
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Run transcription-related suites before admin ones to avoid inter-test interference.
_PRIORITY_PREFIXES = (
    "tests/functional/services/test_transcription_service.py",
    "tests/functional/test_transcription_management.py",
)


def pytest_collection_modifyitems(session, config, items):
    prioritized = []
    remaining = []
    for item in items:
        if item.nodeid.startswith(_PRIORITY_PREFIXES):
            prioritized.append(item)
        else:
            remaining.append(item)
    items[:] = prioritized + remaining

def pytest_configure(config):
    """
    Load environment variables from .env file before any tests are run.
    This ensures that the configuration is available when modules are imported.
    """
    dotenv_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.env'))
    if os.path.exists(dotenv_path):
        # Use override=True to ensure .env settings take precedence
        load_dotenv(dotenv_path=dotenv_path, override=True)
        print(f"Loaded .env file from: {dotenv_path}")
    else:
        print(f"Warning: .env file not found at {dotenv_path}")



@pytest.fixture(scope='function')
def clean_db(app):
    """A fixture to clean the database before each test."""
    with app.app_context():
        from app.database import get_db
        cursor = get_db().cursor()
        cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
        # List of all tables to be truncated
        tables = [
            "user_prompts", "template_prompts", "llm_operations", "transcription_job_leases",
            "transcriptions", "user_usage", "user_api_keys", "users", "roles"
        ]
        for table in tables:
            cursor.execute(f"TRUNCATE TABLE {table}")
        cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
        get_db().commit()

        # Clear any caches
        from app.extensions import cache
        cache.clear()

@pytest.fixture(scope='function')
def client(app):
    """A test client for the app."""
    return app.test_client()
