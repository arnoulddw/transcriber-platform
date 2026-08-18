# app/services/api_clients/transcription/openai_whisper.py
# Client for interacting with the OpenAI Whisper-1 model API.

from typing import Tuple, Optional, Dict, Any

# Shared OpenAI behaviours
from .openai_base import OpenAIBaseTranscriptionClient
from app.config import Config # To access language codes
from app.services.api_clients.exceptions import TranscriptionProcessingError

class OpenAIWhisperTranscriptionAPI(OpenAIBaseTranscriptionClient):
    """
    Handles transcription requests using the OpenAI Whisper-1 model.
    Inherits common workflow from BaseTranscriptionClient.
    """
    MODEL_NAME = "whisper-1" # Model identifier for API calls
    # MAX_CONCURRENT_CHUNKS is now inherited and set from config in BaseTranscriptionClient

    def __init__(self, api_key: str, config: Dict[str, Any]) -> None:
        """Initializes the client and sets API-specific limits from config."""
        super().__init__(api_key, config)
        api_limits = self.config.get('API_LIMITS', {}).get('whisper', {})
        self.SPLIT_THRESHOLD_SECONDS = api_limits.get('duration_s')
        size_mb = api_limits.get('size_mb')
        if size_mb is not None:
            self.SPLIT_THRESHOLD_BYTES = size_mb * 1024 * 1024
        self.logger.debug("Limits set - Duration: %ss, Size: %sMB", self.SPLIT_THRESHOLD_SECONDS, size_mb)

    # --- Implementation of Abstract Methods ---

    def _get_api_name(self) -> str:
        return "OpenAI Whisper" # Updated name

    def _prepare_api_params(self, language_code: str, context_prompt: str, response_format: str,
                            is_chunk: bool, extra_options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Prepare the dictionary of parameters for the Whisper API call."""
        api_params: Dict[str, Any] = {
            "model": (extra_options or {}).get("model") or self.MODEL_NAME,
            "prompt": context_prompt
        }
        log_lang_param_desc = ""
        ui_lang_msg = ""

        # Handle language parameter and response format
        if language_code == 'auto':
            api_params["response_format"] = "verbose_json"
            log_lang_param_desc = "'auto' (detection requested)"
            ui_lang_msg = "Language detection requested."
        elif language_code in Config.SUPPORTED_LANGUAGE_CODES:
            api_params["language"] = language_code
            api_params["response_format"] = "text"
            log_lang_param_desc = f"'{language_code}'"
            ui_lang_msg = f"Language set to '{language_code}'."
        else:
            self.logger.warning("Invalid language code '%s'. Using auto-detection as fallback.", language_code)
            ui_lang_msg = f"Invalid language code '{language_code}'. Using auto-detection as fallback."
            api_params["response_format"] = "verbose_json"
            log_lang_param_desc = "'auto' (fallback detection)"
            language_code = 'auto'

        if not is_chunk or (is_chunk and language_code == 'auto'):
            if ui_lang_msg:
                self._report_progress(ui_lang_msg, False)

        self.logger.debug("Prepared API params (Lang: %s): %s", log_lang_param_desc, {k: v for k, v in api_params.items() if k != 'prompt'})
        return api_params

    def _process_response(self, response: Any, response_format: str) -> Tuple[str, Optional[str]]:
        """
        Parse the OpenAI API response.
        Raises:
            TranscriptionProcessingError: If the response cannot be parsed.
        """
        transcription_text: str = ""
        detected_language: Optional[str] = None

        try:
            if response_format == "verbose_json":
                transcription_text = response.text
                detected_language = response.language
                self.logger.debug("Detected language: %s", detected_language)
            elif response_format == "text":
                transcription_text = response if isinstance(response, str) else str(response)
            else:
                self.logger.warning("Unexpected response format '%s'. Attempting string conversion.", response_format)
                transcription_text = str(response)
        except AttributeError as e:
             self.logger.error("Failed to parse API response object (format: %s): %s. Response: %s", response_format, e, response, exc_info=True)
             raise TranscriptionProcessingError(f"Failed to parse OpenAI response (format: {response_format}).", provider=self._get_api_name()) from e
        except Exception as e:
             self.logger.error("Unexpected error processing API response: %s", e, exc_info=True)
             raise TranscriptionProcessingError(f"Unexpected error processing OpenAI response: {e}", provider=self._get_api_name()) from e

        return transcription_text, detected_language
