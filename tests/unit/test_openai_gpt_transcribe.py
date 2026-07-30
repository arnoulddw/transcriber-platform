from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services.api_clients import get_transcription_client
from app.services.api_clients.exceptions import TranscriptionProcessingError
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


def test_maps_corrupted_or_unsupported_audio_to_actionable_message():
    client = _make_client()

    message = client._map_bad_request_to_user_message(
        "Error code: 400 - {'error': {'message': "
        "'Audio file might be corrupted or unsupported', 'code': 'invalid_value'}}",
        "OpenAI GPT Transcribe",
    )

    assert message == (
        "OpenAI GPT Transcribe could not read this audio file. It may be corrupted "
        "or use an unsupported audio codec. Export it as MP3 or WAV and try again."
    )


def test_reencodes_and_retries_audio_rejected_by_provider(tmp_path):
    client = _make_client()
    audio_path = tmp_path / "recording.m4a"
    audio_path.write_bytes(b"placeholder audio")
    client._call_api = Mock(side_effect=TranscriptionProcessingError(
        "OpenAI GPT Transcribe could not read this audio file. It may be corrupted "
        "or use an unsupported audio codec.",
        provider="OpenAI GPT Transcribe",
    ))
    client._split_and_transcribe = Mock(return_value=("Recovered transcript", "en"))

    result = client.transcribe(
        str(audio_path),
        "en",
        original_filename="recording.m4a",
        audio_length_seconds=60,
    )

    assert result == ("Recovered transcript", "en")
    client._split_and_transcribe.assert_called_once_with(
        str(audio_path),
        "en",
        "",
        "recording.m4a",
        extra_options=None,
    )
