
import pytest
from unittest.mock import patch
from app.services.file_service import DEFAULT_CHUNK_LENGTH_MS, split_audio_file

def test_default_chunk_length_constant():
    """Verify that the DEFAULT_CHUNK_LENGTH_MS constant is set to 7 minutes."""
    expected_ms = 7 * 60 * 1000
    assert DEFAULT_CHUNK_LENGTH_MS == expected_ms, f"Expected {expected_ms}ms (7 min), but got {DEFAULT_CHUNK_LENGTH_MS}ms"

@patch('app.services.file_service.os.path.exists')
@patch('app.services.file_service.validate_file_path')
@patch('app.services.file_service.os.path.getsize')
@patch('app.services.file_service.get_audio_duration')
@patch('app.services.file_service.subprocess.run')
def test_split_audio_file_uses_default_chunk_length(mock_subprocess_run, mock_get_duration, mock_getsize, mock_validate, mock_exists):
    """
    Verify that split_audio_file uses the default chunk length when not specified.
    We'll mock a 15-minute audio file and expect 3 chunks (7+7+1).
    """
    # Setup mocks
    mock_exists.return_value = True
    mock_validate.return_value = True
    mock_get_duration.return_value = (15 * 60, 15)
    mock_getsize.return_value = 1000 # Small chunk size to avoid API-limit warnings
    
    # Call function without specifying chunk_length_ms
    chunks = split_audio_file("/fake/path/audio.mp3", "/fake/temp/dir")
    
    # Verification
    # 15 minutes / 7 minutes = 2.14 -> ceil -> 3 chunks
    assert len(chunks) == 3
    assert mock_subprocess_run.call_count == 3

    first_command = mock_subprocess_run.call_args_list[0].args[0]
    second_command = mock_subprocess_run.call_args_list[1].args[0]
    third_command = mock_subprocess_run.call_args_list[2].args[0]

    assert first_command[first_command.index("-ss") + 1] == "0.000"
    assert first_command[first_command.index("-t") + 1] == "420.000"
    assert second_command[second_command.index("-ss") + 1] == "420.000"
    assert second_command[second_command.index("-t") + 1] == "420.000"
    assert third_command[third_command.index("-ss") + 1] == "840.000"
    assert third_command[third_command.index("-t") + 1] == "60.000"
    assert all(chunk.endswith(".mp3") for chunk in chunks)
