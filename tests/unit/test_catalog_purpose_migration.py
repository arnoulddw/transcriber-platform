"""Migration-level regression coverage for the catalog purpose-set repair."""
from migrations.V20260821_2__catalog_model_purposes_set import (
    _canonical_set,
    _merge_purposes_from_user_keys,
)


class _ScriptedCursor:
    def __init__(self, fetchone_values, fetchall_values):
        self.fetchone_values = iter(fetchone_values)
        self.fetchall_values = iter(fetchall_values)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return next(self.fetchone_values)

    def fetchall(self):
        return next(self.fetchall_values)


def test_migration_repairs_a_clobbered_live_row_from_the_key_purpose_set():
    """A live-only catalog row plus a dual-purpose key becomes dual-purpose."""
    cursor = _ScriptedCursor(
        fetchone_values=[object()],  # user_api_keys table exists
        fetchall_values=[
            [
                {
                    "provider_code": "openai",
                    "model_slug": "gpt-transcribe",
                    "model_purposes": "transcription,live",
                }
            ],
            [{"id": 17, "model_purposes": "live"}],
        ],
    )

    _merge_purposes_from_user_keys(cursor)

    assert cursor.calls[-1][1] == ("transcription,live", 17)
    assert "SET model_purposes = %s" in cursor.calls[-1][0]


def test_migration_skips_llm_only_saved_keys():
    """Migration must not treat an LLM key as a transcription key."""
    cursor = _ScriptedCursor(
        fetchone_values=[object()],  # user_api_keys table exists
        fetchall_values=[
            [
                {
                    "provider_code": "openai",
                    "model_slug": "gpt-4o-mini",
                    "model_purposes": "llm",
                }
            ],
        ],
    )

    _merge_purposes_from_user_keys(cursor)

    # SHOW TABLES plus the saved-key query; no catalog-row lookup is needed.
    assert len(cursor.calls) == 2


def test_migration_canonical_set_drops_unknown_values_and_preserves_both_known_ones():
    assert _canonical_set(("live,bogus", "transcription")) == "transcription,live"
