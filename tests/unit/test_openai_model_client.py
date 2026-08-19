from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.services.api_clients import get_transcription_client
from app.services.api_clients.exceptions import TranscriptionProcessingError
from app.services.api_clients.transcription.openai_model_client import (
    OpenAIModelTranscriptionClient,
)

BASE_CONFIG = {
    "API_LIMITS": {
        "whisper": {"duration_s": None, "size_mb": 25, "api_model_name": "whisper-1", "response_style": "whisper"},
        "gpt-4o-transcribe": {"duration_s": 420, "size_mb": 25, "response_style": "standard"},
        "gpt-transcribe": {"duration_s": None, "size_mb": 25, "response_style": "languages_array"},
    }
}


def _make_client(code="gpt-transcribe", config=None):
    with patch("app.services.api_clients.transcription.openai_base.OpenAI"):
        return OpenAIModelTranscriptionClient(code, "test-key", config or BASE_CONFIG)


# --- whisper style ---

def test_whisper_auto_uses_verbose_json():
    client = _make_client("whisper")
    params = client._prepare_api_params("auto", "", "verbose_json", is_chunk=False)
    assert params["model"] == "whisper-1"
    assert params["response_format"] == "verbose_json"


def test_whisper_explicit_language_uses_language_param_and_text():
    client = _make_client("whisper")
    params = client._prepare_api_params("en", "ctx", "text", is_chunk=False)
    assert params == {"model": "whisper-1", "prompt": "ctx", "language": "en", "response_format": "text"}


def test_whisper_parses_detected_language():
    client = _make_client("whisper")
    response = SimpleNamespace(text="Bonjour", language="fr")
    assert client._process_response(response, "verbose_json") == ("Bonjour", "fr")


# --- standard style (gpt-4o-transcribe) ---

def test_standard_auto_uses_json_and_never_parses_language():
    client = _make_client("gpt-4o-transcribe")
    params = client._prepare_api_params("auto", "", "json", is_chunk=False)
    assert params["model"] == "gpt-4o-transcribe"
    assert params["response_format"] == "json"
    response = SimpleNamespace(text="Hola")
    assert client._process_response(response, "json") == ("Hola", None)


def test_standard_explicit_language_uses_language_param():
    client = _make_client("gpt-4o-transcribe")
    params = client._prepare_api_params("es", "", "text", is_chunk=False)
    assert params == {"model": "gpt-4o-transcribe", "prompt": "", "language": "es", "response_format": "text"}


# --- languages_array style (gpt-transcribe) ---

def test_languages_array_uses_extra_body():
    client = _make_client("gpt-transcribe")
    params = client._prepare_api_params("en", "Payments industry terminology", "text", is_chunk=False)
    assert params == {
        "model": "gpt-transcribe",
        "response_format": "json",
        "prompt": "Payments industry terminology",
        "extra_body": {"languages": ["en"]},
    }


def test_languages_array_auto_omits_language_hints():
    client = _make_client("gpt-transcribe")
    params = client._prepare_api_params("auto", "", "verbose_json", is_chunk=False)
    assert params == {"model": "gpt-transcribe", "response_format": "json"}


def test_languages_array_parses_first_language_code():
    client = _make_client("gpt-transcribe")
    response = SimpleNamespace(text="Bonjour", languages=[SimpleNamespace(code="fr")])
    assert client._process_response(response, "json") == ("Bonjour", "fr")


# --- provider resolution + factory routing (no DB context -> legacy fallback map) ---

@pytest.mark.parametrize("code", [
    "whisper",
    "gpt-4o-transcribe",
    "gpt-transcribe",
])
def test_factory_routes_openai_models_to_parameterized_client(code):
    with patch("app.services.api_clients.transcription.openai_base.OpenAI"):
        client = get_transcription_client(code, "test-key", BASE_CONFIG)
    assert isinstance(client, OpenAIModelTranscriptionClient)
    assert client.CATALOG_MODEL_CODE == code
    assert client.api_model_name == BASE_CONFIG["API_LIMITS"][code].get("api_model_name", code)


def test_resolve_provider_openai_codes():
    from app.services.api_clients import _resolve_transcription_provider
    assert _resolve_transcription_provider("whisper") == "openai"
    assert _resolve_transcription_provider("gpt-4o-transcribe") == "openai"
    assert _resolve_transcription_provider("gpt-transcribe") == "openai"
    assert _resolve_transcription_provider("gpt-live-stuff") == "openai"
    assert _resolve_transcription_provider("assemblyai") == "assemblyai"
    assert _resolve_transcription_provider("openrouter") == "openrouter"


# --- inherited behaviour kept intact ---

def test_maps_corrupted_or_unsupported_audio_to_actionable_message():
    client = _make_client("gpt-transcribe")
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
    client = _make_client("gpt-transcribe")
    audio_path = tmp_path / "recording.m4a"
    audio_path.write_bytes(b"placeholder audio")
    client._call_api = Mock(side_effect=TranscriptionProcessingError(
        "OpenAI GPT Transcribe could not read this audio file. It may be corrupted "
        "or use an unsupported audio codec.",
        provider="OpenAI GPT Transcribe",
    ))
    client._split_and_transcribe = Mock(return_value=("Recovered transcript", "en"))

    result = client.transcribe(
        str(audio_path), "en",
        original_filename="recording.m4a",
        audio_length_seconds=60,
    )

    assert result == ("Recovered transcript", "en")
    client._split_and_transcribe.assert_called_once_with(
        str(audio_path), "en", "", "recording.m4a", extra_options=None,
    )