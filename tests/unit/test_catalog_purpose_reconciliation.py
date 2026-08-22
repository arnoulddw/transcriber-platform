"""Regression tests for catalog purpose-set reconciliation (2026-08-22).

Deleting the ``live`` key for ``openai:gpt-transcribe`` and re-saving it for
transcription only left the catalog row at ``transcription,live`` forever,
because ``register_model_from_provider`` deliberately only accumulates
purposes. The model therefore kept rendering on the Live page. These tests
lock in the shrink path: per-save, per-delete, and the startup healing pass.
"""
import sys
import os
from unittest.mock import Mock, patch

from mysql.connector import Error as MySQLError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.models import transcription_catalog  # noqa: E402
from app.models import user_api_key  # noqa: E402
from app.services import user_service  # noqa: E402


class _ScriptedCursor:
    """Cursor that answers catalog/key SELECTs and records UPDATEs."""

    def __init__(self, catalog_rows=None, key_rows=None):
        self.catalog_rows = catalog_rows or []
        self.key_rows = key_rows or []
        self.updates = []

    def execute(self, sql, params=None):
        if "FROM transcription_models_catalog" in sql and sql.lstrip().startswith("SELECT"):
            self.last_select = "catalog"
        elif "FROM user_api_keys" in sql:
            self.last_select = "keys"
        elif "UPDATE transcription_models_catalog" in sql:
            self.updates.append((sql, params))
            self.last_select = None
        elif sql.lstrip().startswith("SELECT"):
            self.last_select = "catalog"

    def fetchall(self):
        return self.catalog_rows if self.last_select == "catalog" else list(self.key_rows)

    def fetchone(self):
        return None


class _RecordingDatabase:
    def __init__(self):
        self.commit_count = 0

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        pass


def test_reconcile_shrinks_live_when_only_transcription_keys_remain():
    cursor = _ScriptedCursor(key_rows=[{"model_purposes": "transcription"}])
    database = _RecordingDatabase()
    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=database
    ):
        transcription_catalog.reconcile_model_purposes("openai", "gpt-transcribe")

    assert len(cursor.updates) == 1
    _, params = cursor.updates[0]
    assert params == ("transcription", "openai", "gpt-transcribe")


def test_reconcile_drops_live_purpose_when_no_keys_remain():
    """Arnould's incident state: dual-purpose row, live key already deleted."""
    cursor = _ScriptedCursor(key_rows=[])
    database = _RecordingDatabase()
    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=database
    ):
        transcription_catalog.reconcile_model_purposes("openai", "gpt-transcribe")

    _, params = cursor.updates[0]
    assert params == ("transcription", "openai", "gpt-transcribe")


def test_reconcile_keeps_union_when_both_purposes_survive():
    cursor = _ScriptedCursor(key_rows=[{"model_purposes": "live"}, {"model_purposes": "transcription"}])
    database = _RecordingDatabase()
    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=database
    ):
        transcription_catalog.reconcile_model_purposes("openai", "gpt-transcribe")

    _, params = cursor.updates[0]
    assert params == ("transcription,live", "openai", "gpt-transcribe")


def test_reconcile_ignores_unknown_provider_and_blank_code():
    cursor = _ScriptedCursor()
    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=_RecordingDatabase()
    ):
        transcription_catalog.reconcile_model_purposes("gemini", "gpt-transcribe")
        transcription_catalog.reconcile_model_purposes("openai", "")

    assert cursor.updates == []


def test_reconcile_normalizes_legacy_assemblyai_identity():
    cursor = _ScriptedCursor(key_rows=[])
    database = _RecordingDatabase()
    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=database
    ):
        transcription_catalog.reconcile_model_purposes("assemblyai", "assemblyai")

    _, params = cursor.updates[0]
    assert params == ("transcription", "assemblyai", "universal")


def test_reconcile_survives_missing_key_table():
    cursor = Mock()
    err = MySQLError("table missing")
    err.errno = 1146
    cursor.execute.side_effect = err
    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=_RecordingDatabase()
    ):
        # Must not raise: reconciliation is best-effort, like registration.
        transcription_catalog.reconcile_model_purposes("openai", "gpt-transcribe")


def test_live_save_reconciles_before_registering():
    """Saving a key for one purpose only may shrink the catalog purpose set,
    so reconciliation must run before the accumulate-only registration."""
    calls = []

    app_user = Mock(id=7)
    security_service = Mock()
    security_service.encrypt_data.return_value = "encrypted"

    with patch.object(
        user_service.user_model, "get_user_by_id", return_value=app_user
    ), patch.object(
        user_service, "get_security_service", return_value=security_service
    ), patch.object(
        user_service.user_api_key_model, "upsert_api_key", return_value=True
    ) as upsert, patch.object(
        user_service.user_model, "update_user_preferences", return_value=True
    ), patch.object(
        transcription_catalog, "reconcile_model_purposes",
        side_effect=lambda *a: calls.append(("reconcile", a)),
    ), patch.object(
        transcription_catalog, "register_model_from_provider",
        side_effect=lambda **kw: calls.append(("register", kw)),
    ):
        assert user_service.save_user_api_key(
            7, "openai", "sk-test-key", model_name="gpt-transcribe", model_purpose="live"
        ) is True

    upsert.assert_called_once()
    assert [name for name, _ in calls] == ["reconcile", "register"]
    assert calls[0][1] == ("openai", "gpt-transcribe")
    assert calls[1][1]["model_purpose"] == "live"


def test_delete_by_slug_reconciles_deleted_identity():
    with patch.object(
        user_service.user_model, "get_user_by_id", return_value=Mock(id=7)
    ), patch.object(
        user_service.user_api_key_model, "delete_api_key", return_value=True
    ) as delete_key, patch.object(
        transcription_catalog, "reconcile_model_purposes"
    ) as reconcile:
        user_service.delete_user_api_key(7, "openai", model_slug="gpt-transcribe")

    delete_key.assert_called_once()
    reconcile.assert_called_once_with("openai", "gpt-transcribe")


def test_delete_by_id_reconciles_deleted_identity():
    record = {"id": 11, "provider_code": "openai", "model_slug": "gpt-transcribe",
              "model_purposes": "transcription,live"}
    with patch.object(
        user_service.user_model, "get_user_by_id", return_value=Mock(id=7)
    ), patch.object(
        user_service.user_api_key_model,
        "get_api_key_record_by_id",
        return_value=record,
    ) as get_record, patch.object(
        user_service.user_api_key_model, "delete_api_key_by_id", return_value=True
    ) as delete_key, patch.object(
        transcription_catalog, "reconcile_model_purposes"
    ) as reconcile:
        user_service.delete_user_api_key_by_id(7, 11)

    delete_key.assert_called_once_with(7, 11)
    get_record.assert_called_once_with(7, 11)
    reconcile.assert_called_once_with("openai", "gpt-transcribe")


def test_delete_by_id_skips_reconcile_for_provider_wide_key():
    record = {"id": 13, "provider_code": "openai", "model_slug": None,
              "model_purposes": "transcription"}
    with patch.object(
        user_service.user_model, "get_user_by_id", return_value=Mock(id=7)
    ), patch.object(
        user_service.user_api_key_model,
        "get_api_key_record_by_id",
        return_value=record,
    ), patch.object(
        user_service.user_api_key_model, "delete_api_key_by_id", return_value=True
    ), patch.object(
        transcription_catalog, "reconcile_model_purposes"
    ) as reconcile:
        user_service.delete_user_api_key_by_id(7, 13)

    reconcile.assert_not_called()


def test_startup_heal_recomputes_every_active_model_row():
    """The heal pass must not skip dual-purpose rows: a row can be
    'transcription,live' while its live key is already gone."""
    cursor = _ScriptedCursor(
        catalog_rows=[{"provider_code": "openai", "code": "gpt-transcribe"}]
    )
    database = _RecordingDatabase()
    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=database
    ), patch.object(
        transcription_catalog, "reconcile_model_purposes"
    ) as reconcile:
        transcription_catalog._heal_stale_purpose_sets()

    reconcile.assert_called_once_with("openai", "gpt-transcribe")


def test_startup_heal_repairs_exact_incident_state():
    """End-to-end over the real SQL: catalog row 'transcription,live', only a
    transcription key left -> the UPDATE must store 'transcription' so the
    model disappears from get_live_models()."""
    cursor = _ScriptedCursor(
        catalog_rows=[{"provider_code": "openai", "code": "gpt-transcribe"}],
        key_rows=[{"model_purposes": "transcription"}],
    )
    database = _RecordingDatabase()
    with patch.object(transcription_catalog, "get_cursor", return_value=cursor), patch.object(
        transcription_catalog, "get_db", return_value=database
    ):
        transcription_catalog._heal_stale_purpose_sets()

    assert len(cursor.updates) == 1
    _, params = cursor.updates[0]
    assert params == ("transcription", "openai", "gpt-transcribe")


def test_admin_grid_forms_stay_two_columns_on_tablets():
    """Tablet regression: three columns at md squeezed the form controls out
    of alignment; both admin forms must step 1 -> 2 -> 3."""
    models_template = os.path.join(
        os.path.dirname(__file__), "..", "..", "app", "templates", "admin", "models.html"
    )
    costs_template = os.path.join(
        os.path.dirname(__file__), "..", "..", "app", "templates", "admin", "costs.html"
    )
    for path in (models_template, costs_template):
        with open(path, encoding="utf-8") as handle:
            template = handle.read()
        assert 'class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6"' in template
        assert 'class="grid grid-cols-1 md:grid-cols-3 gap-6"' not in template
