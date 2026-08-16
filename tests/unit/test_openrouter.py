import pytest

from app.services.openrouter import normalize_openrouter_model


def test_accepts_vendor_slash_model():
    assert normalize_openrouter_model("  openai/gpt-transcribe  ") == "openai/gpt-transcribe"


def test_accepts_gemini_llm_slug():
    assert normalize_openrouter_model("google/gemini-3.7-flash") == "google/gemini-3.7-flash"


@pytest.mark.parametrize("raw", ["", "   ", "gpt-transcribe", "openai gpt-transcribe", None])
def test_rejects_invalid_slugs(raw):
    with pytest.raises(ValueError):
        normalize_openrouter_model(raw)
