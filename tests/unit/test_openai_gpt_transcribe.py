from types import SimpleNamespace
from unittest.mock import patch

from app.services.api_clients import get_transcription_client
from app.services.api_clients.transcription.openai_gpt_transcribe import (
    OpenAIGPTTranscribeClient,
)


def _make_client():
    config = {
        "API_LIMITS": {
            "gpt-transcribe": {"duration_s": None, "size_mb": 25}
        }
    }
    with patch(
        "app.services.api_clients.transcription.openai_base.OpenAI"
    ):
        return OpenAIGPTTranscribeClient("test-key", config)


def test_prepares_gpt_transcribe_language_hints_with_extra_body():
    client = _make_client()

    params = client._prepare_api_params(
        language_code="en",
        context_prompt="Payments industry terminology",
        response_format="text",
        is_chunk=False,
    )

    assert params == {
        "model": "gpt-transcribe",
        "response_format": "json",
        "prompt": "Payments industry terminology",
        "extra_body": {"languages": ["en"]},
    }


def test_auto_detection_omits_language_hints():
    client = _make_client()

    params = client._prepare_api_params(
        language_code="auto",
        context_prompt="",
        response_format="verbose_json",
        is_chunk=False,
    )

    assert params == {
        "model": "gpt-transcribe",
        "response_format": "json",
    }


def test_processes_detected_language_from_gpt_transcribe_response():
    client = _make_client()
    response = SimpleNamespace(
        text="Bonjour",
        languages=[SimpleNamespace(code="fr")],
    )

    assert client._process_response(response, "json") == ("Bonjour", "fr")


def test_factory_routes_gpt_transcribe_to_its_client():
    config = {
        "API_LIMITS": {
            "gpt-transcribe": {"duration_s": None, "size_mb": 25}
        }
    }

    with patch(
        "app.services.api_clients.transcription.openai_base.OpenAI"
    ):
        client = get_transcription_client("gpt-transcribe", "test-key", config)

    assert isinstance(client, OpenAIGPTTranscribeClient)
