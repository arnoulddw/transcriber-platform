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


def update_price(item_type: str, item_key: str, price: float, billing_unit: Optional[str] = None) -> None:
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
    if billing_unit and billing_unit not in {'per_minute', 'per_1k_tokens', 'per_execution'}:
        raise PricingServiceError("Invalid billing unit.")
    unit = billing_unit or ('per_minute' if item_type == 'transcription' else 'per_1k_tokens')
    try:
        pricing_model.update_prices({item_type: {item_key: numeric_price}}, billing_units={item_key: unit})
    except Exception as e:
        logging.error("[SERVICE:Pricing:UpdateOne] Error updating price: %s", e, exc_info=True)
        raise PricingServiceError(f"Could not update price: {e}") from e



def _price_key_tail(key: str) -> str:
    """Return the model identity tail of a pricing key (after ':' or '/')."""
    normalized = str(key or "").strip().lower()
    return normalized.rsplit(":", 1)[-1].rsplit("/", 1)[-1]


def _resolve_price_by_identity(item_key: str, item_type: str) -> Optional[float]:
    """Find a transcription price by model identity when exact lookup fails.

    Prefers a vendor-suffix match (saved key ends with ``/<requested>`` or
    ``:<requested>``), then an equal-tail match. Only unambiguous results are
    returned: several distinct prices for the same identity resolve to None.
    """
    try:
        all_prices = pricing_model.get_all_prices()
    except Exception as scan_err:
        logging.debug("[SERVICE:Pricing] Identity fallback unavailable: %s", scan_err)
        return None

    candidates = all_prices.get(item_type) or {}
    if not candidates:
        return None

    requested = str(item_key).strip().lower()
    requested_tail = _price_key_tail(requested)

    vendor_matches = {
        saved_key: saved_price
        for saved_key, saved_price in candidates.items()
        if saved_key != requested
        and requested_tail
        and str(saved_key).strip().lower().endswith((f"/{requested_tail}", f":{requested_tail}"))
    }
    if len(set(vendor_matches.values())) > 1:
        logging.info(
            "[SERVICE:Pricing] Ambiguous transcription price for '%s'; skipping fallback.",
            item_key,
        )
        return None
    if vendor_matches:
        resolved_key, price = next(iter(vendor_matches.items()))
        logging.info(
            "[SERVICE:Pricing] Resolved '%s' via vendor-suffix match on '%s'.",
            item_key,
            resolved_key,
        )
        return price

    tail_matches = {
        saved_key: saved_price
        for saved_key, saved_price in candidates.items()
        if requested_tail and _price_key_tail(saved_key) == requested_tail
    }
    if len(set(tail_matches.values())) > 1:
        logging.info(
            "[SERVICE:Pricing] Ambiguous transcription price for '%s'; skipping fallback.",
            item_key,
        )
        return None
    if tail_matches:
        resolved_key, price = next(iter(tail_matches.items()))
        logging.info(
            "[SERVICE:Pricing] Resolved '%s' via tail match on '%s'.",
            item_key,
            resolved_key,
        )
        return price

    return None


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
    # Legacy callers without a model still use the configured model defaults.
    elif item_type == 'title_generation':
        key_to_use = current_app.config.get('TITLE_GENERATION_LLM_MODEL')
    elif item_type == 'workflow':
        key_to_use = current_app.config.get('WORKFLOW_LLM_MODEL')
    # Fallback for other types if needed (though transcription should always have an item_key).
    else:
        key_to_use = current_app.config.get('LLM_PROVIDER')

    log_prefix = f"[SERVICE:Pricing:{key_to_use}:{item_type}]"

    try:
        if not key_to_use:
            return None
        item_key_to_use = str(key_to_use).strip()
        type_to_use = item_type.lower()
        lookup_keys = [item_key_to_use]

        # Canonical catalog keys are provider-qualified. Try that identity first,
        # then the old bare code so existing prices remain usable during rollout.
        if type_to_use == 'transcription':
            try:
                from app.models import transcription_catalog as catalog_model
                model = catalog_model.get_model_by_code(item_key_to_use)
                if model:
                    canonical_key = str(model.get('model_key') or '').strip()
                    local_code = str(model.get('code') or '').strip()
                    lookup_keys = [
                        value for value in (canonical_key, item_key_to_use, local_code)
                        if value and value not in lookup_keys
                    ] + [item_key_to_use]
            except Exception as catalog_err:
                logging.debug("%s Could not resolve catalog key; using legacy price key: %s", log_prefix, catalog_err)

        price = None
        for lookup_key in dict.fromkeys(lookup_keys):
            price = pricing_model.get_price(item_key=lookup_key, item_type=type_to_use)
            if price is not None:
                break

        # --- IDENTITY FALLBACK ---
        # Saved keys and requested keys can disagree on qualification
        # (``openrouter:openai/whisper-large-v3`` vs ``openai/whisper-large-v3``
        # vs ``gpt-4o-mini``). Applies to transcription and LLM types alike:
        # user-selected workflow/title models are priced by model identity,
        # never guessing between different prices.
        if price is None and item_key_to_use:
            price = _resolve_price_by_identity(item_key_to_use, type_to_use)
        # --- END IDENTITY FALLBACK ---

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