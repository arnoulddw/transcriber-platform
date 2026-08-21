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


def test_registering_live_purpose_accumulates_instead_of_clobbering():
    cursor = _RecordingCursor()
    database = _RecordingDatabase()

    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=database
    ):
        transcription_catalog.register_model_from_provider(
            "openai", "gpt-transcribe", "OpenAI GPT Transcribe",
            model_purpose="transcription",
        )
        transcription_catalog.register_model_from_provider(
            "openai", "gpt-transcribe", "OpenAI GPT Transcribe",
            model_purpose="live",
        )

    assert len(cursor.calls) == 2
    first_sql, second_sql = (sql for sql, _ in cursor.calls)
    assert "model_purposes" in first_sql
    assert "model_purpose " not in second_sql.replace("model_purposes ", "")
    # The accumulate idiom must merge purposes on duplicate instead of
    # overwriting them (same contract as user_api_keys.model_purposes).
    for sql in (first_sql, second_sql):
        assert "FIND_IN_SET(VALUES(model_purposes), model_purposes)" in sql
        assert "CONCAT_WS" in sql
    assert cursor.calls[0][1][-1] == "transcription"
    assert cursor.calls[1][1][-1] == "live"


def test_backfill_registers_one_row_with_the_full_purpose_set():
    cursor = _RecordingCursor()
    database = _RecordingDatabase()
    key_rows = [
        {
            "provider_code": "openai",
            "model_slug": "gpt-transcribe",
            "model_purposes": "transcription,live",
        },
    ]

    def fetchall():
        return key_rows

    cursor.execute = lambda sql, params=None: None
    cursor.fetchall = fetchall

    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=database
    ):
        with patch.object(transcription_catalog, "register_model_from_provider") as register:
            transcription_catalog._sync_models_from_saved_keys()

    register.assert_called_once_with(
        provider="openai",
        code="gpt-transcribe",
        display_name="gpt-transcribe",
        model_purpose="transcription,live",
    )


def test_registration_rejects_invalid_purpose_sets_but_accepts_canonical_ones():
    cursor = _RecordingCursor()
    database = _RecordingDatabase()

    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=database
    ):
        transcription_catalog.register_model_from_provider(
            "openai", "model-a", model_purpose="bogus"
        )
        assert cursor.calls == []

        transcription_catalog.register_model_from_provider(
            "openai", "model-b", model_purpose="transcription,live"
        )
        assert [params[-1] for _, params in cursor.calls] == ["transcription,live"]


def test_dual_purpose_row_is_returned_by_both_reader_filters():
    """A 'transcription,live' row must surface in both dropdown sources."""
    cursor = Mock()
    row = {
        "code": "gpt-transcribe",
        "display_name": "OpenAI GPT Transcribe",
        "provider_code": "openai",
        "required_api_key": "openai",
        "permission_key": "use_api_openai",
        "is_default": 0,
        "model_purposes": "transcription,live",
    }
    cursor.fetchall.return_value = [dict(row)]

    with patch.object(transcription_catalog, "get_cursor", return_value=cursor):
        transcription_models = transcription_catalog.get_active_models()
        executed_sql = cursor.execute.call_args[0][0]
        assert "FIND_IN_SET('transcription', m.model_purposes)" in executed_sql
        assert [m["model_key"] for m in transcription_models] == ["openai:gpt-transcribe"]
        assert transcription_models[0]["model_purposes"] == "transcription,live"

        live_models = transcription_catalog.get_live_models()
        live_sql = cursor.execute.call_args[0][0]
        assert "FIND_IN_SET('live', m.model_purposes)" in live_sql
        assert [m["model_key"] for m in live_models] == ["openai:gpt-transcribe"]


def test_admin_models_purpose_filter_uses_membership():
    cursor = Mock()
    cursor.fetchall.return_value = []
    with patch.object(transcription_catalog, "get_cursor", return_value=cursor):
        transcription_catalog.get_all_active_models("live")
    executed_sql = cursor.execute.call_args[0][0]
    assert "FIND_IN_SET(%s, m.model_purposes)" in executed_sql
    assert cursor.execute.call_args[0][1] == ("live",)
