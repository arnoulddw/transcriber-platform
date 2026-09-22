"""Add durable state for signed live transcription sessions."""


def upgrade(db) -> None:
    cursor = db.cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS live_transcription_sessions (
                session_id VARCHAR(64) PRIMARY KEY,
                user_id INT NOT NULL,
                transcription_id VARCHAR(36) NOT NULL UNIQUE,
                provider VARCHAR(32) NOT NULL,
                model VARCHAR(255) NOT NULL,
                transport VARCHAR(32) NOT NULL,
                started_at DOUBLE NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'active',
                version INT UNSIGNED NOT NULL DEFAULT 1,
                last_sequence BIGINT NOT NULL DEFAULT -1,
                last_transcript MEDIUMTEXT NULL,
                created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                hangup_at DATETIME(6) NULL DEFAULT NULL,
                hangup_completed_at DATETIME(6) NULL DEFAULT NULL,
                finalized_at DATETIME(6) NULL DEFAULT NULL,
                revoked_at DATETIME(6) NULL DEFAULT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
                INDEX idx_live_sessions_user_status (user_id, status),
                INDEX idx_live_sessions_status_created (status, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        # The table may have been created by an earlier development revision
        # of this migration. Keep upgrades safe for that already-created table.
        cursor.execute(
            "SHOW COLUMNS FROM live_transcription_sessions LIKE %s",
            ("hangup_completed_at",),
        )
        if cursor.fetchone() is None:
            cursor.execute(
                """
                ALTER TABLE live_transcription_sessions
                ADD COLUMN hangup_completed_at DATETIME(6) NULL DEFAULT NULL
                AFTER hangup_at
                """
            )
    finally:
        cursor.close()
