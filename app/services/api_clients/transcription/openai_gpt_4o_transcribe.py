# app/services/api_clients/transcription/openai_gpt_4o_transcribe.py
# Client for interacting with the OpenAI GPT-4o transcription model.

from typing import Tuple, Optional, Dict, Any

# Shared OpenAI behaviours
from .openai_base import OpenAIBaseTranscriptionClient
from app.config import Config # To access language codes
from app.services.api_clients.exceptions import TranscriptionProcessingError

class OpenAIGPT4OTranscribeClient(OpenAIBaseTranscriptionClient):
    """
    Handles transcription requests using the OpenAI GPT-4o model via the transcriptions endpoint.
    Inherits common workflow from BaseTranscriptionClient.
    """
    API_MODEL_PARAM = "gpt-4o-transcribe"

    def __init__(self, api_key: str, config: Dict[str, Any]) -> None:
        """Initializes the client and sets API-specific limits from config."""
        super().__init__(api_key, config)
        api_limits = self.config.get('API_LIMITS', {}).get('gpt-4o-transcribe', {})
        self.SPLIT_THRESHOLD_SECONDS = api_limits.get('duration_s')
        size_mb = api_limits.get('size_mb')
        if size_mb is not None:
            self.SPLIT_THRESHOLD_BYTES = size_mb * 1024 * 1024
        self.logger.debug(f"Limits set - Duration: {self.SPLIT_THRESHOLD_SECONDS}s, Size: {size_mb}MB")

    # --- Implementation of Abstract Methods ---

    def _get_api_name(self) -> str:
        return "OpenAI GPT-4o Transcribe"

    def _prepare_api_params(self, language_code: str, context_prompt: str, response_format: str,
                            is_chunk: bool, extra_options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Prepare the dictionary of parameters for the OpenAI transcriptions API call."""
        api_params: Dict[str, Any] = {
            "model": (extra_options or {}).get("model") or self.API_MODEL_PARAM,
            "prompt": context_prompt
        }
        log_lang_param_desc = ""
        ui_lang_msg = ""

        if language_code == 'auto':
            api_params["response_format"] = "json"
            log_lang_param_desc = "'auto' (detection requested, format: json)"
            ui_lang_msg = "Language detection requested."
        elif language_code in Config.SUPPORTED_LANGUAGE_CODES:
            api_params["language"] = language_code
            api_params["response_format"] = "text"
            log_lang_param_desc = f"'{language_code}'"
            ui_lang_msg = f"Language set to '{language_code}'."
        else:
            self.logger.warning(f"Invalid language code '{language_code}'. Using auto-detection as fallback.")
            ui_lang_msg = f"Invalid language code '{language_code}'. Using auto-detection as fallback."
            api_params["response_format"] = "json"
            log_lang_param_desc = "'auto' (fallback detection, format: json)"
            language_code = 'auto'

        if not is_chunk or (is_chunk and language_code == 'auto'):
            if ui_lang_msg:
                self._report_progress(ui_lang_msg, False)

        self.logger.debug(f"Prepared API params (Lang: {log_lang_param_desc}): { {k:v for k,v in api_params.items() if k != 'prompt'} }")
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
            if response_format == "json":
                transcription_text = response.text
                self.logger.debug("Received 'json' response format (language not specified by API).")
            elif response_format == "text":
                transcription_text = response if isinstance(response, str) else str(response)
            else:
                self.logger.warning(f"Unexpected response format '{response_format}'. Attempting string conversion.")
                transcription_text = str(response)
        except AttributeError as e:
            self.logger.error(f"Failed to parse API response object (format: {response_format}): {e}. Response: {response}", exc_info=True)
            raise TranscriptionProcessingError(f"Failed to parse OpenAI response (format: {response_format}).", provider=self._get_api_name()) from e
        except Exception as e:
            self.logger.error(f"Unexpected error processing API response: {e}", exc_info=True)
            raise TranscriptionProcessingError(f"Unexpected error processing OpenAI response: {e}", provider=self._get_api_name()) from e

        return transcription_text, None
