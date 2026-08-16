# app/services/api_clients/llm/openai_client.py
# Client for interacting with OpenAI LLM APIs (GPT-3.5, GPT-4, etc.).

from app.logging_config import get_logger
from typing import Optional, Dict, Any, List, Tuple, Type

# Import Base Class and LLM exceptions
from .base_llm_client import BaseLLMClient
from app.services.api_clients.exceptions import (
    LlmApiError,
    LlmConfigurationError,
    LlmGenerationError,
    LlmAuthenticationError,
    LlmRateLimitError,
    LlmSafetyError # OpenAI might raise specific content filter errors
)

# Import OpenAI library and specific errors
try:
    from openai import OpenAI, OpenAIError, APIError, APIConnectionError, RateLimitError, AuthenticationError
    # Import content filtering error if available, e.g., BadRequestError with specific code
    # from openai import BadRequestError
    OPENAI_AVAILABLE = True
    get_logger(__name__).debug("OpenAI library imported successfully.")
except ImportError:
    get_logger(__name__).warning("Failed to import openai library. OpenAI LLM features will not be available.")
    OpenAI = None
    OpenAIError = None
    APIError = None
    APIConnectionError = None
    RateLimitError = None
    AuthenticationError = None
    # BadRequestError = None
    OPENAI_AVAILABLE = False

class OpenAIClient(BaseLLMClient):
    """Handles LLM requests using the OpenAI API (GPT models)."""

    DEFAULT_MODEL = "gpt-4o" # Or another suitable default

    # --- Implementation of Abstract Methods ---

    def _get_api_name(self) -> str:
        return "OpenAI LLM"

    def _initialize_client(self, api_key: str, config: Dict[str, Any]) -> None:
        """Initializes the OpenAI API client."""
        if not OPENAI_AVAILABLE:
            raise ValueError("OpenAI library not installed.")
        try:
            client_kwargs = {"api_key": api_key}
            base_url = config.get("OPENAI_BASE_URL")
            if base_url:
                client_kwargs["base_url"] = base_url
            self.client = OpenAI(**client_kwargs)
            # Allow overriding the default model via application config
            self.default_model = config.get("WORKFLOW_LLM_MODEL", self.DEFAULT_MODEL)
            self.logger.debug("OpenAI client initialized successfully.")
        except OpenAIError as e:
            raise ValueError(f"OpenAI client initialization failed: {e}") from e

    def generate_text(self, prompt: str, **kwargs) -> str:
        """
        Generates text based on a single prompt using OpenAI.
        (Placeholder - uses chat completion endpoint for simplicity)
        """
        if not self.client:
             raise LlmConfigurationError("OpenAI client not initialized.", provider=self._get_api_name())

        # Remove any externally provided model so we can control final value without duplicates
        provided_model = kwargs.pop("model", None)
        model_name = provided_model or getattr(self, "default_model", self.DEFAULT_MODEL)
        logger = get_logger(__name__, model=model_name, component=self._get_api_name())
        logger.debug("Generating text (using chat endpoint)...")

        messages = [{"role": "user", "content": prompt}]
        try:
            return self.chat_completion(messages, model=model_name, **kwargs)
        except (LlmAuthenticationError, LlmRateLimitError, LlmSafetyError, LlmGenerationError, Exception) as e:
             # Re-raise errors from chat_completion
             raise e


    def chat_completion(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """
        Generates a response based on a conversation history using OpenAI chat models.

        Args:
            messages: List of message dicts [{'role': 'user'/'assistant'/'system', 'content': '...'}].
            **kwargs: Supports 'model', 'max_tokens', 'temperature'.

        Returns:
            The generated message content string.

        Raises:
            LlmAuthenticationError, LlmRateLimitError, LlmSafetyError, LlmGenerationError, Exception.
        """
        if not self.client:
             raise LlmConfigurationError("OpenAI client not initialized.", provider=self._get_api_name())

        model_name = kwargs.get("model", getattr(self, "default_model", self.DEFAULT_MODEL))
        logger = get_logger(__name__, model=model_name, component=self._get_api_name())
        logger.debug("Requesting chat completion...")

        params = self._prepare_chat_params(**kwargs)

        try:
            response = self.client.chat.completions.create(
                model=model_name,
                messages=messages,
                max_tokens=params.get("max_tokens"),
                temperature=params.get("temperature")
                # Add other params like top_p if needed
            )

            # Check for content filtering or other issues
            finish_reason = response.choices[0].finish_reason
            if finish_reason == "content_filter":
                logger.warning("Chat completion blocked by content filter.")
                raise LlmSafetyError("OpenAI: Response blocked by content filter.", provider=self._get_api_name())
            elif finish_reason == "length":
                 logger.warning("Chat completion truncated due to max_tokens limit.")
                 # Return truncated content but log warning

            generated_content = response.choices[0].message.content
            if not generated_content:
                 logger.warning(f"Chat completion returned empty content. Finish reason: {finish_reason}")
                 raise LlmGenerationError("OpenAI API returned empty content.", provider=self._get_api_name())

            logger.info(f"Chat completion successful. Finish reason: {finish_reason}")
            return generated_content.strip()

        except AuthenticationError as e:
            logger.error(f"Authentication error: {e}")
            raise LlmAuthenticationError(f"OpenAI: {e}", provider=self._get_api_name()) from e
        except RateLimitError as e:
            logger.warning(f"Rate limit error: {e}")
            raise LlmRateLimitError(f"OpenAI: {e}", provider=self._get_api_name()) from e
        # except BadRequestError as e: # Example for content filter error
        #     if "content_filter" in str(e).lower():
        #          logger.warning(f"Content filter error: {e}")
        #          raise LlmSafetyError(f"OpenAI: {e}", provider=self._get_api_name()) from e
        #     else:
        #          logger.error(f"Bad request error: {e}")
        #          raise LlmGenerationError(f"OpenAI Bad Request: {e}", provider=self._get_api_name()) from e
        except (APIConnectionError, APIError, OpenAIError) as e:
            logger.error(f"API call failed: {e}")
            raise LlmGenerationError(f"OpenAI API Error: {e}", provider=self._get_api_name()) from e
        except Exception as e:
            logger.error(f"Unexpected error during chat completion: {e}", exc_info=True)
            raise LlmGenerationError(f"Unexpected error during OpenAI chat completion: {e}", provider=self._get_api_name()) from e


    def get_embedding(self, text: str, **kwargs) -> List[float]:
        """
        Generates an embedding vector for the given text using OpenAI.
        (Placeholder implementation)
        """
        if not self.client:
             raise LlmConfigurationError("OpenAI client not initialized.", provider=self._get_api_name())

        # Example: model = kwargs.get("model", "text-embedding-ada-002")
        # response = self.client.embeddings.create(input=[text], model=model)
        # return response.data[0].embedding
        self.logger.warning("get_embedding is not yet fully implemented for OpenAI.")
        raise NotImplementedError("OpenAI get_embedding is not implemented.")

    def _get_retryable_errors(self) -> Tuple[Type[Exception], ...]:
        """Return OpenAI-specific retryable errors mapped to our exceptions."""
        # Map OpenAI SDK errors to our custom exception types
        # APIConnectionError is typically retryable
        return (LlmRateLimitError, APIConnectionError)
