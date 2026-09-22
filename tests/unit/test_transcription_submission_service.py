import io
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from werkzeug.datastructures import FileStorage

# This worktree intentionally has no deployment .env file. Keep this service
# test independent from deployment configuration while importing app modules.
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DEPLOYMENT_MODE", "single")
os.environ.setdefault("MYSQL_USER", "test-user")
os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("MYSQL_DB", "test-db")

from app.services import transcription_submission_service as submission


def _dependencies(**overrides):
    values = {
        "catalog": Mock(),
        "files": Mock(),
        "pricing": Mock(),
        "roles": Mock(),
        "transcription": Mock(),
        "queue_submitter": Mock(),
        "transcription_processor": Mock(),
        "model_resolver": Mock(return_value=("openrouter", "openai/whisper")),
    }
    values.update(overrides)
    return submission.SubmissionDependencies(**values)


def test_save_uploaded_audio_is_shared_by_both_route_adapters(tmp_path):
    files = Mock()
    files.validate_file_path.return_value = True
    files.get_audio_duration.return_value = (90.0, 1.5)
    uploaded = FileStorage(stream=io.BytesIO(b"audio bytes"), filename="../meeting.wav")

    metadata = submission.save_uploaded_audio(
        uploaded,
        upload_dir=str(tmp_path),
        job_id="job-123",
        max_size_mb=10,
        files=files,
        log_prefix="[test]",
    )

    assert metadata.original_filename == "meeting.wav"
    assert metadata.temp_filename == str(tmp_path / "job-123_meeting.wav")
    assert metadata.audio_length_seconds == 90.0
    assert metadata.audio_length_minutes == 1.5
    assert (tmp_path / "job-123_meeting.wav").read_bytes() == b"audio bytes"
    files.validate_file_path.assert_called_once_with(metadata.temp_filename, str(tmp_path))
    files.get_audio_duration.assert_called_once_with(metadata.temp_filename)


def test_prepare_submission_resolves_catalog_price_and_reserves_usage_once():
    pricing = Mock()
    pricing.get_price.return_value = 0.2
    roles = Mock()
    roles.reserve_usage_if_allowed.return_value = (True, "")
    resolver = Mock(return_value=("openrouter", "openai/whisper"))
    dependencies = _dependencies(pricing=pricing, roles=roles, model_resolver=resolver)
    user = SimpleNamespace(role=SimpleNamespace(name="member"))

    preparation = submission.prepare_submission(
        dependencies,
        user=user,
        user_id=42,
        api_choice="openrouter:openai/whisper",
        model_lookup={"openrouter:openai/whisper": {"code": "openai/whisper"}},
        submitted_model="openai/whisper",
        audio_length_seconds=90.0,
        audio_length_minutes=1.5,
    )

    resolver.assert_called_once()
    pricing.get_price.assert_called_once_with(
        item_type="transcription",
        item_key="openai/whisper",
    )
    roles.reserve_usage_if_allowed.assert_called_once_with(
        42,
        user.role,
        cost_to_add=pytest.approx(0.3),
        minutes_to_add=1.5,
    )
    assert preparation.provider_code == "openrouter"
    assert preparation.api_model == "openai/whisper"
    assert preparation.cost_to_add == pytest.approx(0.3)


def test_prepare_submission_rolls_back_when_quota_rejects():
    pricing = Mock()
    pricing.get_price.return_value = 0.2
    roles = Mock()
    roles.reserve_usage_if_allowed.return_value = (False, "Usage limit reached")
    dependencies = _dependencies(pricing=pricing, roles=roles)
    user = SimpleNamespace(role=SimpleNamespace(name="member"))
    database = Mock()

    with patch.object(submission, "get_db", return_value=database):
        with pytest.raises(submission.UsageLimitExceededError, match="Usage limit reached"):
            submission.prepare_submission(
                dependencies,
                user=user,
                user_id=42,
                api_choice="openrouter:openai/whisper",
                model_lookup={},
                submitted_model="openai/whisper",
                audio_length_seconds=90.0,
                audio_length_minutes=1.5,
                commit=False,
            )

    roles.reserve_usage_if_allowed.assert_called_once_with(
        42,
        user.role,
        cost_to_add=pytest.approx(0.3),
        minutes_to_add=1.5,
        commit=False,
    )
    database.rollback.assert_called_once_with()


def test_prepare_submission_rolls_back_when_role_is_missing():
    database = Mock()
    pricing = Mock()
    pricing.get_price.return_value = None
    dependencies = _dependencies(pricing=pricing)
    user = SimpleNamespace(role=None)

    with patch.object(submission, "get_db", return_value=database):
        with pytest.raises(submission.NoAssignedRoleError):
            submission.prepare_submission(
                dependencies,
                user=user,
                user_id=42,
                api_choice="openrouter:openai/whisper",
                model_lookup={},
                submitted_model="openai/whisper",
                audio_length_seconds=90.0,
                audio_length_minutes=1.5,
                commit=False,
            )

    database.rollback.assert_called_once_with()


def test_create_and_schedule_job_preserves_durable_queue_payload_contract():
    transcription = Mock()
    queue_submitter = Mock()
    processor = Mock()
    dependencies = _dependencies(
        transcription=transcription,
        queue_submitter=queue_submitter,
        transcription_processor=processor,
    )
    upload = submission.UploadMetadata(
        original_filename="meeting.wav",
        temp_filename="/tmp/job-meeting.wav",
        file_size_mb=2.0,
        audio_length_seconds=90.0,
        audio_length_minutes=1.5,
    )
    preparation = submission.SubmissionPreparation(
        provider_code="openrouter",
        api_model="openai/whisper",
        cost_to_add=0.3,
    )
    app = SimpleNamespace(config={"QUEUE_MAX_ATTEMPTS": 3})

    database = Mock()
    with patch.object(submission, "get_db", return_value=database):
        submission.create_and_schedule_job(
            dependencies,
            app=app,
            job_id="job-123",
            user_id=42,
            upload=upload,
            api_choice="openrouter:openai/whisper",
            preparation=preparation,
            context_prompt_used=True,
            pending_workflow_prompt_text="Summarize",
            pending_workflow_prompt_title="Meeting",
            pending_workflow_prompt_color="#fff",
            pending_workflow_origin_prompt_id=7,
            public_api_invocation=True,
            language_code="en",
            context_prompt="Use concise bullets",
            speaker_diarization_enabled=True,
        )

    transcription.create_transcription_job.assert_called_once_with(
        job_id="job-123",
        user_id=42,
        filename="meeting.wav",
        api_used="openrouter:openai/whisper",
        file_size_mb=2.0,
        audio_length_minutes=1.5,
        context_prompt_used=True,
        pending_workflow_prompt_text="Summarize",
        pending_workflow_prompt_title="Meeting",
        pending_workflow_prompt_color="#fff",
        pending_workflow_origin_prompt_id=7,
        public_api_invocation=True,
        api_model="openai/whisper",
        commit=False,
    )
    queue_submitter.assert_called_once_with(
        app.config,
        processor,
        app,
        "job-123",
        42,
        "/tmp/job-meeting.wav",
        "en",
        "openrouter:openai/whisper",
        "meeting.wav",
        "Use concise bullets",
        "Summarize",
        "Meeting",
        "#fff",
        7,
        True,
        "openai/whisper",
        commit=False,
    )
    database.commit.assert_called_once_with()


def test_create_and_schedule_job_rolls_back_record_when_queue_insert_fails():
    database = Mock()
    transcription = Mock()
    queue_submitter = Mock(side_effect=RuntimeError("queue unavailable"))
    dependencies = _dependencies(
        transcription=transcription,
        queue_submitter=queue_submitter,
    )
    upload = submission.UploadMetadata("audio.wav", "/tmp/audio.wav", 1.0, 0.0, 0.0)
    preparation = submission.SubmissionPreparation("openai", "whisper", 0.0)

    with patch.object(submission, "get_db", return_value=database):
        with pytest.raises(RuntimeError, match="queue unavailable"):
            submission.create_and_schedule_job(
                dependencies,
                app=SimpleNamespace(config={}),
                job_id="job-123",
                user_id=42,
                upload=upload,
                api_choice="openai",
                preparation=preparation,
                context_prompt_used=False,
            )

    transcription.create_transcription_job.assert_called_once()
    assert transcription.create_transcription_job.call_args.kwargs["commit"] is False
    database.rollback.assert_called_once_with()
    database.commit.assert_not_called()
