# migrations/runner.py
import os
import importlib.util
import logging
import sys

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import get_db, close_db
from flask import current_app

def run_migrations():
    """
    Discovers and applies database migrations.
    """
    log_prefix = "[DB:Migrate]"
    logging.info(f"{log_prefix} Starting migration process...")

    try:
        db = get_db()
        cursor = db.cursor()

        # 1. Create schema_migrations table if it doesn't exist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(255) PRIMARY KEY
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """)
        db.commit()

        # 2. Get the list of applied migrations
        cursor.execute("SELECT version FROM schema_migrations")
        applied_migrations = {row[0] for row in cursor.fetchall()}
        logging.info(f"{log_prefix} Found {len(applied_migrations)} applied migrations.")

        # 3. Discover and sort migration files
        migrations_dir = os.path.dirname(__file__)
        migration_files = sorted([
            f for f in os.listdir(migrations_dir)
            if f.startswith('V') and f.endswith('.py')
        ])

        # 4. Apply pending migrations
        for filename in migration_files:
            version = filename.split('__')[0]
            if version not in applied_migrations:
                logging.info(f"{log_prefix} Applying migration: {filename}...")
                try:
                    # Dynamically import the migration module
                    module_name = filename[:-3]
                    file_path = os.path.join(migrations_dir, filename)
                    spec = importlib.util.spec_from_file_location(module_name, file_path)
                    migration_module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(migration_module)

                    # Run the upgrade or up function
                    if hasattr(migration_module, 'upgrade'):
                        migration_module.upgrade(db)
                    elif hasattr(migration_module, 'up'):
                        migration_module.up()
                    else:
                        raise AttributeError(f"Migration module {filename} has no 'upgrade' or 'up' function.")
                    
                    # Record the migration
                    cursor.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                    db.commit()
                    logging.info(f"{log_prefix} Successfully applied and recorded migration: {version}")

                except Exception as e:
                    logging.error(f"{log_prefix} Error applying migration {filename}: {e}", exc_info=True)
                    db.rollback()
                    raise
            else:
                logging.info(f"{log_prefix} Migration already applied: {filename}")

        logging.info(f"{log_prefix} Migration process finished successfully.")

    except Exception as e:
        logging.critical(f"{log_prefix} A critical error occurred during the migration process: {e}", exc_info=True)
        # A migration failure must abort the caller. In particular, bootstrap
        # must not create its initialized marker after a partial migration.
        raise
    finally:
        # The close_db function will be called on app context teardown
        pass

if __name__ == '__main__':
    # This allows running the script directly for manual migrations,
    # but requires a Flask app context to be created first.
    from app import create_app
    
    logging.basicConfig(level=logging.INFO)
    
    app = create_app()
    with app.app_context():
        run_migrations()
