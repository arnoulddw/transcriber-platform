# tests/functional/services/test_llm_service.py
# Contains functional tests for the LLM service.

import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from app.services import llm_service
from app.services.api_clients.exceptions import LlmConfigurationError, LlmApiError, LlmSafetyError
from app.services.pricing_service import PricingServiceError
from app.models.user import User

# --- Fixtures ---

@pytest.fixture
def mock_user(monkeypatch):
    """Fixture for a standard user, mocking the role property."""
    # Mock the 'role' property on the User class to prevent DB access
    mock_role = MagicMock()
    mock_role.has_permission.return_value = False
    monkeypatch.setattr(User, 'role', PropertyMock(return_value=mock_role))
    
    user = User(id=1, username='testuser', email='test@example.com', password_hash='hash', created_at=None, role_id=1)
    return user

@pytest.fixture
def mock_user_with_key_permission(monkeypatch):
    """Fixture for a user with API key management permissions, mocking the role property."""
    # Mock the 'role' property on the User class to prevent DB access
    mock_role = MagicMock()
    mock_role.has_permission.return_value = True
    monkeypatch.setattr(User, 'role', PropertyMock(return_value=mock_role))

    user = User(id=2, username='keyuser', email='key@example.com', password_hash='hash', created_at=None, role_id=2)
    return user

# --- Mocks ---

@pytest.fixture(autouse=True)
def mock_dependencies(monkeypatch, app):
    """Mocks all external dependencies for the llm_service."""
    # Mock get_llm_client
    mock_llm_client = MagicMock()
    mock_llm_client.model_name = 'gemini-3.0-flash'
    mock_llm_client.generate_text.return_value = "Mocked LLM response"
    mock_get_llm_client = MagicMock(return_value=mock_llm_client)
    monkeypatch.setattr('app.services.llm_service.get_llm_client', mock_get_llm_client)

    # Mock user_service
    mock_user_service = MagicMock()
    mock_user_service.get_decrypted_api_key.return_value = None # Default to no key
    mock_user_service.get_admin_decrypted_api_key.return_value = None # Default to no admin key
    monkeypatch.setattr('app.services.llm_service.user_service', mock_user_service)

    # Mock user_model
    mock_user_model = MagicMock()
    monkeypatch.setattr('app.services.llm_service.user_model', mock_user_model)

    # Mock pricing_service
    mock_pricing_service = MagicMock()
    mock_pricing_service.return_value = 0.001
    monkeypatch.setattr('app.services.llm_service.get_pricing_service_price', mock_pricing_service)

    # Mock llm_operation_model
    mock_llm_op_model = MagicMock()
    monkeypatch.setattr('app.services.llm_service.llm_operation_model', mock_llm_op_model)

    # Set config on the test app instance
    app.config['DEPLOYMENT_MODE'] = 'multi'
    app.config['GEMINI_API_KEY'] = 'global_gemini_key'
    app.config['OPENAI_API_KEY'] = 'global_openai_key'
    app.config['WORKFLOW_LLM_MODEL'] = 'gemini-3.0-flash'
    app.config['TITLE_GENERATION_LLM_MODEL'] = 'gemini-3.0-flash'

    # Yield the mocks in a dictionary for tests to access and modify
    yield {
        "get_llm_client": mock_get_llm_client,
        "llm_client": mock_llm_client,
        "user_service": mock_user_service,
        "user_model": mock_user_model,
        "pricing_service": mock_pricing_service,
        "llm_operation_model": mock_llm_op_model,
        "config": app.config
    }

# --- Test Cases ---

def test_generate_text_with_global_key(client, mock_dependencies):
    """
    GIVEN no user or a user without key permissions
    WHEN generate_text_via_llm is called
    THEN it should use the global API key.
    """
    with client.application.app_context():
        result = llm_service.generate_text_via_llm('openai', 'test prompt')
    assert result == "Mocked LLM response"
    mock_dependencies['get_llm_client'].assert_called_with('openai', 'global_openai_key', mock_dependencies['config'])

def test_generate_text_with_explicit_key(client, mock_dependencies):
    """
    GIVEN an explicit API key is provided
    WHEN generate_text_via_llm is called
    THEN it should use the explicit key, ignoring others.
    """
    with client.application.app_context():
        result = llm_service.generate_text_via_llm('openai', 'test prompt', api_key='explicit_key')
    assert result == "Mocked LLM response"
    mock_dependencies['get_llm_client'].assert_called_with('openai', 'explicit_key', mock_dependencies['config'])

def test_generate_text_with_user_key(client, mock_dependencies, mock_user_with_key_permission):
    """
    GIVEN a user with key permissions and a stored key
    WHEN generate_text_via_llm is called
    THEN it should use the user's API key.
    """
    mock_dependencies['user_model'].get_user_by_id.return_value = mock_user_with_key_permission
    mock_dependencies['user_service'].get_decrypted_api_key.return_value = 'user_openai_key'

    with client.application.app_context():
        result = llm_service.generate_text_via_llm('openai', 'test prompt', user_id=mock_user_with_key_permission.id)
    
    assert result == "Mocked LLM response"
    mock_dependencies['user_service'].get_decrypted_api_key.assert_called_with(mock_user_with_key_permission.id, 'openai')
    mock_dependencies['get_llm_client'].assert_called_with('openai', 'user_openai_key', mock_dependencies['config'])

def test_generate_text_fallback_to_global_key_when_user_key_missing(client, mock_dependencies, mock_user_with_key_permission):
    """
    GIVEN a user with key permissions but no stored key
    WHEN generate_text_via_llm is called
    THEN it should fall back to the global API key.
    """
    mock_dependencies['user_model'].get_user_by_id.return_value = mock_user_with_key_permission
    mock_dependencies['user_service'].get_decrypted_api_key.return_value = None # No key for user

    with client.application.app_context():
        result = llm_service.generate_text_via_llm('gemini', 'test prompt', user_id=mock_user_with_key_permission.id)
    
    assert result == "Mocked LLM response"
    mock_dependencies['user_service'].get_decrypted_api_key.assert_called_with(mock_user_with_key_permission.id, 'gemini')
    mock_dependencies['get_llm_client'].assert_called_with('gemini', 'global_gemini_key', mock_dependencies['config'])

def test_generate_text_fallback_to_global_key_when_permission_missing(client, mock_dependencies, mock_user):
    """
    GIVEN a user without key permissions
    WHEN generate_text_via_llm is called
    THEN it should not attempt to get a user key and use the global key.
    """
    mock_dependencies['user_model'].get_user_by_id.return_value = mock_user

    with client.application.app_context():
        result = llm_service.generate_text_via_llm('openai', 'test prompt', user_id=mock_user.id)

    assert result == "Mocked LLM response"
    # Ensure we never tried to fetch the user's key
    mock_dependencies['user_service'].get_decrypted_api_key.assert_not_called()
    mock_dependencies['get_llm_client'].assert_called_with('openai', 'global_openai_key', mock_dependencies['config'])


def test_generate_text_uses_admin_configured_key_when_user_cannot_manage_keys(
    client, mock_dependencies, mock_user
):
    """
    GIVEN a user without key-management permission and an admin-configured model key
    WHEN generate_text_via_llm is called for that model
    THEN it should use the admin key instead of requiring a user or global key.
    """
    mock_dependencies['user_model'].get_user_by_id.return_value = mock_user
    mock_dependencies['user_service'].get_admin_decrypted_api_key.return_value = 'admin_openai_key'

    with client.application.app_context():
        result = llm_service.generate_text_via_llm(
            'openai', 'test prompt', user_id=mock_user.id, model='gpt-4o-transcribe'
        )

    assert result == "Mocked LLM response"
    mock_dependencies['user_service'].get_admin_decrypted_api_key.assert_called_once_with(
        'openai', 'gpt-4o-transcribe'
    )
    mock_dependencies['get_llm_client'].assert_called_with(
        'openai', 'admin_openai_key', mock_dependencies['config']
    )


def test_generate_text_uses_global_openrouter_key_when_user_cannot_manage_keys(client, mock_dependencies, mock_user):
    mock_dependencies['user_model'].get_user_by_id.return_value = mock_user
    mock_dependencies['config']['OPENROUTER_API_KEY'] = 'global_openrouter_key'

    with client.application.app_context():
        result = llm_service.generate_text_via_llm(
            provider_name='OPENROUTER', prompt='Hi', user_id=mock_user.id
        )

    assert result == "Mocked LLM response"
    mock_dependencies['get_llm_client'].assert_called_with(
        'OPENROUTER', 'global_openrouter_key', mock_dependencies['config']
    )


def test_no_api_key_configured_raises_error(client, mock_dependencies):
    """
    GIVEN no global or user API key is available
    WHEN generate_text_via_llm is called
    THEN it should raise LlmConfigurationError.
    """
    # Remove global keys from the mocked config
    mock_dependencies['config']['OPENAI_API_KEY'] = None
    
    with client.application.app_context():
        with pytest.raises(LlmConfigurationError, match="API key for LLM provider 'openai' is not configured"):
            llm_service.generate_text_via_llm('openai', 'test prompt')

@pytest.mark.parametrize("provider,prompt", [
    (None, "prompt"),
    ("openai", None),
    ("", "prompt"),
    ("openai", "")
])
def test_missing_provider_or_prompt_raises_error(client, provider, prompt):
    """
    GIVEN a missing provider or prompt
    WHEN generate_text_via_llm is called
    THEN it should raise a ValueError.
    """
    with client.application.app_context():
        with pytest.raises(ValueError, match="Provider name and prompt are required."):
            llm_service.generate_text_via_llm(provider, prompt)

def test_llm_api_error_is_propagated(client, mock_dependencies):
    """
    GIVEN the LLM client raises an LlmApiError
    WHEN generate_text_via_llm is called
    THEN the error should be propagated.
    """
    mock_dependencies['llm_client'].generate_text.side_effect = LlmApiError("API limit reached", status_code=429)
    
    with client.application.app_context():
        with pytest.raises(LlmApiError, match="API limit reached"):
            llm_service.generate_text_via_llm('openai', 'test prompt')

def test_cost_calculation_is_triggered(client, mock_dependencies):
    """
    GIVEN an operation_id and operation_type are provided
    WHEN generate_text_via_llm completes successfully
    THEN it should call the pricing service and update the operation cost.
    """
    with client.application.app_context():
        llm_service.generate_text_via_llm(
            'gemini',
            'test prompt',
            operation_id=123,
            operation_type='workflow'
        )
    
    mock_dependencies['pricing_service'].assert_called_with(item_type='workflow', item_key='gemini-3.0-flash')
    mock_dependencies['llm_operation_model'].update_llm_operation_cost.assert_called_with(123, 0.001)

def test_cost_calculation_is_skipped_if_no_op_type(client, mock_dependencies):
    """
    GIVEN an operation_id is provided but operation_type is missing
    WHEN generate_text_via_llm completes successfully
    THEN it should NOT call the pricing service.
    """
    with client.application.app_context():
        llm_service.generate_text_via_llm(
            'gemini',
            'test prompt',
            operation_id=123
            # No operation_type
        )
    
    mock_dependencies['pricing_service'].assert_not_called()
    mock_dependencies['llm_operation_model'].update_llm_operation_cost.assert_not_called()
