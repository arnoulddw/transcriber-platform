"""Unit tests for the gemini provider wiring in catalog + client factory."""
import sys
import os
import unittest.mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.models import transcription_catalog as catalog  # noqa: E402
from app.services.api_clients import _resolve_transcription_provider  # noqa: E402


def test_split_model_reference_uses_metadata_provider_hint():
    """A bare gemini model resolves to ('gemini', local code) via the hint."""
    assert catalog.split_model_reference(
        "gemini-3.5-transcribe", provider_code="gemini"
    ) == ("gemini", "gemini-3.5-transcribe")


def test_gemini_metadata_declares_provider_keys():
    metadata = catalog._PROVIDER_METADATA["gemini"]
    assert metadata == {
        "display_name": "Google",
        "permission_key": "use_api_google_gemini",
        "required_api_key": "gemini",
        "client_kind": "gemini",
    }


def test_factory_legacy_fallback_resolves_gemini():
    """The catalog-free fallback maps the gemini model to its provider."""
    assert _resolve_transcription_provider("gemini-3.5-transcribe") == "gemini"


def test_register_model_from_provider_inserts_gemini_row():
    cursor = unittest.mock.MagicMock()
    with unittest.mock.patch.object(catalog, "get_cursor", return_value=cursor), \
            unittest.mock.patch.object(catalog, "get_db") as get_db:
        catalog.register_model_from_provider(
            provider="gemini",
            code="gemini-3.5-transcribe",
            display_name="Gemini 3.5 Transcribe",
        )
    sql = cursor.execute.call_args[0][0]
    params = cursor.execute.call_args[0][1]
    assert "INSERT INTO" in sql and "ON DUPLICATE KEY UPDATE" in sql
    assert (
        "gemini-3.5-transcribe",
        "gemini",
        "Gemini 3.5 Transcribe",
        "use_api_google_gemini",
        "gemini",
        "transcription",
    ) == tuple(params)
    get_db.return_value.commit.assert_called_once()


def test_register_model_from_provider_live_purpose():
    cursor = unittest.mock.MagicMock()
    with unittest.mock.patch.object(catalog, "get_cursor", return_value=cursor), \
            unittest.mock.patch.object(catalog, "get_db"):
        catalog.register_model_from_provider(
            provider="gemini",
            code="gemini-live-model",
            display_name="Gemini Live Model",
            model_purpose="live",
        )
    params = cursor.execute.call_args[0][1]
    assert params[-1] == "live"


def test_register_model_unknown_purpose_is_ignored():
    cursor = unittest.mock.MagicMock()
    with unittest.mock.patch.object(catalog, "get_cursor", return_value=cursor), \
            unittest.mock.patch.object(catalog, "get_db"):
        catalog.register_model_from_provider(
            provider="gemini",
            code="gemini-3.5-transcribe",
            display_name="Gemini 3.5 Transcribe",
            model_purpose="bogus",
        )
    cursor.execute.assert_not_called()


def test_register_model_bare_gemini_code_is_ignored():
    """The bare 'gemini' provider label never registers as a selectable model."""
    cursor = unittest.mock.MagicMock()
    with unittest.mock.patch.object(catalog, "get_cursor", return_value=cursor), \
            unittest.mock.patch.object(catalog, "get_db"):
        catalog.register_model_from_provider(
            provider="gemini",
            code="gemini",
            display_name="Google",
        )
    cursor.execute.assert_not_called()
    assert "gemini-3.5-transcribe" not in catalog.PROVIDER_ONLY_MODEL_CODES
