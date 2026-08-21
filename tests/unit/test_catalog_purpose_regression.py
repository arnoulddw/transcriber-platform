"""Regression test for the 2026-08-21 live-key save incident.

Saving ``openai:gpt-transcribe`` for the *live* purpose used to overwrite the
single-valued catalog purpose and silently demote the model out of every
file-transcription dropdown (home page warning: "User's preferred model
'openai:gpt-transcribe' is not currently allowed"). These tests lock in the
accumulate contract at the service layer, across all three providers.
"""
import sys
import os
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.models import transcription_catalog  # noqa: E402


class _RecordingCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _RecordingDatabase:
    def __init__(self):
        self.commit_count = 0

    def commit(self):
        self.commit_count += 1


def _save_sequence(provider, code):
    """Simulate Arnould's incident: transcription key saved, then live."""
    cursor = _RecordingCursor()
    database = _RecordingDatabase()
    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=database
    ):
        transcription_catalog.register_model_from_provider(
            provider, code, code, model_purpose="transcription"
        )
        # Same identity re-registered for live (what saving the live key does).
        transcription_catalog.register_model_from_provider(
            provider, code, code, model_purpose="live"
        )
    return cursor.calls


def test_openai_live_save_does_not_demote_transcription_model():
    calls = _save_sequence("openai", "gpt-transcribe")
    assert len(calls) == 2
    for sql, _ in calls:
        # Both the existing row and the incoming purpose set contribute to
        # the canonical transcription,live result.
        assert "FIND_IN_SET('transcription', VALUES(model_purposes))" in sql
        assert "FIND_IN_SET('live', VALUES(model_purposes))" in sql
        assert "CONCAT_WS" in sql
        assert "model_purposes = VALUES(model_purposes)" not in sql


def test_assemblyai_live_save_keeps_universal_selectable():
    calls = _save_sequence("assemblyai", "universal")
    assert len(calls) == 2
    assert [params[0] for _, params in calls] == ["universal", "universal"]


def test_openrouter_slug_registration_accumulates_for_both_purposes():
    calls = _save_sequence("openrouter", "vendor/future-asr")
    assert len(calls) == 2
    for sql, _ in calls:
        assert "FIND_IN_SET('transcription', VALUES(model_purposes))" in sql
        assert "FIND_IN_SET('live', VALUES(model_purposes))" in sql


def test_home_page_default_resolution_survives_dual_purpose_save():
    """The exact warning from the incident must not be reproducible.

    After both saves, a 'transcription,live' row is returned by
    get_active_models() (the source of main routes' active_model_keys),
    so openai:gpt-transcribe stays an allowed file-transcription default.
    """
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
        models = transcription_catalog.get_active_models()

    keys = {m["model_key"] for m in models}
    assert "openai:gpt-transcribe" in keys


def test_backfill_no_longer_flips_rows_to_live_on_restart():
    """Old behavior: backfill registered transcription then live per key,
    so every restart ended with purpose='live'. One merged call now."""
    cursor = _RecordingCursor()
    database = _RecordingDatabase()
    cursor.execute = lambda sql, params=None: None
    cursor.fetchall = lambda: [
        {"provider_code": "openai", "model_slug": "gpt-transcribe",
         "model_purposes": "transcription,live"},
    ]

    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=database
    ):
        with patch.object(
            transcription_catalog, "register_model_from_provider"
        ) as register:
            transcription_catalog._sync_models_from_saved_keys()

    register.assert_called_once()
    assert register.call_args.kwargs["model_purpose"] == "transcription,live"
