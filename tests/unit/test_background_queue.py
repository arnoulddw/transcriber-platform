from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from flask import Flask

from app.models import background_job
from app.models import transcription as transcription_model
from app.services.transcription_service import process_transcription
from app.services import workflow_service
from app.services.api_clients.exceptions import LlmRateLimitError
from app.tasks import background_queue, worker, transcription_queue


def test_submit_transcription_job_serializes_reconstructable_args_only():
    with patch.object(
        background_queue.background_job_model, "enqueue_job", return_value=17
    ) as enqueue:
        job_id = transcription_queue.submit_transcription_job(
            {"BACKGROUND_JOB_MAX_ATTEMPTS": 4},
            process_transcription,
            object(),
            "job-id",
            7,
            "/tmp/audio.mp3",
            "en",
            "whisper",
            "audio.mp3",
        )

    assert job_id == 17
    assert enqueue.call_args.args[:2] == ("transcription", {
        "args": ["job-id", 7, "/tmp/audio.mp3", "en", "whisper", "audio.mp3"],
    })
    assert enqueue.call_args.kwargs["max_attempts"] == 4


def test_workflow_payload_does_not_duplicate_transcript_text():
    with patch.object(
        background_queue.background_job_model, "enqueue_job", return_value=21
    ) as enqueue:
        result = background_queue.enqueue_workflow_job(
            {"BACKGROUND_JOB_MAX_ATTEMPTS": 3},
            7,
            "transcription-1",
            42,
            "Summarize this",
            "a very large transcript",
            "OPENROUTER",
            "google/gemini-3.7-flash",
            commit=False,
        )

    assert result == 21
    payload = enqueue.call_args.args[1]
    assert payload == {
        "user_id": 7,
        "transcription_id": "transcription-1",
        "operation_id": 42,
        "prompt": "Summarize this",
        "llm_provider": "OPENROUTER",
        "llm_model": "google/gemini-3.7-flash",
    }
    assert enqueue.call_args.kwargs["commit"] is False


def test_claim_next_job_locks_and_marks_one_row_running():
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = {
        "id": 9,
        "task_type": "title_generation",
        "payload": '{"transcription_id": "t1", "user_id": 7}',
        "attempts": 0,
        "max_attempts": 3,
    }
    cursor.rowcount = 1

    with patch.object(background_job, "get_db", return_value=connection):
        claimed = background_job.claim_next_job("worker-a")

    assert claimed["id"] == 9
    assert claimed["attempts"] == 1
    assert claimed["payload"] == {"transcription_id": "t1", "user_id": 7}
    assert "FOR UPDATE SKIP LOCKED" in cursor.execute.call_args_list[0].args[0]
    connection.commit.assert_called_once_with()


def test_fail_job_retries_then_can_be_terminal():
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.rowcount = 1
    with patch.object(background_job, "get_db", return_value=connection), patch.object(
        background_job, "get_cursor", return_value=cursor
    ):
        assert background_job.fail_job(9, "worker-a", 1, 3, "temporary", 2) == "pending"
        assert background_job.fail_job(9, "worker-a", 3, 3, "final", 2) == "failed"

    assert cursor.execute.call_count == 2
    assert connection.commit.call_count == 2
    assert "status = 'pending'" in cursor.execute.call_args_list[0].args[0]
    assert "status = 'failed'" in cursor.execute.call_args_list[1].args[0]
    assert "available_at = UTC_TIMESTAMP(6)" in cursor.execute.call_args_list[1].args[0]


def test_stale_claim_recovery_is_explicit():
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.fetchall.return_value = [
        {
            "id": 9,
            "task_type": "title_generation",
            "payload": {"transcription_id": "t1", "user_id": 7},
            "attempts": 1,
            "max_attempts": 3,
        }
    ]
    cursor.rowcount = 1
    with patch.object(background_job, "get_db", return_value=connection), patch.object(
        background_job, "get_cursor", return_value=cursor
    ):
        assert background_job.recover_stale_jobs(300) == 1

    sqls = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("status = 'running'" in sql and "claimed_at < DATE_SUB" in sql for sql in sqls)
    assert any("available_at = UTC_TIMESTAMP(6)" in sql for sql in sqls)
    assert not any("UPDATE transcriptions" in sql for sql in sqls)
    connection.commit.assert_called_once_with()


@pytest.mark.parametrize(
    ("task_type", "payload", "domain_sql"),
    [
        (
            "transcription",
            {"args": ["transcription-1", 7, "/tmp/audio.mp3", "en", "whisper", "audio.mp3"]},
            "UPDATE transcriptions",
        ),
        (
            "workflow",
            {
                "user_id": 7,
                "transcription_id": "transcription-1",
                "operation_id": 42,
                "prompt": "Summarize",
                "llm_provider": "OPENROUTER",
            },
            "UPDATE llm_operations",
        ),
        (
            "title_generation",
            {"transcription_id": "transcription-1", "user_id": 7},
            "title_generation_status = 'failed'",
        ),
    ],
)
def test_terminal_stale_recovery_reconciles_active_domain_record(
    task_type, payload, domain_sql
):
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.fetchall.return_value = [
        {
            "id": 9,
            "task_type": task_type,
            "payload": payload,
            "attempts": 3,
            "max_attempts": 3,
        }
    ]
    cursor.rowcount = 1

    with patch.object(background_job, "get_db", return_value=connection), patch.object(
        background_job, "get_cursor", return_value=cursor
    ):
        assert background_job.recover_stale_jobs(300) == 1

    sqls = [call.args[0] for call in cursor.execute.call_args_list]
    assert any(domain_sql in sql for sql in sqls)
    assert any("UPDATE background_jobs" in sql and "status = %s" in sql for sql in sqls)
    if task_type == "transcription":
        assert any(
            "status IN ('pending', 'processing', 'cancelling')" in sql
            for sql in sqls
        )
        assert background_job.STALE_JOB_FAILURE_MESSAGE in str(
            cursor.execute.call_args_list[1].args[1]
        )
    elif task_type == "workflow":
        assert any("status IN ('pending', 'processing')" in sql for sql in sqls)
        assert background_job.STALE_JOB_FAILURE_MESSAGE in str(
            cursor.execute.call_args_list[1].args[1]
        )
    else:
        assert any(
            "title_generation_status IN ('pending', 'processing')" in sql
            for sql in sqls
        )
    connection.commit.assert_called_once_with()


def test_worker_records_retry_when_claimed_task_raises():
    job = {
        "id": 4,
        "task_type": "title_generation",
        "payload": {"transcription_id": "t1", "user_id": 7},
        "attempts": 1,
        "max_attempts": 3,
    }
    app = SimpleNamespace(
        config={
            "BACKGROUND_WORKER_CONCURRENCY": 1,
            "BACKGROUND_JOB_POLL_SECONDS": 0.01,
            "BACKGROUND_JOB_STALE_SECONDS": 300,
        },
        app_context=lambda: nullcontext(),
    )
    stop_after_empty = [job, None]

    with patch.object(worker, "_worker_id", return_value="worker-a"), patch.object(
        worker.background_job_model,
        "recover_stale_jobs",
        return_value=0,
    ), patch.object(
        worker.background_job_model,
        "claim_next_job",
        side_effect=lambda _worker_id: stop_after_empty.pop(0),
    ), patch.object(
        worker.background_job_model, "heartbeat_jobs", return_value=1
    ), patch.object(
        worker.background_job_model, "fail_job", return_value="pending"
    ) as fail_job, patch.object(
        worker.background_job_model, "complete_job"
    ) as complete_job, patch.object(
        worker, "dispatch_job", side_effect=RuntimeError("provider unavailable")
    ):
        assert worker.run_worker(app, run_once=True) == 0

    fail_job.assert_called_once()
    assert fail_job.call_args.args[:5] == (4, "worker-a", 1, 3, "RuntimeError: provider unavailable")
    complete_job.assert_not_called()


def test_dispatch_does_not_mark_failed_transcription_as_succeeded():
    job = {
        "id": 12,
        "task_type": "transcription",
        "payload": {"args": ["transcription-1", 7, "/tmp/audio.mp3", "en", "whisper", "audio.mp3"]},
        "attempts": 1,
        "max_attempts": 3,
    }
    with patch(
        "app.services.transcription_service.process_transcription"
    ) as process, patch.object(
        transcription_model,
        "get_transcription_by_id",
        return_value={"status": "error", "error_message": "provider rejected input"},
    ), patch.object(background_queue, "close_db"):
        with pytest.raises(background_queue.TerminalBackgroundTaskFailure):
            background_queue.dispatch_job(object(), job)

    process.assert_called_once()
    assert process.call_args.kwargs["preserve_input_on_retry"] is True


def test_dispatch_does_not_retain_input_on_final_transcription_attempt():
    job = {
        "id": 14,
        "task_type": "transcription",
        "payload": {"args": ["transcription-1", 7, "/tmp/audio.mp3", "en", "whisper", "audio.mp3"]},
        "attempts": 3,
        "max_attempts": 3,
    }
    with patch(
        "app.services.transcription_service.process_transcription"
    ) as process, patch.object(
        transcription_model,
        "get_transcription_by_id",
        return_value={"status": "error", "error_message": "provider rejected input"},
    ), patch.object(background_queue, "close_db"):
        with pytest.raises(background_queue.TerminalBackgroundTaskFailure):
            background_queue.dispatch_job(object(), job)

    assert process.call_args.kwargs["preserve_input_on_retry"] is False


def test_dispatch_skips_finished_transcription_to_avoid_a_second_provider_call():
    job = {
        "id": 15,
        "task_type": "transcription",
        "payload": {"args": ["transcription-1", 7, "/tmp/audio.mp3", "en", "whisper", "audio.mp3"]},
        "attempts": 1,
        "max_attempts": 3,
    }
    with patch.object(
        transcription_model,
        "get_transcription_by_id",
        return_value={"status": "finished", "transcription_text": "saved text"},
    ), patch(
        "app.services.transcription_service.process_transcription"
    ) as process:
        assert background_queue.dispatch_job(object(), job) == "saved text"

    process.assert_not_called()


def test_dispatch_reads_finished_transcription_after_releasing_old_snapshot():
    job = {
        "id": 17,
        "task_type": "transcription",
        "payload": {"args": ["transcription-1", 7, "/tmp/audio.mp3", "en", "whisper", "audio.mp3"]},
        "attempts": 1,
        "max_attempts": 3,
    }
    snapshot_released = False

    def get_transcription(*_args):
        return {"status": "finished" if snapshot_released else "pending"}

    def release_snapshot():
        nonlocal snapshot_released
        snapshot_released = True

    with patch.object(
        transcription_model, "get_transcription_by_id", side_effect=get_transcription
    ), patch.object(
        background_queue, "close_db", side_effect=release_snapshot
    ), patch(
        "app.services.transcription_service.process_transcription", return_value="saved text"
    ) as process:
        assert background_queue.dispatch_job(object(), job) == "saved text"

    process.assert_called_once()


def test_dispatch_treats_missing_transcription_as_terminal():
    job = {
        "id": 16,
        "task_type": "transcription",
        "payload": {"args": ["missing", 7, "/tmp/audio.mp3", "en", "whisper", "audio.mp3"]},
        "attempts": 1,
        "max_attempts": 3,
    }
    with patch.object(
        transcription_model,
        "get_transcription_by_id",
        return_value=None,
    ), patch(
        "app.services.transcription_service.process_transcription"
    ) as process:
        with pytest.raises(background_queue.TerminalBackgroundTaskFailure):
            background_queue.dispatch_job(object(), job)

    process.assert_not_called()


def test_dispatch_skips_finished_workflow_to_avoid_a_second_provider_call():
    job = {
        "id": 13,
        "task_type": "workflow",
        "payload": {
            "user_id": 7,
            "transcription_id": "transcription-1",
            "operation_id": 42,
            "prompt": "Summarize",
            "llm_provider": "OPENROUTER",
            "llm_model": "model-1",
        },
    }
    finished_operation = {"status": "finished", "result": "already saved"}
    # The dispatch function imports the model lazily, so patch its module
    # without requiring a live database connection.
    with patch(
        "app.models.llm_operation.get_llm_operation_by_id",
        return_value=finished_operation,
    ) as get_operation, patch(
        "app.services.workflow_service.process_workflow_background"
    ) as process:
        assert background_queue.dispatch_job(object(), job) == "already saved"

    get_operation.assert_called_once_with(42, 7)
    process.assert_not_called()


def test_dispatch_reads_finished_workflow_after_releasing_old_snapshot():
    job = {
        "task_type": "workflow",
        "payload": {
            "user_id": 7,
            "transcription_id": "transcription-1",
            "operation_id": 42,
            "prompt": "Summarize",
            "llm_provider": "OPENROUTER",
        },
    }
    snapshot_released = False

    def get_operation(*_args):
        return {"status": "finished" if snapshot_released else "pending"}

    def release_snapshot():
        nonlocal snapshot_released
        snapshot_released = True

    with patch(
        "app.models.llm_operation.get_llm_operation_by_id", side_effect=get_operation
    ), patch.object(
        transcription_model, "get_transcription_by_id", return_value={"transcription_text": "Transcript"}
    ), patch.object(
        background_queue, "close_db", side_effect=release_snapshot
    ), patch(
        "app.services.workflow_service.process_workflow_background", return_value="Summary"
    ) as process:
        assert background_queue.dispatch_job(object(), job) == "Summary"

    process.assert_called_once()


def test_dispatch_reads_generated_title_after_releasing_old_snapshot():
    job = {
        "task_type": "title_generation",
        "payload": {"transcription_id": "transcription-1", "user_id": 7},
    }
    snapshot_released = False

    def get_transcription(*_args):
        return {"title_generation_status": "success" if snapshot_released else "pending"}

    def release_snapshot():
        nonlocal snapshot_released
        snapshot_released = True

    with patch.object(
        transcription_model, "get_transcription_by_id", side_effect=get_transcription
    ), patch.object(
        background_queue, "close_db", side_effect=release_snapshot
    ), patch(
        "app.tasks.title_generation.generate_title_task", return_value="Title"
    ) as process:
        assert background_queue.dispatch_job(object(), job) == "Title"

    process.assert_called_once()


def test_purge_terminal_jobs_targets_only_terminal_rows_in_a_bounded_batch():
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.rowcount = 3
    with patch.object(background_job, "get_db", return_value=connection), patch.object(
        background_job, "get_cursor", return_value=cursor
    ):
        assert background_job.purge_terminal_jobs(30, batch_size=25) == 3

    sql = cursor.execute.call_args.args[0]
    assert "status IN ('succeeded', 'failed')" in sql
    assert "completed_at IS NOT NULL" in sql
    assert "LIMIT %s" in sql
    assert cursor.execute.call_args.args[1] == (30, 25)
    assert connection.commit.call_count == 1


def test_active_transcription_paths_protect_pending_and_running_uploads():
    cursor = Mock()
    cursor.fetchall.return_value = [
        {"file_path": "/uploads/pending.wav"},
        {"file_path": "/uploads/running.wav"},
        {"file_path": None},
    ]
    with patch.object(background_job, "get_cursor", return_value=cursor):
        paths = background_job.get_active_transcription_file_paths()

    assert paths == {"/uploads/pending.wav", "/uploads/running.wav"}
    sql = cursor.execute.call_args.args[0]
    assert "task_type = 'transcription'" in sql
    assert "status IN ('pending', 'running')" in sql


@pytest.mark.parametrize(
    ("retry_pending", "expected_status"),
    [(True, "processing"), (False, "error")],
)
def test_retryable_workflow_keeps_active_status_until_final_attempt(
    retry_pending, expected_status
):
    app = Flask(__name__)
    app.config.update(WORKFLOW_LLM_PROVIDER="GEMINI", WORKFLOW_LLM_MODEL="test-model")
    with patch.object(
        workflow_service.llm_operation_model,
        "get_llm_operation_by_id",
        return_value={"status": "pending"},
    ), patch.object(
        workflow_service.llm_operation_model,
        "update_llm_operation_status",
        return_value=True,
    ) as update_status, patch.object(
        workflow_service.llm_service,
        "generate_text_via_llm",
        side_effect=LlmRateLimitError("temporarily unavailable"),
    ):
        with pytest.raises(background_queue.RetryableBackgroundTaskFailure):
            workflow_service.process_workflow_background(
                app, 7, "transcription-1", 42, "Summarize", "Transcript",
                "GEMINI", "test-model", raise_on_failure=True,
                retry_pending=retry_pending,
            )

    assert update_status.call_args.kwargs["status"] == expected_status
