"""Create the durable background-job queue for existing installations."""


def upgrade(db):
    """Bring an existing database to the queue schema without model startup repairs."""
    cursor = db.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS background_jobs (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            task_type VARCHAR(40) NOT NULL,
            payload JSON NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            attempts INT UNSIGNED NOT NULL DEFAULT 0,
            max_attempts INT UNSIGNED NOT NULL DEFAULT 3,
            available_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            claimed_at DATETIME(6) NULL DEFAULT NULL,
            claimed_by VARCHAR(128) NULL DEFAULT NULL,
            last_error TEXT NULL,
            created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            completed_at DATETIME(6) NULL DEFAULT NULL,
            INDEX idx_background_jobs_claim (status, available_at, id),
            INDEX idx_background_jobs_stale (status, claimed_at),
            INDEX idx_background_jobs_type (task_type, status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    db.commit()
