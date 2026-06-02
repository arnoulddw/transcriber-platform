# app/services/api_clients/llm/gemini_client.py
# Client for interacting with the Google Gemini API for LLM tasks.

from app.logging_config import get_logger
# --- MODIFIED: Remove current_app import, add Dict ---
from typing import Optional, Dict, Any, List, Tuple, Type
# --- END MODIFIED ---

# Import Base Class and LLM exceptions
from .base_llm_client import BaseLLMClient
from app.services.api_clients.exceptions import (
    LlmApiError,
    LlmConfigurationError,
    LlmGenerationError,
    LlmAuthenticationError, # Although Gemini uses API key, keep for consistency
    LlmRateLimitError,
    LlmSafetyError
)

# Import Google GenAI library and specific errors
try:
    from google import genai
    from google.api_core import exceptions as google_exceptions
    from google.genai import types as genai_types
    GOOGLE_GENAI_AVAILABLE = True
    get_logger(__name__).debug("Google GenAI library and types imported successfully.")
except ImportError as e:
    _import_error_message = str(e)
    get_logger(__name__).warning(f"Failed to import google-genai or dependencies: {_import_error_message}. Gemini workflows will not be available.")
    genai = None
    google_exceptions = None
    genai_types = None
    GOOGLE_GENAI_AVAILABLE = False

# Define specific retryable exceptions from google.api_core
RETRYABLE_GOOGLE_ERRORS = (
    google_exceptions.ServiceUnavailable,
    google_exceptions.InternalServerError,
    google_exceptions.DeadlineExceeded,
    google_exceptions.ResourceExhausted, # Rate limiting
) if google_exceptions else ()

class GeminiClient(BaseLLMClient):
    """Handles LLM requests using the Google Gemini API."""

    # --- Implementation of Abstract Methods ---

    def _get_api_name(self) -> str:
        return "Google Gemini"

    # --- MODIFIED: Accept config in _initialize_client ---
    def _initialize_client(self, api_key: str, config: Dict[str, Any]) -> None:
        """Initializes the Gemini client using genai.Client."""
        if not GOOGLE_GENAI_AVAILABLE:
            raise ValueError(f"Google GenAI library not installed (Import Error: {_import_error_message}).")
        try:
            # Use genai.Client as shown in the working code and documentation snippet
            self.client = genai.Client(api_key=api_key)
            # Get model name using the passed config
            self.model_name = self._get_model_name(config) # Store model name for use in generate_text
            self.logger.debug(f"Google GenAI client initialized successfully (will use model '{self.model_name}').")
        except Exception as e:
            # Let the base class handle wrapping this in LlmConfigurationError
            raise ValueError(f"Gemini client initialization failed: {e}") from e
    # --- END MODIFIED ---

    # --- MODIFIED: Accept config in _get_model_name and update default ---
    def _get_model_name(self, config: Dict[str, Any]) -> str:
        """Gets the configured Gemini model name from the passed config dictionary."""
        return config.get('WORKFLOW_LLM_MODEL') or 'gemini-2.0-flash'
    # --- END MODIFIED ---

    def generate_text(self, prompt: str, **kwargs) -> str:
        """
        Generates text based on a single prompt using the configured Gemini model.
        Uses the initialized genai.Client instance.

        Args:
            prompt: The input prompt string.
            **kwargs: Supports 'max_tokens', 'temperature'.

        Returns:
            The generated text string.

        Raises:
            LlmConfigurationError: If client is not initialized.
            LlmRateLimitError, LlmSafetyError, LlmGenerationError, Exception.
        """
        if not self.client:
             raise LlmConfigurationError("Gemini client not initialized.", provider=self._get_api_name())
        if not GOOGLE_GENAI_AVAILABLE:
             raise LlmConfigurationError("Gemini library not available.", provider=self._get_api_name())

        logger = get_logger(__name__, model=self.model_name, component=self._get_api_name())
        logger.debug("Generating text...")
 
        # --- MODIFIED: Get config values from self.config (stored by base class) ---
        max_output_tokens = kwargs.get("max_tokens", self.config.get('WORKFLOW_MAX_OUTPUT_TOKENS', 1024))
        temperature = kwargs.get("temperature", 0.7)
        # --- END MODIFIED ---

        # Define safety settings (adjust thresholds as needed)
        safety_settings_list = [
            genai_types.SafetySetting(
                category=genai_types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                threshold=genai_types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE
            ),
            genai_types.SafetySetting(
                category=genai_types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                threshold=genai_types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE
            ),
            genai_types.SafetySetting(
                category=genai_types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                threshold=genai_types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE
            ),
            genai_types.SafetySetting(
                category=genai_types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                threshold=genai_types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE
            ),
        ]

        generation_config_kwargs = {
            "max_output_tokens": max_output_tokens,
            "temperature": temperature,
            "safety_settings": safety_settings_list,
        }
        if kwargs.get("disable_thinking"):
            if hasattr(genai_types, "ThinkingConfig"):
                generation_config_kwargs["thinking_config"] = genai_types.ThinkingConfig(
                    thinking_budget=0,
                    include_thoughts=False,
                )
                logger.debug("Gemini thinking disabled for this request.")
            else:
                logger.warning("Gemini thinking disable requested, but ThinkingConfig is unavailable in this SDK.")
        generation_config = genai_types.GenerateContentConfig(**generation_config_kwargs)

        try:
            # Respect model overridden in kwargs, fallback to client's default model
            actual_model = kwargs.get('model', self.model_name)
            logger.info(f"Using actual_model: {actual_model}")
            response = self.client.models.generate_content(
                model=actual_model,
                contents=[prompt],
                config=generation_config
            )

            # Response checking logic remains the same
            try:
                generated_text = response.text
            except ValueError as ve:
                 block_reason_str = "Unknown"
                 try:
                     if response.prompt_feedback and response.prompt_feedback.block_reason:
                         block_reason_str = response.prompt_feedback.block_reason.name
                 except AttributeError: pass
                 logger.warning(f"API call returned no valid text. Block Reason: {block_reason_str}")
                 raise LlmSafetyError(f"Gemini: Prompt blocked by safety settings (Reason: {block_reason_str}). Please revise your prompt.", provider=self._get_api_name()) from ve
            except AttributeError:
                 logger.warning(f"API response object missing 'text' attribute. Response: {response}")
                 raise LlmGenerationError("Received an unexpected response format from the Gemini API.", provider=self._get_api_name())

            if not generated_text:
                 logger.warning(f"API call returned empty text. Response: {response}")
                 raise LlmGenerationError("Gemini API returned an empty result.", provider=self._get_api_name())

            logger.info("Content generation successful.")
            return generated_text.strip()

        # Exception handling remains the same
        except google_exceptions.ResourceExhausted as e:
            logger.warning(f"Rate limit error: {e}")
            raise LlmRateLimitError(f"Gemini: {e}", provider=self._get_api_name()) from e
        except google_exceptions.InvalidArgument as e:
             if "safety settings" in str(e).lower():
                 raise LlmSafetyError(f"Gemini: {e}", provider=self._get_api_name()) from e
             else:
                 raise LlmGenerationError(f"Gemini Invalid Argument: {e}", provider=self._get_api_name()) from e
        except google_exceptions.GoogleAPICallError as api_error:
            logger.error(f"API call failed: {api_error}", exc_info=True)
            raise LlmGenerationError(f"Gemini API Error: {api_error}", provider=self._get_api_name()) from api_error
        except Exception as e:
            logger.error(f"Unexpected error during content generation: {e}", exc_info=True)
            if isinstance(e, (LlmApiError, LlmGenerationError, LlmSafetyError, LlmRateLimitError)): raise e
            else: raise LlmGenerationError(f"Unexpected error during Gemini generation: {e}", provider=self._get_api_name()) from e

    # --- chat_completion and get_embedding remain NotImplemented ---
    def chat_completion(self, messages: List[Dict[str, str]], **kwargs) -> str:
        self.logger.warning("chat_completion is not yet fully implemented for Gemini.")
        raise NotImplementedError("Gemini chat_completion is not implemented.")

    def get_embedding(self, text: str, **kwargs) -> List[float]:
        self.logger.warning("get_embedding is not yet implemented for Gemini.")
        raise NotImplementedError("Gemini get_embedding is not implemented.")

    def _get_retryable_errors(self) -> Tuple[Type[Exception], ...]:
        """Return a tuple of retryable Google API errors."""
        return RETRYABLE_GOOGLE_ERRORS
