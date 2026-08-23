"""API key saving accepts any non-empty key value.

Provider API keys are never shape-gated at save time: providers keep
introducing new key formats (e.g. Google's newer ``AQ.…`` expression-style
Gemini keys alongside classic ``AIzaSy…`` keys), so guessing shapes here
breaks valid keys. A bad key surfaces as a provider error when used.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.services import user_service


def _save_key(provider, api_key, model_name):
    """Run save_user_api_key with the persistence layer mocked out."""
    user = SimpleNamespace(id=7)
    security_service = Mock()
    security_service.encrypt_data.return_value = "encrypted"

    with _app_context(), patch.object(
        user_service.user_model, "get_user_by_id", return_value=user
    ), patch.object(
        user_service, "get_security_service", return_value=security_service
    ), patch.object(
        user_service.user_api_key_model, "upsert_api_key", return_value=True
    ) as upsert, patch.object(
        user_service.user_model, "update_user_preferences", return_value=True
    ), patch(
        "app.models.transcription_catalog.register_model_from_provider"
    ):
        result = user_service.save_user_api_key(
            7,
            provider,
            api_key,
            model_name=model_name,
        )

    assert result is True
    return security_service, upsert


class _AppContext:
    def __init__(self):
        from flask import Flask

        self._app = Flask(__name__)
        self._app.config.update(SECRET_KEY="test-secret")

    def __enter__(self):
        self._ctx = self._app.app_context()
        self._ctx.push()
        return self

    def __exit__(self, *exc_info):
        self._ctx.pop()
        return False


def _app_context():
    return _AppContext()


@pytest.mark.parametrize(
    ("provider", "model", "api_key"),
    [
        # Newer Google expression-style Gemini keys.
        ("gemini", "gemini-3.0-flash", "AQ.Ab8RNLExampleKeyValue_123"),
        # Classic Google keys.
        ("gemini", "gemini-3.0-flash", "AIzaSyExampleClassicKeyValue123"),
        # Unknown-future Google shapes must not be guessed away either.
        ("gemini", "gemini-3.0-flash", "totally-new-google-format"),
        # Providers without a stable prefix were never gated.
        ("openai", "gpt-transcribe", "sk-proj-arbitrary-shape"),
        ("assemblyai", "universal", "arbitrary-assemblyai-token"),
        ("openrouter", "x-ai/grok-stt-1.0", "sk-or-v1-arbitrary-shape"),
    ],
)
def test_save_accepts_any_non_empty_key_shape(provider, model, api_key):
    security_service, upsert = _save_key(provider, api_key, model)

    security_service.encrypt_data.assert_called_once_with(api_key)
    assert upsert.call_args.args[:2] == (7, provider)


def test_empty_gemini_key_is_still_rejected():
    # Empty values fail earlier as a required-input error, before any
    # format consideration could apply.
    with pytest.raises(ValueError, match="cannot be empty"):
        _save_key("gemini", "   ", "gemini-3.0-flash")
