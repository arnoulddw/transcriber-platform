import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask

from app.services.api_clients.exceptions import (
    LlmConfigurationError,
    LlmGenerationError,
    LlmRateLimitError,
)
from app.tasks import title_generation


def test_build_title_generation_attempts_keeps_gemma_primary(monkeypatch):
    monkeypatch.setattr(
        title_generation.llm_service,
        "get_provider_for_model_code",
        lambda model: "GEMINI" if model.startswith(("gemini", "gemma")) else None,
    )

    attempts = title_generation._build_title_generation_attempts(
        provider="GEMINI",
        model_name="gemma-4-26b-a4b-it",
        config={"TITLE_GENERATION_FALLBACK_MODELS": ["gemini-3.0-flash"]},
    )

    assert attempts == [
        ("GEMINI", "gemma-4-26b-a4b-it"),
        ("GEMINI", "gemini-3.0-flash"),
    ]


def test_build_title_generation_attempts_deduplicates_fallback(monkeypatch):
    monkeypatch.setattr(
        title_generation.llm_service,
        "get_provider_for_model_code",
        lambda model: "GEMINI",
    )

    attempts = title_generation._build_title_generation_attempts(
        provider="GEMINI",
        model_name="gemini-3.0-flash",
        config={"TITLE_GENERATION_FALLBACK_MODELS": "gemini-3.0-flash, gemini-3.0-flash"},
    )

    assert attempts == [("GEMINI", "gemini-3.0-flash")]


def test_provider_level_errors_are_title_model_retryable():
    assert title_generation._should_try_next_title_model(LlmGenerationError("provider failed"))
    # Config and rate-limit errors are exactly where a fallback model helps.
    assert title_generation._should_try_next_title_model(LlmConfigurationError("bad config"))
    assert title_generation._should_try_next_title_model(LlmRateLimitError("rate limit"))


def test_fallback_attempts_use_separate_operation_records():
    """Each fallback attempt owns its own llm_operations row.

    A timed-out first attempt can then never overwrite the fallback's row:
    the abandoned thread only holds a reference to its own operation id.
    """
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="unit-test-secret",
        TESTING=True,
        TITLE_GENERATION_FALLBACK_MODELS=["gemini-3.0-flash"],
    )

    user = SimpleNamespace(
        role=SimpleNamespace(default_title_generation_model=None),
        enable_auto_title_generation=True,
        is_authenticated=True,
        has_permission=lambda _perm: True,
    )
    created_ids = iter([101, 102])
    create_operation = MagicMock(side_effect=lambda **_kwargs: next(created_ids))
    set_title = MagicMock(return_value=True)
    status_updates = []
    update_status = MagicMock(
        side_effect=lambda op_id, status, **kwargs: status_updates.append((op_id, status, kwargs))
        or True
    )
    release_connection = MagicMock()
    release_primary = threading.Event()
    primary_started = threading.Event()
    thread_args = []
    thread_targets = []
    real_thread = threading.Thread

    def capture_thread(*args, **kwargs):
        thread_args.append(kwargs["args"])
        thread_targets.append(kwargs["target"])
        return real_thread(*args, **kwargs)

    def fake_call(_app, _user_id, _prompt, op_id, _op_type, _provider, _model):
        if op_id == 101:
            primary_started.set()
            release_primary.wait(timeout=1)
            return "Late Primary Title"
        return "Test Title"

    with (
        patch.object(
            title_generation.user_model, "get_user_by_id", return_value=user
        ),
        patch.object(
            title_generation.transcription_model,
            "get_transcription_by_id",
            return_value={"transcription_text": "Some transcript."},
        ),
        patch.object(
            title_generation.transcription_model,
            "update_title_generation_status",
            return_value=True,
        ),
        patch.object(
            title_generation.transcription_model,
            "set_generated_title",
            side_effect=set_title,
        ),
        patch.object(
            title_generation.llm_catalog_model,
            "get_default_title_generation_model_code",
            return_value=None,
        ),
        patch.object(
            title_generation.llm_service, "resolve_user_model_preference", return_value=None
        ),
        patch.object(
            title_generation.llm_service, "resolve_role_model_override", return_value=None
        ),
        patch.object(
            title_generation.llm_service, "get_provider_for_model_code", return_value="GEMINI"
        ),
        patch.object(
            title_generation,
            "limiter",
            SimpleNamespace(limiter=SimpleNamespace(hit=MagicMock(return_value=True))),
        ),
        patch.object(
            title_generation.llm_operation_model, "create_llm_operation", create_operation
        ),
        patch.object(
            title_generation.llm_operation_model, "update_llm_operation_status", update_status
        ),
        patch.object(title_generation, "close_db", release_connection),
        patch.object(
            title_generation, "_call_gemini_for_title", side_effect=fake_call
        ),
        patch.object(title_generation.threading, "Thread", side_effect=capture_thread),
        patch.object(title_generation, "TITLE_GENERATION_TIMEOUT_SECONDS", 0.01),
    ):
        try:
            with app.app_context():
                title_generation.generate_title_task(app, "job-1", 7)
        finally:
            release_primary.set()

    assert primary_started.is_set()
    assert len(thread_args) == 2
    assert len(thread_targets) == 2
    assert thread_targets[0].__defaults__[0] is not thread_targets[1].__defaults__[0]
    assert thread_targets[0].__defaults__[1] is not thread_targets[1].__defaults__[1]

    assert create_operation.call_count == 2
    assert release_connection.call_count == 2
    updated_ops = {call[0] for call in status_updates}
    # Both attempts' records were finalized individually: the failed primary
    # and the successful fallback each own their own row.
    assert update_status.call_count >= 2
    assert (101, "error") in {(op_id, status) for op_id, status, _kwargs in status_updates}
    assert (102, "finished") in {(op_id, status) for op_id, status, _kwargs in status_updates}
    assert 102 in updated_ops
    set_title.assert_called_once_with("job-1", "Test Title")
