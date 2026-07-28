# app/services/display_mapping_service.py
# Shared helpers for building provider/model display-name maps used across admin views.

import logging
from typing import Dict, List

from flask import current_app

from app.models import transcription_catalog as transcription_catalog_model
from app.models import llm_catalog as llm_catalog_model


def _normalize_codes(codes: List[str]) -> List[str]:
    """Returns a de-duplicated list of normalized provider/model codes."""
    normalized: List[str] = []
    for code in codes:
        normalized_code = (code or "").strip()
        if normalized_code and normalized_code not in normalized:
            normalized.append(normalized_code)
    return normalized


def get_transcription_display_map() -> Dict[str, str]:
    """
    Returns an ordered mapping of transcription provider codes to display names.
    Pulls from the transcription catalog when available and falls back to config overrides.
    """
    provider_codes = list(current_app.config.get("TRANSCRIPTION_PROVIDERS", []) or [])
    live_model = current_app.config.get("LIVE_TRANSCRIPTION_MODEL")
    if live_model:
        provider_codes.append(live_model)
    name_fallbacks = current_app.config.get("API_PROVIDER_NAME_MAP", {}) or {}
    try:
        catalog_models = transcription_catalog_model.get_active_models()
    except Exception as catalog_err:
        logging.warning(
            "[DisplayMap] Failed to load transcription catalog for display names: %s",
            catalog_err,
            exc_info=True,
        )
        catalog_models = []

    catalog_display_map = {
        (model.get("code") or "").strip(): model.get("display_name")
        for model in catalog_models
        if model.get("code")
    }

    normalized_codes = _normalize_codes(provider_codes)
    if not normalized_codes:
        normalized_codes = [code for code in catalog_display_map.keys() if code]
    if not normalized_codes:
        normalized_codes = [code for code in name_fallbacks.keys() if code]

    transcription_models: Dict[str, str] = {}
    for code in normalized_codes:
        transcription_models[code] = (
            catalog_display_map.get(code)
            or name_fallbacks.get(code)
            or code
        )
    return transcription_models


def get_workflow_model_display_map() -> Dict[str, str]:
    """
    Returns an ordered mapping of LLM model codes to display names for workflow analytics.
    Pulls from the LLM catalog and uses config overrides as needed.
    """
    name_fallbacks = current_app.config.get("API_PROVIDER_NAME_MAP", {}) or {}
    try:
        catalog_models = llm_catalog_model.get_active_models()
    except Exception as catalog_err:
        logging.warning(
            "[DisplayMap] Failed to load LLM catalog for workflow display names: %s",
            catalog_err,
            exc_info=True,
        )
        catalog_models = []

    workflow_models: Dict[str, str] = {}
    for model in catalog_models:
        code = (model.get("code") or "").strip()
        if not code or code in workflow_models:
            continue
        display_name = model.get("display_name") or name_fallbacks.get(code) or code
        workflow_models[code] = display_name

    if not workflow_models:
        fallback_codes = _normalize_codes(
            [
                current_app.config.get("WORKFLOW_LLM_MODEL"),
                current_app.config.get("LLM_MODEL"),
            ]
        )
        if not fallback_codes:
            fallback_codes = _normalize_codes(
                [
                    current_app.config.get("WORKFLOW_LLM_PROVIDER"),
                    current_app.config.get("LLM_PROVIDER"),
                ]
            )
        for code in fallback_codes:
            display = name_fallbacks.get(code) or code or "LLM"
            workflow_models[code] = display

    return workflow_models
