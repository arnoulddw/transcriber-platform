from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from flask import Flask

from app.forms import ApiKeyForm
from app.models import user_api_key
from app.services import llm_service, user_service


OPENROUTER_KEY = "sk-or-v1-abcdefghijklmnopqrstuvwxyzXYZ"


def _app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-secret",
        WTF_CSRF_ENABLED=False,
        DEPLOYMENT_MODE="multi",
        OPENROUTER_API_KEY="global-openrouter-key",
    )
    return app


def test_api_key_form_accepts_masked_openrouter_key():
    with _app().test_request_context("/"):
        form = ApiKeyForm(
            meta={"csrf": False},
            data={
                "service": "openrouter",
                "api_key": "***XYZ",
                "openrouter_model": "x-ai/grok-stt-1.0",
                "openrouter_model_purpose": "transcription",
            },
        )

        assert form.validate() is True


def test_save_masked_openrouter_key_reuses_saved_key_for_new_slug():
    app = _app()
    user = SimpleNamespace(id=7)
    security_service = Mock()
    security_service.encrypt_data.return_value = "encrypted"

    with app.app_context(), patch.object(
        user_service.user_model, "get_user_by_id", return_value=user
    ), patch.object(
        user_service, "get_decrypted_api_key", return_value=OPENROUTER_KEY
    ) as get_key, patch.object(
        user_service, "get_security_service", return_value=security_service
    ), patch.object(
        user_service.user_api_key_model, "upsert_api_key", return_value=True
    ) as upsert, patch.object(
        user_service.user_model, "update_user_preferences", return_value=True
    ):
        assert user_service.save_user_api_key(
            7,
            "openrouter",
            "***XYZ",
            openrouter_model="x-ai/grok-stt-1.0",
        ) is True

    get_key.assert_called_once_with(7, "openrouter", "x-ai/grok-stt-1.0")
    security_service.encrypt_data.assert_called_once_with(OPENROUTER_KEY)
    upsert.assert_called_once_with(
        7, "openrouter", "encrypted", "x-ai/grok-stt-1.0"
    )


def test_status_exposes_openrouter_slugs_and_only_key_suffixes():
    app = _app()
    user = SimpleNamespace(
        role=SimpleNamespace(allow_public_api_access=False),
        default_openrouter_model=None,
        default_openrouter_llm_model=None,
    )
    records = [
        {
            "provider_code": "openrouter",
            "model_slug": "x-ai/grok-stt-1.0",
            "encrypted_key": "encrypted-new",
        },
        {
            "provider_code": "openrouter",
            "model_slug": "openai/gpt-4o-mini",
            "encrypted_key": "encrypted-old",
        },
    ]

    security_service = Mock()
    security_service.decrypt_data.side_effect = {
        "encrypted-new": "sk-or-v1-newXYZ",
        "encrypted-old": "sk-or-v1-oldABC",
    }.get

    with app.app_context(), patch.object(
        user_service.user_model, "get_user_by_id", return_value=user
    ), patch.object(
        user_service.user_api_key_model,
        "get_api_key_records_by_user",
        return_value=records,
    ), patch.object(
        user_service, "get_security_service", return_value=security_service
    ):
        status = user_service.get_user_api_key_status(7)

    assert status["openrouter"] is True
    assert status["openrouter_keys"] == [
        {"model_slug": "x-ai/grok-stt-1.0", "last_three": "XYZ"},
        {"model_slug": "openai/gpt-4o-mini", "last_three": "ABC"},
    ]


def test_model_upsert_scopes_openrouter_key_by_model_slug():
    cursor = Mock()
    connection = Mock()
    cursor.rowcount = 1

    with patch.object(user_api_key, "get_cursor", return_value=cursor), patch.object(
        user_api_key, "get_db", return_value=connection
    ):
        assert user_api_key.upsert_api_key(
            7, "openrouter", "encrypted", "x-ai/grok-stt-1.0"
        ) is True

    sql, params = cursor.execute.call_args.args
    assert "model_slug" in sql
    assert params == (7, "openrouter", "x-ai/grok-stt-1.0", "encrypted")


def test_llm_service_passes_requested_openrouter_model_to_key_lookup():
    app = _app()
    user = SimpleNamespace(
        id=7,
        role=SimpleNamespace(),
        has_permission=lambda permission: True,
    )
    llm_client = Mock()
    llm_client.generate_text.return_value = "result"

    with app.app_context(), patch.object(
        llm_service.user_model, "get_user_by_id", return_value=user
    ), patch.object(
        llm_service.user_service,
        "get_decrypted_api_key",
        return_value=OPENROUTER_KEY,
    ) as get_key, patch.object(
        llm_service, "get_llm_client", return_value=llm_client
    ):
        assert llm_service.generate_text_via_llm(
            "OPENROUTER",
            "prompt",
            user_id=7,
            model="x-ai/grok-stt-1.0",
        ) == "result"

    get_key.assert_called_once_with(7, "openrouter", "x-ai/grok-stt-1.0")


def test_api_key_modal_contract_contains_requested_copy_and_selected_state():
    template = open(
        "app/templates/layout/modals/api_key_modal.html", encoding="utf-8"
    ).read()
    script = open("app/static/js/user_settings.js", encoding="utf-8").read()

    assert (
        "Enter the model slug exactly as shown in OpenRouter, for ex. openai/gpt-4o-mini "
        in template
    )
    assert "peer-checked:bg-primary/10" in template
    assert "openrouter_keys" in script
    assert "***${" in script


def test_schema_creates_replacement_index_before_dropping_legacy_index():
    cursor = Mock()
    connection = Mock()
    cursor.fetchone.side_effect = [
        {"Type": "timestamp"},
        {"Type": "timestamp"},
        {"Field": "model_slug"},
        {"Field": "last_used_at"},
        None,
        {"Key_name": "uq_user_provider"},
        None,
    ]
    cursor.fetchall.return_value = []

    with patch.object(user_api_key, "get_cursor", return_value=cursor), patch.object(
        user_api_key, "get_db", return_value=connection
    ):
        user_api_key.init_db_command()

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    replacement_index_position = next(
        index
        for index, statement in enumerate(statements)
        if "ADD UNIQUE INDEX uq_user_provider_model" in statement
    )
    legacy_drop_position = next(
        index
        for index, statement in enumerate(statements)
        if "DROP INDEX uq_user_provider" in statement
    )

    assert replacement_index_position < legacy_drop_position
