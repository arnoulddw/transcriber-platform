import contextlib
from unittest.mock import patch

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


def test_non_transcription_types_do_not_use_identity_fallback():
    prices = {"workflow": {"generic": 0.01}}
    with contextlib.ExitStack() as stack:
        for p in _patched_pricing(prices):
            stack.enter_context(p)
        assert get_price("workflow", "Generic") is None
