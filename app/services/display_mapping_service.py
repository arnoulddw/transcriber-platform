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
    """Return display names for selectable transcription model identities.

    Provider metadata is not a model option. Include the qualified identity and
    a bare-code alias so historical analytics can still be labelled while new
    UI selections use the canonical model key.
    """
    try:
        catalog_models = transcription_catalog_model.get_active_models()
    except Exception as catalog_err:
        logging.warning(
            "[DisplayMap] Failed to load transcription catalog for display names: %s",
            catalog_err,
            exc_info=True,
        )
        catalog_models = []

    try:
        catalog_models = list(catalog_models) + transcription_catalog_model.get_live_models()
    except Exception as live_err:
        logging.debug("[DisplayMap] Live model catalog unavailable: %s", live_err, exc_info=True)

    transcription_models: Dict[str, str] = {}
    for model in catalog_models:
        code = str(model.get("code") or "").strip()
        model_key = str(model.get("model_key") or code).strip()
        display_name = model.get("display_name") or code or model_key
        if model_key:
            transcription_models[model_key] = display_name
        if code:
            transcription_models.setdefault(code, display_name)

    return dict(
        sorted(
            transcription_models.items(),
            key=lambda item: (str(item[1]).casefold(), str(item[0]).casefold()),
        )
    )


def get_workflow_model_display_map() -> Dict[str, str]:
    """
    Returns an ordered mapping of LLM model codes to display names for workflow analytics.
    Pulls from the LLM catalog (display_name is authoritative).
    """
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
        display_name = model.get("display_name") or code
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
            display = code or "LLM"
            workflow_models[code] = display

    return workflow_models
