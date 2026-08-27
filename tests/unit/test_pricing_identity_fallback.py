import contextlib
from unittest.mock import patch

from app.models import transcription_catalog
from app.services import pricing_service
from app.services.pricing_service import get_price


def _patched_pricing(prices):
    """Patch both the exact row lookup and the full-table scan."""

    def fake_get_price(item_key, item_type):
        return prices.get(item_type, {}).get(item_key)

    return [
        patch.object(pricing_service.pricing_model, "get_price", side_effect=fake_get_price),
        patch.object(pricing_service.pricing_model, "get_all_prices", return_value=prices),
    ]


def test_exact_lookup_still_wins():
    prices = {
        "transcription": {
            "openrouter:openai/whisper-large-v3": 0.10,
            "openai/whisper-large-v3": 0.20,
        }
    }
    with contextlib.ExitStack() as stack:
        for p in _patched_pricing(prices):
            stack.enter_context(p)
        assert get_price("transcription", "openai/whisper-large-v3") == 0.20


def test_vendor_suffix_fallback_resolves_unique_match():
    prices = {"transcription": {"openrouter:openai/whisper-large-v3": 0.10}}
    with contextlib.ExitStack() as stack:
        for p in _patched_pricing(prices):
            stack.enter_context(p)
        assert get_price("transcription", "openai/whisper-large-v3") == 0.10


def test_tail_fallback_resolves_unique_match():
    prices = {"transcription": {"openrouter:gpt-4o-mini": 0.05}}
    with contextlib.ExitStack() as stack:
        for p in _patched_pricing(prices):
            stack.enter_context(p)
        assert get_price("transcription", "gpt-4o-mini") == 0.05


def test_ambiguous_fallback_returns_none():
    prices = {
        "transcription": {
            "openrouter:vendor-a/whisper": 0.10,
            "openrouter:vendor-b/whisper": 0.20,
        }
    }
    with contextlib.ExitStack() as stack:
        for p in _patched_pricing(prices):
            stack.enter_context(p)
        # Two different prices for the same identity: never guess.
        assert get_price("transcription", "whisper") is None


def test_no_match_returns_none():
    prices = {"transcription": {"openrouter:openai/whisper-large-v3": 0.10}}
    with contextlib.ExitStack() as stack:
        for p in _patched_pricing(prices):
            stack.enter_context(p)
        assert get_price("transcription", "totally-unrelated-model") is None


def test_non_transcription_types_use_identity_fallback_too():
    prices = {"workflow": {"openrouter:google/gemini-3.7-flash": 0.01}}
    with contextlib.ExitStack() as stack:
        for p in _patched_pricing(prices):
            stack.enter_context(p)
        # LLM types resolve by identity the same way transcription does.
        assert get_price("workflow", "google/gemini-3.7-flash") == 0.01


def test_non_transcription_ambiguous_fallback_returns_none():
    prices = {
        "workflow": {
            "openrouter:vendor-a/gemma": 0.01,
            "openrouter:vendor-b/gemma": 0.02,
        }
    }
    with contextlib.ExitStack() as stack:
        for p in _patched_pricing(prices):
            stack.enter_context(p)
        # Two different prices for the same identity: never guess.
        assert get_price("workflow", "gemma") is None


def test_gemini_identity_flows_through_build_model_options():
    """A saved gemini key yields an admin-costs option keyed 'gemini:<code>'
    via the same catalog-driven path as every other provider (no DB needed:
    build_model_options consumes plain dicts like build_pricing_options does)."""
    gemini_row = {
        "code": "gemini-3.5-transcribe",
        "display_name": "Gemini 3.5 Transcribe",
        "provider_code": "gemini",
        "required_api_key": "gemini",
        "permission_key": "use_api_google_gemini",
        "model_purposes": ["transcription"],
    }
    key_status = {
        "provider_keys": {
            "gemini": [{"model_name": "gemini-3.5-transcribe", "model_purposes": ["transcription"]}],
        },
    }

    options = transcription_catalog.build_model_options([gemini_row], key_status)

    assert [entry["model_key"] for entry in options] == ["gemini:gemini-3.5-transcribe"]
    assert options[0]["display_name"] == "Gemini 3.5 Transcribe"
