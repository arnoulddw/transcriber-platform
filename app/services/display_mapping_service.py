# app/services/display_mapping_service.py
# Shared helpers for building provider/model display-name maps used across admin views.

import logging
from typing import Dict, List, Optional, Set

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


def _get_transcription_catalog_models() -> List[dict]:
    """Load active normal/live transcription models for admin lookups."""
    try:
        catalog_models = list(transcription_catalog_model.get_active_models())
    except Exception as catalog_err:
        logging.warning(
            "[DisplayMap] Failed to load transcription catalog for display names: %s",
            catalog_err,
            exc_info=True,
        )
        catalog_models = []

    try:
        catalog_models.extend(transcription_catalog_model.get_live_models())
    except Exception as live_err:
        logging.debug("[DisplayMap] Live model catalog unavailable: %s", live_err, exc_info=True)

    return catalog_models


def _model_key(model: dict) -> str:
    """Return the canonical provider-qualified key for a catalog row."""
    code = str(model.get("code") or "").strip()
    model_key = str(model.get("model_key") or "").strip()
    if model_key:
        return model_key
    provider = str(model.get("provider_code") or model.get("required_api_key") or "").strip().lower()
    return transcription_catalog_model.make_model_key(provider, code)


def get_transcription_display_map() -> Dict[str, str]:
    """Return canonical display names for active transcription models.

    Legacy bare/provider identifiers are resolved by
    ``get_transcription_model_aliases`` before analytics data reaches the
    templates, so this map intentionally contains one entry per model identity.
    """
    transcription_models: Dict[str, str] = {}
    for model in _get_transcription_catalog_models():
        model_key = _model_key(model)
        if not model_key:
            continue
        transcription_models[model_key] = model.get("display_name") or model_key

    return dict(
        sorted(
            transcription_models.items(),
            key=lambda item: (str(item[1]).casefold(), str(item[0]).casefold()),
        )
    )


def get_transcription_model_aliases() -> Dict[str, str]:
    """Return unambiguous legacy identifiers mapped to canonical model keys."""
    alias_candidates: Dict[str, Set[str]] = {}
    provider_candidates: Dict[str, Set[str]] = {}
    for model in _get_transcription_catalog_models():
        model_key = _model_key(model)
        code = str(model.get("code") or "").strip()
        provider = str(model.get("provider_code") or model.get("required_api_key") or "").strip().lower()
        if not model_key:
            continue
        alias_candidates.setdefault(model_key, set()).add(model_key)
        if code:
            alias_candidates.setdefault(code, set()).add(model_key)
        if provider:
            provider_candidates.setdefault(provider, set()).add(model_key)

    for provider, model_keys in provider_candidates.items():
        if len(model_keys) == 1:
            alias_candidates.setdefault(provider, set()).update(model_keys)

    return {
        alias: next(iter(model_keys))
        for alias, model_keys in alias_candidates.items()
        if len(model_keys) == 1
    }


def resolve_transcription_model_key(
    api_used: Optional[str],
    api_model: Optional[str] = None,
    aliases: Optional[Dict[str, str]] = None,
) -> str:
    """Resolve a stored analytics identifier to a canonical model key."""
    raw_identifier = str(api_used or "").strip()
    if not raw_identifier:
        return "Unknown"

    alias_map = aliases if aliases is not None else get_transcription_model_aliases()
    model_name = str(api_model or "").strip()
    if model_name:
        provider = raw_identifier.casefold()
        candidate = transcription_catalog_model.make_model_key(provider, model_name)
        if alias_map.get(candidate) == candidate:
            return candidate

    return alias_map.get(raw_identifier, raw_identifier)


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
