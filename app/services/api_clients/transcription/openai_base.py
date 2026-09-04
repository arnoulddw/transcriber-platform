"""
Common functionality for OpenAI-based transcription clients.
Provides shared client initialisation, retryable error mapping,
and the standard `_call_api` implementation used across models.
"""

from typing import Any, Dict, Optional, Tuple, Type
import re

from openai import (
    OpenAI,
    OpenAIError,
    APIError,
    APIConnectionError,
    RateLimitError,
    AuthenticationError,
    BadRequestError,
)

from .base_transcription_client import BaseTranscriptionClient
from app.services.api_clients.exceptions import (
    TranscriptionProcessingError,
    TranscriptionAuthenticationError,
    TranscriptionRateLimitError,
    TranscriptionQuotaExceededError,
)


class OpenAIBaseTranscriptionClient(BaseTranscriptionClient):
    """Shared behaviour for transcription clients that call the OpenAI API."""

    DEFAULT_TIMEOUT_SECONDS = 120.0

    def _get_api_error_label(self) -> str:
        """Return the service name used in errors from this OpenAI-compatible client."""
        return "OpenAI"

    def _get_error_provider(self, api_name: str) -> str:
        """Return the service provider while retaining OpenAI's model label."""
        return api_name

    @staticmethod
    def _get_upstream_status_code(exc: Exception, default: int) -> int:
        """Read an HTTP status from an SDK error, falling back when absent."""
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
        try:
            return int(status_code)
        except (TypeError, ValueError):
            return default

    def _initialize_client(self, api_key: str) -> None:
        """
        Build the OpenAI client using shared configuration.
        Subclasses can override `_get_additional_openai_client_kwargs` or
        `_get_timeout_config_override_key` to tweak behaviour.
        """
        client_kwargs = self._get_openai_client_kwargs(api_key)
        try:
            self.client = OpenAI(**client_kwargs)
            self.logger.debug(
                "OpenAI client initialised (timeout=%ss, max_retries=%s).",
                client_kwargs.get("timeout"),
                client_kwargs.get("max_retries", "default"),
            )
        except OpenAIError as exc:
            raise ValueError(f"OpenAI client initialization failed: {exc}") from exc

    def _get_openai_client_kwargs(self, api_key: str) -> Dict[str, Any]:
        timeout_seconds = self._get_timeout_seconds()
        kwargs: Dict[str, Any] = {"api_key": api_key, "timeout": timeout_seconds}
        extra_kwargs = self._get_additional_openai_client_kwargs()
        if extra_kwargs:
            kwargs.update(extra_kwargs)
        return kwargs

    def _get_timeout_seconds(self) -> float:
        override_key = self._get_timeout_config_override_key()
        if override_key:
            override_value = self.config.get(override_key)
            if override_value is not None:
                return float(override_value)
        return float(self.config.get("OPENAI_HTTP_TIMEOUT", self.DEFAULT_TIMEOUT_SECONDS))

    def _get_timeout_config_override_key(self) -> Optional[str]:
        """Allow subclasses to provide an alternate timeout config key."""
        return None

    def _get_additional_openai_client_kwargs(self) -> Dict[str, Any]:
        """Hook for subclasses to add kwargs when creating the OpenAI client."""
        return {}

    def _call_api(self, file_handle: Any, api_params: Dict[str, Any]) -> Any:
        """
        Standardised OpenAI transcription call with consistent error mapping.
        """
        api_params_with_file = dict(api_params)
        api_params_with_file["file"] = file_handle

        api_name = self._get_api_name()
        error_label = self._get_api_error_label()
        error_provider = self._get_error_provider(api_name)
        try:
            return self.client.audio.transcriptions.create(**api_params_with_file)
        except AuthenticationError as exc:
            self.logger.error("Authentication error: %s", exc)
            raise TranscriptionAuthenticationError(
                f"{error_label}: {exc}", provider=error_provider
            ) from exc
        except RateLimitError as exc:
            error_body = getattr(exc, "body", {})
            error_type = error_body.get("type") if isinstance(error_body, dict) else None
            if error_type == "insufficient_quota":
                self.logger.error("Insufficient quota error: %s", exc)
                raise TranscriptionQuotaExceededError(
                    f"{error_label}: {exc}", provider=error_provider
                ) from exc
            self.logger.warning("Rate limit error: %s", exc)
            raise TranscriptionRateLimitError(
                f"{error_label}: {exc}", provider=error_provider
            ) from exc
        except BadRequestError as exc:
            error_body_str = str(exc)
            status_code = self._get_upstream_status_code(exc, default=400)
            self.logger.error("API call failed with Bad Request: %s", error_body_str)
            if error_body_str.strip().startswith("<html>"):
                message = (
                    "The API rejected an audio chunk as invalid "
                    "(it may be silent or corrupted)."
                )
                raise TranscriptionProcessingError(
                    message, status_code=status_code, provider=error_provider
                ) from exc
            user_friendly_error = self._map_bad_request_to_user_message(error_body_str, api_name)
            if user_friendly_error:
                raise TranscriptionProcessingError(
                    user_friendly_error, status_code=status_code, provider=error_provider
                ) from exc
            raise TranscriptionProcessingError(
                f"{error_label} API Error: {error_body_str}",
                status_code=status_code,
                provider=error_provider,
            ) from exc
        except APIConnectionError as exc:
            self.logger.warning("API connection error: %s", exc)
            raise
        except (APIError, OpenAIError) as exc:
            self.logger.error("API call failed: %s", exc)
            raise TranscriptionProcessingError(
                f"{error_label} API Error: {exc}",
                status_code=self._get_upstream_status_code(exc, default=500),
                provider=error_provider,
            ) from exc
        except Exception as exc:
            self.logger.error("Unexpected error during OpenAI API call: %s", exc, exc_info=True)
            raise TranscriptionProcessingError(
                f"Unexpected error during {error_label} API call: {exc}",
                provider=error_provider,
            ) from exc

    def _get_retryable_errors(self) -> Tuple[Type[Exception], ...]:
        return (TranscriptionRateLimitError, APIConnectionError)

    def _map_bad_request_to_user_message(self, error_body: str, api_name: str) -> Optional[str]:
        """
        Provides tailored, user-facing explanations for known OpenAI BadRequest errors.
        Returns None when no specialized mapping applies so the caller can fall back
        to the default error text.
        """
        if not error_body:
            return None

        duration_match = re.search(
            r"audio duration\s+(\d+(?:\.\d+)?)\s+seconds\s+is\s+longer\s+than\s+(\d+(?:\.\d+)?)\s+seconds",
            error_body,
            flags=re.IGNORECASE,
        )
        if duration_match:
            actual_seconds = float(duration_match.group(1))
            limit_seconds = float(duration_match.group(2))
            return (
                f"{api_name} can only process up to {self._format_seconds(limit_seconds)} "
                f"per file ({limit_seconds:.0f} seconds). This upload is "
                f"{self._format_seconds(actual_seconds)}. Trim the audio or switch to "
                "a provider that supports longer recordings (e.g., Whisper)."
            )

        if re.search(r"audio file might be corrupted or unsupported", error_body, flags=re.IGNORECASE):
            return (
                f"{api_name} could not read this audio file. It may be corrupted or use "
                "an unsupported audio codec. Export it as MP3 or WAV and try again."
            )

        return None

    @staticmethod
    def _format_seconds(total_seconds: float) -> str:
        """Formats seconds as a compact human-friendly string (e.g., '23m 20s')."""
        seconds_int = max(0, int(round(total_seconds)))
        minutes, secs = divmod(seconds_int, 60)
        hours, minutes = divmod(minutes, 60)
        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if secs or not parts:
            parts.append(f"{secs}s")
        return " ".join(parts)
