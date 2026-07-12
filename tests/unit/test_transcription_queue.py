from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.tasks import transcription_queue


def test_resolve_max_workers_uses_global_capacity():
    assert transcription_queue._resolve_max_workers({"TRANSCRIPTION_MAX_CONCURRENT_JOBS": 3}) == 3
    assert transcription_queue._resolve_max_workers({"TRANSCRIPTION_MAX_CONCURRENT_JOBS": 0}) == 1


def test_waits_then_runs_with_acquired_global_slot():
    connection = Mock()
    target = Mock(return_value="done")
    app = Mock()
    args = (app, "job-id")
    config = {
        "TRANSCRIPTION_MAX_CONCURRENT_JOBS": 2,
        "TRANSCRIPTION_SLOT_POLL_SECONDS": 0.01,
    }

    with patch.object(
        transcription_queue,
        "_try_acquire_slot",
        side_effect=[(None, None), (connection, 2)],
    ), patch.object(transcription_queue, "_append_waiting_message") as waiting, patch.object(
        transcription_queue, "_release_slot"
    ) as release, patch.object(transcription_queue, "_cancel_waiting_job", return_value=False), patch.object(
        transcription_queue, "_heartbeat_lease"
    ), patch.object(transcription_queue, "_mark_lease_running"), patch.object(
        transcription_queue, "_release_lease"
    ), patch.object(transcription_queue, "_heartbeat_loop"), patch.object(transcription_queue.time, "sleep"):
        result = transcription_queue._run_with_global_capacity(config, target, args)

    assert result == "done"
    waiting.assert_called_once_with(args, 2)
    target.assert_called_once_with(*args)
    release.assert_called_once_with(config, connection, 2)


def test_advisory_lock_namespace_is_database_scoped():
    base = {
        "MYSQL_CONFIG": {"host": "db", "port": 3306, "database": "production"},
    }
    other = {
        "MYSQL_CONFIG": {"host": "db", "port": 3306, "database": "test"},
    }

    assert transcription_queue._slot_lock_name(base, 1) != transcription_queue._slot_lock_name(other, 1)


def test_slot_scan_uses_one_connection_for_all_slots():
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.fetchone.side_effect = [(0,), (1,)]
    config = {
        "TRANSCRIPTION_MAX_CONCURRENT_JOBS": 2,
        "MYSQL_CONFIG": {"host": "db", "port": 3306, "database": "app"},
    }

    with patch.object(transcription_queue, "create_standalone_connection", return_value=connection) as connect:
        acquired_connection, slot = transcription_queue._try_acquire_slot(config)

    assert acquired_connection is connection
    assert slot == 2
    connect.assert_called_once()
    assert cursor.execute.call_count == 2
    connection.close.assert_not_called()


def test_cancelled_waiter_exits_without_acquiring_capacity():
    target = Mock()
    args = (Mock(), "job-id")
    config = {"TRANSCRIPTION_MAX_CONCURRENT_JOBS": 2}

    with patch.object(transcription_queue, "_cancel_waiting_job", return_value=True), patch.object(
        transcription_queue, "_try_acquire_slot"
    ) as acquire, patch.object(transcription_queue, "_release_lease") as release:
        result = transcription_queue._run_with_global_capacity(config, target, args)

    assert result is None
    acquire.assert_not_called()
    target.assert_not_called()
    release.assert_called_once_with(args)


def test_recovery_interrupts_only_unowned_or_stale_jobs():
    config = {
        "TRANSCRIPTION_ABANDONED_JOB_SECONDS": 300,
        "MYSQL_CONFIG": {"host": "db", "port": 3306, "database": "app"},
    }
    app = SimpleNamespace(config=config, app_context=lambda: nullcontext())
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.fetchone.side_effect = [(1,), (None,), (1,), (1,), (1,)]
    ownership = [
        {"job_id": "lost-running", "slot_number": 1, "lease_age_seconds": 1},
        {"job_id": "live-running", "slot_number": 2, "lease_age_seconds": 1},
        {"job_id": "stale-reused-slot", "slot_number": 2, "lease_age_seconds": 301},
        {"job_id": "stale-waiting", "slot_number": None, "lease_age_seconds": 301},
        {"job_id": "live-waiting", "slot_number": None, "lease_age_seconds": 10},
    ]

    with patch.object(transcription_queue, "create_standalone_connection", return_value=connection), patch(
        "app.models.transcription_job_lease.get_active_job_ownership", return_value=ownership
    ), patch("app.models.transcription.mark_active_jobs_interrupted", return_value=3) as interrupt, patch(
        "app.models.transcription_job_lease.release"
    ) as release:
        count = transcription_queue.recover_abandoned_jobs(app)

    assert count == 3
    interrupt.assert_called_once_with(["lost-running", "stale-reused-slot", "stale-waiting"])
    assert {call.args[0] for call in release.call_args_list} == {
        "lost-running", "stale-reused-slot", "stale-waiting"
    }


def test_worker_identity_regenerates_after_fork_pid_change():
    transcription_queue._worker_identity = None
    transcription_queue._worker_identity_pid = None
    try:
        with patch.object(transcription_queue.os, "getpid", return_value=100):
            first = transcription_queue._get_worker_id()
            assert transcription_queue._get_worker_id() == first
        with patch.object(transcription_queue.os, "getpid", return_value=101):
            second = transcription_queue._get_worker_id()
        assert second != first
        assert ":100:" in first
        assert ":101:" in second
    finally:
        transcription_queue._worker_identity = None
        transcription_queue._worker_identity_pid = None
