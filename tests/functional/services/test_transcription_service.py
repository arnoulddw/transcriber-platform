# tests/functional/services/test_transcription_service.py
# Contains functional tests for the transcription service.

import pytest
import os
import uuid
from unittest.mock import patch, MagicMock, ANY

from flask import Flask

from app.services import transcription_service
from app.services.api_clients.exceptions import TranscriptionApiError, TranscriptionProcessingError
from app.models import transcription as transcription_model
from app.models.user import get_user_by_username


@pytest.fixture
def mock_audio_file(tmpdir):
    """Creates a dummy file and returns its path."""
    file_path = tmpdir.join("test_audio.mp3")
    # Create a non-empty file
    with open(file_path, "wb") as f:
        f.write(os.urandom(1024))
    return str(file_path)


def test_process_transcription_success(
    app: Flask, logged_in_client_with_permissions, mock_audio_file
):
    """
    GIVEN a valid user, audio file, and job parameters
    WHEN the transcription process is initiated and the API call is successful
    THEN the job status should be updated to 'finished' and the result saved.
    """
    # Get user within app context to ensure we have the latest DB state
    with app.app_context():
        user = get_user_by_username('testuser_permissions')
    job_id = str(uuid.uuid4())
    api_choice = 'whisper'
    expected_text = "This is a test transcription."
    expected_lang = "en"

    # Mock external dependencies
    with patch('app.services.transcription_service.get_transcription_client') as mock_get_client, \
         patch('app.services.transcription_service.file_service.get_audio_duration', return_value=(60.0, 1.0)) as mock_get_duration, \
         patch('app.services.transcription_service.role_model.reserve_usage_if_allowed', return_value=(True, '')) as mock_reserve_usage, \
         patch('app.services.transcription_service.transcription_model') as mock_transcription_model, \
         patch('app.services.transcription_service.generate_title_task') as mock_title_task, \
         patch('app.services.transcription_service.file_service.remove_files', return_value=1) as mock_remove_files, \
         patch('app.services.transcription_service.get_decrypted_api_key', return_value='fake_api_key'):

        # Configure the mock transcription client
        mock_client = MagicMock()
        mock_client.transcribe.return_value = (expected_text, expected_lang)
        mock_get_client.return_value = mock_client

        # Mock the user object found by the service
        with patch('app.services.transcription_service.user_model.get_user_by_id', return_value=user):

            # Run the service function
            transcription_service.process_transcription(
                app=app,
                job_id=job_id,
                user_id=user.id,
                temp_filename=mock_audio_file,
                language_code='en',
                api_choice=api_choice,
                original_filename='test.mp3'
            )

            # Assertions
            # 1. Check status updates
            mock_transcription_model.update_job_status.assert_called_with(job_id, 'processing')

            # 2. Check finalization
            mock_transcription_model.finalize_job_success.assert_called_once_with(
                job_id, expected_text, expected_lang
            )

            # 3. Check durable usage reservation
            mock_reserve_usage.assert_called_once()

            # 4. Check that the transcription client was called correctly
            mock_client.transcribe.assert_called_once_with(
                audio_file_path=mock_audio_file,
                language_code='en',
                progress_callback=ANY,
                original_filename='test.mp3',
                context_prompt="",
                cancel_event=ANY,
                audio_length_seconds=60.0,
                extra_options=None
            )

            # 5. Check that the temp file was removed
            mock_remove_files.assert_called_once_with([mock_audio_file])

            # 6. Check that the title generation task was spawned
            mock_title_task.assert_called_once()


def test_process_transcription_forwards_openrouter_model(tmp_path):
    """Ensure the selected OpenRouter model reaches the transcription client."""
    app = Flask(__name__)
    app.config.update(TESTING=True, DEPLOYMENT_MODE='multi')
    audio_file = tmp_path / 'test.mp3'
    audio_file.write_bytes(b'test audio data')
    mock_audio_file = str(audio_file)
    user = MagicMock(id=42, enable_auto_title_generation=False)
    user._role = MagicMock()
    job_id = str(uuid.uuid4())
    model_slug = 'openai/gpt-transcribe'

    with patch('app.services.transcription_service.get_transcription_client') as mock_get_client, \
         patch('app.services.transcription_service.file_service.get_audio_duration', return_value=(60.0, 1.0)), \
         patch('app.services.transcription_service.role_model.reserve_usage_if_allowed', return_value=(True, '')), \
         patch('app.services.transcription_service.transcription_model') as mock_transcription_model, \
         patch('app.services.transcription_service.generate_title_task'), \
         patch('app.services.transcription_service.file_service.remove_files', return_value=1), \
         patch('app.services.transcription_service.get_decrypted_api_key', return_value='fake_api_key'), \
         patch('app.services.transcription_service.check_permission', return_value=True), \
         patch('app.services.transcription_service.get_pricing_service_price', return_value=None), \
         patch(
             'app.services.transcription_service.transcription_catalog_model.get_model_by_code',
             return_value={'display_name': 'OpenRouter', 'permission_key': 'use_api_openrouter'},
         ):
        mock_transcription_model.get_transcription_by_id.return_value = None
        mock_client = MagicMock()
        mock_client.transcribe.return_value = ('OpenRouter transcript', 'en')
        mock_get_client.return_value = mock_client

        with patch('app.services.transcription_service.user_model.get_user_by_id', return_value=user):
            transcription_service.process_transcription(
                app=app,
                job_id=job_id,
                user_id=user.id,
                temp_filename=mock_audio_file,
                language_code='en',
                api_choice='openrouter',
                original_filename='test.mp3',
                api_model=model_slug,
            )

        mock_get_client.assert_called_once()
        mock_client.transcribe.assert_called_once()
        assert mock_client.transcribe.call_args.kwargs['extra_options'] == {'model': model_slug}


def test_process_transcription_rejects_openrouter_without_model_before_client_creation(tmp_path):
    """Ensure a missing OpenRouter model is recorded without constructing a client."""
    app = Flask(__name__)
    app.config.update(TESTING=True, DEPLOYMENT_MODE='multi')
    audio_file = tmp_path / 'test.mp3'
    audio_file.write_bytes(b'test audio data')
    mock_audio_file = str(audio_file)
    user = MagicMock(id=42, enable_auto_title_generation=False)
    user._role = MagicMock()
    job_id = str(uuid.uuid4())

    with patch('app.services.transcription_service.get_transcription_client') as mock_get_client, \
         patch('app.services.transcription_service.file_service.get_audio_duration', return_value=(60.0, 1.0)), \
         patch('app.services.transcription_service.role_model.reserve_usage_if_allowed', return_value=(True, '')), \
         patch('app.services.transcription_service.transcription_model') as mock_transcription_model, \
         patch('app.services.transcription_service.file_service.remove_files', return_value=1), \
         patch('app.services.transcription_service.check_permission', return_value=True), \
         patch('app.services.transcription_service.get_pricing_service_price', return_value=None), \
         patch(
             'app.services.transcription_service.transcription_catalog_model.get_model_by_code',
             return_value={'display_name': 'OpenRouter', 'permission_key': 'use_api_openrouter'},
         ):
        mock_transcription_model.get_transcription_by_id.return_value = None

        with patch('app.services.transcription_service.user_model.get_user_by_id', return_value=user):
            transcription_service.process_transcription(
                app=app,
                job_id=job_id,
                user_id=user.id,
                temp_filename=mock_audio_file,
                language_code='en',
                api_choice='openrouter',
                original_filename='test.mp3',
            )

        mock_get_client.assert_not_called()
        mock_transcription_model.set_job_error.assert_called_once_with(
            job_id, 'ERROR: OpenRouter model is required.'
        )


def test_process_transcription_with_speaker_diarization(
    app: Flask, logged_in_client_with_permissions, mock_audio_file
):
    """Ensure speaker diarization flag is forwarded to the API client when AssemblyAI is used."""
    with app.app_context():
        user = get_user_by_username('testuser_permissions')
    job_id = str(uuid.uuid4())

    with patch('app.services.transcription_service.get_transcription_client') as mock_get_client, \
         patch('app.services.transcription_service.file_service.get_audio_duration', return_value=(45.0, 0.75)), \
         patch('app.services.transcription_service.role_model.reserve_usage_if_allowed', return_value=(True, '')), \
         patch('app.services.transcription_service.transcription_model'), \
         patch('app.services.transcription_service.generate_title_task'), \
         patch('app.services.transcription_service.file_service.remove_files', return_value=1), \
         patch('app.services.transcription_service.get_decrypted_api_key', return_value='fake_api_key'):

        mock_client = MagicMock()
        mock_client.transcribe.return_value = ("Sample text", "en")
        mock_get_client.return_value = mock_client

        with patch('app.services.transcription_service.user_model.get_user_by_id', return_value=user):
            transcription_service.process_transcription(
                app=app,
                job_id=job_id,
                user_id=user.id,
                temp_filename=mock_audio_file,
                language_code='en',
                api_choice='assemblyai',
                original_filename='test.mp3',
                speaker_diarization_enabled=True
            )

            assert mock_client.transcribe.call_count == 1
            kwargs = mock_client.transcribe.call_args.kwargs
            assert kwargs.get('extra_options') == {
                'speaker_diarization_enabled': True,
                'model': 'universal',
            }


def test_process_transcription_api_error(
    app: Flask, logged_in_client_with_permissions, mock_audio_file
):
    """
    GIVEN a valid user and job
    WHEN the transcription API client raises an error
    THEN the job status should be updated to 'error' with the correct message.
    """
    user = get_user_by_username('testuser_permissions')
    job_id = str(uuid.uuid4())
    api_choice = 'whisper'
    error_message = "The API is down."

    with patch('app.services.transcription_service.get_transcription_client') as mock_get_client, \
         patch('app.services.transcription_service.file_service.get_audio_duration', return_value=(60.0, 1.0)), \
         patch('app.services.transcription_service.role_model.reserve_usage_if_allowed', return_value=(True, '')), \
         patch('app.services.transcription_service.transcription_model') as mock_transcription_model, \
         patch('app.services.transcription_service.get_decrypted_api_key', return_value='fake_api_key'):

        mock_client = MagicMock()
        mock_client.transcribe.side_effect = TranscriptionProcessingError(error_message)
        mock_get_client.return_value = mock_client

        with patch('app.services.transcription_service.user_model.get_user_by_id', return_value=user):
            transcription_service.process_transcription(
                app=app,
                job_id=job_id,
                user_id=user.id,
                temp_filename=mock_audio_file,
                language_code='en',
                api_choice=api_choice,
                original_filename='test.mp3'
            )

            mock_transcription_model.set_job_error.assert_called_once_with(
                job_id, f"ERROR: {error_message}"
            )
            mock_transcription_model.finalize_job_success.assert_not_called()


def test_process_transcription_cancellation(
    app: Flask, logged_in_client_with_permissions, mock_audio_file
):
    """
    GIVEN a job that is in progress
    WHEN a cancellation is requested
    THEN the job status should be updated to 'cancelled' and cleanup should occur.
    """
    user = get_user_by_username('testuser_permissions')
    job_id = str(uuid.uuid4())
    api_choice = 'whisper'

    with patch('app.services.transcription_service.get_transcription_client'), \
         patch('app.services.transcription_service.file_service.get_audio_duration', return_value=(60.0, 1.0)), \
         patch('app.services.transcription_service.role_model.reserve_usage_if_allowed', return_value=(True, '')), \
         patch('app.services.transcription_service.transcription_model') as mock_transcription_model:

        # Simulate that the job is marked for cancellation in the DB
        mock_transcription_model.get_transcription_by_id.return_value = {'status': 'cancelling'}

        with patch('app.services.transcription_service.user_model.get_user_by_id', return_value=user):
            transcription_service.process_transcription(
                app=app,
                job_id=job_id,
                user_id=user.id,
                temp_filename=mock_audio_file,
                language_code='en',
                api_choice=api_choice,
                original_filename='test.mp3'
            )

            # Check that the status is updated to 'cancelled' at the end
            mock_transcription_model.update_job_status.assert_called_with(job_id, 'cancelled')
            mock_transcription_model.finalize_job_success.assert_not_called()
            mock_transcription_model.set_job_error.assert_not_called()


def test_process_transcription_permission_denied(
    app: Flask, logged_in_client, mock_audio_file
):
    """
    GIVEN a user without the required permission for a specific API
    WHEN the transcription process is initiated
    THEN the job should fail with a 'PermissionError'.
    """
    user = get_user_by_username('testuser') # This user has no special permissions
    job_id = str(uuid.uuid4())
    api_choice = 'assemblyai' # Assume the default role does not allow this

    with patch('app.services.transcription_service.transcription_model') as mock_transcription_model:
        with patch('app.services.transcription_service.user_model.get_user_by_id', return_value=user):
            transcription_service.process_transcription(
                app=app,
                job_id=job_id,
                user_id=user.id,
                temp_filename=mock_audio_file,
                language_code='en',
                api_choice=api_choice,
                original_filename='test.mp3'
            )

            mock_transcription_model.set_job_error.assert_called_once_with(
                job_id, "ERROR: Permission denied to use the 'assemblyai' API."
            )


def test_process_transcription_with_pending_workflow(
    app: Flask, logged_in_client_with_permissions, mock_audio_file
):
    """
    GIVEN a successful transcription that had a pending workflow
    WHEN the transcription finishes
    THEN the workflow_service should be called to start the workflow.
    """
    user = get_user_by_username('testuser_permissions')
    job_id = str(uuid.uuid4())
    api_choice = 'whisper'
    pending_prompt = "Summarize this."

    with patch('app.services.transcription_service.get_transcription_client') as mock_get_client, \
         patch('app.services.transcription_service.file_service.get_audio_duration', return_value=(60.0, 1.0)), \
         patch('app.services.transcription_service.role_model.reserve_usage_if_allowed', return_value=(True, '')), \
         patch('app.services.transcription_service.transcription_model'), \
         patch('app.services.transcription_service.generate_title_task'), \
         patch('app.services.transcription_service.workflow_service.start_workflow') as mock_start_workflow, \
         patch('app.services.transcription_service.get_decrypted_api_key', return_value='fake_api_key'):

        mock_client = MagicMock()
        mock_client.transcribe.return_value = ("Some text", "en")
        mock_get_client.return_value = mock_client

        with patch('app.services.transcription_service.user_model.get_user_by_id', return_value=user):
            transcription_service.process_transcription(
                app=app,
                job_id=job_id,
                user_id=user.id,
                temp_filename=mock_audio_file,
                language_code='en',
                api_choice=api_choice,
                original_filename='test.mp3',
                pending_workflow_prompt_text=pending_prompt,
                pending_workflow_origin_prompt_id=123
            )

            mock_start_workflow.assert_called_once_with(
                user_id=user.id,
                transcription_id=job_id,
                prompt=pending_prompt,
                prompt_id=123
            )
