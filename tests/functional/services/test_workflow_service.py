import uuid
import pytest
from unittest.mock import patch, MagicMock

from app.services import workflow_service
from app.services.workflow_service import (
    InvalidPromptError,
    TranscriptionNotFoundError,
    WorkflowError,
    WorkflowInProgressError,
    OperationNotFoundError,
)
from app.models import (
    transcription as transcription_model,
    llm_operation as llm_operation_model,
    role as role_model,
)
from app.services import user_service
from app.models import user as user_model
from app.models.user import get_user_by_username


@pytest.fixture
def workflow_user(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
        workflow_role = role_model.create_role(
            name=f"workflow_test_role_{uuid.uuid4().hex[:8]}",
            description="Workflow test role with required permissions",
            permissions={
                "allow_workflows": True,
                "use_api_openai_whisper": True,
                "allow_auto_title_generation": True,
                "allow_download_transcript": True,
                "allow_speaker_diarization": True,
            },
        )
        if workflow_role:
            updated = user_model.update_user_role(user.id, workflow_role.id)
            print(f"[TEST] Assigned workflow role {workflow_role.name} (id={workflow_role.id}) to user {user.id}; updated={updated}")
            assigned_role = role_model.get_role_by_id(workflow_role.id)
            print(f"[TEST] Newly created role name: {assigned_role.name if assigned_role else 'None'}")
            user = user_model.get_user_by_id(user.id)
            print(f"[TEST] User role after reassignment: {user.role.name if user.role else 'None'} (role_id={user.role_id})")
        else:
            print("[TEST] Failed to create workflow role for workflow_user fixture")
        app.config.setdefault("WORKFLOW_LLM_PROVIDER", "GEMINI")
        app.config.setdefault("WORKFLOW_LLM_MODEL", "gemini-2.0-flash")
        return user


def _create_finished_transcription(app, user_id, transcription_id=None):
    if transcription_id is None:
        transcription_id = str(uuid.uuid4())
    with app.app_context():
        transcription_model.create_transcription_job(
            job_id=transcription_id,
            user_id=user_id,
            filename="sample.mp3",
            api_used="whisper",
            file_size_mb=1.0,
            audio_length_minutes=1.0,
            context_prompt_used=False,
        )
        transcription_model.finalize_job_success(
            transcription_id, "Transcript text", "en"
        )
    return transcription_id


def test_start_workflow_success(app, workflow_user):
    transcription_id = _create_finished_transcription(app, workflow_user.id)
    with patch(
        "app.services.workflow_service.llm_operation_model.create_llm_operation",
        return_value=55,
    ) as mock_create, patch(
        "app.services.workflow_service.threading.Thread"
    ) as mock_thread, patch(
        "app.services.workflow_service.role_model.reserve_usage_if_allowed",
        return_value=(True, ""),
    ), patch(
        "app.services.workflow_service.check_permission",
        return_value=True,
    ):
        mock_thread.return_value = MagicMock()
        operation_id = workflow_service.start_workflow(
            workflow_user.id, transcription_id, "Summarize this transcript."
        )

    assert operation_id == 55
    mock_create.assert_called_once_with(
        user_id=workflow_user.id,
        provider="GEMINI",
        operation_type="workflow",
        input_text="Summarize this transcript.",
        transcription_id=transcription_id,
        prompt_id=None,
        status="pending",
    )
    mock_thread.assert_called_once()
    mock_thread.return_value.start.assert_called_once()


def test_start_workflow_uses_saved_prompt(app, workflow_user):
    transcription_id = _create_finished_transcription(app, workflow_user.id)
    with app.app_context():
        saved_prompt = user_service.save_user_prompt(
            workflow_user.id,
            title="Summary",
            prompt_text="Use my saved prompt",
            color="#000000",
        )

    with patch(
        "app.services.workflow_service.llm_operation_model.create_llm_operation",
        return_value=88,
    ) as mock_create, patch(
        "app.services.workflow_service.threading.Thread"
    ) as mock_thread, patch(
        "app.services.workflow_service.role_model.reserve_usage_if_allowed",
        return_value=(True, ""),
    ), patch(
        "app.services.workflow_service.check_permission",
        return_value=True,
    ):
        mock_thread.return_value = MagicMock()
        operation_id = workflow_service.start_workflow(
            workflow_user.id,
            transcription_id,
            prompt=None,
            prompt_id=saved_prompt.id,
        )

    assert operation_id == 88
    args, kwargs = mock_create.call_args
    assert kwargs["input_text"] == "Use my saved prompt"
    assert kwargs["prompt_id"] == saved_prompt.id
    mock_thread.return_value.start.assert_called_once()


def test_start_workflow_invalid_prompt_id(app, workflow_user):
    transcription_id = _create_finished_transcription(app, workflow_user.id)
    with patch(
        "app.services.workflow_service.role_model.reserve_usage_if_allowed",
        return_value=(True, ""),
    ), patch(
        "app.services.workflow_service.check_permission",
        return_value=True,
    ):
        with pytest.raises(InvalidPromptError):
            workflow_service.start_workflow(
                workflow_user.id,
                transcription_id,
                prompt=None,
                prompt_id=999,
            )


def test_start_workflow_rejects_empty_prompt(app, workflow_user):
    transcription_id = _create_finished_transcription(app, workflow_user.id)
    with patch(
        "app.services.workflow_service.role_model.reserve_usage_if_allowed",
        return_value=(True, ""),
    ), patch(
        "app.services.workflow_service.check_permission",
        return_value=True,
    ):
        with pytest.raises(InvalidPromptError):
            workflow_service.start_workflow(
                workflow_user.id,
                transcription_id,
                prompt="   ",
                prompt_id=None,
            )


def test_start_workflow_detects_pending_operation(app, workflow_user):
    transcription_id = _create_finished_transcription(app, workflow_user.id)
    with app.app_context():
        llm_operation_model.create_llm_operation(
            user_id=workflow_user.id,
            provider="GEMINI",
            operation_type="workflow",
            input_text="Existing",
            transcription_id=transcription_id,
            prompt_id=None,
            status="pending",
        )

    with patch(
        "app.services.workflow_service.role_model.reserve_usage_if_allowed",
        return_value=(True, ""),
    ), patch(
        "app.services.workflow_service.check_permission",
        return_value=True,
    ):
        with pytest.raises(WorkflowInProgressError):
            workflow_service.start_workflow(
                workflow_user.id,
                transcription_id,
                prompt="New prompt",
            )


def test_start_workflow_requires_finished_transcription(app, workflow_user):
    transcription_id = str(uuid.uuid4())
    with app.app_context():
        transcription_model.create_transcription_job(
            job_id=transcription_id,
            user_id=workflow_user.id,
            filename="processing.mp3",
            api_used="whisper",
            file_size_mb=1.0,
            audio_length_minutes=1.0,
            context_prompt_used=False,
        )

    with patch(
        "app.services.workflow_service.role_model.reserve_usage_if_allowed",
        return_value=(True, ""),
    ), patch(
        "app.services.workflow_service.check_permission",
        return_value=True,
    ):
        with pytest.raises(WorkflowError):
            workflow_service.start_workflow(
                workflow_user.id,
                transcription_id,
                prompt="Prompt",
            )


def test_start_workflow_transcription_not_found(app, workflow_user):
    with patch(
        "app.services.workflow_service.role_model.reserve_usage_if_allowed",
        return_value=(True, ""),
    ), patch(
        "app.services.workflow_service.check_permission",
        return_value=True,
    ):
        with pytest.raises(TranscriptionNotFoundError):
            workflow_service.start_workflow(
                workflow_user.id,
                transcription_id="missing",
                prompt="Prompt",
            )


def test_edit_workflow_result_success(app, workflow_user):
    with patch(
        "app.services.workflow_service.llm_operation_model.update_llm_operation_result",
        return_value=True,
    ) as mock_update:
        workflow_service.edit_workflow_result(
            workflow_user.id, operation_id=10, new_result="Updated result"
        )

    mock_update.assert_called_once_with(
        operation_id=10, user_id=workflow_user.id, new_result="Updated result"
    )


def test_edit_workflow_result_not_found(app, workflow_user):
    with patch(
        "app.services.workflow_service.llm_operation_model.update_llm_operation_result",
        return_value=False,
    ):
        with pytest.raises(OperationNotFoundError):
            workflow_service.edit_workflow_result(
                workflow_user.id, operation_id=999, new_result="Nope"
            )


def test_delete_workflow_result_clears_records(app, workflow_user):
    transcription_id = _create_finished_transcription(app, workflow_user.id)
    with app.app_context():
        op_id = llm_operation_model.create_llm_operation(
            user_id=workflow_user.id,
            provider="GEMINI",
            operation_type="workflow",
            input_text="Existing",
            transcription_id=transcription_id,
            prompt_id=None,
            status="finished",
        )
        cursor = workflow_service.get_cursor()
        cursor.execute(
            """
            UPDATE transcriptions
            SET llm_operation_id=%s,
                llm_operation_status='finished',
                llm_operation_result='Result text',
                llm_operation_error=NULL,
                llm_operation_ran_at=NOW()
            WHERE id=%s
            """,
            (op_id, transcription_id),
        )
        workflow_service.get_db().commit()

    workflow_service.delete_workflow_result(
        workflow_user.id, transcription_id=transcription_id
    )

    with app.app_context():
        result = llm_operation_model.get_llm_operation_by_id(op_id, workflow_user.id)
        assert result is None
        transcription = transcription_model.get_transcription_by_id(
            transcription_id, workflow_user.id
        )
        assert transcription["llm_operation_id"] is None
        assert transcription["llm_operation_status"] is None
        assert transcription["llm_operation_result"] is None


def test_delete_workflow_result_transcription_not_found(app, workflow_user):
    with pytest.raises(TranscriptionNotFoundError):
        workflow_service.delete_workflow_result(
            workflow_user.id, transcription_id="missing-id"
        )
