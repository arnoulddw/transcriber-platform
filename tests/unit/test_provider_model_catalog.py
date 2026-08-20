from unittest.mock import Mock, patch

from app.models import transcription_catalog
from app.services.api_clients.transcription.assemblyai import AssemblyAITranscriptionAPI


class _RecordingCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))


class _RecordingDatabase:
    def __init__(self):
        self.commit_count = 0

    def commit(self):
        self.commit_count += 1


def test_new_models_for_supported_providers_register_through_one_catalog_path():
    cursor = _RecordingCursor()
    database = _RecordingDatabase()

    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=database
    ):
        transcription_catalog.register_model_from_provider(
            "openai", "future-openai-transcribe", "Future OpenAI Transcribe"
        )
        transcription_catalog.register_model_from_provider(
            "assemblyai", "future-assembly-model", "Future AssemblyAI Model"
        )
        transcription_catalog.register_model_from_provider(
            "openrouter", "vendor/future-asr", "Vendor Future ASR"
        )

    assert [params[0] for _, params in cursor.calls] == [
        "future-openai-transcribe",
        "future-assembly-model",
        "vendor/future-asr",
    ]
    assert [params[1] for _, params in cursor.calls] == [
        "openai",
        "assemblyai",
        "openrouter",
    ]
    assert database.commit_count == 3


def test_provider_and_retired_codes_are_not_registered_as_models():
    cursor = _RecordingCursor()
    database = _RecordingDatabase()

    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=database
    ):
        transcription_catalog.register_model_from_provider("openai", "openai")
        transcription_catalog.register_model_from_provider("assemblyai", "assemblyai")
        transcription_catalog.register_model_from_provider("openrouter", "openrouter")
        transcription_catalog.register_model_from_provider("openai", "whisper")
        transcription_catalog.register_model_from_provider(
            "openai", "gpt-4o-transcribe-diarize"
        )

    assert [params[0] for _, params in cursor.calls] == ["universal"]
    assert [params[1] for _, params in cursor.calls] == ["assemblyai"]
    assert database.commit_count == 1


def test_new_assemblyai_model_is_sent_as_the_provider_local_speech_model():
    config = {
        "API_LIMITS": {
            "assemblyai": {"duration_s": None, "size_mb": None},
            "future-assembly-model": {"duration_s": None, "size_mb": None},
        },
        "TRANSCRIPTION_WORKERS": 1,
    }

    with patch.object(AssemblyAITranscriptionAPI, "_initialize_client", return_value=None):
        client = AssemblyAITranscriptionAPI(
            "test-key", config, model_code="future-assembly-model"
        )

    params = client._prepare_api_params(
        language_code="auto",
        context_prompt="",
        response_format="json",
        is_chunk=False,
    )

    assert client.CATALOG_MODEL_CODE == "future-assembly-model"
    assert params["speech_models"] == ["future-assembly-model"]


def test_bare_duplicate_model_code_requires_a_canonical_provider_key():
    cursor = Mock()
    cursor.fetchall.return_value = [
        {
            "code": "shared-transcribe",
            "display_name": "OpenAI Shared Transcribe",
            "provider_code": "openai",
            "required_api_key": "openai",
        },
        {
            "code": "shared-transcribe",
            "display_name": "AssemblyAI Shared Transcribe",
            "provider_code": "assemblyai",
            "required_api_key": "assemblyai",
        },
    ]

    with patch.object(transcription_catalog, "get_cursor", return_value=cursor):
        assert transcription_catalog.get_model_by_code("shared-transcribe") is None


def test_live_model_catalog_does_not_expose_provider_rows():
    cursor = Mock()
    cursor.fetchall.return_value = [
        {
            "code": "openrouter",
            "display_name": "OpenRouter",
            "provider_code": "openrouter",
            "required_api_key": "openrouter",
        },
        {
            "code": "future-live-model",
            "display_name": "Future Live Model",
            "provider_code": "openai",
            "required_api_key": "openai",
        },
    ]

    with patch.object(transcription_catalog, "get_cursor", return_value=cursor):
        models = transcription_catalog.get_live_models()

    assert [model["code"] for model in models] == ["future-live-model"]


def test_catalog_request_resolution_returns_provider_local_code_for_openai_model():
    from app.api.transcriptions import _resolve_catalog_model_parameters

    model_key = "openai:future-openai-transcribe"
    provider, model_code = _resolve_catalog_model_parameters(
        model_key,
        {
            model_key: {
                "code": "future-openai-transcribe",
                "model_key": model_key,
                "provider_code": "openai",
                "required_api_key": "openai",
            }
        },
    )

    assert (provider, model_code) == ("openai", "future-openai-transcribe")


def test_legacy_provider_row_still_resolves_without_provider_metadata():
    from app.services import api_clients

    legacy_row = {
        "display_name": "OpenRouter",
        "permission_key": "use_api_openrouter",
    }
    with patch.object(transcription_catalog, "get_model_by_code", return_value=legacy_row):
        provider, local_code, row = api_clients._resolve_transcription_model_reference("openrouter")

    assert (provider, local_code, row) == ("openrouter", "", legacy_row)
