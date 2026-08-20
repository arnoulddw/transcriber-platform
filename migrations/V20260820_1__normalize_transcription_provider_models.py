"""Separate transcription providers from selectable model records.

The application startup path also runs the same normalization defensively.
This migration exists for installations that run the migration runner
explicitly and is safe to apply more than once before it is recorded.
"""

from __future__ import annotations


PROVIDERS_TABLE = "transcription_providers_catalog"
MODELS_TABLE = "transcription_models_catalog"

PROVIDERS = (
    ("assemblyai", "AssemblyAI", "assemblyai", "use_api_assemblyai", "assemblyai"),
    ("openai", "OpenAI", "openai", "use_api_openai", "openai"),
    ("openrouter", "OpenRouter", "openrouter", "use_api_openrouter", "openrouter"),
)


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute("SHOW TABLES LIKE %s", (table_name,))
    return cursor.fetchone() is not None


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s", (column_name,))
    return cursor.fetchone() is not None


def _index_exists(cursor, table_name: str, index_name: str) -> bool:
    cursor.execute(
        f"SHOW INDEX FROM {table_name} WHERE Key_name = %s",
        (index_name,),
    )
    return cursor.fetchone() is not None


def _ensure_schema(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PROVIDERS_TABLE} (
            provider_code VARCHAR(80) PRIMARY KEY,
            display_name VARCHAR(120) NOT NULL,
            required_api_key VARCHAR(80) NOT NULL,
            permission_key VARCHAR(120) DEFAULT NULL,
            client_kind VARCHAR(80) NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MODELS_TABLE} (
            id INT PRIMARY KEY AUTO_INCREMENT,
            code VARCHAR(255) NOT NULL,
            provider_code VARCHAR(80) DEFAULT NULL,
            display_name VARCHAR(120) NOT NULL,
            permission_key VARCHAR(120) DEFAULT NULL,
            required_api_key VARCHAR(80) DEFAULT NULL,
            sort_order INT NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            model_purpose VARCHAR(20) NOT NULL DEFAULT 'transcription',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_transcription_provider_model (provider_code, code)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    if not _column_exists(cursor, MODELS_TABLE, "provider_code"):
        cursor.execute(
            f"ALTER TABLE {MODELS_TABLE} ADD COLUMN provider_code VARCHAR(80) DEFAULT NULL AFTER code"
        )
    if not _column_exists(cursor, MODELS_TABLE, "model_purpose"):
        cursor.execute(
            f"ALTER TABLE {MODELS_TABLE} ADD COLUMN model_purpose VARCHAR(20) NOT NULL DEFAULT 'transcription' AFTER is_default"
        )
    if not _index_exists(cursor, MODELS_TABLE, "idx_transcription_models_provider"):
        cursor.execute(
            f"ALTER TABLE {MODELS_TABLE} ADD INDEX idx_transcription_models_provider (provider_code)"
        )


def _seed_providers(cursor) -> None:
    for row in PROVIDERS:
        cursor.execute(
            f"""
            INSERT INTO {PROVIDERS_TABLE} (
                provider_code, display_name, required_api_key,
                permission_key, client_kind, is_active
            ) VALUES (%s, %s, %s, %s, %s, TRUE)
            ON DUPLICATE KEY UPDATE
                display_name = VALUES(display_name),
                required_api_key = VALUES(required_api_key),
                permission_key = VALUES(permission_key),
                client_kind = VALUES(client_kind),
                is_active = TRUE
            """,
            row,
        )


def _ensure_model_identity_schema(cursor) -> None:
    """Allow provider-local model codes while preserving safe uniqueness."""
    cursor.execute(f"SHOW COLUMNS FROM {MODELS_TABLE} LIKE 'code'")
    column = cursor.fetchone()
    column_type = column.get("Type", "") if isinstance(column, dict) else (column[1] if column and len(column) > 1 else "")
    if "255" not in str(column_type):
        cursor.execute(f"ALTER TABLE {MODELS_TABLE} MODIFY COLUMN code VARCHAR(255) NOT NULL")

    cursor.execute(f"SHOW INDEX FROM {MODELS_TABLE}")
    rows = cursor.fetchall() or []
    grouped = {}
    unique_names = set()
    for row in rows:
        if isinstance(row, dict):
            name = str(row.get("Key_name") or "")
            column_name = str(row.get("Column_name") or "")
            non_unique = row.get("Non_unique")
        else:
            name = str(row[2]) if len(row) > 2 else ""
            column_name = str(row[4]) if len(row) > 4 else ""
            non_unique = row[1] if len(row) > 1 else 1
        if not name:
            continue
        grouped.setdefault(name, []).append(column_name)
        if not bool(non_unique):
            unique_names.add(name)

    for name, columns in grouped.items():
        if name != "PRIMARY" and name in unique_names and columns == ["code"]:
            safe_name = "".join(char for char in name if char.isalnum() or char in "_$")
            if safe_name:
                cursor.execute(f"ALTER TABLE {MODELS_TABLE} DROP INDEX `{safe_name}`")

    if not _index_exists(cursor, MODELS_TABLE, "uq_transcription_provider_model"):
        cursor.execute(
            f"ALTER TABLE {MODELS_TABLE} ADD UNIQUE INDEX uq_transcription_provider_model (provider_code, code)"
        )


def _widen_reference_columns(cursor) -> None:
    """Make room for provider-qualified keys on existing installations."""
    for table_name in ("users", "roles"):
        if not _table_exists(cursor, table_name):
            continue
        for column_name in ("default_transcription_model", "default_live_transcription_model"):
            if not _column_exists(cursor, table_name, column_name):
                continue
            cursor.execute(f"ALTER TABLE {table_name} MODIFY COLUMN {column_name} VARCHAR(255) DEFAULT NULL")


def _normalize_catalog_rows(cursor) -> None:
    cursor.execute(
        f"""
        UPDATE {MODELS_TABLE}
        SET provider_code = LOWER(required_api_key)
        WHERE (provider_code IS NULL OR TRIM(provider_code) = '')
          AND LOWER(COALESCE(required_api_key, '')) IN ('openai', 'assemblyai', 'openrouter')
        """
    )
    cursor.execute(
        f"""
        UPDATE {MODELS_TABLE}
        SET provider_code = 'openrouter'
        WHERE (provider_code IS NULL OR TRIM(provider_code) = '')
          AND code LIKE '%/%'
        """
    )
    cursor.execute(
        f"""
        UPDATE {MODELS_TABLE} AS m
        INNER JOIN {PROVIDERS_TABLE} AS p ON p.provider_code = m.provider_code
        SET m.permission_key = COALESCE(m.permission_key, p.permission_key),
            m.required_api_key = COALESCE(m.required_api_key, p.required_api_key)
        WHERE m.provider_code IN ('openai', 'assemblyai', 'openrouter')
        """
    )
    cursor.execute(
        f"""
        UPDATE {MODELS_TABLE}
        SET provider_code = 'assemblyai',
            permission_key = 'use_api_assemblyai',
            required_api_key = 'assemblyai',
            display_name = CASE
                WHEN LOWER(TRIM(display_name)) IN ('assemblyai', 'universal')
                THEN 'AssemblyAI Universal'
                ELSE display_name
            END
        WHERE LOWER(code) = 'universal'
          AND (provider_code IS NULL OR TRIM(provider_code) = ''
               OR LOWER(COALESCE(required_api_key, '')) = 'assemblyai')
        """
    )
    cursor.execute(
        f"""
        INSERT INTO {MODELS_TABLE} (
            code, provider_code, display_name, permission_key, required_api_key,
            sort_order, is_active, is_default, model_purpose
        )
        SELECT 'universal', 'assemblyai', 'AssemblyAI Universal',
               'use_api_assemblyai', 'assemblyai', 0, TRUE, FALSE, 'transcription'
        FROM DUAL
        WHERE EXISTS (SELECT 1 FROM {MODELS_TABLE} WHERE LOWER(code) = 'assemblyai')
          AND NOT EXISTS (SELECT 1 FROM {MODELS_TABLE} WHERE LOWER(code) = 'universal')
        """
    )
    cursor.execute(
        f"""
        UPDATE {MODELS_TABLE}
        SET is_active = FALSE
        WHERE LOWER(code) IN (
            'openai', 'assemblyai', 'openrouter', 'whisper',
            'gpt-4o-transcribe-diarize'
        )
        """
    )


def _normalize_current_references(cursor) -> None:
    for table_name in ("users", "roles"):
        if not _table_exists(cursor, table_name):
            continue
        cursor.execute(
            f"""
            UPDATE {table_name}
            SET default_transcription_model = 'assemblyai:universal'
            WHERE LOWER(COALESCE(default_transcription_model, '')) IN ('assemblyai', 'universal')
            """
        )
        cursor.execute(
            f"""
            UPDATE {table_name}
            SET default_transcription_model = CASE
                WHEN LOWER(default_openrouter_model) LIKE 'openrouter:%'
                THEN default_openrouter_model
                ELSE CONCAT('openrouter:', default_openrouter_model)
            END
            WHERE LOWER(COALESCE(default_transcription_model, '')) = 'openrouter'
              AND NULLIF(TRIM(COALESCE(default_openrouter_model, '')), '') IS NOT NULL
            """
        )
        cursor.execute(
            f"""
            UPDATE {table_name} AS target
            INNER JOIN {MODELS_TABLE} AS model
                ON model.code = target.default_transcription_model
               AND model.is_active = TRUE
            SET target.default_transcription_model = CONCAT(model.provider_code, ':', model.code)
            WHERE target.default_transcription_model NOT LIKE '%:%'
              AND model.provider_code IS NOT NULL
              AND (
                  SELECT COUNT(*) FROM {MODELS_TABLE} AS candidate
                  WHERE candidate.code = target.default_transcription_model
                    AND candidate.is_active = TRUE
              ) = 1
            """
        )
        cursor.execute(
            f"""
            UPDATE {table_name}
            SET default_transcription_model = NULL
            WHERE LOWER(COALESCE(default_transcription_model, '')) IN (
                'openrouter', 'whisper', 'gpt-4o-transcribe-diarize'
            )
            """
        )

        if _column_exists(cursor, table_name, "default_live_transcription_model"):
            cursor.execute(
                f"""
                UPDATE {table_name}
                SET default_live_transcription_model = 'assemblyai:universal'
                WHERE LOWER(COALESCE(default_live_transcription_model, '')) IN ('assemblyai', 'universal')
                """
            )
            cursor.execute(
                f"""
                UPDATE {table_name} AS target
                INNER JOIN {MODELS_TABLE} AS model
                    ON model.code = target.default_live_transcription_model
                   AND model.is_active = TRUE
                   AND model.model_purpose = 'live'
                SET target.default_live_transcription_model = CONCAT(model.provider_code, ':', model.code)
                WHERE target.default_live_transcription_model NOT LIKE '%:%'
                  AND model.provider_code IS NOT NULL
                  AND (
                      SELECT COUNT(*) FROM {MODELS_TABLE} AS candidate
                      WHERE candidate.code = target.default_live_transcription_model
                        AND candidate.is_active = TRUE
                        AND candidate.model_purpose = 'live'
                  ) = 1
                """
            )

    if _table_exists(cursor, "pricing"):
        cursor.execute(
            """
            INSERT IGNORE INTO pricing (catalog_code, price, item_type)
            SELECT 'assemblyai:universal', price, item_type
            FROM pricing
            WHERE catalog_code IN ('assemblyai', 'universal') AND item_type = 'transcription'
            """
        )


def upgrade(db) -> None:
    cursor = db.cursor()
    try:
        _ensure_schema(cursor)
        _seed_providers(cursor)
        _widen_reference_columns(cursor)
        _normalize_catalog_rows(cursor)
        _ensure_model_identity_schema(cursor)
        _normalize_current_references(cursor)
        db.commit()
    finally:
        cursor.close()
