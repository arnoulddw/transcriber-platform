"""Dedicated durable background-job worker process."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import logging
import os
import socket
import threading
import time
import uuid
from typing import Any, Dict, Optional

from app.models import background_job as background_job_model
from app.tasks.background_queue import (
    _retry_delay_seconds,
    dispatch_job,
)


LOGGER = logging.getLogger(__name__)


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _setting(config, key: str, fallback: Any, converter):
    value = config.get(key, fallback)
    try:
        return converter(value)
    except (TypeError, ValueError):
        LOGGER.warning("Invalid %s=%r; falling back to %r.", key, value, fallback)
        return fallback


def _worker_concurrency(config) -> int:
    return max(
        1,
        _setting(
            config,
            "BACKGROUND_WORKER_CONCURRENCY",
            config.get("TRANSCRIPTION_MAX_CONCURRENT_JOBS", 2),
            int,
        ),
    )


def _poll_seconds(config) -> float:
    return max(0.1, _setting(config, "BACKGROUND_JOB_POLL_SECONDS", 1.0, float))


def _stale_seconds(config) -> int:
    return max(
        1,
        _setting(
            config,
            "BACKGROUND_JOB_STALE_SECONDS",
            config.get("TRANSCRIPTION_ABANDONED_JOB_SECONDS", 300),
            int,
        ),
    )


def _execute_claimed_job(app, job: Dict[str, Any]) -> Any:
    """Run one payload with an independent Flask context in its worker thread."""
    with app.app_context():
        return dispatch_job(app, job)


def _finish_job(app, worker_id: str, future: Future, job: Dict[str, Any], config) -> bool:
    job_id = int(job["id"])
    try:
        future.result()
    except Exception as exc:
        retryable = bool(getattr(exc, "retryable", True))
        attempts = int(job.get("attempts") or 0)
        max_attempts = int(job.get("max_attempts") or 1)
        # Domain handlers have already persisted terminal errors. Do not
        # repeat a paid provider call just because the queue is finalizing it.
        effective_max_attempts = max_attempts if retryable else attempts
        with app.app_context():
            status = background_job_model.fail_job(
                job_id,
                worker_id,
                attempts,
                effective_max_attempts,
                f"{type(exc).__name__}: {exc}",
                _retry_delay_seconds(max(1, attempts), config),
            )
        LOGGER.error(
            "Background job %s (%s) failed with status %s: %s",
            job_id,
            job.get("task_type"),
            status,
            exc,
            exc_info=True,
        )
        return False

    with app.app_context():
        finalized = background_job_model.complete_job(job_id, worker_id)
    if not finalized:
        LOGGER.error("Background job %s completed but its claim could not be finalized.", job_id)
        return False
    LOGGER.info("Background job %s (%s) completed.", job_id, job.get("task_type"))
    return True


def run_worker(
    app,
    *,
    stop_event: Optional[threading.Event] = None,
    run_once: bool = False,
) -> int:
    """Run the durable queue until stopped, or one bounded batch in tests.

    Claims are made and finalized on the worker's coordinating thread. Task
    calls run in a bounded executor, while ``FOR UPDATE SKIP LOCKED`` in the
    model makes claims safe when multiple worker processes share MySQL.
    """
    stop_event = stop_event or threading.Event()
    config = app.config
    concurrency = _worker_concurrency(config)
    poll_seconds = _poll_seconds(config)
    stale_seconds = _stale_seconds(config)
    worker_id = _worker_id()
    recovery_interval = max(1.0, min(float(stale_seconds) / 2.0, 60.0))
    heartbeat_interval = max(1.0, min(float(stale_seconds) / 3.0, 15.0))
    last_recovery = 0.0
    last_heartbeat = 0.0
    completed = 0
    in_flight: Dict[Future, Dict[str, Any]] = {}

    LOGGER.info(
        "Starting background worker %s with concurrency=%s, poll=%ss, stale=%ss.",
        worker_id,
        concurrency,
        poll_seconds,
        stale_seconds,
    )
    executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="background-job")
    try:
        while not stop_event.is_set():
            now = time.monotonic()
            if now - last_recovery >= recovery_interval:
                with app.app_context():
                    recovered = background_job_model.recover_stale_jobs(stale_seconds)
                if recovered:
                    LOGGER.warning("Recovered %s stale background job claim(s).", recovered)
                last_recovery = now

            if in_flight and now - last_heartbeat >= heartbeat_interval:
                with app.app_context():
                    background_job_model.heartbeat_jobs(
                        [job["id"] for job in in_flight.values()],
                        worker_id,
                    )
                last_heartbeat = now

            while len(in_flight) < concurrency and not stop_event.is_set():
                with app.app_context():
                    job = background_job_model.claim_next_job(worker_id)
                if not job:
                    break
                in_flight[executor.submit(_execute_claimed_job, app, job)] = job

            if run_once and not in_flight:
                break
            if not in_flight:
                stop_event.wait(poll_seconds)
                continue

            done, _ = wait(
                in_flight,
                timeout=poll_seconds,
                return_when=FIRST_COMPLETED,
            )
            if run_once and not done:
                # A one-shot invocation still waits for its claimed batch;
                # this branch only keeps heartbeat/recovery work available for
                # long-running tasks while the batch is in flight.
                continue
            for future in done:
                job = in_flight.pop(future)
                if _finish_job(app, worker_id, future, job, config):
                    completed += 1
            if run_once and not in_flight:
                break
    finally:
        executor.shutdown(wait=True, cancel_futures=False)
        LOGGER.info("Background worker %s stopped.", worker_id)
    return completed


__all__ = ["run_worker"]
