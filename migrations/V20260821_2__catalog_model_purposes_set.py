"""Convert the catalog's single model_purpose into a comma purpose set.

A catalog model identity (provider_code, code) must be able to serve both
``transcription`` and ``live`` at once. Previously the single-valued
``model_purpose`` column was overwritten whenever a key was saved for the
other purpose (e.g. saving ``openai:gpt-transcribe`` for live silently
removed it from the file-transcription dropdowns).

This migration:
  1. Adds ``model_purposes VARCHAR(64)`` (same shape as
     ``user_api_keys.model_purposes``).
  2. Merges any legacy ``model_purpose`` value into the new set.
  3. Merges all known purposes from saved ``user_api_keys`` rows into
     matching catalog models (repairs rows clobbered by earlier saves).
  4. Drops the legacy ``model_purpose`` column.

Safe to run multiple times and safe on databases where the application's
defensive startup path already performed the conversion.
"""

from __future__ import annotations


MODELS_TABLE = "transcription_models_catalog"
KEYS_TABLE = "user_api_keys"

VALID_PURPOSES = ("live", "transcription")


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute("SHOW TABLES LIKE %s", (table_name,))
    return cursor.fetchone() is not None


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s", (column_name,))
    return cursor.fetchone() is not None


def _canonical_set(raw_values) -> str:
    """Merge raw purpose values into a canonical comma string.

    Fixed order (``transcription`` first) mirrors
    ``transcription_catalog.canonicalize_model_purposes`` so both conversion
    paths converge on identical stored values.
    """
    purposes = set()
    for raw in raw_values:
        if raw is None:
            continue
        for item in str(raw).split(","):
            cleaned = item.strip().lower()
            if cleaned in VALID_PURPOSES:
                purposes.add(cleaned)
    if not purposes:
        purposes = {"transcription"}
    return ",".join(
        purpose for purpose in ("transcription", "live") if purpose in purposes
    )


def _merge_legacy_column(cursor) -> None:
    """Fold every legacy ``model_purpose`` value into ``model_purposes``."""
    cursor.execute(
        f"SELECT id, model_purpose, model_purposes FROM {MODELS_TABLE}"
    )
    rows = cursor.fetchall() or []
    for row in rows:
        if isinstance(row, dict):
            row_id = row.get("id")
            values = (row.get("model_purpose"), row.get("model_purposes"))
        else:
            row_id = row[0]
            values = (row[1], row[2])
        merged = _canonical_set(values)
        cursor.execute(
            f"UPDATE {MODELS_TABLE} SET model_purposes = %s WHERE id = %s",
            (merged, row_id),
        )


def _merge_purposes_from_user_keys(cursor) -> None:
    """Merge every known purpose from saved keys into catalog rows.

    This repairs a row independently even when the app's defensive startup
    backfill has not run yet: the exact old failure left the catalog row as
    ``live`` while the key row still correctly contained
    ``transcription,live``.
    """
    if not _table_exists(cursor, KEYS_TABLE):
        return
    cursor.execute(
        f"""
        SELECT DISTINCT provider_code, TRIM(model_slug) AS model_slug, model_purposes
        FROM {KEYS_TABLE}
        WHERE model_slug IS NOT NULL AND TRIM(model_slug) <> ''
        """
    )
    rows = cursor.fetchall() or []
    for row in rows:
        entry = row if isinstance(row, dict) else {
            "provider_code": row[0], "model_slug": row[1], "model_purposes": row[2],
        }
        provider = str(entry.get("provider_code") or "").strip().lower()
        slug = str(entry.get("model_slug") or "").strip()
        if not provider or not slug:
            continue
        if provider == "assemblyai" and slug.casefold() == "assemblyai":
            slug = "universal"
        cursor.execute(
            f"""
            SELECT id, model_purposes FROM {MODELS_TABLE}
            WHERE code = %s
              AND COALESCE(NULLIF(provider_code, ''), required_api_key) = %s
            """,
            (slug, provider),
        )
        matches = cursor.fetchall() or []
        for match in matches:
            if isinstance(match, dict):
                row_id = match.get("id")
                current = match.get("model_purposes")
            else:
                row_id, current = match[0], match[1]
            merged = _canonical_set((current, entry.get("model_purposes")))
            if merged != str(current or ""):
                cursor.execute(
                    f"UPDATE {MODELS_TABLE} SET model_purposes = %s WHERE id = %s",
                    (merged, row_id),
                )


def upgrade(db) -> None:
    cursor = db.cursor()
    try:
        if not _table_exists(cursor, MODELS_TABLE):
            # Nothing to migrate; the app's schema-ensure creates the new shape.
            return

        legacy_present = _column_exists(cursor, MODELS_TABLE, "model_purpose")
        set_present = _column_exists(cursor, MODELS_TABLE, "model_purposes")

        if not set_present:
            cursor.execute(
                f"""
                ALTER TABLE {MODELS_TABLE}
                ADD COLUMN model_purposes VARCHAR(64) NOT NULL DEFAULT 'transcription'
                AFTER is_default
                """
            )

        if legacy_present:
            _merge_legacy_column(cursor)

        _merge_purposes_from_user_keys(cursor)

        if legacy_present:
            cursor.execute(
                f"ALTER TABLE {MODELS_TABLE} DROP COLUMN model_purpose"
            )

        db.commit()
    finally:
        cursor.close()
