from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.api_clients.exceptions import LlmGenerationError, LlmSafetyError
from app.services.api_clients.llm.gemini_client import GeminiClient


def _make_client_with_response(response):
    with patch.object(GeminiClient, "_initialize_client", lambda self, key, config: None):
        client = GeminiClient("test-key", {"WORKFLOW_MAX_OUTPUT_TOKENS": 128})
    client.model_name = client.DEFAULT_MODEL
    generate_content = MagicMock(return_value=response)
    client.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    return client, generate_content


def test_explicit_prompt_block_with_empty_text_is_a_safety_error():
    response = SimpleNamespace(
        text=None,
        prompt_feedback=SimpleNamespace(
            block_reason=SimpleNamespace(name="PROHIBITED_CONTENT")
        ),
    )
    client, generate_content = _make_client_with_response(response)

    with pytest.raises(LlmSafetyError, match="PROHIBITED_CONTENT"):
        client.generate_text("blocked prompt", model="gemma-test")

    config = generate_content.call_args.kwargs["config"]
    assert config.automatic_function_calling.disable is True


def test_empty_unblocked_response_remains_a_generation_error():
    response = SimpleNamespace(text=None, prompt_feedback=None)
    client, _ = _make_client_with_response(response)

    with pytest.raises(LlmGenerationError, match="empty result"):
        client.generate_text("ordinary prompt", model="gemma-test")
