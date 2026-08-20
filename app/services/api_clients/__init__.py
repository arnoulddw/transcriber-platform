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
from .transcription.openai_model_client import OpenAIModelTranscriptionClient
from .transcription.openrouter import OpenRouterTranscriptionClient
from .llm.gemini_client import GeminiClient
from .llm.openai_client import OpenAIClient

# Import Custom Exceptions
from .exceptions import ApiClientError, TranscriptionApiError, LlmApiError, TranscriptionConfigurationError, LlmConfigurationError # Added missing imports

# --- Provider resolution ---

# Static fallback used only when no catalog context exists (e.g. unit tests
# without a DB). In the running app the provider comes from the catalog
# row's required_api_key — the single source of truth for dispatch.
_LEGACY_PROVIDER_FALLBACK: Dict[str, str] = {
    "assemblyai": "assemblyai",
    "universal": "assemblyai",
    "whisper": "openai",
    "gpt-4o-transcribe": "openai",
    "gpt-transcribe": "openai",
    "openrouter": "openrouter",
}


def _resolve_transcription_provider(model_code: str) -> str:
    """Resolve the provider bucket for a transcription model code.

    Primary source: the catalog row's ``required_api_key`` (a fixed provider:
    openai / assemblyai / openrouter). Falls back to a static map when no
    catalog context exists (unit tests without a DB) or the row is missing.
    """
    code = str(model_code or "").strip()
    try:
        from app.models import transcription_catalog as catalog_model
        row = catalog_model.get_model_by_code(code)
        if row and row.get("required_api_key"):
            return str(row["required_api_key"]).strip().lower()
    except Exception:
        pass
    if code.startswith("gpt-live-"):  # live models use the OpenAI realtime bucket
        return "openai"
    return _LEGACY_PROVIDER_FALLBACK.get(code, code)


# --- Factory Methods ---

def get_transcription_client(provider_name: str, api_key: str, config: Dict[str, Any]) -> BaseTranscriptionClient:
    """
    Factory method to get the appropriate transcription client instance.

    Args:
        provider_name: The catalog model code (e.g., "whisper", "gpt-transcribe",
            "assemblyai", "openrouter").
        api_key: The API key for the provider.
        config: The Flask application configuration dictionary.

    Returns:
        An instance of a class derived from BaseTranscriptionClient.

    Raises:
        ValueError: If the model code is unsupported or api_key is missing.
        TranscriptionConfigurationError: If client initialization fails.
    """
    logging.debug(f"[API Factory] Requesting transcription client for model: {provider_name}")
    if not api_key:
        raise ValueError(f"API key is required to initialize the '{provider_name}' transcription client.")

    provider = _resolve_transcription_provider(provider_name)
    try:
        if provider == "openai":
            # One parameterized client for every OpenAI-model: the model name
            # and response style come from API_LIMITS[model_code].
            return OpenAIModelTranscriptionClient(provider_name, api_key, config)
        if provider == "assemblyai":
            return AssemblyAITranscriptionAPI(api_key, config, model_code=provider_name)
        if provider == "openrouter":
            return OpenRouterTranscriptionClient(api_key, config)
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
        if provider_lower.startswith("openrouter"):
            routed_config = dict(config)
            routed_config["OPENAI_BASE_URL"] = config.get(
                "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            )
            return OpenAIClient(api_key, routed_config)
        if provider_lower.startswith("gemini"):
            return GeminiClient(api_key, config)
        if provider_lower.startswith("openai") or provider_lower.startswith("gpt"): # Allow gpt-* prefix
            return OpenAIClient(api_key, config)
        # Add other LLM providers here
        # elif provider_name.startswith("anthropic") or provider_name.startswith("claude"):
        #     return AnthropicClient(api_key, config)
        else:
            logging.error(f"[API Factory] Unsupported LLM provider requested: {provider_name}")
            raise ValueError(f"Unsupported LLM provider: {provider_name}")
    except ValueError as ve:
        raise ve
    except Exception as e:
        logging.error(f"[API Factory] Failed to initialize LLM client for '{provider_name}': {e}", exc_info=True)
        if isinstance(e, LlmApiError):
            raise e
        raise LlmConfigurationError(f"Failed to initialize LLM client for {provider_name}: {e}", provider=provider_name) from e