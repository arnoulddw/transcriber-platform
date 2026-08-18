from contextlib import ExitStack
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from flask import Flask

from app.forms import AdminRoleForm, UserProfileForm
from app.models import llm_catalog
from app.models import transcription_catalog
from app.models.user.model import _map_row_to_user
from app.models.user.repository import update_user_preferences
from app.services import user_service


TRANSCRIPTION_MODELS = [
    {"code": "whisper", "display_name": "Whisper", "permission_key": None},
    {"code": "openrouter", "display_name": "OpenRouter", "permission_key": None},
]

# Key status used by the profile/role form fixtures: one OpenRouter
# transcription slug so the openrouter option resolves to a real model.
OPENROUTER_KEY_STATUS = {
    "provider_keys": {
        "openrouter": [
            {"model_name": "openai/gpt-transcribe", "model_purposes": ["transcription"]},
        ],
    },
}


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
        is_authenticated=True,
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
                patch("app.forms.llm_catalog_model.get_llm_model_options", return_value=[])
            )
            stack.enter_context(
                patch(
                    "app.forms.user_service.get_effective_key_status",
                    return_value=OPENROUTER_KEY_STATUS,
                )
            )
            stack.enter_context(
                patch(
                    "app.forms.user_service.get_aggregate_api_key_status",
                    return_value=OPENROUTER_KEY_STATUS,
                )
            )
            stack.enter_context(
                patch(
                    "app.forms.user_service.resolve_effective_openrouter_model",
                    return_value="openai/gpt-transcribe",
                )
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
            "default_title_generation_model": "gemma-4-26b-a4b-it",
            "default_workflow_model": "google/gemini-3.7-flash",
        }
    )

    assert user is not None
    assert user.default_openrouter_model == "openai/gpt-transcribe"
    assert user.default_title_generation_model == "gemma-4-26b-a4b-it"
    assert user.default_workflow_model == "google/gemini-3.7-flash"


def test_profile_form_exposes_permitted_llm_model_choices(form_context):
    llm_models = [
        {
            "code": "gemini-3.0-flash",
            "display_name": "Gemini 3.0 Flash",
            "permission_key": "use_api_google_gemini",
        },
        {
            "code": "gpt-4.1",
            "display_name": "OpenAI GPT-4.1",
            "permission_key": None,
        },
        {
            "code": "restricted-model",
            "display_name": "Restricted Model",
            "permission_key": "use_restricted_model",
        },
    ]
    authenticated_user = SimpleNamespace(
        is_authenticated=True,
        username="testuser",
        email="test@example.com",
        has_permission=lambda permission: permission == "use_api_google_gemini",
    )

    with patch("app.forms.current_user", authenticated_user), patch(
        "app.forms.llm_catalog_model.get_llm_model_options", return_value=llm_models
    ), patch(
        "app.forms.user_service.get_effective_key_status",
        return_value=OPENROUTER_KEY_STATUS,
    ), patch(
        "app.forms.user_service.resolve_effective_openrouter_model", return_value=None
    ):
        form = UserProfileForm(data=_form_data(UserProfileForm))

    expected_model_choices = [
        ("gemini-3.0-flash", "Gemini 3.0 Flash"),
        ("gpt-4.1", "OpenAI GPT-4.1"),
    ]
    workflow_choices = list(form.default_workflow_model.choices or [])
    auxiliary_choices = list(form.default_title_generation_model.choices or [])
    assert workflow_choices[0][0] == ""
    assert workflow_choices[1:] == expected_model_choices
    assert auxiliary_choices[0][0] == ""
    assert auxiliary_choices[1:] == expected_model_choices
    assert form.validate() is True


def test_llm_model_catalog_hides_models_without_an_available_provider_key():
    models = [
        {
            "code": "google/gemini-3.7-flash",
            "display_name": "Gemini 3.7 Flash (OpenRouter)",
            "required_api_key": "openrouter",
        },
        {
            "code": "gemini-3.0-flash",
            "display_name": "Gemini 3.0 Flash",
            "required_api_key": "gemini",
        },
        {
            "code": "local-model",
            "display_name": "Local Model",
            "required_api_key": None,
        },
    ]

    available_models = llm_catalog.filter_models_by_api_key_status(
        models, {"openrouter": True, "gemini": False}
    )

    assert [model["code"] for model in available_models] == [
        "google/gemini-3.7-flash",
        "local-model",
    ]


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


def test_repository_persists_new_llm_model_preferences():
    cursor = _Cursor()
    connection = _Connection()

    with patch("app.models.user.repository.get_cursor", return_value=cursor), patch(
        "app.models.user.repository.get_db", return_value=connection
    ):
        assert update_user_preferences(
            7,
            None,
            None,
            None,
            None,
            default_title_generation_model="gemma-4-26b-a4b-it",
            default_workflow_model="google/gemini-3.7-flash",
        ) is True

    assert "default_title_generation_model = %s" in cursor.calls[0][0]
    assert "default_workflow_model = %s" in cursor.calls[0][0]
    assert cursor.calls[0][1] == (
        "gemma-4-26b-a4b-it",
        "google/gemini-3.7-flash",
        7,
    )


def test_saved_openrouter_slug_is_visible_in_transcription_model_selectors():
    config = open("app/config.py", encoding="utf-8").read()
    compose = open("docker-compose.yml", encoding="utf-8").read()
    index_template = open("app/templates/index.html", encoding="utf-8").read()
    bootstrap_template = open(
        "app/templates/layout/scripts/base_bootstrap.html", encoding="utf-8"
    ).read()
    profile_script = open("app/static/js/profile.js", encoding="utf-8").read()
    main_init = open("app/static/js/main_init.js", encoding="utf-8").read()

    assert "gpt-transcribe,openrouter" in config
    assert "TRANSCRIPTION_PROVIDERS: ${TRANSCRIPTION_PROVIDERS:-" in compose
    assert "window.DEFAULT_OPENROUTER_MODEL" in bootstrap_template
    assert "data.default_openrouter_model || window.DEFAULT_OPENROUTER_MODEL" in profile_script
    assert "model_slug" in profile_script
    assert "initial_key_status.get('openrouter_keys', [])" not in index_template
    assert "model.model_slug" in index_template
    assert "TRANSCRIPTION_MODEL_CATALOG=available_transcription_models" in open("app/__init__.py", encoding="utf-8").read()
    assert "transcription_models=available_transcription_models" in open("app/__init__.py", encoding="utf-8").read()
    assert 'type="hidden" id="openrouterModelInput"' in index_template


def test_live_openrouter_configuration_is_documented_and_detected():
    config_source = Path("app/config.py").read_text(encoding="utf-8")
    env_example = Path(".env.example").read_text(encoding="utf-8")
    live_service = Path("app/services/live_transcription_service.py").read_text(encoding="utf-8")
    main_init = Path("app/static/js/main_init.js").read_text(encoding="utf-8")
    profile_script = Path("app/static/js/profile.js").read_text(encoding="utf-8")

    assert "replace('/', '_')" in config_source
    assert "LIVE_TRANSCRIPTION_PROVIDER_OPENAI_GPT_TRANSCRIBE=openrouter" in env_example
    assert '"transport": "openrouter-sse"' in live_service
    assert "updateSelectedOpenRouterModel" in main_init
    assert "openrouterModelField" not in main_init
    assert "const apiKeyStatus = window.API_KEY_STATUS || {};" in profile_script
    assert "opt.disabled = true;" in profile_script
    assert "missingKeyMarker" in profile_script


def test_shared_transcription_model_expansion_includes_all_openrouter_slugs():
    models = [
        {"code": "whisper", "display_name": "Whisper", "permission_key": None, "required_api_key": "openai"},
        {"code": "openrouter", "display_name": "OpenRouter", "permission_key": None, "required_api_key": "openrouter"},
    ]

    expanded = transcription_catalog.expand_models_for_ui(
        models,
        {"openrouter_keys": [
            {"model_slug": "x-ai/grok-stt-1.0"},
            {"model_slug": "openai/gpt-transcribe"},
        ]},
    )

    assert [(model["code"], model["display_name"], model.get("model_slug")) for model in expanded] == [
        ("whisper", "Whisper", None),
        ("openrouter", "x-ai/grok-stt-1.0", "x-ai/grok-stt-1.0"),
        ("openrouter", "openai/gpt-transcribe", "openai/gpt-transcribe"),
    ]


def test_scoped_transcription_key_only_expands_the_matching_catalog_model():
    models = [
        {"code": "whisper", "display_name": "Whisper", "permission_key": None, "required_api_key": "openai"},
        {"code": "gpt-4o-transcribe", "display_name": "GPT-4o", "permission_key": None, "required_api_key": "openai"},
        {"code": "gpt-transcribe", "display_name": "GPT", "permission_key": None, "required_api_key": "openai"},
    ]

    expanded = transcription_catalog.expand_models_for_ui(
        models,
        {
            "provider_keys": {
                "openai": [
                    {"model_name": "gpt-transcribe", "model_purposes": ["transcription"]},
                    {"model_name": "gpt-4.1", "model_purposes": ["llm"]},
                ],
            },
        },
    )

    assert [(model["code"], model.get("model_name")) for model in expanded] == [
        ("whisper", None),
        ("gpt-4o-transcribe", None),
        ("gpt-transcribe", "gpt-transcribe"),
    ]
    assert sum(model.get("model_name") == "gpt-transcribe" for model in expanded) == 1


def test_transcription_model_expansion_deduplicates_saved_model_names():
    models = [
        {"code": "openrouter", "display_name": "OpenRouter", "permission_key": None, "required_api_key": "openrouter"},
    ]

    expanded = transcription_catalog.expand_models_for_ui(
        models,
        {
            "provider_keys": {
                "openrouter": [
                    {"model_slug": "openai/gpt-transcribe", "model_purposes": ["transcription"]},
                    {"model_name": "openai/gpt-transcribe", "model_purposes": ["transcription"]},
                ],
            },
        },
    )

    assert [(model["code"], model.get("model_slug")) for model in expanded] == [
        ("openrouter", "openai/gpt-transcribe"),
    ]


def test_live_model_catalog_deduplicates_configured_and_user_models(form_context):
    from flask import current_app

    current_app.config.update(
        API_PROVIDER_NAME_MAP={"gpt-live-transcribe": "GPT Live", "vendor/live": "Vendor Live"},
        LIVE_TRANSCRIPTION_MODEL="gpt-live-transcribe",
        LIVE_TRANSCRIPTION_MODELS=["gpt-live-transcribe", "gpt-live-transcribe"],
        LIVE_TRANSCRIPTION_PROVIDERS={"gpt-live-transcribe": "openai"},
    )

    live_models = transcription_catalog.get_live_models({
        "provider_keys": {
            "openrouter": [
                {"model_slug": "vendor/live", "model_purposes": ["live"]},
                {"model_slug": "vendor/live", "model_purposes": ["live"]},
            ],
        },
    })

    assert [model["code"] for model in live_models] == ["gpt-live-transcribe", "vendor/live"]


def test_context_resolves_openrouter_label_after_loading_key_status():
    app_source = open("app/__init__.py", encoding="utf-8").read()
    key_status_call = "initial_key_status = user_service.get_effective_key_status(user)"
    effective_model_call = "user_service.resolve_effective_openrouter_model(user, initial_key_status)"

    assert app_source.index(key_status_call) < app_source.index(effective_model_call)


def test_resolve_effective_openrouter_model_uses_saved_key_slug():
    user = SimpleNamespace(
        id=7,
        default_openrouter_model=None,
        role=SimpleNamespace(default_openrouter_model=None),
    )

    with patch.object(
        user_service,
        "get_user_api_key_status",
        return_value={"openrouter_keys": [{"model_slug": "x-ai/grok-stt-1.0"}]},
    ):
        assert user_service.resolve_effective_openrouter_model(user) == "x-ai/grok-stt-1.0"


def test_resolve_effective_openrouter_model_prefers_user_and_role_defaults():
    role = SimpleNamespace(default_openrouter_model="role/model")
    user = SimpleNamespace(id=7, default_openrouter_model=None, role=role)
    assert user_service.resolve_effective_openrouter_model(
        user, {"openrouter_keys": [{"model_slug": "key/model"}]}
    ) == "role/model"

    user.default_openrouter_model = "user/model"
    assert user_service.resolve_effective_openrouter_model(
        user, {"openrouter_keys": [{"model_slug": "key/model"}]}
    ) == "user/model"


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


def test_service_passes_new_llm_model_preferences():
    current_user = SimpleNamespace(
        username="testuser",
        email="test@example.com",
        first_name=None,
        last_name=None,
        default_content_language="auto",
        default_transcription_model="whisper",
        default_title_generation_model=None,
        default_workflow_model=None,
        default_openrouter_model=None,
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
                "default_title_generation_model": " gemma-4-26b-a4b-it ",
                "default_workflow_model": " google/gemini-3.7-flash ",
                "default_openrouter_model": None,
                "enable_auto_title_generation": False,
                "language": "en",
            },
        )

    update_preferences.assert_called_once_with(
        7,
        "auto",
        "whisper",
        False,
        "en",
        None,
        default_title_generation_model="gemma-4-26b-a4b-it",
        default_workflow_model="google/gemini-3.7-flash",
    )
