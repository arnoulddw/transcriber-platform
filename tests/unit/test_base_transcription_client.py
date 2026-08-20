import logging
import threading
from typing import Any, Dict, Optional, Tuple
from unittest.mock import patch

import pytest

from app.services.api_clients.exceptions import TranscriptionProcessingError
from app.services.api_clients.transcription.base_transcription_client import (
    BaseTranscriptionClient,
)


class StubTranscriptionClient(BaseTranscriptionClient):
    CATALOG_MODEL_CODE = "stub"

    def __init__(self, chunk_results):
        self.chunk_results = chunk_results
        self.progress_messages = []
        self.has_transcription_warning = False
        super().__init__("test-key", {"TRANSCRIPTION_WORKERS": 2})
        self.cancel_event = threading.Event()
        self.progress_callback = self._collect_progress

    def _collect_progress(self, message, is_error=False):
        self.progress_messages.append((message, is_error))

    def _initialize_client(self, api_key):
        pass

    def _prepare_api_params(
        self,
        language_code: str,
        context_prompt: str,
        response_format: str,
        is_chunk: bool,
        extra_options: Optional[Dict[str, Any]] = None,
    ):
        return {}

    def _call_api(self, file_handle, api_params):
        return None

    def _process_response(self, response, response_format) -> Tuple[str, Optional[str]]:
        return "", None

    def _get_retryable_errors(self):
        return ()

    def _get_api_name(self):
        return "Stub Transcription"

    def _transcribe_single_chunk_with_retry(
        self,
        chunk_path: str,
        idx: int,
        total_chunks: int,
        language_code: str,
        response_format: str,
        context_prompt: str = "",
        log_prefix: str = "",
        max_retries: int = 3,
        extra_options: Optional[Dict[str, Any]] = None,
    ):
        return self.chunk_results[idx]


def _make_chunks(tmp_path, count, *, non_empty=True):
    chunks = []
    for index in range(1, count + 1):
        chunk = tmp_path / f"chunk_{index:03d}.mp3"
        chunk.write_bytes(b"audio" if non_empty else b"")
        chunks.append(str(chunk))
    return chunks


def _run_split(client, chunk_files):
    with patch(
        "app.services.api_clients.transcription.base_transcription_client.file_service.split_audio_file",
        return_value=chunk_files,
    ):
        return client._split_and_transcribe(
            "/tmp/source.mp3",
            "en",
            display_filename="source.mp3",
        )


def test_split_and_transcribe_fails_when_a_chunk_result_is_missing(tmp_path):
    chunk_files = _make_chunks(tmp_path, 2)
    client = StubTranscriptionClient({1: ("First chunk", None), 2: None})

    with pytest.raises(TranscriptionProcessingError, match="did not return a transcription result"):
        _run_split(client, chunk_files)


def test_split_and_transcribe_aggregates_all_chunk_results_in_order(tmp_path):
    chunk_files = _make_chunks(tmp_path, 3)
    client = StubTranscriptionClient({
        1: ("First chunk", None),
        2: ("Second chunk", None),
        3: ("Third chunk", None),
    })

    result = _run_split(client, chunk_files)

    assert result == ("First chunk Second chunk Third chunk", "en")
    assert client.has_transcription_warning is False


def test_split_and_transcribe_warns_for_empty_text_from_non_trivial_chunk(tmp_path, caplog):
    chunk_files = _make_chunks(tmp_path, 2)
    client = StubTranscriptionClient({1: ("First chunk", None), 2: ("  ", None)})

    with caplog.at_level(logging.WARNING):
        result = _run_split(client, chunk_files)

    assert result == ("First chunk", "en")
    assert client.has_transcription_warning is True
    assert any("Chunk 2/2 returned no transcription text" in record.message for record in caplog.records)
    assert any(
        not is_error and "Chunk 2/2 returned no transcription text" in message
        for message, is_error in client.progress_messages
    )


def test_split_and_transcribe_does_not_warn_for_empty_source_chunk(tmp_path, caplog):
    chunk_files = _make_chunks(tmp_path, 1, non_empty=False)
    client = StubTranscriptionClient({1: ("", None)})

    with caplog.at_level(logging.WARNING):
        result = _run_split(client, chunk_files)

    assert result == ("", "en")
    assert client.has_transcription_warning is False
    assert not any("returned no transcription text" in record.message for record in caplog.records)
