# app/services/api_clients/transcription/openai_model_client.py
"""Single parameterized client for every OpenAI transcription model.

Per-model behaviour (API model name, upload limits, response parsing style)
comes from ``API_LIMITS[model_code]`` in config — adding a new OpenAI model
requires no new class, only a catalog row (created when the API key is saved)
and, optionally, an ``API_LIMITS`` entry for non-default behaviour.
"""

from typing import Any, Dict, Optional, Tuple

from app.config import Config
from app.services.api_clients.exceptions import TranscriptionProcessingError

from .openai_base import OpenAIBaseTranscriptionClient


class OpenAIModelTranscriptionClient(OpenAIBaseTranscriptionClient):
    """One client for whisper, gpt-4o-transcribe, gpt-transcribe, and any
    future OpenAI transcription model registered in the catalog."""

    # Default when the class is used without a model code (should not happen).
    CATALOG_MODEL_CODE: str = ""

    def __init__(self, model_code: str, api_key: str, config: Dict[str, Any]) -> None:
        # Set BEFORE super().__init__(): the base ctor resolves/caches
        # _get_api_name(), which reads self.CATALOG_MODEL_CODE.
        self.CATALOG_MODEL_CODE = str(model_code or "").strip()
        limits = (config.get("API_LIMITS") or {}).get(self.CATALOG_MODEL_CODE, {})
        self.api_model_name = str(limits.get("api_model_name") or self.CATALOG_MODEL_CODE)
        self.response_style = str(limits.get("response_style") or "standard")

        super().__init__(api_key, config)

        self.SPLIT_THRESHOLD_SECONDS = limits.get("duration_s")
        size_mb = limits.get("size_mb")
        if size_mb is not None:
            self.SPLIT_THRESHOLD_BYTES = size_mb * 1024 * 1024
        self.logger.debug(
            "Limits set (style=%s, model=%s) - Duration: %ss, Size: %sMB",
            self.response_style, self.api_model_name,
            self.SPLIT_THRESHOLD_SECONDS, size_mb,
        )

    # --- Implementation of Abstract Methods ---

    def _prepare_api_params(
        self,
        language_code: str,
        context_prompt: str,
        response_format: str,
        is_chunk: bool,
        extra_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        api_params: Dict[str, Any] = {
            "model": (extra_options or {}).get("model") or self.api_model_name,
        }
        ui_lang_msg = ""
        language_code = str(language_code or "auto")

        if self.response_style == "whisper":
            # whisper: verbose_json carries response.language; text otherwise.
            if language_code == "auto":
                api_params["response_format"] = "verbose_json"
                ui_lang_msg = "Language detection requested."
            elif language_code in Config.SUPPORTED_LANGUAGE_CODES:
                api_params.update({"language": language_code, "response_format": "text"})
                ui_lang_msg = f"Language set to '{language_code}'."
            else:
                api_params["response_format"] = "verbose_json"
                ui_lang_msg = f"Invalid language code '{language_code}'. Using auto-detection as fallback."
                language_code = "auto"
            api_params["prompt"] = context_prompt

        elif self.response_style == "languages_array":
            # gpt-transcribe: always json; explicit language goes in extra_body.
            api_params["response_format"] = "json"
            if context_prompt:
                api_params["prompt"] = context_prompt
            if language_code == "auto":
                ui_lang_msg = "Language detection requested."
            elif language_code in Config.SUPPORTED_LANGUAGE_CODES:
                api_params["extra_body"] = {"languages": [language_code]}
                ui_lang_msg = f"Language set to '{language_code}'."
            else:
                ui_lang_msg = f"Invalid language code '{language_code}'. Using auto-detection as fallback."
                language_code = "auto"

        else:  # standard (gpt-4o-transcribe and any future OpenAI model)
            api_params["prompt"] = context_prompt
            if language_code == "auto":
                api_params["response_format"] = "json"
                ui_lang_msg = "Language detection requested."
            elif language_code in Config.SUPPORTED_LANGUAGE_CODES:
                api_params.update({"language": language_code, "response_format": "text"})
                ui_lang_msg = f"Language set to '{language_code}'."
            else:
                api_params["response_format"] = "json"
                ui_lang_msg = f"Invalid language code '{language_code}'. Using auto-detection as fallback."
                language_code = "auto"

        if not is_chunk or language_code == "auto":
            if ui_lang_msg:
                self._report_progress(ui_lang_msg, False)

        self.logger.debug(
            "Prepared %s API params (style=%s): %s",
            self.response_style, self.response_style,
            {k: v for k, v in api_params.items() if k != "prompt"},
        )
        return api_params

    def _process_response(
        self, response: Any, response_format: str
    ) -> Tuple[str, Optional[str]]:
        """Parse the OpenAI API response according to the model's style."""
        try:
            if self.response_style == "whisper":
                if response_format == "verbose_json":
                    return response.text, response.language
                return (response if isinstance(response, str) else str(response)), None

            if self.response_style == "languages_array":
                text = response.text
                languages = getattr(response, "languages", None) or []
                detected_language = None
                if languages:
                    first = languages[0]
                    detected_language = (
                        first.get("code") if isinstance(first, dict)
                        else getattr(first, "code", None)
                    )
                return text, detected_language

            # standard
            if response_format == "json":
                return response.text, None
            return (response if isinstance(response, str) else str(response)), None

        except AttributeError as e:
            self.logger.error(
                "Failed to parse API response object (format: %s): %s. Response: %s",
                response_format, e, response, exc_info=True,
            )
            raise TranscriptionProcessingError(
                f"Failed to parse OpenAI response (format: {response_format}).",
                provider=self._get_api_name(),
            ) from e
        except Exception as e:
            self.logger.error("Unexpected error processing API response: %s", e, exc_info=True)
            raise TranscriptionProcessingError(
                f"Unexpected error processing OpenAI response: {e}",
                provider=self._get_api_name(),
            ) from e