"""API key saving accepts any non-empty key value.

Provider API keys are never shape-gated at save time: providers keep
introducing new key formats (e.g. Google's newer ``AQ.…`` expression-style
Gemini keys alongside classic ``AIzaSy…`` keys), so guessing shapes here
breaks valid keys. A bad key surfaces as a provider error when used.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.forms import ApiKeyForm
from app.services import user_service


def _form_app():
    from flask import Flask

    app = Flask(__name__)
    app.config.update(SECRET_KEY="test-secret", WTF_CSRF_ENABLED=False)
    return app


def _validate_form(service, model_name, model_purpose="transcription"):
    with _form_app().test_request_context("/"):
        form = ApiKeyForm(
            meta={"csrf": False},
            data={
                "service": service,
                "api_key": "arbitrary-key-shape",
                "model_name": model_name,
                "model_purpose": model_purpose,
            },
        )
        valid = form.validate()
    return valid, form.errors.get("model_name")


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


@pytest.mark.parametrize("model_purpose", ["transcription", "live"])
def test_gemini_requires_model_name_for_transcription_and_live(model_purpose):
    valid, errors = _validate_form("gemini", "", model_purpose)

    assert valid is False
    assert errors == ["Google model name is required."]


def test_gemini_allows_empty_model_name_for_llm_purpose():
    valid, errors = _validate_form("gemini", "", "llm")

    assert valid is True
    assert errors is None


@pytest.mark.parametrize(
    ("model_name", "expect_valid"),
    [
        ("gemini-3.5-transcribe", True),
        ("has space", False),
        ("contains/slash", False),
    ],
)
def test_gemini_model_name_shape_rules(model_name, expect_valid):
    valid, _ = _validate_form("gemini", model_name, "transcription")

    assert valid is expect_valid


@pytest.mark.parametrize("service", ["openai", "assemblyai"])
def test_other_providers_still_allow_empty_model_name(service):
    valid, errors = _validate_form(service, "", "transcription")

    assert valid is True
    assert errors is None
