"""ffprobe duration parsing in file_service."""

from unittest.mock import patch

from app.services import file_service


def _run_probe(stdout: str):
    with patch.object(file_service.os.path, "exists", return_value=True), patch.object(
        file_service.subprocess, "run",
        return_value=type("R", (), {"stdout": stdout, "stderr": ""})(),
    ):
        return file_service.get_audio_duration("/tmp/fake.mp3")


def test_stream_duration_without_duration_ts_is_used():
    """Some containers expose stream duration without duration_ts."""
    import json
    media = {
        "streams": [
            {"codec_type": "video"},  # no duration: skipped
            {"codec_type": "audio", "duration": "95.75"},
        ]
    }
    seconds, minutes = _run_probe(json.dumps(media))
    assert seconds == 95.75
    assert minutes == round(95.75 / 60.0, 2)


def test_format_duration_still_wins_over_streams():
    import json
    media = {
        "format": {"duration": "60.0"},
        "streams": [{"duration": "999.0"}],
    }
    seconds, _minutes = _run_probe(json.dumps(media))
    assert seconds == 60.0


def test_unparseable_output_returns_zeroes():
    seconds, minutes = _run_probe("not json")
    assert (seconds, minutes) == (0.0, 0.0)
