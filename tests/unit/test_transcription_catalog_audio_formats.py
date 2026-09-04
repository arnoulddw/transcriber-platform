"""Tests for per-model audio-format metadata in the transcription catalog."""

from unittest.mock import Mock, patch

from flask import Flask

from app.models import transcription_catalog
from migrations.V20260904_1__catalog_model_audio_formats import upgrade


MAI_MODEL = "microsoft/mai-transcribe-2"
MAI_FORMATS = ["mp3", "wav", "flac"]


class _MigrationCursor:
    def __init__(self, fetchone_values):
        self.fetchone_values = iter(fetchone_values)
        self.calls = []
        self.closed = False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return next(self.fetchone_values)

    def close(self):
        self.closed = True


class _MigrationDatabase:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


def test_audio_formats_are_canonicalized_from_text_json_and_extensions():
    assert transcription_catalog.canonicalize_supported_audio_formats(
        [".MP3", "wav", "mp3", " FLAC "]
    ) == "mp3,wav,flac"
    assert transcription_catalog.canonicalize_supported_audio_formats(
        '["wav", "mp3"]'
    ) == "wav,mp3"
    assert transcription_catalog.canonicalize_supported_audio_formats(
        "wav, .flac"
    ) == "wav,flac"


def test_row_to_model_exposes_supported_formats_as_a_list():
    row = {
        "code": MAI_MODEL,
        "provider_code": "openrouter",
        "display_name": "MAI Transcribe 2",
        "required_api_key": "openrouter",
        "supported_audio_formats": "mp3,wav,flac",
    }

    model = transcription_catalog._row_to_model(row)

    assert model["supported_audio_formats"] == MAI_FORMATS


def test_get_supported_audio_formats_reads_catalog_metadata():
    with patch.object(
        transcription_catalog,
        "get_model_by_code",
        return_value={"supported_audio_formats": "mp3,wav,flac"},
    ):
        assert transcription_catalog.get_supported_audio_formats(
            "openrouter:" + MAI_MODEL
        ) == MAI_FORMATS


def test_known_model_uses_config_metadata_when_catalog_row_is_missing():
    with patch.object(transcription_catalog, "get_model_by_code", return_value=None):
        assert transcription_catalog.get_supported_audio_formats(
            "openrouter:" + MAI_MODEL
        ) == MAI_FORMATS


def test_unknown_model_without_metadata_returns_none():
    with patch.object(transcription_catalog, "get_model_by_code", return_value=None):
        assert transcription_catalog.get_supported_audio_formats(
            "openrouter:vendor/future-asr"
        ) is None


def test_configured_format_lookup_does_not_query_catalog():
    with patch.object(transcription_catalog, "get_model_by_code") as get_model:
        assert transcription_catalog.get_configured_supported_audio_formats(
            MAI_MODEL, provider_code="openrouter"
        ) == MAI_FORMATS

    get_model.assert_not_called()


def test_app_config_can_describe_a_new_model_without_client_changes():
    app = Flask(__name__)
    app.config[transcription_catalog.MODEL_METADATA_CONFIG_KEY] = {
        "openrouter:vendor/future-asr": {
            "supported_audio_formats": ["wav", ".FLAC"],
        },
    }

    with app.app_context(), patch.object(
        transcription_catalog, "get_model_by_code", return_value=None
    ):
        assert transcription_catalog.get_supported_audio_formats(
            "openrouter:vendor/future-asr"
        ) == ["wav", "flac"]


def test_registering_known_model_persists_configured_audio_formats():
    cursor = Mock()
    database = Mock()

    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=database
    ):
        transcription_catalog.register_model_from_provider(
            "openrouter", MAI_MODEL, "MAI Transcribe 2"
        )

    sql, params = cursor.execute.call_args[0]
    assert "supported_audio_formats" in sql
    assert params[-1] == "mp3,wav,flac"
    database.commit.assert_called_once()


def test_registering_model_without_metadata_keeps_legacy_parameter_shape():
    cursor = Mock()
    database = Mock()

    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=database
    ):
        transcription_catalog.register_model_from_provider(
            "openai", "future-transcribe", "Future Transcribe"
        )

    sql, params = cursor.execute.call_args[0]
    assert "supported_audio_formats" not in sql
    assert len(params) == 6


def test_audio_formats_migration_adds_column_and_backfills_known_model():
    cursor = _MigrationCursor([object(), None])
    database = _MigrationDatabase(cursor)

    upgrade(database)

    assert any("ADD COLUMN supported_audio_formats" in sql for sql, _ in cursor.calls)
    assert cursor.calls[-1][1] == (
        "mp3,wav,flac",
        "openrouter",
        MAI_MODEL,
    )
    assert database.committed is True
    assert cursor.closed is True
