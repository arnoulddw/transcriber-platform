from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services import file_service


def test_flac_is_accepted_as_an_upload_format():
    assert file_service.allowed_file("recording.FLAC") is True


def test_transcode_audio_file_returns_one_output_and_preserves_source(tmp_path):
    source = tmp_path / "recording.m4a"
    source_bytes = b"original audio bytes"
    source.write_bytes(source_bytes)
    progress_messages = []

    def create_output(source_path, output_path, target_format, _cancellation_check):
        assert source_path == str(source)
        assert target_format == "mp3"
        Path(output_path).write_bytes(b"transcoded audio")

    with patch.object(file_service, "_transcode_audio_ffmpeg", side_effect=create_output) as transcode:
        output_path = file_service.transcode_audio_file(
            str(source),
            str(tmp_path),
            progress_callback=lambda message, is_error: progress_messages.append(
                (message, is_error)
            ),
        )

    assert output_path is not None
    assert Path(output_path).parent == tmp_path
    assert Path(output_path).suffix == ".mp3"
    assert Path(output_path).read_bytes() == b"transcoded audio"
    assert source.read_bytes() == source_bytes
    transcode.assert_called_once()
    assert progress_messages == [
        ("Checking cancellation before audio transcoding...", False)
    ]


def test_transcode_audio_ffmpeg_uses_shared_audio_encoding_settings():
    process = MagicMock()
    process.poll.return_value = 0
    process.returncode = 0
    process.communicate.return_value = ("", "")

    with patch.object(file_service.subprocess, "Popen", return_value=process) as popen:
        file_service._transcode_audio_ffmpeg(
            "/input.m4a",
            "/tmp/output.mp3",
            "mp3",
        )

    command = popen.call_args.args[0]
    assert command[:4] == ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    assert command[command.index("-vn") + 1] == "-ac"
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-ar") + 1] == str(file_service.CHUNK_AUDIO_SAMPLE_RATE)
    assert command[command.index("-b:a") + 1] == file_service.CHUNK_AUDIO_BITRATE
    assert command[command.index("-f") + 1] == "mp3"
    assert command[-1] == "/tmp/output.mp3"


def test_transcode_audio_ffmpeg_terminates_when_cancelled():
    process = MagicMock()
    process.returncode = None
    process.poll.return_value = None

    def terminate():
        process.returncode = -15
        process.poll.return_value = -15

    process.terminate.side_effect = terminate

    with patch.object(file_service.subprocess, "Popen", return_value=process):
        with pytest.raises(InterruptedError, match="cancelled"):
            file_service._transcode_audio_ffmpeg(
                "/input.m4a",
                "/tmp/output.mp3",
                "mp3",
                cancellation_check=lambda: True,
            )

    process.terminate.assert_called_once()


def test_transcode_audio_file_cleans_partial_output_on_ffmpeg_failure(tmp_path):
    source = tmp_path / "recording.m4a"
    source_bytes = b"original audio bytes"
    source.write_bytes(source_bytes)
    output_paths = []

    def fail_after_partial_output(_source_path, output_path, _target_format, _cancellation_check):
        output_paths.append(output_path)
        Path(output_path).write_bytes(b"partial")
        raise RuntimeError("ffmpeg failed")

    with patch.object(
        file_service,
        "_transcode_audio_ffmpeg",
        side_effect=fail_after_partial_output,
    ):
        output_path = file_service.transcode_audio_file(str(source), str(tmp_path))

    assert output_path is None
    assert output_paths and not Path(output_paths[0]).exists()
    assert source.read_bytes() == source_bytes


def test_transcode_audio_file_cleans_partial_output_and_propagates_cancellation(tmp_path):
    source = tmp_path / "recording.m4a"
    source.write_bytes(b"original audio bytes")
    output_paths = []

    def cancel_after_partial_output(_source_path, output_path, _target_format, _cancellation_check):
        output_paths.append(output_path)
        Path(output_path).write_bytes(b"partial")
        raise InterruptedError("cancelled")

    with patch.object(
        file_service,
        "_transcode_audio_ffmpeg",
        side_effect=cancel_after_partial_output,
    ):
        with pytest.raises(InterruptedError, match="cancelled"):
            file_service.transcode_audio_file(str(source), str(tmp_path))

    assert output_paths and not Path(output_paths[0]).exists()
    assert source.read_bytes() == b"original audio bytes"


def test_transcode_audio_file_rejects_source_outside_temp_directory(tmp_path):
    temp_dir = tmp_path / "allowed"
    temp_dir.mkdir()
    source = tmp_path / "outside.m4a"
    source.write_bytes(b"audio")

    with patch.object(file_service, "_transcode_audio_ffmpeg") as transcode:
        output_path = file_service.transcode_audio_file(str(source), str(temp_dir))

    assert output_path is None
    transcode.assert_not_called()


def test_transcode_audio_file_rejects_unsupported_target_format(tmp_path):
    source = tmp_path / "recording.m4a"
    source.write_bytes(b"audio")

    with patch.object(file_service, "_transcode_audio_ffmpeg") as transcode:
        output_path = file_service.transcode_audio_file(
            str(source), str(tmp_path), target_format="wav"
        )

    assert output_path is None
    transcode.assert_not_called()
