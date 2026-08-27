"""Unit tests for the Gemini transcription client (Interactions API)."""
import io
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from app.services.api_clients import get_transcription_client
from app.services.api_clients.exceptions import (
    TranscriptionAuthenticationError,
    TranscriptionProcessingError,
    TranscriptionRateLimitError,
)
from app.services.api_clients.transcription.gemini import GeminiTranscriptionClient


MODULE = "app.services.api_clients.transcription.gemini"


def _fake_handle(path="/tmp/audio.mp3"):
    handle = io.StringIO()
    handle.name = path
    return handle


def _make_config():
    return {
        "API_LIMITS": {
            "gemini-3.5-transcribe": {"duration_s": 3300, "size_mb": None, "rate_limit_rpm": None},
        },
    }


def _make_client(config=None):
    with patch(f"{MODULE}.genai") as mock_genai:
        client = GeminiTranscriptionClient("gemini-key", config or _make_config())
    return client, mock_genai


class _FakeGenai:
    """Context manager faking the module-level genai + genai_errors symbols."""

    def __init__(self, config=None):
        self._stack = ExitStack()
        self.config = config or _make_config()

    def __enter__(self):
        self.mock_genai = self._stack.enter_context(patch(f"{MODULE}.genai"))
        fake_errors = MagicMock()
        fake_errors.APIError = _FakeAPIError
        self._stack.enter_context(patch(f"{MODULE}.genai_errors", fake_errors))
        self.client = GeminiTranscriptionClient("gemini-key", self.config)
        upload = MagicMock()
        upload.name = "files/abc123"
        upload.uri = "https://files.example/abc123"
        upload.mime_type = "audio/mpeg"
        self.upload = upload
        self.mock_genai.Client.return_value.files.upload.return_value = upload
        return self

    def __exit__(self, *exc_info):
        return self._stack.__exit__(*exc_info)


# --- _prepare_api_params ---


def test_auto_language_uses_empty_language_codes():
    client, _ = _make_client()
    params = client._prepare_api_params("auto", "", "json", False)
    assert params["transcription_config"]["language_codes"] == []


def test_explicit_language_passes_short_code_as_is():
    client, _ = _make_client()
    params = client._prepare_api_params("en", "", "json", False)
    assert params["transcription_config"]["language_codes"] == ["en"]


def test_invalid_language_falls_back_to_auto_detection():
    client, _ = _make_client()
    params = client._prepare_api_params("xx", "", "json", False)
    assert params["transcription_config"]["language_codes"] == []


def test_context_prompt_becomes_custom_vocabulary_terms():
    client, _ = _make_client()
    params = client._prepare_api_params(
        "auto", "Noud, Adyen\nAdyen avans, scalapay", "json", False
    )
    vocab = params["transcription_config"]["custom_vocabulary"]
    assert vocab == ["Noud", "Adyen", "Adyen avans", "scalapay"]


def test_context_prompt_vocabulary_dedupes_case_insensitively():
    client, _ = _make_client()
    params = client._prepare_api_params("auto", "Adyen, adyen ,ADYEN", "json", False)
    vocab = params["transcription_config"]["custom_vocabulary"]
    assert vocab == ["Adyen"]


def test_empty_prompt_omits_custom_vocabulary_key():
    client, _ = _make_client()
    params = client._prepare_api_params("auto", "   ", "json", False)
    assert "custom_vocabulary" not in params["transcription_config"]
    assert "mode" not in params["transcription_config"]


def test_custom_vocabulary_is_capped_at_1000_terms():
    client, _ = _make_client()
    prompt = ", ".join(f"term{i}" for i in range(1500))
    params = client._prepare_api_params("auto", prompt, "json", False)
    vocab = params["transcription_config"]["custom_vocabulary"]
    assert len(vocab) == 1000
    assert vocab[0] == "term0"


def test_progress_messages_suppressed_for_chunks():
    client, _ = _make_client()
    reported = []
    with patch.object(client, "_report_progress", side_effect=lambda m, e=False: reported.append(m)):
        client._prepare_api_params("auto", "Noud, Adyen", "json", is_chunk=True)
    # The vocabulary progress line is chunk-suppressed; the auto-detect notice
    # follows the sibling guard (not is_chunk or auto).
    assert reported == ["Language detection requested."]


def test_auto_language_reports_detection_once():
    client, _ = _make_client()
    reported = []
    with patch.object(client, "_report_progress", side_effect=lambda m, e=False: reported.append(m)):
        client._prepare_api_params("auto", "", "json", is_chunk=False)
    assert "Language detection requested." in reported


def test_explicit_language_reports_language_set():
    client, _ = _make_client()
    reported = []
    with patch.object(client, "_report_progress", side_effect=lambda m, e=False: reported.append(m)):
        client._prepare_api_params("es", "", "json", is_chunk=False)
    assert "Language set to 'es'." in reported


# --- _call_api ---


class _FakeAPIError(Exception):
    def __init__(self, code, message="boom", status="INVALID_ARGUMENT"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@pytest.fixture()
def fake_interaction():
    interaction = MagicMock()
    interaction.output_text = "  hello transcript  "
    return interaction


def test_call_uploads_transcribes_and_deletes(fake_interaction):
    with _FakeGenai() as fake:
        fake.mock_genai.Client.return_value.interactions.create.return_value = fake_interaction
        api_params = {"transcription_config": {"language_codes": []}}
        result = fake.client._call_api(_fake_handle(), api_params)

        assert result is fake_interaction
        fake.mock_genai.Client.return_value.files.upload.assert_called_once_with(file="/tmp/audio.mp3")
        kwargs = fake.mock_genai.Client.return_value.interactions.create.call_args.kwargs
        assert kwargs["model"] == "gemini-3.5-transcribe"
        assert kwargs["input"] == [
            {"type": "audio", "uri": fake.upload.uri, "mime_type": fake.upload.mime_type}
        ]
        assert kwargs["generation_config"] == api_params
        fake.mock_genai.Client.return_value.files.delete.assert_called_once_with(name=fake.upload.name)


def test_upload_failure_maps_error_and_skips_delete(fake_interaction):
    with _FakeGenai() as fake:
        fake.mock_genai.Client.return_value.files.upload.side_effect = _FakeAPIError(400, "bad audio")

        with pytest.raises(TranscriptionProcessingError) as excinfo:
            fake.client._call_api(_fake_handle(), {"transcription_config": {}})
    assert "Gemini API Error" in str(excinfo.value)
    fake.mock_genai.Client.return_value.files.delete.assert_not_called()


def test_auth_errors_map_to_authentication_error(fake_interaction):
    for code in (401, 403):
        with _FakeGenai() as fake:
            fake.mock_genai.Client.return_value.interactions.create.side_effect = _FakeAPIError(code, "denied")
            with pytest.raises(TranscriptionAuthenticationError):
                fake.client._call_api(_fake_handle(), {"transcription_config": {}})


def test_rate_limit_error_maps_to_rate_limit_error():
    with _FakeGenai() as fake:
        interaction = MagicMock()
        interaction.output_text = "text"
        fake.mock_genai.Client.return_value.interactions.create.return_value = interaction
        fake.mock_genai.Client.return_value.interactions.create.side_effect = _FakeAPIError(429, "slow down")

        with pytest.raises(TranscriptionRateLimitError):
            fake.client._call_api(_fake_handle(), {"transcription_config": {}})


def test_bad_request_and_server_errors_map_to_processing_error():
    for code in (400, 500, 503):
        with _FakeGenai() as fake:
            interaction = MagicMock()
            interaction.output_text = "text"
            fake.mock_genai.Client.return_value.interactions.create.side_effect = _FakeAPIError(code, f"err{code}")

            with pytest.raises(TranscriptionProcessingError) as excinfo:
                fake.client._call_api(_fake_handle(), {"transcription_config": {}})
            assert str(code) in str(excinfo.value)


def test_delete_failure_never_breaks_the_call(fake_interaction):
    with _FakeGenai() as fake:
        fake.mock_genai.Client.return_value.interactions.create.return_value = fake_interaction
        fake.mock_genai.Client.return_value.files.delete.side_effect = RuntimeError("cleanup boom")
        result = fake.client._call_api(_fake_handle(), {"transcription_config": {}})
    assert result is fake_interaction


# --- _process_response / retryable ---


def test_process_response_strips_text_and_returns_none_language(fake_interaction):
    client, _ = _make_client()
    text, language = client._process_response(fake_interaction, None)
    assert text == "hello transcript"
    assert language is None


def test_process_response_empty_text_raises_processing_error():
    client, _ = _make_client()
    interaction = MagicMock()
    interaction.output_text = ""
    with pytest.raises(TranscriptionProcessingError):
        client._process_response(interaction, None)


def test_process_response_none_output_raises_processing_error():
    client, _ = _make_client()
    interaction = MagicMock()
    interaction.output_text = None
    with pytest.raises(TranscriptionProcessingError):
        client._process_response(interaction, None)


def test_process_response_malformed_object_raises_processing_error():
    client, _ = _make_client()

    class Broken:
        @property
        def output_text(self):
            raise AttributeError("nope")

    with pytest.raises(TranscriptionProcessingError):
        client._process_response(Broken(), None)


def test_retryable_errors_are_rate_limit_only():
    client, _ = _make_client()
    assert client._get_retryable_errors() == (TranscriptionRateLimitError,)


# --- limits wiring ---


def test_limits_wired_from_config():
    client, _ = _make_client()
    assert client.CATALOG_MODEL_CODE == "gemini-3.5-transcribe"
    assert client.SPLIT_THRESHOLD_SECONDS == 3300
    # Files API accepts large media: keep the byte threshold high so the
    # duration rule governs splitting.
    assert client.SPLIT_THRESHOLD_BYTES >= 1024 * 1024 * 1024


def test_blank_model_code_falls_back_to_catalog_default():
    with patch(f"{MODULE}.genai"):
        client = GeminiTranscriptionClient("gemini-key", _make_config(), model_code="   ")
    assert client.CATALOG_MODEL_CODE == "gemini-3.5-transcribe"


# --- factory ---


def test_factory_routes_provider_qualified_gemini_reference():
    with patch(f"{MODULE}.genai"):
        client = get_transcription_client(
            "gemini:gemini-3.5-transcribe", "key", _make_config()
        )
    assert isinstance(client, GeminiTranscriptionClient)
    assert client.CATALOG_MODEL_CODE == "gemini-3.5-transcribe"


def test_factory_routes_legacy_gemini_model_code():
    with patch(f"{MODULE}.genai"):
        client = get_transcription_client("gemini-3.5-transcribe", "key", _make_config())
    assert isinstance(client, GeminiTranscriptionClient)
    assert client.CATALOG_MODEL_CODE == "gemini-3.5-transcribe"
