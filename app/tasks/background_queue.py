"""Application-facing helpers for the durable background-job queue.

The web process only serializes work into ``background_jobs``.  The worker
process is the sole caller of :func:`dispatch_job`, which keeps Flask route and
service facades stable while making the work reconstructable after a restart.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from app.database import close_db
from app.models import background_job as background_job_model


LOGGER = logging.getLogger(__name__)
DEFAULT_MAX_ATTEMPTS = 3


class BackgroundTaskFailure(Exception):
    """A task reached a known failure state that the queue must record."""

    retryable = False


class RetryableBackgroundTaskFailure(BackgroundTaskFailure):
    """A task failed before completion and may safely be attempted again."""

    retryable = True


class TerminalBackgroundTaskFailure(BackgroundTaskFailure):
    """A task failure is already represented in its domain record."""

    retryable = False


def _max_attempts(config: Optional[Mapping[str, Any]]) -> int:
    configured = (config or {}).get("BACKGROUND_JOB_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)
    try:
        return max(1, int(configured))
    except (TypeError, ValueError):
        LOGGER.warning("Invalid background-job retry count %r; falling back to %s.", configured, DEFAULT_MAX_ATTEMPTS)
        return DEFAULT_MAX_ATTEMPTS


def _retry_delay_seconds(attempts: int, config: Optional[Mapping[str, Any]] = None) -> int:
    """Return a bounded exponential delay before a failed retry."""
    configured_cap = (config or {}).get("BACKGROUND_JOB_RETRY_DELAY_MAX_SECONDS", 300)
    try:
        cap = max(1, int(configured_cap))
    except (TypeError, ValueError):
        cap = 300
    return min(cap, max(1, 2 ** max(0, attempts - 1)))


def enqueue_transcription_job(
    config: Mapping[str, Any],
    target,
    *args: Any,
    commit: bool = True,
) -> int:
    """Persist a transcription call without serializing the Flask app object."""
    target_name = getattr(target, "__name__", "")
    if target_name != "process_transcription":
        raise ValueError(f"Unsupported transcription target: {target_name or target!r}")
    if len(args) < 2:
        raise ValueError("A transcription queue call must include app and job id arguments.")

    # The first argument is the process-local Flask app. Every other value is
    # a scalar reconstructable by the dedicated worker.
    payload = {"args": list(args[1:])}
    return background_job_model.enqueue_job(
        "transcription",
        payload,
        max_attempts=_max_attempts(config),
        commit=commit,
    )


def enqueue_workflow_job(
    config: Mapping[str, Any],
    user_id: int,
    transcription_id: str,
    operation_id: int,
    prompt: str,
    transcript_text: str,
    llm_provider: str,
    llm_model: Optional[str],
    *,
    commit: bool = True,
) -> int:
    """Persist a workflow call, optionally as part of its creation transaction.

    ``transcript_text`` is accepted for the service facade but deliberately is
    not copied into the queue row. The worker reloads the finished
    transcription by id, avoiding large JSON packets and stale duplicated data.
    """
    return background_job_model.enqueue_job(
        "workflow",
        {
            "user_id": user_id,
            "transcription_id": transcription_id,
            "operation_id": operation_id,
            "prompt": prompt,
            "llm_provider": llm_provider,
            "llm_model": llm_model,
        },
        max_attempts=_max_attempts(config),
        commit=commit,
    )


def enqueue_title_generation(
    config: Mapping[str, Any],
    transcription_id: str,
    user_id: int,
    *,
    commit: bool = True,
) -> int:
    """Persist an automatic-title call for the dedicated worker."""
    return background_job_model.enqueue_job(
        "title_generation",
        {"transcription_id": transcription_id, "user_id": user_id},
        max_attempts=_max_attempts(config),
        commit=commit,
    )


def _require_payload_fields(payload: Mapping[str, Any], fields, task_type: str) -> None:
    missing = [field for field in fields if field not in payload]
    if missing:
        raise TerminalBackgroundTaskFailure(
            f"{task_type} background job payload is missing: {', '.join(missing)}."
        )


def dispatch_job(app, job: Mapping[str, Any]) -> Any:
    """Dispatch one claimed row to its stable service/task facade."""
    task_type = job.get("task_type")
    payload = job.get("payload") or {}
    if task_type == "transcription":
        from app.services.transcription_service import process_transcription
        from app.models import transcription as transcription_model

        args = payload.get("args")
        if not isinstance(args, list):
            raise TerminalBackgroundTaskFailure(
                "Transcription background job payload is missing args."
            )
        if len(args) < 6:
            raise TerminalBackgroundTaskFailure(
                "Transcription background job payload is incomplete."
            )
        # Queue-owned attempts retain the source file when the service reports
        # a retryable failure. Terminal failures still clean it up in the
        # service's normal finally block.
        attempts = int(job.get("attempts") or 0)
        max_attempts = int(job.get("max_attempts") or 1)
        can_retry = attempts < max_attempts
        existing_transcription = transcription_model.get_transcription_by_id(
            args[0],
            args[1],
        )
        if not existing_transcription:
            raise TerminalBackgroundTaskFailure(
                "Transcription background job record is missing."
            )
        if existing_transcription and existing_transcription.get("status") == "finished":
            # The provider result is already durable. A queue retry must not
            # charge the provider a second time just because queue finalization
            # was interrupted.
            return existing_transcription.get("transcription_text")
        # The service uses a nested app context and commits on another
        # connection. Release this read snapshot before checking its result.
        close_db()
        result = process_transcription(
            app,
            *args,
            preserve_input_on_retry=(
                can_retry and bool(payload.get("preserve_input_on_retry", True))
            ),
        )
        transcription = transcription_model.get_transcription_by_id(args[0], args[1])
        status = transcription.get("status") if transcription else None
        if status == "finished":
            return result
        if status in {"error", "cancelled", "interrupted"}:
            raise TerminalBackgroundTaskFailure(
                transcription.get("error_message") if transcription else "Transcription failed."
            )
        raise RetryableBackgroundTaskFailure(
            f"Transcription job {args[0]} did not reach a terminal state."
        )
    if task_type == "workflow":
        from app.services.workflow_service import process_workflow_background
        from app.models import transcription as transcription_model
        from app.models import llm_operation as llm_operation_model

        _require_payload_fields(
            payload,
            ("user_id", "transcription_id", "operation_id", "prompt", "llm_provider"),
            "Workflow",
        )

        operation = llm_operation_model.get_llm_operation_by_id(
            payload["operation_id"],
            payload["user_id"],
        )
        if not operation:
            raise TerminalBackgroundTaskFailure("Workflow operation is missing.")
        if operation and operation.get("status") == "finished":
            # A worker may have crashed after the provider call but before
            # the queue row was finalized. Do not pay for a second call when
            # the domain record already contains the result.
            return operation.get("result")

        transcription = transcription_model.get_transcription_by_id(
            payload["transcription_id"],
            payload["user_id"],
        )
        if not transcription or not transcription.get("transcription_text"):
            raise TerminalBackgroundTaskFailure(
                "Workflow background job transcription is missing or empty."
            )

        close_db()
        result = process_workflow_background(
            app,
            payload["user_id"],
            payload["transcription_id"],
            payload["operation_id"],
            payload["prompt"],
            transcription["transcription_text"],
            payload["llm_provider"],
            payload.get("llm_model"),
            raise_on_failure=True,
            retry_pending=int(job.get("attempts") or 0) < int(job.get("max_attempts") or 1),
        )
        operation = llm_operation_model.get_llm_operation_by_id(
            payload["operation_id"],
            payload["user_id"],
        )
        if operation and operation.get("status") == "finished":
            return result
        if operation.get("status") == "error":
            raise TerminalBackgroundTaskFailure(
                operation.get("error") or "Workflow operation failed."
            )
        raise RetryableBackgroundTaskFailure(
            f"Workflow operation {payload['operation_id']} did not reach a terminal state."
        )
    if task_type == "title_generation":
        from app.tasks.title_generation import generate_title_task
        from app.models import transcription as transcription_model

        _require_payload_fields(
            payload,
            ("transcription_id", "user_id"),
            "Title generation",
        )

        transcription = transcription_model.get_transcription_by_id(
            payload["transcription_id"],
            payload["user_id"],
        )
        if not transcription:
            raise TerminalBackgroundTaskFailure("Title generation transcription is missing.")
        if transcription.get("title_generation_status") == "success":
            # The title is already durable. A retry of the queue row must not
            # issue another paid provider request.
            return transcription.get("generated_title")

        close_db()
        result = generate_title_task(
            app,
            payload["transcription_id"],
            payload["user_id"],
            raise_on_failure=True,
            retry_pending=int(job.get("attempts") or 0) < int(job.get("max_attempts") or 1),
        )
        transcription = transcription_model.get_transcription_by_id(
            payload["transcription_id"],
            payload["user_id"],
        )
        if transcription and transcription.get("title_generation_status") == "success":
            return result
        if transcription and transcription.get("title_generation_status") in {"failed", "disabled"}:
            raise TerminalBackgroundTaskFailure("Title generation failed.")
        raise RetryableBackgroundTaskFailure(
            f"Title generation for {payload['transcription_id']} did not reach a terminal state."
        )
    raise TerminalBackgroundTaskFailure(
        f"Unsupported background task type: {task_type!r}"
    )


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "BackgroundTaskFailure",
    "RetryableBackgroundTaskFailure",
    "TerminalBackgroundTaskFailure",
    "_max_attempts",
    "_retry_delay_seconds",
    "dispatch_job",
    "enqueue_title_generation",
    "enqueue_transcription_job",
    "enqueue_workflow_job",
]
