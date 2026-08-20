import pytest
from unittest.mock import MagicMock, patch

from app.services.file_service import (
    DEFAULT_CHUNK_LENGTH_MS,
    _segment_audio_ffmpeg,
    split_audio_file,
)


def test_default_chunk_length_constant():
    assert DEFAULT_CHUNK_LENGTH_MS == 7 * 60 * 1000


@patch('app.services.file_service.validate_file_path', return_value=True)
@patch('app.services.file_service.os.path.getsize', return_value=1000)
@patch('app.services.file_service.get_audio_duration', return_value=(15 * 60, 15))
@patch('app.services.file_service._segment_audio_ffmpeg')
def test_split_audio_file_segments_source_in_one_ffmpeg_pass(
    mock_segment,
    _mock_get_duration,
    _mock_getsize,
    _mock_validate,
    tmp_path,
):
    """A 15-minute source is segmented once into three sorted chunk files."""
    source = tmp_path / 'audio.mp3'
    source.write_bytes(b'source')

    def create_mock_segments(_source, output_pattern, _seconds, _check):
        for index in range(1, 4):
            output_path = output_pattern.replace('%03d', f'{index:03d}')
            with open(output_path, 'wb') as segment:
                segment.write(b'chunk')

    mock_segment.side_effect = create_mock_segments

    chunks = split_audio_file(str(source), str(tmp_path))

    assert len(chunks) == 3
    assert chunks == sorted(chunks)
    mock_segment.assert_called_once()
    args = mock_segment.call_args.args
    assert args[0] == str(source)
    assert args[2] == 420.0
    assert args[1].endswith('_chunk_%03d.mp3')
    assert all(chunk.endswith('.mp3') for chunk in chunks)


@patch('app.services.file_service.validate_file_path', return_value=True)
@patch('app.services.file_service.os.path.getsize', return_value=1000)
@patch('app.services.file_service.get_audio_duration', return_value=(15 * 60, 15))
@patch('app.services.file_service._segment_audio_ffmpeg')
def test_split_audio_file_rejects_chunk_count_mismatch(
    mock_segment,
    _mock_get_duration,
    _mock_getsize,
    _mock_validate,
    tmp_path,
):
    source = tmp_path / 'audio.mp3'
    source.write_bytes(b'source')

    def create_incomplete_segments(_source, output_pattern, _seconds, _check):
        for index in range(1, 3):
            output_path = output_pattern.replace('%03d', f'{index:03d}')
            with open(output_path, 'wb') as segment:
                segment.write(b'chunk')

    mock_segment.side_effect = create_incomplete_segments
    progress_messages = []

    chunks = split_audio_file(
        str(source),
        str(tmp_path),
        progress_callback=lambda message, is_error=False: progress_messages.append((message, is_error)),
    )

    assert chunks == []
    assert any(
        is_error and 'Expected 3 audio chunks but FFmpeg created 2.' in message
        for message, is_error in progress_messages
    )
    assert not list(tmp_path.glob('audio_chunk_*.mp3'))


@patch('app.services.file_service.validate_file_path', return_value=True)
@patch('app.services.file_service.os.path.getsize', return_value=1000)
@patch('app.services.file_service.get_audio_duration', return_value=(15 * 60, 15))
@patch('app.services.file_service._segment_audio_ffmpeg', return_value=None)
def test_split_audio_file_rejects_missing_ffmpeg_outputs(
    _mock_segment,
    _mock_get_duration,
    _mock_getsize,
    _mock_validate,
    tmp_path,
):
    source = tmp_path / 'audio.mp3'
    source.write_bytes(b'source')
    assert split_audio_file(str(source), str(tmp_path)) == []


def test_segment_audio_uses_ffmpeg_segment_muxer():
    process = MagicMock()
    process.poll.return_value = 0
    process.returncode = 0
    process.communicate.return_value = ('', '')

    with patch('app.services.file_service.subprocess.Popen', return_value=process) as popen:
        _segment_audio_ffmpeg('/input.wav', '/tmp/chunk_%03d.mp3', 420.0)

    command = popen.call_args.args[0]
    assert command[command.index('-f') + 1] == 'segment'
    assert command[command.index('-segment_time') + 1] == '420.000'
    assert command[command.index('-segment_start_number') + 1] == '1'
    assert command[-1] == '/tmp/chunk_%03d.mp3'


def test_segment_audio_terminates_when_cancelled():
    process = MagicMock()
    process.returncode = None
    process.poll.return_value = None

    def terminate():
        process.returncode = -15
        process.poll.return_value = -15

    process.terminate.side_effect = terminate

    with patch('app.services.file_service.subprocess.Popen', return_value=process):
        with pytest.raises(InterruptedError):
            _segment_audio_ffmpeg(
                '/input.wav', '/tmp/chunk_%03d.mp3', 420.0,
                cancellation_check=lambda: True,
            )

    process.terminate.assert_called_once()
