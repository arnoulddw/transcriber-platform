from unittest.mock import patch

from app.services.api_clients.transcription.openrouter import OpenRouterTranscriptionClient


def _make_client():
    config = {
        "API_LIMITS": {"openrouter": {"duration_s": None, "size_mb": 25}},
        "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    }
    with patch("app.services.api_clients.transcription.openai_base.OpenAI"):
        return OpenRouterTranscriptionClient("sk-or-test", config)


def test_sets_openrouter_base_url():
    config = {
        "API_LIMITS": {"openrouter": {"size_mb": 25}},
        "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    }
    with patch("app.services.api_clients.transcription.openai_base.OpenAI") as mock_openai:
        OpenRouterTranscriptionClient("sk-or-test", config)
    assert mock_openai.call_args.kwargs["base_url"] == "https://openrouter.ai/api/v1"
    assert mock_openai.call_args.kwargs["api_key"] == "sk-or-test"


def test_uses_slug_from_extra_options():
    client = _make_client()
    params = client._prepare_api_params(
        language_code="en",
        context_prompt="ignored",
        response_format="json",
        is_chunk=False,
        extra_options={"model": "openai/gpt-transcribe"},
    )
    assert params["model"] == "openai/gpt-transcribe"
    assert params["language"] == "en"
    assert params["response_format"] == "json"
    assert "prompt" not in params


def test_auto_language_omits_language_param():
    client = _make_client()
    params = client._prepare_api_params(
        language_code="auto",
        context_prompt="",
        response_format="json",
        is_chunk=False,
        extra_options={"model": "openai/whisper-1"},
    )
    assert "language" not in params
