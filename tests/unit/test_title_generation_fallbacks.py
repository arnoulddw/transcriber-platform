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
        config={"TITLE_GENERATION_FALLBACK_MODELS": ["gemini-2.0-flash"]},
    )

    assert attempts == [
        ("GEMINI", "gemma-4-26b-a4b-it"),
        ("GEMINI", "gemini-2.0-flash"),
    ]


def test_build_title_generation_attempts_deduplicates_fallback(monkeypatch):
    monkeypatch.setattr(
        title_generation.llm_service,
        "get_provider_for_model_code",
        lambda model: "GEMINI",
    )

    attempts = title_generation._build_title_generation_attempts(
        provider="GEMINI",
        model_name="gemini-2.0-flash",
        config={"TITLE_GENERATION_FALLBACK_MODELS": "gemini-2.0-flash, gemini-2.0-flash"},
    )

    assert attempts == [("GEMINI", "gemini-2.0-flash")]


def test_only_generation_errors_are_title_model_retryable():
    assert title_generation._should_try_next_title_model(LlmGenerationError("provider failed"))
    assert not title_generation._should_try_next_title_model(LlmConfigurationError("bad config"))
    assert not title_generation._should_try_next_title_model(LlmRateLimitError("rate limit"))
