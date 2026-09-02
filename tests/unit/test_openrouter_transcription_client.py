from unittest.mock import patch

from app.services.api_clients import get_transcription_client
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
    assert client.RETURNS_DETECTED_LANGUAGE is False


def test_transcription_factory_routes_openrouter():
    config = {
        "API_LIMITS": {"openrouter": {"size_mb": 25}},
        "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    }
    with patch("app.services.api_clients.transcription.openai_base.OpenAI"):
        client = get_transcription_client("openrouter", "sk-or-test", config)
    assert isinstance(client, OpenRouterTranscriptionClient)


def test_prepare_params_warns_once_on_context_prompt():
    client = _make_client()
    reported = []
    client._report_progress = lambda msg, is_error=False: reported.append(msg)
    for _ in range(2):
        client._prepare_api_params(
            language_code="auto",
            context_prompt="Project Falcon budget review",
            response_format="json",
            is_chunk=False,
            extra_options={"model": "openai/whisper-1"},
        )
    warnings = [m for m in reported if "context prompt" in m.lower()]
    assert len(warnings) == 1


def test_prepare_params_no_warning_without_prompt():
    client = _make_client()
    reported = []
    client._report_progress = lambda msg, is_error=False: reported.append(msg)
    client._prepare_api_params(
        language_code="auto",
        context_prompt="",
        response_format="json",
        is_chunk=False,
        extra_options={"model": "openai/whisper-1"},
    )
    assert not any("context prompt" in m.lower() for m in reported)
