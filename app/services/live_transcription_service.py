import hashlib
import hmac
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from flask import current_app
from flask_babel import gettext as _
from itsdangerous import BadSignature, URLSafeSerializer

from app.core.decorators import check_permission
from app.models import role as role_model
from app.models import transcription as transcription_model
from app.models import transcription_catalog as transcription_catalog_model
from app.services import pricing_service, user_service
from app.services.user_service import MissingApiKeyError
from app.tasks.title_generation import generate_title_task


LOGGER = logging.getLogger(__name__)
SESSION_TOKEN_SALT = "live-transcription-session-v1"
MAX_CONTEXT_WORDS = 120
MAX_TRANSCRIPT_CHARS = 10_000_000
MAX_SESSION_DURATION_MINUTES = 120
RETRYABLE_SESSION_STATUS_CODES = frozenset({502, 503, 504})
ENDED_CALL_STATUS_CODES = frozenset({404, 409})


class LiveTranscriptionError(Exception):
    """Base error for live transcription operations."""


class LiveTranscriptionValidationError(LiveTranscriptionError):
    """The user supplied invalid live transcription settings."""


class LiveTranscriptionPermissionError(LiveTranscriptionError):
    """The user cannot use a requested live transcription capability."""


class LiveTranscriptionUpstreamError(LiveTranscriptionError):
    """OpenAI could not create the realtime session."""


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt=SESSION_TOKEN_SALT)


def _resolve_openai_api_key(user) -> str:
    mode = current_app.config["DEPLOYMENT_MODE"]
    if mode == "multi":
        api_key = user_service.get_decrypted_api_key(user.id, "openai")
        if api_key:
            return api_key
        if check_permission(user, "allow_api_key_management"):
            raise MissingApiKeyError(_("OpenAI API key is not configured."))
    api_key = current_app.config.get("OPENAI_API_KEY")
    if not api_key:
        raise MissingApiKeyError(_("OpenAI API key is not configured."))
    return api_key


def _validate_settings(user, language_code: str, context_prompt: str) -> tuple[str, str]:
    language = (language_code or "auto").strip().lower()
    prompt = (context_prompt or "").strip()
    supported_languages = transcription_catalog_model.get_language_map()
    if language != "auto" and language not in supported_languages:
        raise LiveTranscriptionValidationError(_("Select a supported transcription language."))
    if prompt and not check_permission(user, "allow_context_prompt"):
        raise LiveTranscriptionPermissionError(
            _("You do not have permission to use a context prompt.")
        )
    if len(prompt.split()) > MAX_CONTEXT_WORDS:
        raise LiveTranscriptionValidationError(
            _("Context prompt cannot exceed %(count)s words.", count=MAX_CONTEXT_WORDS)
        )
    return language, prompt


def build_session_config(model: str, language: str, prompt: str) -> Dict[str, Any]:
    transcription: Dict[str, Any] = {
        "model": model,
        "delay": "low",
    }
    if prompt:
        transcription["prompt"] = prompt
    if language != "auto":
        transcription["languages"] = [language]
    return {
        "type": "transcription",
        "audio": {
            "input": {
                "transcription": transcription,
                "turn_detection": None,
            }
        },
    }


def _safety_identifier(user_id: int) -> str:
    secret = current_app.config["SECRET_KEY"].encode("utf-8")
    digest = hmac.new(secret, f"live:{user_id}".encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:64]


def _call_id_from_location(location: str) -> Optional[str]:
    match = re.search(r"/realtime/calls/([^/?#]+)", location or "")
    return match.group(1) if match else None


def create_session(
    user, sdp: str, language_code: str, context_prompt: str
) -> Dict[str, str]:
    if not isinstance(sdp, str) or not sdp.strip():
        raise LiveTranscriptionValidationError(_("A WebRTC session offer is required."))
    language, prompt = _validate_settings(user, language_code, context_prompt)
    api_key = _resolve_openai_api_key(user)
    model = current_app.config.get("LIVE_TRANSCRIPTION_MODEL", "gpt-live-transcribe")
    session_config = build_session_config(model, language, prompt)

    max_retries = max(
        0, int(current_app.config.get("LIVE_TRANSCRIPTION_SESSION_MAX_RETRIES", 1))
    )
    retry_delay = max(
        0.0,
        float(current_app.config.get("LIVE_TRANSCRIPTION_SESSION_RETRY_DELAY", 0.25)),
    )
    response = None
    for attempt in range(max_retries + 1):
        try:
            response = httpx.post(
                "https://api.openai.com/v1/realtime/calls",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "OpenAI-Safety-Identifier": _safety_identifier(user.id),
                },
                files={
                    "sdp": (None, sdp, "application/sdp"),
                    "session": (None, json.dumps(session_config), "application/json"),
                },
                timeout=current_app.config.get("OPENAI_HTTP_TIMEOUT", 120),
            )
        except httpx.HTTPError as exc:
            LOGGER.error("OpenAI realtime session request failed: %s", exc)
            raise LiveTranscriptionUpstreamError(
                _("Could not connect to the live transcription service.")
            ) from exc

        if (
            response.status_code not in RETRYABLE_SESSION_STATUS_CODES
            or attempt >= max_retries
        ):
            break
        LOGGER.warning(
            "OpenAI realtime session returned transient HTTP %s; retrying (%s/%s).",
            response.status_code,
            attempt + 1,
            max_retries,
        )
        if retry_delay:
            time.sleep(retry_delay)

    assert response is not None
    if response.status_code >= 400:
        LOGGER.error(
            "OpenAI realtime session returned HTTP %s: %s",
            response.status_code,
            response.text[:1000],
        )
        raise LiveTranscriptionUpstreamError(
            _("The live transcription service rejected the session.")
        )

    call_id = _call_id_from_location(response.headers.get("Location", ""))
    if not call_id:
        LOGGER.error("OpenAI realtime session response did not include a call ID.")
        raise LiveTranscriptionUpstreamError(
            _("The live transcription service returned an incomplete session.")
        )

    transcription_id = str(uuid.uuid4())
    token = _serializer().dumps(
        {
            "user_id": user.id,
            "transcription_id": transcription_id,
            "call_id": call_id,
            "started_at": time.time(),
            "language": language,
            "context_prompt_used": bool(prompt),
        }
    )
    return {"answer_sdp": response.text, "session_token": token}


def _decode_session_token(token: str) -> Dict[str, Any]:
    if not token:
        raise LiveTranscriptionValidationError(_("The live session token is missing."))
    try:
        payload = _serializer().loads(token)
    except BadSignature as exc:
        raise LiveTranscriptionValidationError(_("The live session token is invalid.")) from exc
    required_fields = {
        "user_id",
        "transcription_id",
        "started_at",
        "language",
        "context_prompt_used",
    }
    if not isinstance(payload, dict) or not required_fields.issubset(payload):
        raise LiveTranscriptionValidationError(_("The live session token is invalid."))
    return payload


def hangup_session(user, session_token: str) -> Dict[str, bool]:
    payload = _decode_session_token(session_token)
    if int(payload["user_id"]) != int(user.id):
        raise LiveTranscriptionPermissionError(
            _("This live session belongs to another user.")
        )
    call_id = payload.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise LiveTranscriptionValidationError(_("The live session cannot be stopped."))

    try:
        response = httpx.post(
            f"https://api.openai.com/v1/realtime/calls/{call_id}/hangup",
            headers={"Authorization": f"Bearer {_resolve_openai_api_key(user)}"},
            timeout=current_app.config.get("OPENAI_HTTP_TIMEOUT", 120),
        )
    except httpx.HTTPError as exc:
        LOGGER.warning(
            "Could not explicitly stop OpenAI realtime call %s: %s",
            call_id,
            exc,
        )
        raise LiveTranscriptionUpstreamError(
            _("Could not stop the live transcription service.")
        ) from exc

    if (
        response.status_code >= 400
        and response.status_code not in ENDED_CALL_STATUS_CODES
    ):
        LOGGER.warning(
            "OpenAI realtime hangup returned HTTP %s for call %s.",
            response.status_code,
            call_id,
        )
        raise LiveTranscriptionUpstreamError(
            _("Could not stop the live transcription service.")
        )
    return {"stopped": True}


def finalize_session(user, session_token: str, transcript: str) -> Dict[str, Any]:
    payload = _decode_session_token(session_token)
    if int(payload["user_id"]) != int(user.id):
        raise LiveTranscriptionPermissionError(
            _("This live session belongs to another user.")
        )

    transcription_id = str(payload["transcription_id"])
    existing = transcription_model.get_transcription_by_id(transcription_id, user.id)
    if existing:
        if existing.get("api_used") != current_app.config.get(
            "LIVE_TRANSCRIPTION_MODEL", "gpt-live-transcribe"
        ):
            raise LiveTranscriptionPermissionError(
                _("The live session identifier is already in use.")
            )
        if existing.get("status") == "finished":
            return {"transcription_id": transcription_id, "saved": True}

    text = transcript.strip() if isinstance(transcript, str) else ""
    if not text:
        raise LiveTranscriptionValidationError(_("There is no transcript to save."))
    if len(text) > MAX_TRANSCRIPT_CHARS:
        raise LiveTranscriptionValidationError(
            _("The live transcript is too large to save.")
        )

    started_at = float(payload["started_at"])
    duration_minutes = min(
        MAX_SESSION_DURATION_MINUTES,
        max(0.0, (time.time() - started_at) / 60.0),
    )
    language = payload["language"] if payload["language"] != "auto" else "und"
    model = current_app.config.get("LIVE_TRANSCRIPTION_MODEL", "gpt-live-transcribe")
    filename = datetime.now(timezone.utc).strftime(
        "Live transcription %Y-%m-%d %H-%M UTC"
    )
    price = pricing_service.get_price("transcription", model)
    cost = (price or 0.0) * duration_minutes

    if not existing:
        transcription_model.create_transcription_job(
            transcription_id,
            user.id,
            filename,
            model,
            0.0,
            duration_minutes,
            bool(payload["context_prompt_used"]),
        )
    transcription_model.update_transcription_cost(transcription_id, cost)
    transcription_model.finalize_job_success(transcription_id, text, language)
    role_model.increment_usage(user.id, cost, duration_minutes)

    if user.enable_auto_title_generation and check_permission(
        user, "allow_auto_title_generation"
    ):
        app = current_app._get_current_object()
        threading.Thread(
            target=generate_title_task,
            args=(app, transcription_id, user.id),
            daemon=True,
        ).start()
    else:
        transcription_model.update_title_generation_status(transcription_id, "disabled")

    return {"transcription_id": transcription_id, "saved": True}
