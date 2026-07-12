"""Bounded transcription executor with MySQL-coordinated global capacity."""

from concurrent.futures import Future, ThreadPoolExecutor
import atexit
import hashlib
import logging
import os
import socket
import threading
import time
import uuid
from typing import Any, Callable, Optional

from app.database import create_standalone_connection

_executor: Optional[ThreadPoolExecutor] = None
_max_workers: Optional[int] = None
_worker_identity: Optional[str] = None
_worker_identity_pid: Optional[int] = None
_worker_identity_lock = threading.Lock()
_worker_heartbeat_thread: Optional[threading.Thread] = None
_worker_heartbeat_stop = threading.Event()
_worker_heartbeat_lock = threading.Lock()
_recovery_check_lock = threading.Lock()
_last_recovery_check = 0.0


def _get_worker_id() -> str:
    """Return a process-unique identity, regenerating automatically after fork."""
    global _worker_identity, _worker_identity_pid
    pid = os.getpid()
    with _worker_identity_lock:
        if _worker_identity is None or _worker_identity_pid != pid:
            _worker_identity_pid = pid
            _worker_identity = f"{socket.gethostname()}:{pid}:{uuid.uuid4().hex[:12]}"
        return _worker_identity


def _resolve_max_workers(app_config: dict) -> int:
    configured = app_config.get("TRANSCRIPTION_MAX_CONCURRENT_JOBS", 2)
    try:
        return max(1, int(configured))
    except (TypeError, ValueError):
        logging.warning("Invalid transcription capacity %r; falling back to 1.", configured)
        return 1


def _lock_namespace(app_config: dict) -> str:
    mysql_config = app_config["MYSQL_CONFIG"]
    identity = f"{mysql_config['host']}:{mysql_config['port']}:{mysql_config['database']}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


def _slot_lock_name(app_config: dict, slot_number: int) -> str:
    return f"transcriber:{_lock_namespace(app_config)}:slot:{slot_number}"


def _recovery_lock_name(app_config: dict) -> str:
    return f"transcriber:{_lock_namespace(app_config)}:recovery"


def get_executor(app_config: dict) -> ThreadPoolExecutor:
    """Return a lazily-created process-local executor bounded by global capacity."""
    global _executor, _max_workers
    max_workers = _resolve_max_workers(app_config)
    if _executor is None or _max_workers != max_workers:
        if _executor is not None:
            _executor.shutdown(wait=True, cancel_futures=False)
        _executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="transcription")
        _max_workers = max_workers
        logging.info("Transcription executor initialized with %s local worker(s).", max_workers)
    return _executor


def _try_acquire_slot(app_config: dict):
    """Return a dedicated connection and slot number when capacity is available."""
    connection = create_standalone_connection(app_config["MYSQL_CONFIG"])
    cursor = connection.cursor()
    try:
        for slot_number in range(1, _resolve_max_workers(app_config) + 1):
            cursor.execute("SELECT GET_LOCK(%s, 0)", (_slot_lock_name(app_config, slot_number),))
            row = cursor.fetchone()
            if row and row[0] == 1:
                return connection, slot_number
    except Exception:
        connection.close()
        raise
    finally:
        cursor.close()
    connection.close()
    return None, None


def _release_slot(app_config: dict, connection, slot_number: int) -> None:
    if connection is None:
        return
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT RELEASE_LOCK(%s)", (_slot_lock_name(app_config, slot_number),))
        cursor.fetchone()
        cursor.close()
    except Exception:
        logging.exception("Failed to release transcription slot %s.", slot_number)
    finally:
        connection.close()


def _with_job_context(args: tuple[Any, ...], callback: Callable[..., Any], *callback_args: Any):
    if len(args) < 2:
        return None
    app, job_id = args[0], args[1]
    with app.app_context():
        return callback(job_id, *callback_args)


def _register_waiting_lease(args: tuple[Any, ...]) -> None:
    from app.models import transcription_job_lease
    _with_job_context(args, transcription_job_lease.register_waiting, _get_worker_id())


def _worker_heartbeat_loop(app, interval: float = 15) -> None:
    from app.models import transcription_job_lease
    while not _worker_heartbeat_stop.wait(interval):
        try:
            with app.app_context():
                transcription_job_lease.heartbeat_worker(_get_worker_id())
        except Exception:
            logging.exception("Failed to heartbeat queued transcription leases for worker %s.", _get_worker_id())


def _ensure_worker_heartbeat(app) -> None:
    global _worker_heartbeat_thread
    with _worker_heartbeat_lock:
        if _worker_heartbeat_thread is not None and _worker_heartbeat_thread.is_alive():
            return
        _worker_heartbeat_stop.clear()
        _worker_heartbeat_thread = threading.Thread(
            target=_worker_heartbeat_loop,
            args=(app,),
            name="transcription-worker-heartbeat",
            daemon=True,
        )
        _worker_heartbeat_thread.start()


def _heartbeat_lease(args: tuple[Any, ...]) -> None:
    from app.models import transcription_job_lease
    _with_job_context(args, transcription_job_lease.heartbeat, _get_worker_id())


def _release_lease(args: tuple[Any, ...]) -> None:
    from app.models import transcription_job_lease
    _with_job_context(args, transcription_job_lease.release, _get_worker_id())


def _mark_lease_running(args: tuple[Any, ...], slot_number: int) -> None:
    from app.models import transcription_job_lease
    _with_job_context(args, transcription_job_lease.mark_running, _get_worker_id(), slot_number)


def _append_waiting_message(args: tuple[Any, ...], max_jobs: int) -> None:
    from app.models import transcription as transcription_model
    _with_job_context(
        args,
        transcription_model.update_job_progress,
        f"Waiting for an available transcription slot (system capacity: {max_jobs}).",
    )


def _cancel_waiting_job(args: tuple[Any, ...]) -> bool:
    """Finalize a cancelled waiter without consuming a global processing slot."""
    if len(args) < 2:
        return False
    app, job_id = args[0], args[1]
    with app.app_context():
        from app.models import transcription as transcription_model
        from app.services import file_service

        job = transcription_model.get_transcription_by_id(job_id)
        if not job or job.get("status") not in ("cancelling", "cancelled"):
            return False
        if job.get("status") != "cancelled":
            transcription_model.update_job_status(job_id, "cancelled")
            transcription_model.update_job_progress(job_id, "Transcription cancelled while waiting for capacity.")
        if len(args) > 3 and args[3]:
            file_service.remove_files([args[3]])
        return True


def _heartbeat_loop(args: tuple[Any, ...], stop_event: threading.Event, lock_connection, interval: float = 15) -> None:
    while not stop_event.wait(interval):
        try:
            lock_connection.ping(reconnect=False, attempts=1, delay=0)
            _heartbeat_lease(args)
        except Exception:
            logging.exception("Failed to heartbeat transcription lease for job %s.", args[1])


def _run_with_global_capacity(app_config: dict, target: Callable[..., Any], args: tuple[Any, ...]) -> Any:
    max_jobs = _resolve_max_workers(app_config)
    poll_seconds = float(app_config.get("TRANSCRIPTION_SLOT_POLL_SECONDS", 2))
    connection = None
    slot_number = None
    waiting_logged = False
    heartbeat_stop = threading.Event()
    heartbeat_thread = None
    try:
        while connection is None:
            if _cancel_waiting_job(args):
                return None
            connection, slot_number = _try_acquire_slot(app_config)
            if connection is None:
                if not waiting_logged:
                    _append_waiting_message(args, max_jobs)
                    waiting_logged = True
                _heartbeat_lease(args)
                time.sleep(poll_seconds)

        assert slot_number is not None
        _mark_lease_running(args, slot_number)
        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(args, heartbeat_stop, connection),
            name=f"transcription-heartbeat-{str(args[1])[:8]}",
            daemon=True,
        )
        heartbeat_thread.start()
        logging.info("Acquired global transcription slot %s/%s.", slot_number, max_jobs)
        return target(*args)
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2)
        if slot_number is not None:
            _release_slot(app_config, connection, slot_number)
            logging.info("Released global transcription slot %s/%s.", slot_number, max_jobs)
        try:
            _release_lease(args)
        except Exception:
            logging.exception("Failed to release transcription lease for job %s.", args[1])


def recover_abandoned_jobs(app) -> int:
    """Interrupt only jobs whose worker lease or advisory lock is no longer live."""
    app_config = app.config
    connection = create_standalone_connection(app_config["MYSQL_CONFIG"])
    cursor = connection.cursor()
    acquired = False
    try:
        cursor.execute("SELECT GET_LOCK(%s, 0)", (_recovery_lock_name(app_config),))
        acquired = bool(cursor.fetchone()[0])
        if not acquired:
            return 0

        with app.app_context():
            from app.models import transcription as transcription_model
            from app.models import transcription_job_lease

            ownership = transcription_job_lease.get_active_job_ownership()
            stale_seconds = int(app_config.get("TRANSCRIPTION_ABANDONED_JOB_SECONDS", 300))
            abandoned = []
            for record in ownership:
                slot_number = record.get("slot_number")
                lease_is_stale = int(record.get("lease_age_seconds") or 0) >= stale_seconds
                if slot_number is not None:
                    cursor.execute("SELECT IS_USED_LOCK(%s)", (_slot_lock_name(app_config, slot_number),))
                    if cursor.fetchone()[0] is None or lease_is_stale:
                        abandoned.append(record["job_id"])
                    continue

                if lease_is_stale:
                    abandoned.append(record["job_id"])

            count = transcription_model.mark_active_jobs_interrupted(abandoned)
            for job_id in abandoned:
                transcription_job_lease.release(job_id)
            return count
    finally:
        if acquired:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (_recovery_lock_name(app_config),))
            cursor.fetchone()
        cursor.close()
        connection.close()


def maybe_recover_abandoned_jobs(app, interval_seconds: float = 60) -> int:
    """Run stale-job recovery at most once per interval in this web worker."""
    global _last_recovery_check
    now = time.monotonic()
    if now - _last_recovery_check < interval_seconds:
        return 0
    if not _recovery_check_lock.acquire(blocking=False):
        return 0
    try:
        now = time.monotonic()
        if now - _last_recovery_check < interval_seconds:
            return 0
        _last_recovery_check = now
        try:
            return recover_abandoned_jobs(app)
        except Exception:
            logging.exception("Periodic abandoned-job recovery failed; continuing normal request handling.")
            return 0
    finally:
        _recovery_check_lock.release()


def submit_transcription_job(app_config: dict, target: Callable[..., Any], *args: Any) -> Future:
    """Submit a job under the system-wide MySQL advisory-lock capacity."""
    _register_waiting_lease(args)
    if args:
        _ensure_worker_heartbeat(args[0])
    try:
        return get_executor(app_config).submit(_run_with_global_capacity, app_config, target, args)
    except Exception:
        _release_lease(args)
        raise


def shutdown_executor() -> None:
    global _executor, _max_workers, _worker_heartbeat_thread
    if _executor is not None:
        _executor.shutdown(wait=True, cancel_futures=False)
        _executor = None
        _max_workers = None
    _worker_heartbeat_stop.set()
    if _worker_heartbeat_thread is not None:
        _worker_heartbeat_thread.join(timeout=2)
        _worker_heartbeat_thread = None


atexit.register(shutdown_executor)
