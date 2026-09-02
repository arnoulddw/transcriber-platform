# app/models/pricing.py
# Defines the Pricing model and database interaction functions for MySQL.

import logging
from typing import Optional, Dict, Any
from mysql.connector import Error as MySQLError
from app.database import get_db, get_cursor

def init_db_command() -> None:
    """Initializes the 'pricing' table schema."""
    cursor = get_cursor()
    log_prefix = "[DB:Schema:MySQL]"
    logging.debug(f"{log_prefix} Checking/Initializing 'pricing' table...")
    try:
        # Ensure the table exists before trying to modify it
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS pricing (
                id INT PRIMARY KEY AUTO_INCREMENT,
                catalog_code VARCHAR(255) NOT NULL,
                price DECIMAL(18, 8) NOT NULL,
                billing_unit ENUM('per_minute', 'per_1k_tokens', 'per_execution') NOT NULL DEFAULT 'per_minute',
                item_type ENUM('transcription', 'workflow', 'title_generation') NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_item_type_code (item_type, catalog_code),
                INDEX idx_catalog_code (catalog_code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            '''
        )
        get_db().commit()
        logging.debug(f"{log_prefix} 'pricing' table schema verified/initialized.")

        cursor.execute("SHOW COLUMNS FROM pricing LIKE 'billing_unit'")
        billing_unit_col = cursor.fetchone()
        cursor.fetchall()
        if not billing_unit_col:
            logging.info(f"{log_prefix} Adding 'billing_unit' column to 'pricing' table.")
            cursor.execute(
                "ALTER TABLE pricing ADD COLUMN billing_unit ENUM('per_minute', 'per_1k_tokens', 'per_execution') NOT NULL DEFAULT 'per_minute' AFTER price"
            )
            cursor.execute(
                "UPDATE pricing SET billing_unit = 'per_1k_tokens' WHERE item_type IN ('workflow', 'title_generation')"
            )

        # Normalize legacy column/index names
        cursor.execute("SHOW COLUMNS FROM pricing LIKE 'item_key'")
        legacy_item_key = cursor.fetchone()
        cursor.fetchall()
        cursor.execute("SHOW COLUMNS FROM pricing LIKE 'catalog_code'")
        catalog_col = cursor.fetchone()
        cursor.fetchall()
        if legacy_item_key and not catalog_col:
            logging.info(f"{log_prefix} Renaming legacy 'item_key' column to 'catalog_code'.")
            cursor.execute("ALTER TABLE pricing CHANGE COLUMN item_key catalog_code VARCHAR(255) NOT NULL")
        try:
            cursor.execute("ALTER TABLE pricing DROP INDEX item_key")
        except MySQLError:
            pass
        try:
            cursor.execute("ALTER TABLE pricing DROP INDEX uq_item_type_key")
        except MySQLError:
            pass
        cursor.execute("SHOW INDEX FROM pricing WHERE Key_name = 'uq_item_type_code'")
        unique_exists = cursor.fetchone()
        cursor.fetchall()
        if not unique_exists:
            logging.info(f"{log_prefix} Ensuring composite unique index on (catalog_code, item_type).")
            cursor.execute("ALTER TABLE pricing ADD UNIQUE INDEX uq_item_type_code (catalog_code, item_type)")
    except MySQLError as err:
        logging.error(f"{log_prefix} Error during 'pricing' table initialization: {err}", exc_info=True)
        get_db().rollback()
        raise
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass

def get_price(item_key: str, item_type: str) -> Optional[float]:
    """Retrieves the price for a given item key and type."""
    # SWAP to ensure correct order
    if item_type not in ['transcription', 'workflow', 'title_generation']:
        item_key, item_type = item_type, item_key

    log_prefix = f"[DB:Pricing:{item_type}:{item_key}]"
    sql = "SELECT price FROM pricing WHERE catalog_code = %s AND item_type = %s ORDER BY updated_at DESC LIMIT 1"
    cursor = get_cursor()
    price = None
    try:
        cursor.execute(sql, (item_key, item_type))
        result = cursor.fetchone()
        if result:
            price = float(result['price'])
    except MySQLError as err:
        logging.error(f"{log_prefix} Error retrieving price: {err}", exc_info=True)
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
    return price


def get_all_prices() -> Dict[str, Any]:
    """Retrieves all prices from the database."""
    log_prefix = "[DB:Pricing]"
    sql = "SELECT catalog_code, price, item_type FROM pricing"
    cursor = get_cursor()
    prices = {}
    try:
        cursor.execute(sql)
        rows = cursor.fetchall()
        for row in rows:
            if row['item_type'] not in prices:
                prices[row['item_type']] = {}
            prices[row['item_type']][row['catalog_code']] = float(row['price'])
    except MySQLError as err:
        logging.error(f"{log_prefix} Error retrieving all prices: {err}", exc_info=True)
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass
    return prices

def update_prices(
    pricing_data: Dict[str, Dict[str, float]],
    billing_units: Optional[Dict[str, str]] = None,
) -> None:
    """
    Updates or inserts prices in the database.
    """
    log_prefix = "[DB:Pricing:Update]"
    sql = """
        INSERT INTO pricing (item_type, catalog_code, price)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE price = VALUES(price)
    """
    cursor = get_cursor()
    try:
        for item_type, models in pricing_data.items():
            for item_key, price in models.items():
                cursor.execute(sql, (item_type, str(item_key).strip(), price))
                if billing_units and item_key in billing_units:
                    cursor.execute(
                        "UPDATE pricing SET billing_unit = %s WHERE item_type = %s AND catalog_code = %s",
                        (billing_units[item_key], item_type, str(item_key).strip())
                    )
        get_db().commit()
        logging.debug(f"{log_prefix} Database prices updated successfully.")
    except MySQLError as err:
        logging.error(f"{log_prefix} Error updating prices in database: {err}", exc_info=True)
        get_db().rollback()
        raise
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass

