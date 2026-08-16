from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from flask import Flask

from app.services import workflow_service
from app.services import llm_service
from app.tasks import title_generation


@pytest.fixture
def llm_app_context():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-secret",
        LLM_PROVIDERS=["GEMINI", "OPENAI", "OPENROUTER"],
        GEMINI_MODELS=["gemini-3.0-flash"],
        OPENAI_MODELS=["gpt-4o"],
        OPENROUTER_MODELS=["google/gemini-3.7-flash"],
    )
    with app.app_context():
        yield


def test_resolve_user_model_preference_returns_catalog_provider(llm_app_context):
    user = SimpleNamespace(
        default_workflow_model=" google/gemini-3.7-flash ",
        has_permission=Mock(return_value=True),
    )
    catalog_entry = {
        "code": "google/gemini-3.7-flash",
        "provider": "OPENROUTER",
        "permission_key": "use_api_openrouter",
        "is_active": True,
    }

    with patch.object(llm_service.llm_catalog_model, "get_model_by_code", return_value=catalog_entry):
        result = llm_service.resolve_user_model_preference(user, "default_workflow_model")

    assert result == ("OPENROUTER", "google/gemini-3.7-flash")
    user.has_permission.assert_called_once_with("use_api_openrouter")


def test_resolve_user_model_preference_rejects_model_without_permission(llm_app_context):
    user = SimpleNamespace(
        default_title_generation_model="gpt-4o",
        has_permission=Mock(return_value=False),
    )
    catalog_entry = {
        "code": "gpt-4o",
        "provider": "OPENAI",
        "permission_key": "use_api_openai",
        "is_active": True,
    }

    with patch.object(llm_service.llm_catalog_model, "get_model_by_code", return_value=catalog_entry):
        result = llm_service.resolve_user_model_preference(user, "default_title_generation_model")

    assert result is None
    user.has_permission.assert_called_once_with("use_api_openai")


def test_resolve_user_model_preference_rejects_inactive_or_unknown_models(llm_app_context):
    user = SimpleNamespace(
        default_workflow_model="retired-model",
        has_permission=Mock(return_value=True),
    )
    inactive_entry = {
        "code": "retired-model",
        "provider": "OPENAI",
        "permission_key": None,
        "is_active": False,
    }

    with patch.object(llm_service.llm_catalog_model, "get_model_by_code", return_value=inactive_entry):
        assert llm_service.resolve_user_model_preference(user, "default_workflow_model") is None

    with patch.object(llm_service.llm_catalog_model, "get_model_by_code", return_value=None), patch.object(
        llm_service, "get_provider_for_model_code", return_value=None
    ):
        user.default_workflow_model = "unknown-model"
        assert llm_service.resolve_user_model_preference(user, "default_workflow_model") is None


def test_workflow_start_uses_user_workflow_model(llm_app_context):
    user = SimpleNamespace(role=SimpleNamespace(default_workflow_model=None))
    cursor = Mock()
    cursor.fetchone.return_value = None
    created_thread = Mock()

    with patch.object(workflow_service.user_model, "get_user_by_id", return_value=user), patch.object(
        workflow_service, "check_permission", return_value=True
    ), patch.object(
        workflow_service.transcription_model,
        "get_transcription_by_id",
        return_value={"status": "finished", "transcription_text": "Transcript text"},
    ), patch.object(workflow_service, "get_cursor", return_value=cursor), patch.object(
        workflow_service.role_model, "reserve_usage_if_allowed", return_value=(True, "")
    ), patch.object(
        workflow_service.llm_service,
        "resolve_user_model_preference",
        return_value=("OPENROUTER", "google/gemini-3.7-flash"),
    ), patch.object(
        workflow_service.llm_catalog_model,
        "get_default_workflow_model_code",
        return_value=None,
    ), patch.object(
        workflow_service.llm_operation_model,
        "create_llm_operation",
        return_value=41,
    ) as create_operation, patch.object(
        workflow_service.threading, "Thread", return_value=created_thread
    ) as create_thread:
        operation_id = workflow_service.start_workflow(7, "transcription-1", "Summarize this")

    assert operation_id == 41
    create_operation.assert_called_once_with(
        user_id=7,
        provider="OPENROUTER",
        operation_type="workflow",
        input_text="Summarize this",
        transcription_id="transcription-1",
        prompt_id=None,
        status="pending",
    )
    thread_args = create_thread.call_args.kwargs["args"]
    assert thread_args[-2:] == ("OPENROUTER", "google/gemini-3.7-flash")
    created_thread.start.assert_called_once_with()


def test_title_generation_uses_user_auxiliary_model(llm_app_context):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-secret",
        TITLE_GENERATION_LLM_PROVIDER="GEMINI",
        TITLE_GENERATION_LLM_MODEL="gemma-4-26b-a4b-it",
        TITLE_GENERATION_FALLBACK_MODELS=[],
    )
    user = SimpleNamespace(
        enable_auto_title_generation=True,
        role=SimpleNamespace(default_title_generation_model=None),
    )
    created_thread = Mock()
    created_thread.is_alive.return_value = False

    with patch.object(title_generation.transcription_model, "update_title_generation_status", return_value=True), patch.object(
        title_generation.user_model, "get_user_by_id", return_value=user
    ), patch.object(title_generation, "check_permission", return_value=True), patch.object(
        title_generation.transcription_model,
        "get_transcription_by_id",
        return_value={"transcription_text": "Transcript text"},
    ), patch.object(
        title_generation.llm_service,
        "resolve_user_model_preference",
        return_value=("OPENAI", "gpt-4o"),
    ), patch.object(
        title_generation.llm_catalog_model,
        "get_default_title_generation_model_code",
        return_value=None,
    ), patch.object(
        title_generation.llm_operation_model,
        "create_llm_operation",
        return_value=77,
    ), patch.object(
        title_generation.llm_operation_model, "update_llm_operation_status"
    ), patch.object(
        title_generation,
        "limiter",
        SimpleNamespace(limiter=SimpleNamespace(test=Mock(return_value=True), hit=Mock())),
    ), patch.object(
        title_generation.threading, "Thread", return_value=created_thread
    ) as create_thread:
        title_generation.generate_title_task(app, "transcription-2", 7)

    thread_args = create_thread.call_args.kwargs["args"]
    assert thread_args[-2:] == ("OPENAI", "gpt-4o")
