"""Compatibility facade for durable transcription queue submission.

The web process only serializes work into ``background_jobs``. The dedicated
worker in :mod:`app.tasks.worker` owns execution and concurrency.
"""

from typing import Any, Callable, Mapping
import logging
import threading
import time

from app.models import background_job as background_job_model
from app.tasks.background_queue import enqueue_transcription_job
_recovery_check_lock = threading.Lock()
_last_recovery_check = 0.0


def recover_abandoned_jobs(app) -> int:
    """Recover stale durable claims and reconcile terminal domain records."""
    stale_seconds = app.config.get(
        "BACKGROUND_JOB_STALE_SECONDS",
        app.config.get("TRANSCRIPTION_ABANDONED_JOB_SECONDS", 300),
    )
    try:
        stale_seconds = int(stale_seconds)
    except (TypeError, ValueError):
        stale_seconds = 300
    with app.app_context():
        return background_job_model.recover_stale_jobs(stale_seconds)


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


def submit_transcription_job(
    app_config: Mapping[str, Any],
    target: Callable[..., Any],
    *args: Any,
    commit: bool = True,
) -> int:
    """Persist a transcription payload for the dedicated worker process."""
    return enqueue_transcription_job(app_config, target, *args, commit=commit)


def shutdown_executor() -> None:
    """Compatibility no-op for callers that used the old local executor."""
    return None
