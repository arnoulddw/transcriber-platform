"""Title-generation fallback model selection."""

from app.services.api_clients.exceptions import (
    LlmApiError,
    LlmConfigurationError,
    LlmGenerationError,
    LlmRateLimitError,
    LlmSafetyError,
)
from app.tasks.title_generation import _should_try_next_title_model


def test_provider_level_errors_try_the_fallback_model():
    assert _should_try_next_title_model(LlmGenerationError()) is True
    assert _should_try_next_title_model(LlmConfigurationError()) is True
    assert _should_try_next_title_model(LlmRateLimitError()) is True
    assert _should_try_next_title_model(ValueError("bad model")) is True


def test_non_retryable_and_unknown_errors_do_not():
    # Safety blocks apply to the content, not the model: a different
    # fallback model would hit the same filter.
    assert _should_try_next_title_model(LlmSafetyError()) is False
    assert _should_try_next_title_model(RuntimeError("unexpected")) is False
