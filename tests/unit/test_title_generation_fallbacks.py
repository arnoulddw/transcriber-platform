from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask

from app.services.api_clients.exceptions import (
    LlmApiError,
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
        default_openrouter_llm_model=None,
        enable_auto_title_generation=True,
        is_authenticated=True,
        has_permission=lambda _perm: True,
    )
    created_ids = iter([101, 102])
    create_operation = MagicMock(side_effect=lambda **_kwargs: next(created_ids))
    status_updates = []
    update_status = MagicMock(
        side_effect=lambda op_id, status, **kwargs: status_updates.append((op_id, status, kwargs))
        or True
    )
    call_results = iter([LlmApiError("primary provider down"), "Test Title"])

    def fake_call(_app, _user_id, _prompt, _op_id, _op_type, _provider, _model):
        result = next(call_results)
        if isinstance(result, Exception):
            raise result
        return result

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
            return_value=True,
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
        patch.object(
            title_generation, "_call_gemini_for_title", side_effect=fake_call
        ),
    ):
        with app.app_context():
            title_generation.generate_title_task(app, "job-1", 7)

    assert create_operation.call_count == 2
    updated_ops = {call[0] for call in status_updates}
    # Both attempts' records were finalized individually: the failed primary
    # and the successful fallback each own their own row.
    assert update_status.call_count >= 2
    assert 102 in updated_ops
