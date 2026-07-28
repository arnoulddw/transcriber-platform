# app/services/api_clients/__init__.py
# This file makes the 'api_clients' directory a Python package
# within the 'services' package.
# It also contains factory methods for creating API client instances.

import logging
# --- MODIFIED: Import Dict ---
from typing import Union, Dict, Any # To type hint the return value
# --- END MODIFIED ---

# Import Base Classes (Optional, but good for type hinting)
from .transcription.base_transcription_client import BaseTranscriptionClient
from .llm.base_llm_client import BaseLLMClient

# Import Specific Client Implementations
from .transcription.assemblyai import AssemblyAITranscriptionAPI
from .transcription.openai_whisper import OpenAIWhisperTranscriptionAPI
from .transcription.openai_gpt_4o_transcribe import OpenAIGPT4OTranscribeClient
from .transcription.openai_gpt_transcribe import OpenAIGPTTranscribeClient
from .llm.gemini_client import GeminiClient
from .llm.openai_client import OpenAIClient

# Import Custom Exceptions
from .exceptions import ApiClientError, TranscriptionApiError, LlmApiError, TranscriptionConfigurationError, LlmConfigurationError # Added missing imports

# --- Factory Methods ---

def get_transcription_client(provider_name: str, api_key: str, config: Dict[str, Any]) -> BaseTranscriptionClient:
    """
    Factory method to get the appropriate transcription client instance.

    Args:
        provider_name: The name of the transcription provider (e.g., "assemblyai", "whisper", "gpt-transcribe").
        api_key: The API key for the provider.
        config: The Flask application configuration dictionary.

    Returns:
        An instance of a class derived from BaseTranscriptionClient.

    Raises:
        ValueError: If the provider_name is unsupported or api_key is missing.
        TranscriptionConfigurationError: If client initialization fails.
    """
    logging.debug(f"[API Factory] Requesting transcription client for provider: {provider_name}")
    if not api_key:
        raise ValueError(f"API key is required to initialize the '{provider_name}' transcription client.")

    try:
        if provider_name == "assemblyai":
            return AssemblyAITranscriptionAPI(api_key, config)
        elif provider_name == "whisper":
            return OpenAIWhisperTranscriptionAPI(api_key, config)
        elif provider_name == "gpt-4o-transcribe":
            return OpenAIGPT4OTranscribeClient(api_key, config)
        elif provider_name == "gpt-transcribe":
            return OpenAIGPTTranscribeClient(api_key, config)
        else:
            logging.error(f"[API Factory] Unsupported transcription provider requested: {provider_name}")
            raise ValueError(f"Unsupported transcription provider: {provider_name}")
    except ValueError as ve: # Catch API key missing error or unsupported provider
        raise ve
    except Exception as e: # Catch initialization errors from client constructors (which should raise TranscriptionConfigurationError)
        logging.error(f"[API Factory] Failed to initialize transcription client for '{provider_name}': {e}", exc_info=True)
        # Re-raise the original error if it's already the correct type, otherwise wrap it
        if isinstance(e, TranscriptionApiError):
            raise e
        else:
            # This case might occur if the constructor raises an unexpected error type
            raise TranscriptionConfigurationError(f"Failed to initialize client for {provider_name}: {e}", provider=provider_name) from e

# --- MODIFIED: Accept config dictionary ---
def get_llm_client(provider_name: str, api_key: str, config: Dict[str, Any]) -> BaseLLMClient:
    """
    Factory method to get the appropriate LLM client instance.

    Args:
        provider_name: The name of the LLM provider (e.g., "gemini", "openai").
        api_key: The API key for the provider.
        config: The Flask application configuration dictionary.

    Returns:
        An instance of a class derived from BaseLLMClient.

    Raises:
        ValueError: If the provider_name is unsupported or api_key is missing.
        LlmConfigurationError: If client initialization fails.
    """
    logging.debug(f"[API Factory] Requesting LLM client for provider: {provider_name}")
    if not api_key:
        raise ValueError(f"API key is required to initialize the '{provider_name}' LLM client.")

    try:
        # Allow for model specifics in provider name, e.g., "gemini-1.5-flash"
        provider_lower = provider_name.lower()
        if provider_lower.startswith("gemini"):
            # Pass config to the client constructor
            return GeminiClient(api_key, config)
        elif provider_lower.startswith("openai") or provider_lower.startswith("gpt"): # Allow gpt-* prefix
            # Pass config to the client constructor (even if not used yet)
            return OpenAIClient(api_key, config)
        # Add other LLM providers here
        # elif provider_name.startswith("anthropic") or provider_name.startswith("claude"):
        #     return AnthropicClient(api_key, config)
        else:
            logging.error(f"[API Factory] Unsupported LLM provider requested: {provider_name}")
            raise ValueError(f"Unsupported LLM provider: {provider_name}")
    except ValueError as ve: # Catch API key missing error or unsupported provider
        raise ve
    except Exception as e: # Catch initialization errors from client constructors (which should raise LlmConfigurationError)
        logging.error(f"[API Factory] Failed to initialize LLM client for '{provider_name}': {e}", exc_info=True)
        # Re-raise the original error if it's already the correct type, otherwise wrap it
        if isinstance(e, LlmApiError):
            raise e
        else:
            # This case might occur if the constructor raises an unexpected error type
            raise LlmConfigurationError(f"Failed to initialize client for {provider_name}: {e}", provider=provider_name) from e
# --- END MODIFIED ---

# Union type for convenience if needed elsewhere, though direct use of base classes is often better
ApiClient = Union[BaseTranscriptionClient, BaseLLMClient]
