import base64
import binascii
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
from app.services.openrouter import normalize_openrouter_model
from app.services.user_service import MissingApiKeyError
from app.tasks.title_generation import generate_title_task


LOGGER = logging.getLogger(__name__)
SESSION_TOKEN_SALT = "live-transcription-session-v1"
MAX_CONTEXT_WORDS = 120
MAX_TRANSCRIPT_CHARS = 10_000_000
MAX_SESSION_DURATION_MINUTES = 120
RETRYABLE_SESSION_STATUS_CODES = frozenset({502, 503, 504})
ENDED_CALL_STATUS_CODES = frozenset({404, 409})
MAX_OPENROUTER_CHUNK_BYTES = 8 * 1024 * 1024
OPENROUTER_AUDIO_FORMATS = frozenset({"wav", "mp3", "flac", "m4a", "ogg", "webm", "aac"})
OPENROUTER_STT_MODELS = frozenset({
    "nvidia/nemotron-3.5-asr-streaming-multilingual-0.6b",
    "mistralai/voxtral-small-24b-2507-stt",
    "mistralai/voxtral-mini-3b-2507",
    "qwen/qwen3-asr-1.7b",
    "qwen/qwen3-asr-0.6b",
    "openai/gpt-transcribe",
    "openai/whisper-large-v3",
    "openai/whisper-large-v3-turbo",
    "openai/whisper-1",
    "openai/gpt-4o-transcribe",
})


def _configured_openrouter_stt_models() -> frozenset[str]:
    configured = current_app.config.get("OPENROUTER_LIVE_TRANSCRIPTION_MODELS", [])
    if isinstance(configured, str):
        configured = configured.split(",")
    return frozenset(
        str(model).strip()
        for model in configured
        if str(model).strip()
    )


def _resolve_openrouter_stt_models() -> frozenset[str]:
    """Return known and explicitly configured OpenRouter STT models."""
    return OPENROUTER_STT_MODELS | _configured_openrouter_stt_models()


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


def _resolve_live_model(user, requested: Optional[str] = None) -> str:
    configured = requested or getattr(user, "default_live_transcription_model", None)
    if not configured:
        configured = getattr(getattr(user, "role", None), "default_live_transcription_model", None)
    model = str(configured or current_app.config.get("LIVE_TRANSCRIPTION_MODEL", "gpt-live-transcribe")).strip()
    provider_config = current_app.config.get("LIVE_TRANSCRIPTION_PROVIDERS", {}) or {}
    allowed_models = {
        str(value).strip()
        for value in current_app.config.get("LIVE_TRANSCRIPTION_MODELS", [])
        if str(value).strip()
    }
    if user and current_app.config.get("DEPLOYMENT_MODE") == "multi":
        try:
            key_status = user_service.get_user_api_key_status(user.id)
            for entry in (key_status.get("provider_keys", {}).get("openrouter", []) or []):
                purposes = entry.get("model_purposes", []) or []
                model_name = str(entry.get("model_slug") or entry.get("model_name") or "").strip()
                if "live" in purposes and model_name:
                    allowed_models.add(model_name)
        except Exception:
            LOGGER.debug("Could not add user-configured OpenRouter Live models.", exc_info=True)
    configured_provider = str(provider_config.get(model, "")).strip().lower()
    is_openrouter_model = configured_provider == "openrouter" or "/" in model
    if is_openrouter_model:
        allowed_models.add(model)
    if not allowed_models:
        allowed_models.add(model)
    if allowed_models and model not in allowed_models:
        raise LiveTranscriptionValidationError(_("The selected Live transcription model is not available."))
    provider = _resolve_provider(user, model)
    if provider == "openrouter":
        if model not in _resolve_openrouter_stt_models():
            raise LiveTranscriptionValidationError(
                _("The selected OpenRouter model is not listed as a transcription model.")
            )
        try:
            return normalize_openrouter_model(model)
        except ValueError as exc:
            raise LiveTranscriptionValidationError(_(str(exc))) from exc
    if not model or any(char.isspace() for char in model) or '/' in model:
        raise LiveTranscriptionValidationError(_("The selected Live transcription model is invalid."))
    return model


def _resolve_provider(user, model: str) -> str:
    configured = current_app.config.get('LIVE_TRANSCRIPTION_PROVIDERS', {}).get(model)
    if configured:
        return str(configured).strip().lower()
    return "openrouter" if "/" in model else "openai"


def _resolve_provider_api_key(user, provider: str, model: Optional[str] = None) -> str:
    mode = current_app.config["DEPLOYMENT_MODE"]
    if mode == "multi":
        api_key = user_service.get_decrypted_api_key(user.id, provider, model)
        if api_key:
            return api_key
        if check_permission(user, "allow_api_key_management"):
            raise MissingApiKeyError(_("%(provider)s API key is not configured.", provider=provider.title()))
    api_key = current_app.config.get(f"{provider.upper()}_API_KEY")
    if not api_key:
        raise MissingApiKeyError(_("%(provider)s API key is not configured.", provider=provider.title()))
    return api_key


def _resolve_openai_api_key(user, model: Optional[str] = None) -> str:
    return _resolve_provider_api_key(user, "openai", model)


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
    user, sdp: str, language_code: str, context_prompt: str,
    requested_model: Optional[str] = None,
) -> Dict[str, str]:
    language, prompt = _validate_settings(user, language_code, context_prompt)
    model = _resolve_live_model(user, requested_model)
    provider = _resolve_provider(user, model)
    if provider == "openrouter":
        # OpenRouter documents HTTP audio input plus SSE model output, not a
        # WebRTC/WebSocket realtime session. The browser uses this signed
        # token while posting short, valid WAV payloads to the chunk endpoint.
        _resolve_provider_api_key(user, provider, model)
        transcription_id = str(uuid.uuid4())
        token = _serializer().dumps(
            {
                "user_id": user.id,
                "transcription_id": transcription_id,
                "started_at": time.time(),
                "language": language,
                "context_prompt_used": bool(prompt),
                "model": model,
                "provider": provider,
                "transport": "openrouter-sse",
            }
        )
        return {
            "answer_sdp": "",
            "session_token": token,
            "transport": "openrouter-sse",
        }
    if not isinstance(sdp, str) or not sdp.strip():
        raise LiveTranscriptionValidationError(_("A WebRTC session offer is required."))
    if provider != "openai":
        raise LiveTranscriptionValidationError(
            _("The selected Live transcription provider is not supported by this runtime yet.")
        )
    api_key = _resolve_provider_api_key(user, provider, model)
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
            "model": model,
            "provider": provider,
            "transport": "openai-webrtc",
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
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        payload["model"] = current_app.config.get("LIVE_TRANSCRIPTION_MODEL", "gpt-live-transcribe")
    if not payload.get("provider"):
        payload["provider"] = _resolve_provider(None, payload["model"])
    return payload


def _iter_sse_data(lines):
    """Yield complete SSE data fields and ignore OpenRouter keep-alives."""
    data_lines = []
    for raw_line in lines:
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else str(raw_line)
        line = line.rstrip("\r")
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield "\n".join(data_lines)


def transcribe_openrouter_chunk(
    user,
    session_token: str,
    audio_data: str,
    audio_format: str,
    sequence: int,
) -> Dict[str, Any]:
    """Transcribe one browser-produced audio chunk through OpenRouter SSE."""
    payload = _decode_session_token(session_token)
    if int(payload["user_id"]) != int(user.id):
        raise LiveTranscriptionPermissionError(_("This live session belongs to another user."))
    if payload.get("provider") != "openrouter":
        raise LiveTranscriptionValidationError(_("This live session does not use OpenRouter."))
    if not isinstance(sequence, int) or sequence < 0:
        raise LiveTranscriptionValidationError(_("The live audio sequence is invalid."))
    if not isinstance(audio_data, str) or not audio_data:
        raise LiveTranscriptionValidationError(_("The live audio chunk is missing."))
    normalized_format = str(audio_format or "").strip().lower().split(";")[0]
    if normalized_format not in OPENROUTER_AUDIO_FORMATS:
        raise LiveTranscriptionValidationError(_("The live audio format is not supported."))
    try:
        decoded_audio = base64.b64decode(audio_data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise LiveTranscriptionValidationError(_("The live audio chunk is invalid.")) from exc
    if not decoded_audio or len(decoded_audio) > MAX_OPENROUTER_CHUNK_BYTES:
        raise LiveTranscriptionValidationError(_("The live audio chunk is too large."))

    model = str(payload.get("model") or "").strip()
    api_key = _resolve_provider_api_key(user, "openrouter", model)
    request_body: Dict[str, Any] = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Transcribe only the spoken words in this audio. Return only the transcript.",
                },
                {
                    "type": "input_audio",
                    "input_audio": {"data": audio_data, "format": normalized_format},
                },
            ],
        }],
        "stream": True,
    }
    language = payload.get("language")
    if language and language != "auto":
        request_body["messages"][0]["content"][0]["text"] += f" The language is {language}."

    transcript_parts = []
    try:
        base_url = current_app.config.get(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        ).rstrip("/")
        with httpx.stream(
            "POST",
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=request_body,
            timeout=current_app.config.get("OPENAI_HTTP_TIMEOUT", 120),
        ) as response:
            if response.status_code >= 400:
                LOGGER.error("OpenRouter live request returned HTTP %s.", response.status_code)
                raise LiveTranscriptionUpstreamError(
                    _("OpenRouter rejected the live audio chunk.")
                )
            for raw_event in _iter_sse_data(response.iter_lines()):
                if raw_event == "[DONE]":
                    break
                try:
                    event = json.loads(raw_event)
                except json.JSONDecodeError:
                    LOGGER.warning("Ignoring malformed OpenRouter live SSE event.")
                    continue
                if event.get("error"):
                    raise LiveTranscriptionUpstreamError(
                        _("OpenRouter reported an error while transcribing live audio.")
                    )
                choices = event.get("choices") or []
                delta = choices[0].get("delta") if choices else {}
                content = delta.get("content") if isinstance(delta, dict) else None
                if isinstance(content, str) and content:
                    transcript_parts.append(content)
    except LiveTranscriptionUpstreamError:
        raise
    except httpx.HTTPError as exc:
        LOGGER.error("OpenRouter live request failed: %s", exc)
        raise LiveTranscriptionUpstreamError(
            _("Could not connect to the OpenRouter live transcription service.")
        ) from exc

    return {"sequence": sequence, "transcript": "".join(transcript_parts).strip()}


def hangup_session(user, session_token: str) -> Dict[str, bool]:
    payload = _decode_session_token(session_token)
    if int(payload["user_id"]) != int(user.id):
        raise LiveTranscriptionPermissionError(
            _("This live session belongs to another user.")
        )
    if payload.get("provider") == "openrouter":
        return {"stopped": True}
    call_id = payload.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise LiveTranscriptionValidationError(_("The live session cannot be stopped."))

    try:
        response = httpx.post(
            f"https://api.openai.com/v1/realtime/calls/{call_id}/hangup",
            headers={"Authorization": f"Bearer {_resolve_openai_api_key(user, payload.get('model'))}"},
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
    session_model = str(payload.get("model") or _resolve_live_model(user))
    existing = transcription_model.get_transcription_by_id(transcription_id, user.id)
    if existing:
        if existing.get("api_used") != session_model:
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
    model = session_model
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
