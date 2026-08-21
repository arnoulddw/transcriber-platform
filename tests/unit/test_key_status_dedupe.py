"""Purpose-aware dedupe in aggregate/admin key status collection."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services import user_service


def _record(key_id, provider, slug, purposes):
    return {
        "id": key_id,
        "provider_code": provider,
        "model_slug": slug,
        "model_purposes": purposes,
        "encrypted_key": f"encrypted-{key_id}",
    }


def _collect(records):
    status = user_service._new_empty_key_status()
    security = Mock()
    security.decrypt_data.side_effect = lambda value: f"plain-{value}"
    logger = Mock()
    user_service._collect_key_status_entries(status, records, security, logger)
    return status


def test_same_model_different_purposes_are_both_kept():
    status = _collect(
        [
            _record(1, "openai", "gpt-transcribe", "transcription"),
            _record(2, "openai", "gpt-transcribe", "llm"),
        ]
    )

    entries = status["provider_keys"]["openai"]
    assert len(entries) == 2
    purposes = {tuple(entry["model_purposes"]) for entry in entries}
    assert purposes == {("transcription",), ("llm",)}


def test_identical_entries_are_still_deduped():
    status = _collect(
        [
            _record(1, "openai", "gpt-transcribe", "transcription"),
            _record(2, "openai", "gpt-transcribe", "transcription"),
        ]
    )

    entries = status["provider_keys"]["openai"]
    assert len(entries) == 1


def test_same_model_same_purpose_different_users_deduped():
    """Aggregate across users: identical (model, purpose) collapses to one entry."""
    status = user_service._new_empty_key_status()
    security = Mock()
    security.decrypt_data.side_effect = lambda value: f"plain-{value}"
    user_service._collect_key_status_entries(
        status,
        [
            {**_record(1, "openai", "gpt-transcribe", "transcription"), "user_id": 1},
            {**_record(2, "openai", "gpt-transcribe", "transcription"), "user_id": 2},
        ],
        security,
        Mock(),
    )

    assert len(status["provider_keys"]["openai"]) == 1
