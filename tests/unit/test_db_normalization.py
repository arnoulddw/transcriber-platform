from unittest.mock import Mock, patch
from app.models.transcription.serialization import _map_row_to_transcription_dict
from app.models.transcription_utils.filtering import VALID_TRANSCRIPTION_COLUMNS_FOR_FILTERING
from app.models import llm_operation as llm_operation_model
from app.services import pricing_service, user_service


def test_valid_filtering_columns_do_not_contain_llm_operation():
    assert "llm_operation_id" not in VALID_TRANSCRIPTION_COLUMNS_FOR_FILTERING
    assert "llm_operation_status" not in VALID_TRANSCRIPTION_COLUMNS_FOR_FILTERING
    assert "llm_operation_result" not in VALID_TRANSCRIPTION_COLUMNS_FOR_FILTERING
    assert "llm_operation_error" not in VALID_TRANSCRIPTION_COLUMNS_FOR_FILTERING
    assert "llm_operation_ran_at" not in VALID_TRANSCRIPTION_COLUMNS_FOR_FILTERING


def test_get_latest_workflow_operation_for_transcription_query():
    cursor = Mock()
    cursor.fetchone.return_value = {
        "id": 42,
        "transcription_id": "job-123",
        "operation_type": "workflow",
        "status": "finished",
        "result": "Summarized text",
        "error": None,
        "cost": 0.005,
        "input_text": "Prompt",
        "model": "gpt-4o",
        "provider": "openai",
        "created_at": None,
        "completed_at": None,
    }
    with patch.object(llm_operation_model, "get_cursor", return_value=cursor):
        op = llm_operation_model.get_latest_workflow_operation_for_transcription("job-123", user_id=1)

    assert op is not None
    assert op["id"] == 42
    assert op["result"] == "Summarized text"
    cursor.execute.assert_called_once()
    sql, params = cursor.execute.call_args[0]
    assert "WHERE transcription_id = %s AND operation_type = 'workflow'" in sql
    assert params == ("job-123", 1)


def test_pricing_service_update_price_with_billing_unit():
    with patch.object(pricing_service.pricing_model, "update_prices") as mock_update:
        pricing_service.update_price("transcription", "openai/whisper-1", 0.006, billing_unit="per_minute")
        mock_update.assert_called_once_with(
            {"transcription": {"openai/whisper-1": 0.006}},
            billing_units={"openai/whisper-1": "per_minute"},
        )


def test_pricing_service_update_price_rejects_invalid_unit():
    import pytest
    with pytest.raises(pricing_service.PricingServiceError, match="Invalid billing unit"):
        pricing_service.update_price("transcription", "openai/whisper-1", 0.006, billing_unit="per_lightyear")


def test_authenticate_public_api_key_delegates_to_public_api_key_model():
    fake_user = Mock()
    fake_user.id = 99
    with patch("app.services.user_service._hash_public_api_key", return_value="hashed_key"), \
         patch("app.services.user_service.public_api_key_model.get_user_by_public_api_key_hash", return_value=fake_user) as mock_get:
        user = user_service.authenticate_public_api_key("test-key-abc")
        assert user == fake_user
        mock_get.assert_called_once_with("hashed_key")
