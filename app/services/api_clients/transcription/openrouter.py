from typing import Any, Dict, Optional, Tuple

from app.services.api_clients.exceptions import TranscriptionProcessingError
from .openai_base import OpenAIBaseTranscriptionClient


class OpenRouterTranscriptionClient(OpenAIBaseTranscriptionClient):
    # The catalog code is set to the selected vendor/model slug. The provider
    # name remains only as the compatibility default for older callers.
    CATALOG_MODEL_CODE = "openrouter"

    def __init__(
        self,
        api_key: str,
        config: Dict[str, Any],
        model_code: str = "openrouter",
    ) -> None:
        self.model_code = str(model_code or "openrouter").strip() or "openrouter"
        self.CATALOG_MODEL_CODE = self.model_code
        # OpenRouter's transcription endpoint has no portable prompt support,
        # so a user context prompt cannot be applied. Report that once per job.
        self._context_prompt_unsupported_reported = False
        super().__init__(api_key, config)
        api_limits = self._resolve_split_limits(self.model_code, "openrouter")
        self.SPLIT_THRESHOLD_SECONDS = api_limits.get("duration_s")
        size_mb = api_limits.get("size_mb")
        if size_mb is not None:
            self.SPLIT_THRESHOLD_BYTES = size_mb * 1024 * 1024

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
        if context_prompt and not self._context_prompt_unsupported_reported:
            self._context_prompt_unsupported_reported = True
            self._report_progress(
                "Warning: OpenRouter transcription models do not support context prompts; the prompt was ignored.",
                False,
            )
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
