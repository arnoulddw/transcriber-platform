import pytest

from app.services.openrouter import normalize_openrouter_model, resolve_openrouter_model


def test_accepts_vendor_slash_model():
    assert normalize_openrouter_model("  openai/gpt-transcribe  ") == "openai/gpt-transcribe"


def test_accepts_gemini_llm_slug():
    assert normalize_openrouter_model("google/gemini-3.7-flash") == "google/gemini-3.7-flash"


@pytest.mark.parametrize("raw", ["", "   ", "gpt-transcribe", "openai gpt-transcribe", None])
def test_rejects_invalid_slugs(raw):
    with pytest.raises(ValueError):
        normalize_openrouter_model(raw)


def test_resolve_non_openrouter_returns_none_with_submitted_slug():
    assert resolve_openrouter_model("whisper", "openai/gpt-transcribe") is None


def test_resolve_openrouter_returns_submitted_slug():
    assert resolve_openrouter_model("openrouter", "openai/gpt-transcribe") == "openai/gpt-transcribe"


def test_resolve_openrouter_requires_a_valid_slug():
    with pytest.raises(ValueError, match="OpenRouter model is required\\."):
        resolve_openrouter_model("openrouter", None)
