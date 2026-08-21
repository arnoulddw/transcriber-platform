"""Client defaults stay provider-native.

Neither LLM client may read WORKFLOW_LLM_MODEL (an OpenRouter slug) as its
fallback model: every caller passes the operation's model explicitly, and a
cross-provider default would send an invalid model name whenever one is
omitted.
"""

from unittest.mock import MagicMock, patch

from app.services.api_clients.llm.gemini_client import GeminiClient
from app.services.api_clients.llm.openai_client import OpenAIClient


def _client_config(**overrides):
    config = {"OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1"}
    config.update(overrides)
    return config


def test_gemini_client_default_model_is_provider_native():
    with patch.object(GeminiClient, "_initialize_client", lambda self, key, cfg: None):
        client = GeminiClient("test-key", _client_config(WORKFLOW_LLM_MODEL="google/gemini-3.7-flash"))

    assert client._get_model_name(client.config) == GeminiClient.DEFAULT_MODEL == "gemini-3.0-flash"


def test_openai_client_default_model_is_provider_native():
    with patch("app.services.api_clients.llm.openai_client.OpenAI", MagicMock()), patch(
        "app.services.api_clients.llm.openai_client.OPENAI_AVAILABLE", True
    ):
        client = OpenAIClient("test-key", _client_config(WORKFLOW_LLM_MODEL="google/gemini-3.7-flash"))

    assert client.default_model == OpenAIClient.DEFAULT_MODEL == "gpt-4.1"
