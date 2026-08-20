from app.models.transcription.serialization import _map_row_to_transcription_dict


def test_transcription_warning_defaults_to_false_for_legacy_row():
    row = {
        "id": "legacy-job",
        "progress_log": [],
    }

    mapped = _map_row_to_transcription_dict(row)

    assert mapped is not None
    assert mapped["has_transcription_warning"] is False


def test_transcription_warning_is_normalized_to_boolean():
    row = {
        "id": "warned-job",
        "progress_log": [],
        "has_transcription_warning": 1,
    }

    mapped = _map_row_to_transcription_dict(row)

    assert mapped is not None
    assert mapped["has_transcription_warning"] is True
