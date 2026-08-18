"""Client for OpenAI's GPT Transcribe model."""

from typing import Any, Dict, Optional, Tuple

from app.config import Config
from app.services.api_clients.exceptions import TranscriptionProcessingError

from .openai_base import OpenAIBaseTranscriptionClient


class OpenAIGPTTranscribeClient(OpenAIBaseTranscriptionClient):
    """Transcribe recorded audio with the ``gpt-transcribe`` model."""

    API_MODEL_PARAM = "gpt-transcribe"

    def __init__(self, api_key: str, config: Dict[str, Any]) -> None:
        super().__init__(api_key, config)
        api_limits = self.config.get("API_LIMITS", {}).get("gpt-transcribe", {})
        self.SPLIT_THRESHOLD_SECONDS = api_limits.get("duration_s")
        size_mb = api_limits.get("size_mb")
        if size_mb is not None:
            self.SPLIT_THRESHOLD_BYTES = size_mb * 1024 * 1024

    def _get_api_name(self) -> str:
        return "OpenAI GPT Transcribe"

    def _prepare_api_params(
        self,
        language_code: str,
        context_prompt: str,
        response_format: str,
        is_chunk: bool,
        extra_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        api_params: Dict[str, Any] = {
            "model": (extra_options or {}).get("model") or self.API_MODEL_PARAM,
            "response_format": "json",
        }
        if context_prompt:
            api_params["prompt"] = context_prompt

        ui_lang_msg: str
        if language_code == "auto":
            ui_lang_msg = "Language detection requested."
        elif language_code in Config.SUPPORTED_LANGUAGE_CODES:
            api_params["extra_body"] = {"languages": [language_code]}
            ui_lang_msg = f"Language set to '{language_code}'."
        else:
            self.logger.warning(
                "Invalid language code '%s'. Using auto-detection as fallback.",
                language_code,
            )
            ui_lang_msg = (
                f"Invalid language code '{language_code}'. "
                "Using auto-detection as fallback."
            )
            language_code = "auto"

        if not is_chunk or language_code == "auto":
            self._report_progress(ui_lang_msg, False)

        return api_params

    def _process_response(
        self, response: Any, response_format: str
    ) -> Tuple[str, Optional[str]]:
        try:
            transcription_text = response.text
            languages = getattr(response, "languages", None) or []
            detected_language = None
            if languages:
                first_language = languages[0]
                detected_language = (
                    first_language.get("code")
                    if isinstance(first_language, dict)
                    else getattr(first_language, "code", None)
                )
            return transcription_text, detected_language
        except AttributeError as exc:
            raise TranscriptionProcessingError(
                "Failed to parse OpenAI GPT Transcribe response.",
                provider=self._get_api_name(),
            ) from exc
