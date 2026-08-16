from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from flask import Flask

from app.forms import AdminRoleForm, UserProfileForm
from app.models.user.model import _map_row_to_user
from app.models.user.repository import update_user_preferences
from app.services import user_service


TRANSCRIPTION_MODELS = [
    {"code": "whisper", "display_name": "Whisper", "permission_key": None},
    {"code": "openrouter", "display_name": "OpenRouter", "permission_key": None},
]


@pytest.fixture
def form_context():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-secret",
        WTF_CSRF_ENABLED=False,
        SUPPORTED_LANGUAGES=["en"],
        SUPPORTED_LANGUAGE_NAMES={"auto": "Automatic Detection"},
    )
    fake_user = SimpleNamespace(
        is_authenticated=False,
        username="testuser",
        email="test@example.com",
    )
    with app.test_request_context("/"):
        with ExitStack() as stack:
            stack.enter_context(patch("app.forms.current_user", fake_user))
            stack.enter_context(patch("app.forms.get_user_by_username", return_value=None))
            stack.enter_context(patch("app.forms.get_user_by_email", return_value=None))
            stack.enter_context(patch("app.forms.get_role_by_name", return_value=None))
            stack.enter_context(
                patch(
                    "app.forms.transcription_catalog_model.get_active_languages",
                    return_value=[{"code": "auto", "display_name": "Automatic Detection"}],
                )
            )
            stack.enter_context(
                patch(
                    "app.forms.transcription_catalog_model.get_active_models",
                    return_value=TRANSCRIPTION_MODELS,
                )
            )
            stack.enter_context(
                patch("app.forms.llm_catalog_model.get_active_models", return_value=[])
            )
            yield


def _form_data(form_type, transcription_model="openrouter", slug=" openai/gpt-transcribe "):
    data = {
        "name": "test-role",
        "description": "A test role",
        "default_transcription_model": transcription_model,
        "default_openrouter_model": slug,
    }
    if form_type is UserProfileForm:
        data.update(
            {
                "username": "testuser",
                "email": "test@example.com",
                "default_content_language": "auto",
                "language": "en",
            }
        )
    return data


@pytest.mark.parametrize("form_type", [UserProfileForm, AdminRoleForm])
def test_forms_normalize_valid_openrouter_slug(form_context, form_type):
    form = form_type(data=_form_data(form_type))

    assert form.validate() is True
    assert form.default_openrouter_model.data == "openai/gpt-transcribe"


@pytest.mark.parametrize("form_type", [UserProfileForm, AdminRoleForm])
def test_forms_reject_invalid_openrouter_slug(form_context, form_type):
    form = form_type(data=_form_data(form_type, slug="not-a-slug"))

    assert form.validate() is False
    assert "default_openrouter_model" in form.errors


@pytest.mark.parametrize("form_type", [UserProfileForm, AdminRoleForm])
def test_forms_clear_slug_when_default_model_is_not_openrouter(form_context, form_type):
    form = form_type(data=_form_data(form_type, transcription_model="whisper"))

    assert form.validate() is True
    assert form.default_openrouter_model.data is None


def test_user_row_mapping_includes_default_openrouter_model():
    user = _map_row_to_user(
        {
            "id": 7,
            "username": "testuser",
            "email": "test@example.com",
            "created_at": "2026-08-16T00:00:00",
            "default_openrouter_model": "openai/gpt-transcribe",
        }
    )

    assert user is not None
    assert user.default_openrouter_model == "openai/gpt-transcribe"


class _Cursor:
    rowcount = 1

    def __init__(self):
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))


class _Connection:
    def __init__(self):
        self.commit = Mock()
        self.rollback = Mock()


def test_repository_persists_and_clears_default_openrouter_model():
    cursor = _Cursor()
    connection = _Connection()

    with patch("app.models.user.repository.get_cursor", return_value=cursor), patch(
        "app.models.user.repository.get_db", return_value=connection
    ):
        assert update_user_preferences(
            7, None, None, None, None, "openai/gpt-transcribe"
        ) is True
        assert update_user_preferences(7, None, None, None, None, None) is True

    assert "default_openrouter_model = %s" in cursor.calls[0][0]
    assert cursor.calls[0][1] == ("openai/gpt-transcribe", 7)
    assert cursor.calls[1][1] == (None, 7)


def test_repository_old_call_shape_does_not_clear_new_preference():
    cursor = _Cursor()
    connection = _Connection()

    with patch("app.models.user.repository.get_cursor", return_value=cursor), patch(
        "app.models.user.repository.get_db", return_value=connection
    ):
        assert update_user_preferences(7, None, None, True, None) is True

    assert "default_openrouter_model = %s" not in cursor.calls[0][0]


def test_service_normalizes_and_passes_openrouter_default():
    current_user = SimpleNamespace(
        username="testuser",
        email="test@example.com",
        first_name=None,
        last_name=None,
        default_content_language="auto",
        default_transcription_model="openrouter",
        default_openrouter_model="old/model",
        enable_auto_title_generation=False,
        language="en",
    )
    update_preferences = Mock(return_value=True)

    with patch.object(user_service.user_model, "get_user_by_id", return_value=current_user), patch.object(
        user_service.user_model, "update_user_preferences", update_preferences
    ):
        user_service.update_profile(
            7,
            {
                "username": "testuser",
                "email": "test@example.com",
                "first_name": None,
                "last_name": None,
                "default_content_language": "auto",
                "default_transcription_model": "openrouter",
                "default_openrouter_model": " openai/gpt-transcribe ",
                "enable_auto_title_generation": False,
                "language": "en",
            },
        )

    update_preferences.assert_called_once_with(
        7, "auto", "openrouter", False, "en", "openai/gpt-transcribe"
    )


def test_service_clears_stale_openrouter_default_for_non_openrouter_model():
    current_user = SimpleNamespace(
        username="testuser",
        email="test@example.com",
        first_name=None,
        last_name=None,
        default_content_language="auto",
        default_transcription_model="whisper",
        default_openrouter_model="old/model",
        enable_auto_title_generation=False,
        language="en",
    )
    update_preferences = Mock(return_value=True)

    with patch.object(user_service.user_model, "get_user_by_id", return_value=current_user), patch.object(
        user_service.user_model, "update_user_preferences", update_preferences
    ):
        user_service.update_profile(
            7,
            {
                "username": "testuser",
                "email": "test@example.com",
                "first_name": None,
                "last_name": None,
                "default_content_language": "auto",
                "default_transcription_model": "whisper",
                "default_openrouter_model": "stale/model",
                "enable_auto_title_generation": False,
                "language": "en",
            },
        )

    update_preferences.assert_called_once_with(7, "auto", "whisper", False, "en", None)
