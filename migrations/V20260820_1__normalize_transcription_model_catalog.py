"""Normalize legacy transcription provider rows and AssemblyAI keys.

The catalog is the source of truth for selectable transcription models. This
migration keeps historical usage/pricing rows intact while removing provider
labels and retired models from active catalog data.
"""

import logging


logger = logging.getLogger(__name__)


def upgrade(db) -> None:
    """Apply the catalog cleanup; every statement is safe to run once only."""
    cursor = db.cursor()
    try:
        # AssemblyAI's provider-wide legacy key represented the Universal
        # speech model. Create the real model row before moving key records.
        cursor.execute(
            """
            INSERT INTO transcription_models_catalog (
                code, display_name, permission_key, required_api_key,
                sort_order, is_active, is_default, model_purpose
            )
            VALUES (
                'universal', 'AssemblyAI Universal', 'use_api_assemblyai',
                'assemblyai', 0, TRUE, FALSE, 'transcription'
            )
            ON DUPLICATE KEY UPDATE
                display_name = IF(
                    LOWER(TRIM(display_name)) IN ('', 'assemblyai', 'universal'),
                    VALUES(display_name),
                    display_name
                ),
                permission_key = VALUES(permission_key),
                required_api_key = VALUES(required_api_key),
                is_active = TRUE,
                model_purpose = 'transcription'
            """
        )

        # Prefer an already-scoped Universal key when a user has both a legacy
        # provider-wide/provider-label row and the new row. The unique index on
        # (user_id, provider_code, model_slug) then makes the following updates
        # idempotent and collision-free.
        cursor.execute(
            """
            DELETE legacy
            FROM user_api_keys AS legacy
            INNER JOIN user_api_keys AS universal
                ON universal.user_id = legacy.user_id
               AND universal.provider_code = 'assemblyai'
               AND universal.model_slug = 'universal'
            WHERE legacy.provider_code = 'assemblyai'
              AND LOWER(TRIM(legacy.model_slug)) IN ('', 'assemblyai')
              AND legacy.id <> universal.id
            """
        )
        cursor.execute(
            """
            DELETE legacy_blank
            FROM user_api_keys AS legacy_blank
            INNER JOIN user_api_keys AS legacy_named
                ON legacy_named.user_id = legacy_blank.user_id
               AND legacy_named.provider_code = 'assemblyai'
               AND LOWER(TRIM(legacy_named.model_slug)) = 'assemblyai'
            WHERE legacy_blank.provider_code = 'assemblyai'
              AND COALESCE(TRIM(legacy_blank.model_slug), '') = ''
              AND legacy_blank.id <> legacy_named.id
            """
        )
        cursor.execute(
            """
            UPDATE user_api_keys
            SET model_slug = 'universal'
            WHERE provider_code = 'assemblyai'
              AND LOWER(TRIM(model_slug)) IN ('', 'assemblyai')
            """
        )

        # Provider labels and retired models must not be active catalog rows.
        # Historical rows remain available for reporting and old job records.
        cursor.execute(
            """
            UPDATE transcription_models_catalog
            SET is_active = FALSE
            WHERE LOWER(code) IN (
                'openai', 'assemblyai', 'whisper',
                'gpt-4o-transcribe-diarize'
            )
            """
        )

        # Defaults pointing at a provider label or retired model are invalid.
        # NULL lets the existing default-selection logic choose the first valid
        # permitted model instead of inventing a replacement.
        cursor.execute(
            """
            UPDATE users
            SET default_transcription_model = CASE
                WHEN LOWER(default_transcription_model) = 'assemblyai' THEN 'universal'
                WHEN LOWER(default_transcription_model) IN (
                    'openai', 'openrouter', 'whisper',
                    'gpt-4o-transcribe-diarize'
                ) THEN NULL
                ELSE default_transcription_model
            END
            WHERE default_transcription_model IS NOT NULL
            """
        )
        cursor.execute(
            """
            UPDATE roles
            SET default_transcription_model = CASE
                WHEN LOWER(default_transcription_model) = 'assemblyai' THEN 'universal'
                WHEN LOWER(default_transcription_model) IN (
                    'openai', 'openrouter', 'whisper',
                    'gpt-4o-transcribe-diarize'
                ) THEN NULL
                ELSE default_transcription_model
            END
            WHERE default_transcription_model IS NOT NULL
            """
        )

        # Preserve the old AssemblyAI price as history, while making the new
        # model code the price used by the Costs page and runtime billing.
        cursor.execute(
            """
            INSERT INTO pricing (item_type, catalog_code, price)
            SELECT item_type, 'universal', price
            FROM pricing
            WHERE item_type = 'transcription'
              AND catalog_code = 'assemblyai'
            ON DUPLICATE KEY UPDATE price = VALUES(price)
            """
        )

        db.commit()
        logger.info("[DB:Migrate] Normalized transcription model catalog and AssemblyAI keys.")
    except Exception:
        db.rollback()
        raise
    finally:
        cursor.close()
