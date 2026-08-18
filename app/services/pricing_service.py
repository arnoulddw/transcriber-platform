# app/services/pricing_service.py
# Contains business logic for managing pricing.

from flask import current_app
import logging
from typing import Dict, Any, Optional
from app.models import pricing as pricing_model

class PricingServiceError(Exception):
    """Custom exception for pricing service errors."""
    pass

def get_all_prices() -> Dict[str, Any]:
    """
    Retrieves all prices from the database.
    Returns a dictionary of prices.
    """
    log_prefix = "[SERVICE:Pricing]"
    try:
        prices = pricing_model.get_all_prices()
        logging.debug(f"{log_prefix} Retrieved all prices.")
        return prices
    except Exception as e:
        logging.error(f"{log_prefix} Error retrieving all prices: {e}", exc_info=True)
        raise PricingServiceError(f"Could not retrieve all prices: {e}")


def update_prices(pricing_data: Dict[str, Dict[str, float]]) -> None:
    """
    Updates prices in the database.
    pricing_data is a dictionary structured like:
    {
        "transcription": {"MODEL_NAME": 0.01, ...},
        "workflow": {"PROVIDER": 0.002, ...}
    }
    """
    log_prefix = "[SERVICE:Pricing:Update]"
    try:
        pricing_model.update_prices(pricing_data)
        logging.debug(f"{log_prefix} Successfully updated prices.")
    except Exception as e:
        logging.error(f"{log_prefix} Error updating prices: {e}", exc_info=True)
        raise PricingServiceError(f"Could not update prices: {e}")


def update_price(item_type: str, item_key: str, price: float) -> None:
    """Validate and save one catalog item's price."""
    if item_type not in {'transcription', 'workflow', 'title_generation'}:
        raise PricingServiceError("Invalid pricing section.")
    if not item_key or not isinstance(item_key, str):
        raise PricingServiceError("A model must be selected.")
    try:
        numeric_price = float(price)
    except (TypeError, ValueError) as exc:
        raise PricingServiceError("Price must be a number.") from exc
    if numeric_price < 0:
        raise PricingServiceError("Price cannot be negative.")
    try:
        pricing_model.update_prices({item_type: {item_key: numeric_price}})
    except Exception as e:
        logging.error("[SERVICE:Pricing:UpdateOne] Error updating price: %s", e, exc_info=True)
        raise PricingServiceError(f"Could not update price: {e}") from e


def get_price(item_type: str, item_key: Optional[str] = None) -> Optional[float]:
    """
    Retrieves the price for a given item type.
    If item_key is provided (e.g., a specific transcription model), it's used for the lookup.
    Otherwise, it falls back to the current LLM_PROVIDER from config.
    Returns the price as a float, or None if not found.
    """
    # If an explicit item_key (like a model name) is given, use it.
    if item_key:
        key_to_use = item_key
    # For title generation and workflows, use the specific provider from config.
    elif item_type == 'title_generation':
        key_to_use = current_app.config.get('TITLE_GENERATION_LLM_PROVIDER')
    elif item_type == 'workflow':
        key_to_use = current_app.config.get('WORKFLOW_LLM_PROVIDER')
    # Fallback for other types if needed (though transcription should always have an item_key).
    else:
        key_to_use = current_app.config.get('LLM_PROVIDER')

    log_prefix = f"[SERVICE:Pricing:{key_to_use}:{item_type}]"

    try:
        if not key_to_use:
            return None
        # Standardize on lowercase for all lookups
        item_key_to_use = key_to_use.lower()
        type_to_use = item_type.lower()

        price = pricing_model.get_price(item_key=item_key_to_use, item_type=type_to_use)

        # --- BACKWARD COMPATIBILITY ---
        # If no price is found for the specific model, try falling back to the generic key.
        if price is None and (type_to_use == 'title_generation' or type_to_use == 'workflow'):
            fallback_key = type_to_use  # e.g., 'title_generation' or 'workflow'
            logging.warning(f"{log_prefix} No specific price found for '{item_key_to_use}'. "
                            f"Attempting fallback to generic key '{fallback_key}'.")
            price = pricing_model.get_price(item_key=fallback_key, item_type=type_to_use)
            if price is not None:
                logging.info(f"{log_prefix} Found price using fallback key '{fallback_key}': {price}")
        # --- END BACKWARD COMPATIBILITY ---

        if price is not None:
            logging.debug(f"{log_prefix} Retrieved price from DB: {price}.")
        else:
            logging.warning(f"{log_prefix} No price found in DB for item_key '{item_key_to_use}' and item_type '{type_to_use}', including fallback.")
        return price
    except Exception as e:
        logging.error(f"{log_prefix} Error retrieving price from DB: {e}", exc_info=True)
        raise PricingServiceError(f"Could not retrieve price for item_key '{key_to_use}' and item_type '{item_type}': {e}")