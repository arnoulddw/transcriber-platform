import io
from unittest.mock import patch

import httpx
import pytest
from openai import BadRequestError

from app.services.api_clients.exceptions import TranscriptionProcessingError
from app.services.api_clients.transcription.openai_model_client import (
    OpenAIModelTranscriptionClient,
)
from app.services.api_clients.transcription.openrouter import OpenRouterTranscriptionClient


def _bad_request_error(status_code=400):
    request = httpx.Request("POST", "https://example.test/audio/transcriptions")
    response = httpx.Response(status_code, request=request)
    return BadRequestError(
        f"Provider returned {status_code}",
        response=response,
        body={"error": {"message": f"Provider returned {status_code}", "code": status_code}},
    )


def _make_openrouter_client():
    config = {
        "API_LIMITS": {"openrouter": {"duration_s": None, "size_mb": 25}},
        "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    }
    with patch("app.services.api_clients.transcription.openai_base.OpenAI"):
        return OpenRouterTranscriptionClient(
            "sk-or-test", config, model_code="microsoft/mai-transcribe-2"
        )


def _make_openai_client():
    config = {
        "API_LIMITS": {
            "gpt-transcribe": {
                "duration_s": None,
                "size_mb": 25,
                "response_style": "languages_array",
            }
        }
    }
    with patch("app.services.api_clients.transcription.openai_base.OpenAI"):
        return OpenAIModelTranscriptionClient("gpt-transcribe", "test-key", config)


def test_openrouter_bad_request_uses_provider_label_and_preserves_status():
    client = _make_openrouter_client()
    client.client.audio.transcriptions.create.side_effect = _bad_request_error()

    with pytest.raises(TranscriptionProcessingError) as excinfo:
        client._call_api(io.BytesIO(b"audio"), {"model": "microsoft/mai-transcribe-2"})

    assert str(excinfo.value).startswith("OpenRouter API Error:")
    assert excinfo.value.provider == "OpenRouter"
    assert excinfo.value.status_code == 400


def test_openai_bad_request_keeps_existing_label_and_model_attribution():
    client = _make_openai_client()
    client.client.audio.transcriptions.create.side_effect = _bad_request_error()

    with pytest.raises(TranscriptionProcessingError) as excinfo:
        client._call_api(io.BytesIO(b"audio"), {"model": "gpt-transcribe"})

    assert str(excinfo.value).startswith("OpenAI API Error:")
    assert excinfo.value.provider == client._get_api_name()
    assert excinfo.value.status_code == 400
