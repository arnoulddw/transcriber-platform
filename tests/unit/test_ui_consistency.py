from pathlib import Path
from unittest.mock import patch


def _set_minimal_app_environment(monkeypatch):
    for key, value in {
        "SECRET_KEY": "ui-consistency-test-secret",
        "MYSQL_USER": "test",
        "MYSQL_PASSWORD": "test",
        "MYSQL_DB": "test_db",
        "GOOGLE_CLIENT_ID": "test-google-client-id",
        "DEPLOYMENT_MODE": "multi",
    }.items():
        monkeypatch.setenv(key, value)


def test_manage_api_key_purpose_controls_are_independent_of_service_selection():
    template = Path("app/templates/layout/modals/api_key_modal.html").read_text(encoding="utf-8")
    script = Path("app/static/js/user_settings.js").read_text(encoding="utf-8")

    assert "id=\"liveModelSettings\"" not in template
    assert "id=\"apiKeyLiveModel\"" not in template
    assert "purposeInputs.forEach(input => { input.disabled = !service; })" not in script
    assert "providerSelect.value = selectedOption.dataset.provider" not in script
    assert "input.addEventListener('change', () => updateModelSettings" in script


def test_admin_role_labels_match_user_settings_labels():
    admin_form_source = Path("app/forms.py").read_text(encoding="utf-8")
    settings_template = Path("app/templates/layout/modals/profile_modal.html").read_text(encoding="utf-8")

    assert "_('Default Auxiliary LLM Model')" in admin_form_source
    assert "_('Default Workflow LLM Model')" in admin_form_source
    assert "Default Auxiliary LLM Model" in settings_template
    assert "Default Workflow LLM Model" in settings_template
    assert "_('Default Title Generation Model')" not in admin_form_source
    assert "_('Default Workflow Model')" not in admin_form_source


def test_role_label_translations_match_the_updated_source_strings():
    for path in Path("app/translations").glob("*/LC_MESSAGES/messages.po"):
        source = path.read_text(encoding="utf-8")
        assert 'msgid "Default Auxiliary LLM Model"' in source
        assert 'msgid "Default Workflow LLM Model"' in source
        assert 'msgid "Default Title Generation Model"' not in source
        assert 'msgid "Default Workflow Model"' not in source


def test_admin_pricing_options_are_model_specific_and_include_saved_models(monkeypatch):
    _set_minimal_app_environment(monkeypatch)
    from app.admin_panel.routes import build_pricing_options

    with patch(
        "app.admin_panel.routes.llm_catalog_model.get_active_models",
        return_value=[
            {"code": "gemini-3.0-flash", "display_name": "Gemini 3.0 Flash"},
        ],
    ), patch(
        "app.admin_panel.routes.user_api_key_model.get_distinct_model_names",
        return_value=["openai/gpt-4o-transcribe", "google/gemini-3.7-flash"],
    ), patch(
        "app.admin_panel.routes.transcription_catalog_model.get_active_models",
        return_value=[
            {"code": "whisper", "display_name": "OpenAI"},
            {"code": "openrouter", "display_name": "OpenRouter"},
        ],
    ):
        options = build_pricing_options()

    assert options["transcription"]["openai/gpt-4o-transcribe"] == "openai/gpt-4o-transcribe"
    assert options["transcription"]["google/gemini-3.7-flash"] == "google/gemini-3.7-flash"
    assert options["title_generation"]["gemini-3.0-flash"] == "gemini-3.0-flash"
    assert options["workflow"] == options["title_generation"]
    assert "OpenRouter" not in options["transcription"].values()


def test_admin_cost_template_uses_the_same_model_option_shape_for_all_sections():
    template = Path("app/templates/admin/costs.html").read_text(encoding="utf-8")

    assert template.count("pricing_options.transcription.items()") == 1
    assert template.count("pricing_options.title_generation.items()") == 1
    assert template.count("pricing_options.workflow.items()") == 1
    assert template.count("class=\"input-select pricing-model-select\"") == 3
    assert "title_generation_models" not in template
    assert "workflow_models" not in template


def test_admin_default_model_grid_keeps_labels_on_one_line():
    template = Path("app/templates/admin/create_edit_role.html").read_text(encoding="utf-8")

    assert "lg:grid-cols-4 gap-6 items-start" in template
    assert template.count("form-label whitespace-nowrap") == 4
    assert "Default Title Generation Model" not in template


def test_admin_default_model_grid_order_matches_user_settings():
    role_template = Path("app/templates/admin/create_edit_role.html").read_text(encoding="utf-8")
    settings_template = Path("app/templates/layout/modals/profile_modal.html").read_text(encoding="utf-8")

    role_fields = [
        role_template.index("form.default_transcription_model"),
        role_template.index("form.default_live_transcription_model"),
        role_template.index("form.default_workflow_model"),
        role_template.index("form.default_title_generation_model"),
    ]
    settings_fields = [
        settings_template.index("profileDefaultModel"),
        settings_template.index("profileDefaultLiveModel"),
        settings_template.index("profileDefaultWorkflowModel"),
        settings_template.index("profileDefaultAuxiliaryModel"),
    ]

    assert role_fields == sorted(role_fields)
    assert settings_fields == sorted(settings_fields)


class _Cursor:
    def __init__(self):
        self.rows = [
            {"model_slug": "gpt-4o"},
            {"model_slug": "openai/gpt-4o"},
            {"model_slug": ""},
        ]

    def execute(self, sql):
        self.sql = sql

    def fetchall(self):
        return self.rows


def test_saved_api_key_model_names_are_read_from_model_slug_column(monkeypatch):
    _set_minimal_app_environment(monkeypatch)
    from app.models import user_api_key

    cursor = _Cursor()
    with patch.object(user_api_key, "get_cursor", return_value=cursor):
        names = user_api_key.get_distinct_model_names()

    assert names == ["gpt-4o", "openai/gpt-4o"]
    assert "model_slug" in cursor.sql
    assert "DISTINCT" in cursor.sql


def test_admin_pricing_route_only_passes_the_canonical_options_context():
    route_source = Path("app/admin_panel/routes.py").read_text(encoding="utf-8")

    assert "pricing_options = build_pricing_options()" in route_source
    assert "transcription_models=transcription_models" not in route_source
    assert "title_generation_models=title_generation_models" not in route_source
    assert "workflow_models=workflow_models" not in route_source


def test_live_purpose_keeps_the_entered_model_name():
    script = Path("app/static/js/user_settings.js").read_text(encoding="utf-8")

    assert "const purpose = document.querySelector('input[name=\"model_purpose\"]:checked')?.value || 'transcription';" in script
    assert "modelNameInput.value = liveModelSelect.value" not in script
    assert "formData.set('model_purpose', purpose);" in script
    assert "formData.set('model_name', modelName);" in script


def test_api_key_entries_render_explicit_model_purpose_badges():
    template = Path("app/templates/layout/modals/api_key_modal.html").read_text(encoding="utf-8")
    script = Path("app/static/js/user_settings.js").read_text(encoding="utf-8")
    service = Path("app/services/user_service.py").read_text(encoding="utf-8")

    assert "MODEL_PURPOSE_LABELS" in script
    assert "badge badge-muted text-xs" in script
    assert "badge.textContent = label" in script
    assert "textContent = `[${label}]`" not in script
    assert "model_purposes" in service
    assert "modelPurposePreviewBadges" in template


def test_api_key_modal_uses_provider_specific_model_help_and_mobile_purpose_layout():
    template = Path("app/templates/layout/modals/api_key_modal.html").read_text(encoding="utf-8")
    script = Path("app/static/js/user_settings.js").read_text(encoding="utf-8")

    assert 'id="modelNameHint"' in template
    assert "Enter the OpenAI model name only, without the vendor prefix." in script
    assert "Enter the Google model name only, without the vendor prefix." in script
    assert "Enter the OpenRouter model as vendor/model." in script
    assert "grid grid-cols-1 gap-2 sm:grid-cols-3" in template
    assert "Usage tags:" not in template
    assert "Choose whether this model should handle" not in template
    assert "Use the provider model name" not in template


def test_transcription_selectors_dedupe_catalog_codes():
    index_template = Path("app/templates/index.html").read_text(encoding="utf-8")
    profile_script = Path("app/static/js/profile.js").read_text(encoding="utf-8")
    catalog = Path("app/models/transcription_catalog.py").read_text(encoding="utf-8")

    assert "seen_transcription_models = namespace(codes=[])" in index_template
    assert "const seenTranscriptionModels = new Set();" in profile_script
    assert "seen_codes: set[str] = set()" in catalog
    assert "get_live_models" in catalog


def test_pricing_model_keys_are_not_rewritten_to_provider_names():
    pricing_model = Path("app/models/pricing.py").read_text(encoding="utf-8")

    assert "cursor.execute(sql, (item_type, str(item_key).strip(), price))" in pricing_model
    assert "item_key.lower()" not in pricing_model
    assert "item_key.upper()" not in pricing_model
    assert "TITLE_GENERATION_LLM_PROVIDER" not in pricing_model
    assert "WORKFLOW_LLM_PROVIDER" not in pricing_model


def test_pricing_fallbacks_use_model_defaults_not_provider_names():
    pricing_service = Path("app/services/pricing_service.py").read_text(encoding="utf-8")

    assert "TITLE_GENERATION_LLM_MODEL" in pricing_service
    assert "WORKFLOW_LLM_MODEL" in pricing_service
    assert "TITLE_GENERATION_LLM_PROVIDER" not in pricing_service
    assert "WORKFLOW_LLM_PROVIDER" not in pricing_service
