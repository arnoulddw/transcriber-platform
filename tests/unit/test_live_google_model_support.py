from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Flask
from flask_babel import Babel

from app.services import live_transcription_service as service


def test_google_live_model_is_rejected_before_the_openai_runtime_is_called(monkeypatch):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-secret",
        DEPLOYMENT_MODE="single",
        OPENAI_API_KEY="openai-key",
        LIVE_TRANSCRIPTION_MODEL="gemini-2.5-flash-native-audio",
        LIVE_TRANSCRIPTION_MODELS=["gemini-2.5-flash-native-audio"],
        LIVE_TRANSCRIPTION_PROVIDERS={"gemini-2.5-flash-native-audio": "google"},
    )
    Babel(app)
    monkeypatch.setattr(service, "_validate_settings", lambda *_: ("auto", ""))
    post = MagicMock()
    monkeypatch.setattr(service.httpx, "post", post)

    user = SimpleNamespace(id=7)
    with app.app_context(), pytest.raises(service.LiveTranscriptionValidationError, match="not supported"):
        service.create_session(user, "offer-sdp", "auto", "", "gemini-2.5-flash-native-audio")

    post.assert_not_called()


def test_live_model_allowlist_accepts_a_google_named_model_before_upstream_call():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-secret",
        LIVE_TRANSCRIPTION_MODEL="gemini-2.5-flash-native-audio",
        LIVE_TRANSCRIPTION_MODELS=["gemini-2.5-flash-native-audio"],
    )
    Babel(app)

    with app.app_context():
        assert service._resolve_live_model(
            SimpleNamespace(default_live_transcription_model=None, role=None),
            "gemini-2.5-flash-native-audio",
        ) == "gemini-2.5-flash-native-audio"


def test_live_provider_configuration_defaults_existing_models_to_openai():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-secret",
        LIVE_TRANSCRIPTION_MODEL="gpt-live-transcribe",
        LIVE_TRANSCRIPTION_MODELS=["gpt-live-transcribe"],
    )
    Babel(app)

    with app.app_context():
        assert service._resolve_provider(SimpleNamespace(), "gpt-live-transcribe") == "openai"


def test_live_provider_api_key_lookup_is_model_scoped(monkeypatch):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-secret",
        DEPLOYMENT_MODE="multi",
        LIVE_TRANSCRIPTION_PROVIDERS={"gemini-2.5-flash-native-audio": "gemini"},
        GEMINI_API_KEY="global-gemini-key",
    )
    Babel(app)
    user = SimpleNamespace(id=7)
    monkeypatch.setattr(service, "check_permission", lambda *_: True)
    monkeypatch.setattr(service.user_service, "get_decrypted_api_key", lambda *_: None)

    with app.app_context():
        with pytest.raises(service.MissingApiKeyError):
            service._resolve_provider_api_key(user, "gemini", "gemini-2.5-flash-native-audio")
