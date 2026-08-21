# tests/functional/api/test_llm_api.py
# Contains functional tests for the LLM API endpoints.

import pytest
from unittest.mock import MagicMock

# --- Fixtures ---

@pytest.fixture(autouse=True)
def mock_llm_dependencies(monkeypatch):
    """Mocks all external dependencies for the LLM API tests."""
    # Mock llm_operation_model
    mock_llm_op_model = MagicMock()
    monkeypatch.setattr('app.api.llm.llm_operation_model', mock_llm_op_model)

    yield {
        "llm_operation_model": mock_llm_op_model
    }

# --- Test Cases ---

# --- Tests for /operations/<id>/status endpoint ---

def test_get_llm_operation_status_success(logged_in_client, mock_llm_dependencies):
    """
    GIVEN a logged-in user and a valid operation ID they own
    WHEN the /api/llm/operations/<id>/status endpoint is called
    THEN it should return a 200 OK with the operation status.
    """
    mock_op = {
        'status': 'finished',
        'result': 'Final result',
        'error': None,
        'provider': 'test_provider',
        'operation_type': 'test_op',
        'created_at': '2023-01-01T00:00:00',
        'completed_at': '2023-01-01T00:01:00',
        'transcription_id': 1,
        'prompt_id': 1
    }
    mock_llm_dependencies['llm_operation_model'].get_llm_operation_by_id.return_value = mock_op

    response = logged_in_client.get('/api/llm/operations/123/status')

    assert response.status_code == 200
    json_data = response.get_json()
    assert json_data['operation_id'] == 123
    assert json_data['status'] == 'finished'
    assert json_data['result'] == 'Final result'
    mock_llm_dependencies['llm_operation_model'].get_llm_operation_by_id.assert_called_with(123, 1) # user_id=1 from logged_in_client

def test_get_llm_operation_status_not_found(logged_in_client, mock_llm_dependencies):
    """
    GIVEN a logged-in user
    WHEN the /api/llm/operations/<id>/status endpoint is called with an unknown ID
    THEN it should return a 404 Not Found.
    """
    mock_llm_dependencies['llm_operation_model'].get_llm_operation_by_id.return_value = None

    response = logged_in_client.get('/api/llm/operations/999/status')

    assert response.status_code == 404
    json_data = response.get_json()
    assert 'error' in json_data
    assert json_data['error'] == "We could not find that AI operation."

def test_get_llm_operation_status_access_denied(logged_in_client, mock_llm_dependencies):
    """
    GIVEN a logged-in user
    WHEN they request an operation they do not own
    THEN it should return a 403 Forbidden.
    """
    # Simulate finding the operation without user_id, but not with it
    mock_llm_dependencies['llm_operation_model'].get_llm_operation_by_id.side_effect = [
        None, # Call with user_id fails
        {'id': 456} # Call without user_id succeeds
    ]

    response = logged_in_client.get('/api/llm/operations/456/status')

    assert response.status_code == 403

def test_get_llm_operation_status_requires_login(client):
    """
    GIVEN no logged-in user
    WHEN the /api/llm/operations/<id>/status endpoint is called
    THEN it should return a 401 Unauthorized.
    """
    response = client.get('/api/llm/operations/123/status')
    assert response.status_code == 401
