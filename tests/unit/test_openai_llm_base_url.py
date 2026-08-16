from unittest.mock import patch
from app.services.api_clients.llm.openai_client import OpenAIClient


def test_openai_client_passes_base_url_when_configured():
    with patch("app.services.api_clients.llm.openai_client.OpenAI") as mock_openai:
        OpenAIClient("sk-test", {"OPENAI_BASE_URL": "https://openrouter.ai/api/v1"})
    mock_openai.assert_called_once()
    kwargs = mock_openai.call_args.kwargs
    assert kwargs["api_key"] == "sk-test"
    assert kwargs["base_url"] == "https://openrouter.ai/api/v1"


def test_openai_client_omits_base_url_by_default():
    with patch("app.services.api_clients.llm.openai_client.OpenAI") as mock_openai:
        OpenAIClient("sk-test", {})
    kwargs = mock_openai.call_args.kwargs
    assert "base_url" not in kwargs
