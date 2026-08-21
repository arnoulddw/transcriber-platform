"""Broken stored API keys must fail loudly instead of falling back."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import InvalidToken
from flask import Flask
import pytest

from app.services import llm_service, user_service


def _app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-secret",
        DEPLOYMENT_MODE="multi",
        OPENAI_API_KEY="global-openai-key",
    )
    return app


def test_get_decrypted_api_key_raises_on_undecryptable_key():
    app = _app()
    record = {"id": 5, "encrypted_key": "corrupted-ciphertext"}
    security = Mock()
    security.decrypt_data.side_effect = InvalidToken()

    with app.app_context(), patch.object(
        user_service.user_model, "get_user_by_id", return_value=SimpleNamespace(id=7)
    ), patch.object(
        user_service.user_api_key_model,
        "get_api_key_record",
        return_value=record,
    ), patch.object(
        user_service, "get_security_service", return_value=security
    ):
        with pytest.raises(user_service.ApiKeyDecryptionError):
            user_service.get_decrypted_api_key(7, "openai")


def test_get_admin_decrypted_api_key_raises_on_undecryptable_key():
    app = _app()
    records = [
        {"id": 11, "user_id": 3, "model_slug": "", "encrypted_key": "corrupted"},
    ]
    security = Mock()
    security.decrypt_data.side_effect = InvalidToken()

    with app.app_context(), patch.object(
        user_service.user_api_key_model,
        "get_admin_api_key_records",
        return_value=records,
    ), patch.object(
        user_service, "get_security_service", return_value=security
    ):
        with pytest.raises(user_service.ApiKeyDecryptionError):
            user_service.get_admin_decrypted_api_key("openai")


def test_llm_service_does_not_fall_back_to_global_key_when_user_key_is_broken():
    app = _app()
    user = SimpleNamespace(id=7, role=object())
    user.has_permission = lambda permission: permission == "allow_api_key_management"

    with app.app_context(), patch.object(
        user_service.user_model, "get_user_by_id", return_value=user
    ), patch.object(
        user_service,
        "get_decrypted_api_key",
        side_effect=user_service.ApiKeyDecryptionError("broken key"),
    ) as get_key:
        with pytest.raises(user_service.ApiKeyDecryptionError):
            llm_service.generate_text_via_llm(
                provider_name="openai",
                prompt="hello",
                user_id=7,
            )

    get_key.assert_called_once_with(7, "openai")


def test_missing_key_still_returns_none_and_falls_back():
    """A genuinely absent key keeps the existing fallback behaviour."""
    app = _app()
    user = SimpleNamespace(id=7, role=object())
    user.has_permission = lambda permission: False  # no key management

    client = Mock()
    client.generate_text.return_value = "generated"

    with app.app_context(), patch.object(
        user_service.user_model, "get_user_by_id", return_value=user
    ), patch.object(
        user_service, "get_decrypted_api_key", return_value=None
    ), patch.object(
        user_service, "get_admin_decrypted_api_key", return_value=None
    ), patch.object(
        llm_service, "get_llm_client", return_value=client
    ) as get_client:
        result = llm_service.generate_text_via_llm(
            provider_name="openai",
            prompt="hello",
            user_id=7,
        )

    assert result == "generated"
    # Fell back to the global OPENAI_API_KEY.
    get_client.assert_called_once_with("openai", "global-openai-key", app.config)
