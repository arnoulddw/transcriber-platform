# app/services/api_clients/transcription/assemblyai.py
# Client for interacting with the AssemblyAI transcription API.

import os
import re
from typing import Tuple, Optional, Dict, Any, Type, List

import assemblyai as aai # Official AssemblyAI SDK
from assemblyai.types import TranscriptError, TranscriptStatus, LanguageCode, WordBoost

# Import Base Class and project config/exceptions
from .base_transcription_client import BaseTranscriptionClient
from app.config import Config # To access language codes
from app.services.api_clients.exceptions import (
    TranscriptionApiError,
    TranscriptionConfigurationError,
    TranscriptionProcessingError,
    TranscriptionAuthenticationError # AssemblyAI SDK might raise TranscriptError for auth issues too
)

class AssemblyAITranscriptionAPI(BaseTranscriptionClient):
    """
    Handles transcription requests using the AssemblyAI API.
    Inherits common workflow from BaseTranscriptionClient.
    Note: AssemblyAI SDK handles chunking internally for large files via transcribe(),
          so the base class splitting logic might not be strictly necessary if only using this.
          The base class's `transcribe` will call this class's `_call_api` directly.
    """
    # AssemblyAI handles concurrency internally, so we set this to 1.
    # The base class will still use this value if its _split_and_transcribe is called,
    # but for AssemblyAI, it's more of a formality as the SDK manages it.
    # We override the max_concurrent_chunks in the __init__ for this client.

    MAX_CONTEXT_PROMPT_WORDS = 120
    WORD_BOOST_MAX_TERMS = 20
    WORD_BOOST_MAX_WORDS_PER_TERM = 4

    # Resolves display names from the catalog (single source of truth).
    CATALOG_MODEL_CODE = "universal"

    def __init__(
        self,
        api_key: str,
        config: Dict[str, Any],
        model_code: Optional[str] = None,
    ) -> None:
        """Initializes the client and sets API-specific limits from config."""
        self.model_code = str(model_code or self.CATALOG_MODEL_CODE).strip() or "universal"
        if self.model_code.casefold() == "assemblyai":
            self.model_code = "universal"
        self.CATALOG_MODEL_CODE = self.model_code
        super().__init__(api_key, config)
        # Override max_concurrent_chunks for AssemblyAI as it handles this internally
        self.max_concurrent_chunks = 1
        self.logger.info("Max concurrent chunks set to 1 (handled by SDK).")

        api_limits = self.config.get('API_LIMITS', {}).get('assemblyai', {})
        self.SPLIT_THRESHOLD_SECONDS = api_limits.get('duration_s')
        size_mb = api_limits.get('size_mb')
        if size_mb is not None:
            self.SPLIT_THRESHOLD_BYTES = size_mb * 1024 * 1024
        else:
            # AssemblyAI has no hard size limit, so we can set it very high
            # to prevent the base client from splitting based on size.
            self.SPLIT_THRESHOLD_BYTES = 1024 * 1024 * 1024 # 1 GB
        self.logger.info("Limits set - Duration: %ss, Size: %sMB", self.SPLIT_THRESHOLD_SECONDS, size_mb)


    # --- Implementation of Abstract Methods ---

    def _initialize_client(self, api_key: str) -> None:
        """Configures the AssemblyAI SDK."""
        try:
            # Configure the AssemblyAI SDK globally with the provided key
            aai.settings.api_key = self.api_key
            # Store an instance for consistency, though SDK often uses global settings
            self.client = aai.Transcriber()
            self.logger.info("Client initialized and SDK configured.")
        except Exception as e:
            # Let the Base Class __init__ handle raising TranscriptionConfigurationError
            raise ValueError(f"AssemblyAI SDK configuration failed: {e}") from e

    def _prepare_api_params(self, language_code: str, context_prompt: str, response_format: str,
                            is_chunk: bool, extra_options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Prepare the TranscriptionConfig parameters for AssemblyAI."""
        # Use the configured provider-local model when supplied; preserve the
        # historical Universal fallback for legacy provider-wide keys.
        model_name = (extra_options or {}).get('model') or self.model_code
        config_params: Dict[str, Any] = {
            'speech_models': [model_name],
        }
        log_lang_param_desc = ""
        ui_lang_msg = ""

        context_terms = self._extract_context_terms(context_prompt)
        if context_terms:
            config_params['keyterms_prompt'] = context_terms
            config_params['word_boost'] = context_terms
            config_params['boost_param'] = WordBoost.high
            self.logger.info(
                "Context prompt forwarded to AssemblyAI via keyterms_prompt/word_boost (terms: %d).",
                len(context_terms)
            )

        if language_code == 'auto':
            config_params['language_detection'] = True
            log_lang_param_desc = "'auto' (detection requested)"
            ui_lang_msg = "Language detection enabled."
        elif language_code in Config.SUPPORTED_LANGUAGE_CODES:
            try:
                config_params['language_code'] = LanguageCode(language_code) # Use SDK enum
                log_lang_param_desc = f"'{language_code}'"
                ui_lang_msg = f"Language set to '{language_code}'."
            except ValueError:
                self.logger.warning("Language code '%s' not recognized by AssemblyAI SDK enum. Falling back to auto-detection.", language_code)
                ui_lang_msg = f"Language code '{language_code}' not recognized by AssemblyAI SDK. Using auto-detection."
                config_params['language_detection'] = True
                log_lang_param_desc = "'auto' (fallback detection)"
                language_code = 'auto' # Update effective code
        else:
            self.logger.warning("Invalid language code '%s'. Using auto-detection as fallback.", language_code)
            ui_lang_msg = f"Invalid language code '{language_code}'. Using auto-detection as fallback."
            config_params['language_detection'] = True
            log_lang_param_desc = "'auto' (fallback detection)"
            language_code = 'auto' # Update effective code

        # Report language choice progress only once per file/chunk set
        enable_speaker_labels = bool(extra_options and extra_options.get('speaker_diarization_enabled'))
        if enable_speaker_labels:
            config_params['speaker_labels'] = True
            self.logger.info("Speaker diarization (speaker_labels) enabled for this transcription.")
            if not is_chunk:
                self._report_progress("Identifying speakers in the transcript...", False)

        if not is_chunk: # AssemblyAI handles splitting internally, only report once
            if ui_lang_msg:
                self._report_progress(ui_lang_msg, False)

        self.logger.debug("Prepared API config params (Lang: %s): %s", log_lang_param_desc, config_params)
        # Return the config params, which will be used to create TranscriptionConfig in _call_api
        return config_params

    def _call_api(self, file_handle: Any, api_params: Dict[str, Any]) -> Any:
        """
        Submits the transcription job using the AssemblyAI SDK.
        The SDK handles uploading the file from the handle's path.
        Args:
            file_handle: The opened file handle (used to get the file path).
            api_params: Dictionary containing parameters for TranscriptionConfig.
        Raises:
            TranscriptionAuthenticationError: If authentication fails.
            TranscriptionProcessingError: For other API call errors.
            FileNotFoundError: If the file path is invalid.
            Exception: For unexpected errors.
        """
        try:
            file_path = file_handle.name
            if not file_path or not os.path.exists(file_path):
                 raise FileNotFoundError(f"File path not available or file does not exist: {file_path}")

            config_obj = aai.TranscriptionConfig(**api_params)
            transcriber = aai.Transcriber(config=config_obj)

            self._report_progress(f"Uploading and processing audio with {self._get_api_name()}...")
            transcript = transcriber.transcribe(file_path) # This blocks until completion or error
            self.logger.info("API request finished. Transcript status: %s", transcript.status)
            return transcript # Return the completed Transcript object

        except TranscriptError as aai_error:
            error_msg = str(aai_error)
            self.logger.error("AssemblyAI Universal API Error: %s", error_msg, exc_info=True)
            # Attempt to map AssemblyAI errors to our custom exceptions
            if "authorization" in error_msg.lower() or "authentication" in error_msg.lower():
                raise TranscriptionAuthenticationError(f"AssemblyAI Universal: {error_msg}", provider=self._get_api_name()) from aai_error
            # Add more specific error mappings if needed (e.g., for rate limits if identifiable)
            else:
                raise TranscriptionProcessingError(f"AssemblyAI Universal: {error_msg}", provider=self._get_api_name()) from aai_error
        except FileNotFoundError as fnf:
            self.logger.error("File not found during API call: %s", fnf, exc_info=True)
            raise fnf # Re-raise FileNotFoundError
        except Exception as e:
            self.logger.error("Unexpected error during API call: %s", e, exc_info=True)
            # Wrap unexpected errors in our custom exception
            raise TranscriptionProcessingError(f"Unexpected error during AssemblyAI Universal API call: {e}", provider=self._get_api_name()) from e

    def _process_response(self, response: Any, response_format: str) -> Tuple[str, Optional[str]]:
        """
        Parse the AssemblyAI Universal Transcript object.
        Raises:
            TranscriptionProcessingError: If the transcript status is 'error'.
        """
        transcript: aai.Transcript = response
        transcription_text: Optional[str] = None
        detected_language: Optional[str] = None

        if transcript.status == TranscriptStatus.error:
            error_detail = transcript.error or "Unknown AssemblyAI Universal error"
            raise TranscriptionProcessingError(f"AssemblyAI Universal transcription failed: {error_detail}", provider=self._get_api_name())

        if transcript.status != TranscriptStatus.completed:
            raise TranscriptionProcessingError(f"AssemblyAI Universal transcription finished with unexpected status: {transcript.status}", provider=self._get_api_name())

        transcription_text = transcript.text
        diarized_utterances = getattr(transcript, 'utterances', None)
        if diarized_utterances:
            diarized_lines = []
            for utterance in diarized_utterances:
                speaker_label = getattr(utterance, 'speaker', '') or ''
                utterance_text = getattr(utterance, 'text', '').strip()
                if not utterance_text:
                    continue
                prefix = f"Speaker {speaker_label}: " if speaker_label else ""
                diarized_lines.append(f"{prefix}{utterance_text}")
            diarized_text = "\n".join(diarized_lines).strip()
            if diarized_text:
                transcription_text = diarized_text
                self.logger.info("Using diarized transcript with %d utterances.", len(diarized_lines))
        detected_lang_val = getattr(transcript, 'language_code', None)
        if detected_lang_val:
            detected_language = str(detected_lang_val)
            self.logger.info("Detected language: %s", detected_language)
        else:
            self.logger.info("Language code not found in transcript response.")

        return transcription_text or "", detected_language

    def _get_retryable_errors(self) -> Tuple[Type[Exception], ...]:
        """Return AssemblyAI-specific retryable errors (if any identified)."""
        # AssemblyAI SDK handles many retries internally.
        # We might add specific TranscriptError cases here if needed, but start with none.
        return ()

    def _extract_context_terms(self, raw_prompt: str) -> List[str]:
        """
        Parse the raw context prompt and extract short vocab terms that can be passed
        to AssemblyAI via keyterms_prompt / word_boost.
        """
        if not raw_prompt:
            return []

        tokens = re.split(r'[\n,;]+', raw_prompt)
        vocab_terms: List[str] = []
        seen = set()
        for token in tokens:
            candidate = token.strip()
            if not candidate:
                continue
            words = re.findall(r'\S+', candidate)
            if not words:
                continue
            if len(words) > self.MAX_CONTEXT_PROMPT_WORDS:
                words = words[:self.MAX_CONTEXT_PROMPT_WORDS]
                candidate = " ".join(words)
            word_count = len(words)
            if word_count == 0 or word_count > self.WORD_BOOST_MAX_WORDS_PER_TERM:
                # For long phrases, try to capture the final WORD_BOOST_MAX_WORDS_PER_TERM tokens
                candidate = " ".join(words[-self.WORD_BOOST_MAX_WORDS_PER_TERM:])
                word_count = len(candidate.split())
                if word_count == 0:
                    continue
            lower_candidate = candidate.lower()
            if lower_candidate in seen:
                continue
            seen.add(lower_candidate)
            vocab_terms.append(candidate)
            if len(vocab_terms) >= self.WORD_BOOST_MAX_TERMS:
                break

        return vocab_terms
