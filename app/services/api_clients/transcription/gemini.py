# app/services/api_clients/transcription/gemini.py
"""Gemini transcription client for the batch Interactions API.

Talks to Google's ``gemini-3.5-transcribe`` model: audio is uploaded via the
Files API, transcribed with ``interactions.create`` using a
``transcription_config`` generation config, and the uploaded file is deleted
afterwards. Adding a new Gemini transcription model requires no new class,
only a catalog row (created when the API key is saved) and, optionally, an
``API_LIMITS`` entry for non-default behaviour.
"""

from typing import Any, Dict, Optional, Tuple

from app.core.utils import split_vocabulary_terms
from app.logging_config import get_logger
from app.config import Config

from .base_transcription_client import BaseTranscriptionClient
from app.services.api_clients.exceptions import (
    TranscriptionApiError,
    TranscriptionProcessingError,
    TranscriptionAuthenticationError,
    TranscriptionRateLimitError,
)

# Import the Google GenAI library lazily-tolerantly like the LLM twin in
# app/services/api_clients/llm/gemini_client.py so the module can be imported
# on hosts without the optional dependency.
try:
    from google import genai
    from google.genai import errors as genai_errors
    GOOGLE_GENAI_AVAILABLE = True
except ImportError as e:
    _import_error_message = str(e)
    get_logger(__name__).warning(
        f"Failed to import google-genai or dependencies: {_import_error_message}. "
        "Gemini transcription will not be available."
    )
    genai = None
    genai_errors = None
    GOOGLE_GENAI_AVAILABLE = False


class GeminiTranscriptionClient(BaseTranscriptionClient):
    """One client for gemini-3.5-transcribe and any future Gemini
    transcription model registered in the catalog."""

    # Default when the class is used without a model code.
    CATALOG_MODEL_CODE: str = "gemini-3.5-transcribe"

    def __init__(
        self,
        api_key: str,
        config: Dict[str, Any],
        model_code: str = "gemini-3.5-transcribe",
    ) -> None:
        # Set BEFORE super().__init__(): the base ctor resolves/caches
        # _get_api_name(), which reads self.CATALOG_MODEL_CODE.
        self.CATALOG_MODEL_CODE = str(model_code or "").strip() or "gemini-3.5-transcribe"
        # The Interactions API takes the catalog code verbatim; there is no
        # API_LIMITS override mapping catalog codes to provider-native names.
        self.api_model_name = self.CATALOG_MODEL_CODE
        limits = (config.get("API_LIMITS") or {}).get(self.CATALOG_MODEL_CODE, {})

        super().__init__(api_key, config)

        self.SPLIT_THRESHOLD_SECONDS = limits.get("duration_s")
        size_mb = limits.get("size_mb")
        if size_mb is not None:
            self.SPLIT_THRESHOLD_BYTES = size_mb * 1024 * 1024
        else:
            # The Files API accepts large media, so keep the byte threshold at
            # 1 GB and let the duration rule govern splitting instead.
            self.SPLIT_THRESHOLD_BYTES = 1024 * 1024 * 1024
        self.logger.info(
            "Limits set - Duration: %ss, Size: %sMB",
            self.SPLIT_THRESHOLD_SECONDS, size_mb,
        )

    # --- Implementation of Abstract Methods ---

    def _initialize_client(self, api_key: str) -> None:
        """Builds the GenAI client (cheap constructor, no network at init)."""
        if not GOOGLE_GENAI_AVAILABLE:
            raise ValueError(
                f"Google GenAI library not installed (Import Error: {_import_error_message})."
            )
        self._genai_client = genai.Client(api_key=api_key)
        self.logger.debug("Google GenAI client initialized successfully.")

    def _prepare_api_params(
        self,
        language_code: str,
        context_prompt: str,
        response_format: str,
        is_chunk: bool,
        extra_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        transcription_config: Dict[str, Any] = {}
        ui_lang_msg = ""
        language_code = str(language_code or "auto")

        if language_code == "auto":
            # Empty language_codes asks Google to auto-detect the language.
            transcription_config["language_codes"] = []
            ui_lang_msg = "Language detection requested."
        elif language_code in Config.SUPPORTED_LANGUAGE_CODES:
            # BCP-47 accepts primary-language subtags, so the repo's short
            # codes ('en', 'es', ...) are passed as-is without region suffixes.
            transcription_config["language_codes"] = [language_code]
            ui_lang_msg = f"Language set to '{language_code}'."
        else:
            self.logger.warning(
                "Invalid language code '%s'. Using auto-detection as fallback.", language_code
            )
            transcription_config["language_codes"] = []
            ui_lang_msg = f"Invalid language code '{language_code}'. Using auto-detection as fallback."
            language_code = "auto"

        vocabulary = split_vocabulary_terms(context_prompt)
        if vocabulary:
            transcription_config["custom_vocabulary"] = vocabulary
            if not is_chunk:
                self._report_progress(
                    f"Using context prompt as custom vocabulary ({len(vocabulary)} terms).",
                    False,
                )

        if not is_chunk or language_code == "auto":
            if ui_lang_msg:
                self._report_progress(ui_lang_msg, False)

        self.logger.debug("Prepared Gemini transcription config: %s", transcription_config)
        return {"transcription_config": transcription_config}

    def _call_api(self, file_handle: Any, api_params: Dict[str, Any]) -> Any:
        """
        Uploads the audio to the Files API and runs one Interactions call.
        Args:
            file_handle: The opened file handle (used to get the file path).
            api_params: Dictionary containing the transcription_config payload.
        Raises:
            TranscriptionAuthenticationError: If authentication fails.
            TranscriptionRateLimitError: On rate limiting (retryable).
            TranscriptionProcessingError: For other API call errors.
        """
        api_name = self._get_api_name()
        file_path = getattr(file_handle, "name", None)
        upload = None
        try:
            if not file_path:
                raise ValueError("File path not available from the file handle.")
            upload = self._genai_client.files.upload(file=file_path)
            interaction = self._genai_client.interactions.create(
                model=self.api_model_name,
                input=[
                    {
                        "type": "audio",
                        "uri": upload.uri,
                        "mime_type": upload.mime_type,
                    }
                ],
                generation_config=api_params,
            )
            return interaction
        except genai_errors.APIError as exc:
            exc_message = str(getattr(exc, "message", "") or exc)
            if getattr(exc, "code", None) in (401, 403):
                self.logger.error("Authentication error (%s): %s", exc.code, exc_message)
                raise TranscriptionAuthenticationError(
                    f"{api_name}: {exc_message}", provider=api_name
                ) from exc
            if getattr(exc, "code", None) == 429:
                self.logger.warning("Rate limit error: %s", exc_message)
                raise TranscriptionRateLimitError(
                    f"{api_name}: {exc_message}", provider=api_name
                ) from exc
            self.logger.error("Gemini API Error (%s): %s", exc.code, exc_message)
            raise TranscriptionProcessingError(
                f"Gemini API Error: {exc.code} {exc_message}", provider=api_name
            ) from exc
        except Exception as exc:
            if isinstance(exc, TranscriptionApiError):
                raise
            self.logger.error(
                "Unexpected error during Gemini API call: %s", exc, exc_info=True
            )
            raise TranscriptionProcessingError(
                f"Unexpected error during Gemini API call: {exc}", provider=api_name
            ) from exc
        finally:
            if upload is not None:
                try:
                    self._genai_client.files.delete(name=upload.name)
                except Exception as cleanup_exc:
                    self.logger.debug(
                        "Best-effort delete of uploaded file '%s' failed: %s",
                        upload.name, cleanup_exc,
                    )

    def _process_response(
        self, response: Any, response_format: str
    ) -> Tuple[str, Optional[str]]:
        """Extract the transcript text; Google exposes no detected language."""
        try:
            text = response.output_text
        except AttributeError as e:
            self.logger.error(
                "Failed to parse Gemini response object: %s. Response: %s",
                e, response, exc_info=True,
            )
            raise TranscriptionProcessingError(
                f"Failed to parse Gemini response.",
                provider=self._get_api_name(),
            ) from e
        text = (text or "").strip()
        if not text:
            raise TranscriptionProcessingError(
                f"Gemini returned an empty transcript.",
                provider=self._get_api_name(),
            )
        return text, None

    def _get_retryable_errors(self) -> Tuple[type, ...]:
        return (TranscriptionRateLimitError,)
