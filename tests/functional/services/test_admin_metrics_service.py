# tests/functional/services/test_admin_metrics_service.py

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

from app.services import admin_metrics_service
from app.services.exceptions import AdminServiceError

# --- Test Data ---

MOCK_NOW = datetime.now(timezone.utc)

MOCK_TIME_PERIODS = {
    "24h": {"start": MOCK_NOW - timedelta(hours=24), "end": MOCK_NOW},
    "7d": {"start": MOCK_NOW - timedelta(days=7), "end": MOCK_NOW},
    "30d": {"start": MOCK_NOW - timedelta(days=30), "end MOCK_NOW": MOCK_NOW},
    "all": {"start": None, "end": None},
}

# --- Fixtures ---

@pytest.fixture
def mock_db_utils():
    """Mocks database utility functions to isolate service logic."""
    with patch('app.services.admin_metrics_service.user_utils', autospec=True) as mock_user_utils, \
         patch('app.services.admin_metrics_service.transcription_utils', autospec=True) as mock_transcription_utils, \
         patch('app.services.admin_metrics_service.display_mapping_service.get_workflow_model_display_map') as mock_workflow_display_map:
        
        # Default return values
        mock_user_utils.count_all_users.return_value = 100
        mock_user_utils.count_active_users_in_range.return_value = 10
        mock_user_utils.count_new_users_in_range.return_value = 5
        mock_user_utils.get_users_hitting_limits.return_value = [{'id': 1, 'username': 'testuser'}]

        mock_transcription_utils.count_jobs_in_range.return_value = 50
        mock_transcription_utils.sum_minutes_in_range.return_value = 120.5
        mock_transcription_utils.get_api_distribution_in_range.return_value = {'whisper': 30, 'assemblyai': 20}
        mock_transcription_utils.get_language_distribution_in_range.return_value = {'en': 40, 'es': 10}
        mock_transcription_utils.count_successful_title_generations_in_range.return_value = 25
        mock_transcription_utils.get_workflow_model_distribution.return_value = {'gemini-2.0-flash': 15}
        mock_transcription_utils.get_common_error_messages_in_range.return_value = [('Error A', 5)]
        mock_transcription_utils.get_common_workflow_error_messages.return_value = [('Workflow Error B', 3)]
        mock_transcription_utils.get_cost_analytics_by_component.return_value = {'transcriptions': 50.0, 'workflows': 25.0}
        mock_transcription_utils.get_cost_analytics_by_role.return_value = {'admin': {'total_cost': 75.0, 'user_count': 1}}
        mock_transcription_utils.count_workflow_jobs_with_filters.return_value = 2
        mock_workflow_display_map.return_value = {'gemini-2.0-flash': 'Gemini 2.0 Flash'}

        yield mock_user_utils, mock_transcription_utils

# --- Tests ---

def test_get_admin_dashboard_metrics_success(app, mock_db_utils):
    """
    Tests that dashboard metrics are fetched and calculated correctly.
    """
    with app.app_context():
        metrics = admin_metrics_service.get_admin_dashboard_metrics()

        assert metrics is not None
        assert not metrics['error']
        assert metrics['total_users'] == 100
        assert metrics['active_users']['24h'] == 10
        assert metrics['jobs_submitted']['7d'] > 0
        assert metrics['minutes_processed']['30d'] > 0
        assert 'error_rate' in metrics
        assert 'workflow_error_rate' in metrics

def test_get_usage_analytics_metrics_success(app, mock_db_utils):
    """
    Tests that usage analytics metrics are fetched and calculated correctly.
    """
    with app.app_context():
        metrics = admin_metrics_service.get_usage_analytics_metrics()

        assert metrics is not None
        assert not metrics['error']
        assert metrics['jobs_submitted']['24h'] > 0
        assert metrics['api_jobs_distribution']['7d']['whisper'] == 30
        assert metrics['language_distribution']['all']['en'] == 40
        assert 'context_prompt_usage' in metrics
        assert 'download_usage' in metrics

def test_get_user_insights_metrics_success(app, mock_db_utils):
    """
    Tests that user insights metrics are fetched correctly.
    """
    with app.app_context():
        metrics = admin_metrics_service.get_user_insights_metrics()

        assert metrics is not None
        assert not metrics['error']
        assert metrics['new_signups']['24h'] == 5
        assert len(metrics['users_hitting_limits']) == 1
        assert metrics['users_hitting_limits'][0]['username'] == 'testuser'

def test_get_performance_error_metrics_success(app, mock_db_utils):
    """
    Tests that performance and error metrics are fetched and calculated correctly.
    """
    mock_user_utils, mock_transcription_utils = mock_db_utils
    # Specific setup for this test
    mock_user_utils, mock_transcription_utils = mock_db_utils
    
    api_counts = {
        'gpt-transcribe': (40, 4),
        'gpt-4o-transcribe': (50, 2),
        'whisper': (30, 3),
        'assemblyai': (20, 5),
        'gpt-live-transcribe': (10, 1),
    }

    def count_jobs(_start, _end, **filters):
        api = filters.get('api_used')
        if api:
            jobs, errors = api_counts[api]
            return errors if filters.get('status') == 'error' else jobs
        if filters.get('llm_operation_status__ne') == 'idle':
            return 20
        if filters.get('llm_operation_status') == 'error':
            return 4
        if filters.get('status') == 'error':
            return 10
        return 100

    mock_transcription_utils.count_jobs_in_range.side_effect = count_jobs

    with app.app_context():
        metrics = admin_metrics_service.get_performance_error_metrics()

        assert metrics is not None
        assert not metrics['error']
        assert metrics['overall_transcription_error_rate']['24h'] == 10.0
        assert metrics['api_transcription_error_rates']['7d']['whisper'] == 10.0
        assert (
            metrics['api_transcription_error_rates']['7d']['gpt-live-transcribe']
            == 10.0
        )
        assert metrics['common_transcription_errors']['30d'][0][0] == 'Error A'
        assert metrics['overall_workflow_error_rate']['all'] == 20.0

def test_get_cost_analytics_success(app, mock_db_utils):
    """
    Tests that cost analytics are fetched and calculated correctly.
    """
    with app.app_context():
        metrics = admin_metrics_service.get_cost_analytics()

        assert metrics is not None
        assert not metrics['error']
        assert metrics['by_component']['24h']['transcriptions'] == 50.0
        assert metrics['by_role']['7d']['admin']['total_cost'] == 75.0

def test_get_user_usage_metrics_success(app, mock_db_utils):
    """
    Tests that usage metrics for a single user are fetched correctly.
    """
    with app.app_context():
        metrics = admin_metrics_service.get_user_usage_metrics(user_id=1)

        assert metrics is not None
        assert not metrics['error']
        assert metrics['costs']['24h'] == 75.0
        assert metrics['transcriptions']['7d'] > 0
        assert metrics['audio_processed']['all'] > 0

def test_safe_division_by_zero():
    """
    Tests that the _safe_division helper handles division by zero.
    """
    assert admin_metrics_service._safe_division(10, 0) == 0.0
    assert admin_metrics_service._safe_division(10, 0, default=1.0) == 1.0
    assert admin_metrics_service._safe_division(10, 2) == 5.0

def test_error_handling_in_metrics_functions(app):
    """
    Tests that service functions handle exceptions gracefully and return an error message.
    """
    with patch('app.services.admin_metrics_service.user_utils.count_all_users', side_effect=Exception("DB Down")):
        with app.app_context():
            metrics = admin_metrics_service.get_admin_dashboard_metrics()
            assert metrics['error'] is not None
            assert "Unexpected error" in metrics['error']
