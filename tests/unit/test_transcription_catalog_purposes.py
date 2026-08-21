"""Unit tests for catalog model-purpose canonicalization (Task 1)."""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.models.transcription_catalog import (  # noqa: E402
    VALID_MODEL_PURPOSES,
    canonicalize_model_purposes,
)


def test_canonicalizes_and_normalizes_a_mixed_string():
    assert canonicalize_model_purposes("Live, Transcription") == "transcription,live"


def test_accepts_a_list_of_purposes():
    assert canonicalize_model_purposes(["live"]) == "live"
    assert canonicalize_model_purposes(["live", "transcription"]) == "transcription,live"


def test_defaults_to_transcription_when_empty():
    assert canonicalize_model_purposes(None) == "transcription"
    assert canonicalize_model_purposes("") == "transcription"
    assert canonicalize_model_purposes("   ") == "transcription"


def test_drops_unknown_purposes():
    assert canonicalize_model_purposes("live,bogus") == "live"
    assert canonicalize_model_purposes("bogus") == "transcription"


def test_deduplicates_purposes():
    assert canonicalize_model_purposes("live,live,transcription") == "transcription,live"


def test_valid_purposes_are_the_two_known_kinds():
    assert VALID_MODEL_PURPOSES == {"transcription", "live"}
