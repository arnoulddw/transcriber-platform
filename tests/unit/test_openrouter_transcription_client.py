from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.services.api_clients import get_transcription_client
from app.services.api_clients.transcription.openrouter import OpenRouterTranscriptionClient


def _make_client(model_code="openrouter"):
    config = {
        "API_LIMITS": {"openrouter": {"duration_s": None, "size_mb": 25}},
        "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
        "TRANSCRIPTION_MODEL_METADATA": {
            "openrouter:microsoft/mai-transcribe-2": {
                "supported_audio_formats": ["mp3", "wav", "flac"],
            },
        },
    }
    with patch("app.services.api_clients.transcription.openai_base.OpenAI"):
        return OpenRouterTranscriptionClient("sk-or-test", config, model_code=model_code)


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


def test_mai_transcribe_converts_unsupported_audio_before_api_call(tmp_path):
    client = _make_client("microsoft/mai-transcribe-2")
    audio_path = tmp_path / "recording.m4a"
    audio_path.write_bytes(b"placeholder audio")
    transcoded_path = tmp_path / "recording_transcoded.mp3"
    transcoded_path.write_bytes(b"transcoded audio")
    api_paths = []

    def call_api(file_handle, _params):
        api_paths.append(file_handle.name)
        return SimpleNamespace(text="Transcript")

    client._call_api = Mock(side_effect=call_api)
    client._split_and_transcribe = Mock()

    with patch(
        "app.services.api_clients.transcription.base_transcription_client.file_service.transcode_audio_file",
        return_value=str(transcoded_path),
    ) as transcode, patch(
        "app.services.api_clients.transcription.base_transcription_client.file_service.get_audio_duration",
        return_value=(60, 1),
    ):
        result = client.transcribe(
            str(audio_path),
            "en",
            original_filename="recording.m4a",
            audio_length_seconds=60,
            extra_options={"model": "microsoft/mai-transcribe-2"},
        )

    assert result == ("Transcript", "en")
    transcode.assert_called_once_with(
        str(audio_path),
        str(tmp_path),
        target_format="mp3",
        progress_callback=client._report_progress,
        cancellation_check=transcode.call_args.kwargs["cancellation_check"],
    )
    assert api_paths == [str(transcoded_path)]
    client._split_and_transcribe.assert_not_called()
    assert not transcoded_path.exists()


@pytest.mark.parametrize("extension", ["mp3", "wav", "flac"])
def test_mai_transcribe_keeps_supported_audio_on_single_file_path(tmp_path, extension):
    client = _make_client("microsoft/mai-transcribe-2")
    audio_path = tmp_path / f"recording.{extension}"
    audio_path.write_bytes(b"placeholder audio")
    client._call_api = Mock(return_value=SimpleNamespace(text="Transcript"))
    client._split_and_transcribe = Mock()

    result = client.transcribe(
        str(audio_path),
        "en",
        original_filename=audio_path.name,
        audio_length_seconds=60,
        extra_options={"model": "microsoft/mai-transcribe-2"},
    )

    assert result == ("Transcript", "en")
    client._split_and_transcribe.assert_not_called()
    client._call_api.assert_called_once()


def test_other_openrouter_models_keep_original_audio_path(tmp_path):
    client = _make_client("openai/whisper-1")
    audio_path = tmp_path / "recording.m4a"
    audio_path.write_bytes(b"placeholder audio")
    client._call_api = Mock(return_value=SimpleNamespace(text="Transcript"))
    client._split_and_transcribe = Mock()

    result = client.transcribe(
        str(audio_path),
        "en",
        original_filename="recording.m4a",
        audio_length_seconds=60,
        extra_options={"model": "openai/whisper-1"},
    )

    assert result == ("Transcript", "en")
    client._split_and_transcribe.assert_not_called()
    client._call_api.assert_called_once()


def test_provider_wide_client_uses_requested_model_format_metadata():
    client = _make_client("openrouter")

    assert client._get_transcode_target(
        "/tmp/recording.m4a",
        {"model": "microsoft/mai-transcribe-2"},
    ) == "mp3"


def test_provider_wide_client_uses_builtin_metadata_with_minimal_config():
    client = _make_client("openrouter")
    client.config.pop("TRANSCRIPTION_MODEL_METADATA")

    assert client._get_transcode_target(
        "/tmp/recording.m4a",
        {"model": "microsoft/mai-transcribe-2"},
    ) == "mp3"
