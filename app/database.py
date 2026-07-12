# app/database.py
# Handles database connection setup and teardown for MySQL using connection pooling.

import logging
import mysql.connector # <<< Import MySQL connector
from mysql.connector import pooling, Error, InterfaceError
from flask import g, current_app, Flask # <<< Import Flask for type hinting
from typing import Optional # <<< ADDED THIS IMPORT

# --- Global Connection Pool ---
# Initialize the pool when the module is loaded.
# Requires app context during initialization to get config, so we delay actual pool creation.
db_pool: Optional[pooling.MySQLConnectionPool] = None


def create_standalone_connection(mysql_config: dict):
    """Create a non-pooled connection for connection-scoped MySQL locks."""
    return mysql.connector.connect(
        host=mysql_config['host'],
        port=mysql_config['port'],
        user=mysql_config['user'],
        password=mysql_config['password'],
        database=mysql_config['database'],
        auth_plugin='mysql_native_password',
    )

def init_pool(app: Flask) -> None:
    """Initializes the MySQL connection pool."""
    global db_pool
    if db_pool is not None:
        logging.warning("[DB:Pool] Attempted to initialize connection pool more than once.")
        return

    try:
        mysql_config = app.config['MYSQL_CONFIG']
        safe_config = dict(mysql_config)
        if safe_config.get('password'):
            safe_config['password'] = '***redacted***'
        logging.debug(f"[DB:Pool] Initializing MySQL connection pool with config: {safe_config}")
        db_pool = mysql.connector.pooling.MySQLConnectionPool(
            pool_name=mysql_config['pool_name'],
            pool_size=mysql_config['pool_size'],
            host=mysql_config['host'],
            port=mysql_config['port'],
            user=mysql_config['user'],
            password=mysql_config['password'],
            database=mysql_config['database'],
            # Recommended settings for reliability
            pool_reset_session=True, # Reset session variables on connection release
            auth_plugin='mysql_native_password' # Explicitly set for compatibility
        )
        logging.info("[DB:Pool] MySQL connection pool initialized successfully.")
    except Error as err:
        logging.critical(f"[DB:Pool] Failed to initialize MySQL connection pool: {err}", exc_info=True)
        # Application might not be able to start without a DB pool
        raise RuntimeError(f"Failed to initialize database connection pool: {err}") from err
    except Exception as e:
        logging.critical(f"[DB:Pool] Unexpected error initializing MySQL connection pool: {e}", exc_info=True)
        raise RuntimeError(f"Unexpected error initializing database connection pool: {e}") from e


def get_db() -> mysql.connector.connection.MySQLConnection:
    """
    Gets a connection from the MySQL pool for the current application context.
    Stores the connection in Flask's 'g' object.
    
    For database operations, prefer using get_cursor() to get the dictionary cursor.
    This function is primarily for cases where the connection object itself is needed.
    """
    if db_pool is None:
        logging.critical("[DB:Get] Connection pool is not initialized. Call init_pool() first.")
        raise RuntimeError("Database connection pool not available.")

    if 'db_conn' not in g:
        try:
            # Get connection from the pool
            g.db_conn = db_pool.get_connection()
            # Create a cursor that returns results as dictionaries
            g.db_cursor = g.db_conn.cursor(dictionary=True)
            logging.debug("[DB:Get] Acquired MySQL connection from pool.")
        except Error as err:
            logging.error(f"[DB:Get] Failed to get connection from pool: {err}", exc_info=True)
            # Propagate the error
            raise ConnectionError(f"Failed to get database connection: {err}") from err
        except Exception as e:
            logging.error(f"[DB:Get] Unexpected error getting DB connection: {e}", exc_info=True)
            raise ConnectionError(f"Unexpected error getting database connection: {e}") from e

    # Return the connection object itself, although operations often use the cursor
    return g.db_conn


def get_cursor() -> mysql.connector.cursor.MySQLCursorDict:
    """
    Gets a dictionary cursor for the current application context.
    Creates the connection and cursor if they don't exist.
    """
    # Ensure the connection and cursor are created
    if 'db_cursor' not in g:
        get_db()  # This will initialize g.db_conn and g.db_cursor

    return g.db_cursor


def close_db(e: Optional[Exception] = None) -> None:
    """
    Closes the cursor and returns the connection to the pool.
    Called automatically on application context teardown.
    """
    cursor = g.pop('db_cursor', None)
    conn = g.pop('db_conn', None)

    if cursor is not None:
        try:
            # Consume any unread results to prevent "Unread result found" errors.
            # This ensures the connection is clean before being returned to the pool.
            while cursor.nextset():
                pass
            cursor.close()
            logging.debug("[DB:Close] MySQL cursor closed.")
        except (Error, InterfaceError) as err:
            logging.warning(f"[DB:Close] Error closing cursor (might be already closed or invalid): {err}")
        except Exception as ex:
            logging.error(f"[DB:Close] Unexpected error closing MySQL cursor: {ex}", exc_info=True)

    if conn is not None:
        try:
            # For pooled connections, conn.close() returns it to the pool.
            conn.close()
            logging.debug("[DB:Close] MySQL connection returned to pool.")
        except Error as err:
            logging.error(f"[DB:Close] Error returning MySQL connection to pool: {err}", exc_info=True)
        except Exception as ex:
            logging.error(f"[DB:Close] Unexpected error returning MySQL connection to pool: {ex}", exc_info=True)

    if e is not None:
        logging.debug(f"[DB:Close] Closing DB connection due to exception in request: {e}")


def init_app(app: Flask) -> None:
    """
    Initializes the connection pool and registers DB functions with the Flask app.
    Called by the application factory in __init__.py.
    """
    # Initialize the pool using the app's config
    init_pool(app)

    # Register the close_db function to be called when the app context tears down
    app.teardown_appcontext(close_db)
    logging.debug("[DB] Database close function registered with Flask app.")
    # Note: Database schema initialization (creating tables) is handled
    # by the 'init-db' CLI command or automatic initialization sequence.
