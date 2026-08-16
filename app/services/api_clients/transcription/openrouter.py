from typing import Any, Dict, Optional, Tuple

from app.services.api_clients.exceptions import TranscriptionProcessingError
from .openai_base import OpenAIBaseTranscriptionClient


class OpenRouterTranscriptionClient(OpenAIBaseTranscriptionClient):
    def __init__(self, api_key: str, config: Dict[str, Any]) -> None:
        super().__init__(api_key, config)
        api_limits = self.config.get("API_LIMITS", {}).get("openrouter", {})
        self.SPLIT_THRESHOLD_SECONDS = api_limits.get("duration_s")
        size_mb = api_limits.get("size_mb")
        if size_mb is not None:
            self.SPLIT_THRESHOLD_BYTES = size_mb * 1024 * 1024

    def _get_api_name(self) -> str:
        return "OpenRouter"

    def _get_additional_openai_client_kwargs(self) -> Dict[str, Any]:
        return {
            "base_url": self.config.get(
                "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            )
        }

    def _prepare_api_params(
        self,
        language_code: str,
        context_prompt: str,
        response_format: str,
        is_chunk: bool,
        extra_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        extra_options = extra_options or {}
        model = extra_options.get("model")
        if not model:
            raise TranscriptionProcessingError(
                "OpenRouter model is required.", provider=self._get_api_name()
            )
        api_params: Dict[str, Any] = {
            "model": model,
            "response_format": "json",
        }
        if language_code and language_code != "auto":
            api_params["language"] = language_code
        return api_params

    def _process_response(
        self, response: Any, response_format: str
    ) -> Tuple[str, Optional[str]]:
        try:
            text = response.text
            language = getattr(response, "language", None)
            return text, language
        except AttributeError as exc:
            raise TranscriptionProcessingError(
                "Failed to parse OpenRouter transcription response.",
                provider=self._get_api_name(),
            ) from exc
