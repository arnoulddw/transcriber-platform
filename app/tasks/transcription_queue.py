"""Bounded in-process executor for transcription jobs."""

from concurrent.futures import ThreadPoolExecutor, Future
import atexit
import fcntl
import logging
import os
from typing import Any, Callable, Optional

_executor: Optional[ThreadPoolExecutor] = None
_max_workers: Optional[int] = None


def _resolve_max_workers(app_config: dict) -> int:
    configured = app_config.get("TRANSCRIPTION_QUEUE_WORKERS", 1)
    try:
        return max(1, int(configured))
    except (TypeError, ValueError):
        logging.warning("Invalid transcription queue worker count %r; falling back to 1.", configured)
        return 1


def get_executor(app_config: dict) -> ThreadPoolExecutor:
    """Return a lazily-created process-local transcription executor."""
    global _executor, _max_workers
    max_workers = _resolve_max_workers(app_config)
    if _executor is None or _max_workers != max_workers:
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=False)
        _executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="transcription",
        )
        _max_workers = max_workers
        logging.info("Transcription queue initialized with %s worker(s).", max_workers)
    return _executor


def _run_with_optional_global_lock(app_config: dict, target: Callable[..., Any], args: tuple[Any, ...]) -> Any:
    lock_file = app_config.get("TRANSCRIPTION_GLOBAL_LOCK_FILE")
    if not lock_file:
        return target(*args)

    lock_dir = os.path.dirname(lock_file)
    if lock_dir:
        os.makedirs(lock_dir, exist_ok=True)
    with open(lock_file, "a", encoding="utf-8") as lock_handle:
        logging.info("Waiting for transcription global lock: %s", lock_file)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            logging.info("Acquired transcription global lock.")
            return target(*args)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            logging.info("Released transcription global lock.")


def submit_transcription_job(app_config: dict, target: Callable[..., Any], *args: Any) -> Future:
    """Submit a transcription job to the bounded in-process queue."""
    return get_executor(app_config).submit(_run_with_optional_global_lock, app_config, target, args)


def shutdown_executor() -> None:
    global _executor, _max_workers
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=False)
        _executor = None
        _max_workers = None


atexit.register(shutdown_executor)
