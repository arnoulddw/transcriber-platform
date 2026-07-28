# app/services/admin_metrics_service.py
# Contains business logic for fetching and calculating admin dashboard metrics.

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List, Tuple

from flask import current_app # To access config and app context

# Import necessary model utilities
from app.models import user_utils
from app.models import transcription_utils
from app.models import transcription as transcription_model

# Shared display-map helpers
from app.services import display_mapping_service
# Import MySQL error class for potential specific checks if needed
from mysql.connector import Error as MySQLError

# Import the shared exception class
from .exceptions import AdminServiceError

# ---------------------------------------------------------------------------
# Simple in-process TTL cache for admin metrics.
# Admin pages are read-only aggregations; caching for 60 s reduces DB load
# from 30+ queries per page load to near-zero between cache refreshes.
# ---------------------------------------------------------------------------
_METRICS_CACHE_TTL = 60  # seconds
_metrics_cache: Dict[str, tuple] = {}  # key -> (value, expires_at)
_metrics_cache_lock = threading.Lock()


def _cache_get(key: str) -> Any:
    with _metrics_cache_lock:
        entry = _metrics_cache.get(key)
        if entry and time.monotonic() < entry[1]:
            return entry[0]
    return None


def _cache_set(key: str, value: Any) -> None:
    with _metrics_cache_lock:
        _metrics_cache[key] = (value, time.monotonic() + _METRICS_CACHE_TTL)


def invalidate_metrics_cache() -> None:
    """Clear all cached metrics. Intended for use in tests to prevent cache bleed between test runs."""
    with _metrics_cache_lock:
        _metrics_cache.clear()

# --- Helper Functions ---

def _get_time_periods() -> Dict[str, Dict[str, Optional[datetime]]]:
    """Returns a dictionary defining common time periods for analytics."""
    now = datetime.now(timezone.utc)
    return {
        "24h": {"start": now - timedelta(hours=24), "end": now},
        "7d": {"start": now - timedelta(days=7), "end": now},
        "30d": {"start": now - timedelta(days=30), "end": now},
        "all": {"start": None, "end": None}, # Represents all time
    }

def _safe_division(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Performs division, returning default value if denominator is zero."""
    if denominator == 0:
        return default
    return numerator / denominator


def _get_supported_llm_models() -> Tuple[List[str], Dict[str, str]]:
    """
    Returns the list of active LLM model codes and corresponding display names.
    Falls back to configured defaults if the catalog is unavailable or empty.
    """
    display_map = display_mapping_service.get_workflow_model_display_map()
    codes = list(display_map.keys())
    if not codes:
        fallback = current_app.config.get('LLM_MODEL')
        if fallback:
            codes = [fallback]
            display_map = {fallback: display_map.get(fallback) or fallback}
    return codes, display_map

# --- Dashboard Metrics ---
def get_admin_dashboard_metrics() -> Dict[str, Any]:  # noqa: C901
    """
    Retrieves key metrics for the new admin dashboard overview.
    Requires app context for database access.
    Includes workflow metrics.
    Metrics for "Jobs Submitted" and "Minutes Processed" now count 'finished' or 'cancelled' jobs.
    Error rates are calculated based on all relevant jobs.
    """
    log_prefix = "[SERVICE:Admin:Dashboard]"
    cached = _cache_get('dashboard')
    if cached is not None:
        return cached
    metrics = {
        'total_users': 0,
        'active_users': {},
        'jobs_submitted': {},
        'minutes_processed': {},
        'error_rate': {},
        'workflows_run': {},
        'workflow_error_rate': {},
        'error': None
    }
    time_periods = _get_time_periods()
    # Define relevant statuses for volume/duration metrics
    relevant_statuses_for_volume = ('finished', 'cancelled')

    try:
        with current_app.app_context():
            metrics['total_users'] = user_utils.count_all_users()
            for key, period in time_periods.items():
                start, end = period["start"], period["end"]
                metrics['active_users'][key] = user_utils.count_active_users_in_range(start, end)

                # Transcription Metrics (Volume & Duration based on 'finished' or 'cancelled')
                total_relevant_transcription_jobs = transcription_utils.count_jobs_in_range(
                    start, end, status__in=relevant_statuses_for_volume
                )
                metrics['jobs_submitted'][key] = total_relevant_transcription_jobs
                
                total_relevant_minutes = transcription_utils.sum_minutes_in_range(
                    start, end, status__in=relevant_statuses_for_volume
                )
                metrics['minutes_processed'][key] = round(total_relevant_minutes, 1)

                # Transcription Error Rate (based on all jobs: finished, cancelled, error)
                total_transcription_jobs_for_error_rate = transcription_utils.count_jobs_in_range(
                    start, end, status__in=['finished', 'cancelled', 'error']
                )
                total_transcription_errors = transcription_utils.count_jobs_in_range(start, end, status='error')
                error_rate_percent = _safe_division(total_transcription_errors, total_transcription_jobs_for_error_rate) * 100
                metrics['error_rate'][key] = round(error_rate_percent, 2)

                # Workflow Metrics (remains based on workflow_status)
                total_workflows_run = transcription_utils.count_jobs_in_range(start, end, llm_operation_status='finished') # Use llm_operation_status
                metrics['workflows_run'][key] = total_workflows_run
                
                total_workflows_attempted = transcription_utils.count_jobs_in_range(start, end, llm_operation_status__ne='idle') # Use llm_operation_status
                total_workflow_errors = transcription_utils.count_jobs_in_range(start, end, llm_operation_status='error') # Use llm_operation_status
                workflow_error_rate_percent = _safe_division(total_workflow_errors, total_workflows_attempted) * 100
                metrics['workflow_error_rate'][key] = round(workflow_error_rate_percent, 2)

        _cache_set('dashboard', metrics)
        logging.debug(f"{log_prefix} Retrieved dashboard metrics.")
        return metrics

    except MySQLError as db_err:
        logging.error(f"{log_prefix} Database error retrieving dashboard metrics: {db_err}", exc_info=True)
        metrics['error'] = "Database error retrieving metrics."
        return metrics
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error retrieving dashboard metrics: {e}", exc_info=True)
        metrics['error'] = "Unexpected error retrieving metrics."
        return metrics

# --- Usage Analytics Metrics ---
def get_usage_analytics_metrics() -> Dict[str, Any]:
    """
    Retrieves detailed usage metrics for the Usage Analytics page.
    Requires app context for database access.
    Volume/Duration metrics (Jobs Submitted, Minutes Processed, API Distributions)
    now count 'finished' or 'cancelled' jobs.
    Feature usage denominators are updated accordingly.
    Language distribution remains based on 'finished' jobs.
    """
    log_prefix = "[SERVICE:Admin:UsageAnalytics]"
    cached = _cache_get('usage_analytics')
    if cached is not None:
        return cached
    metrics = {
        'jobs_submitted': {},
        'minutes_processed': {},
        'api_jobs_distribution': {},
        'api_minutes_distribution': {},
        'language_distribution': {},
        'context_prompt_usage': {},
        'public_api_usage': {},
        'download_usage': {},
        'workflows_run': {},
        'workflow_model_distribution': {},
        'auto_title_success': {},
        'error': None
    }
    time_periods = _get_time_periods()
    supported_apis = ['gpt-transcribe', 'gpt-4o-transcribe', 'whisper', 'assemblyai']
    supported_workflow_models, workflow_display_map = _get_supported_llm_models()
    # Define relevant statuses for volume/duration metrics
    relevant_statuses_for_volume = ('finished', 'cancelled')

    try:
        with current_app.app_context():
            metrics['workflow_model_display_map'] = workflow_display_map
            for key, period in time_periods.items():
                start, end = period["start"], period["end"]

                # Transcription Metrics (Volume & Duration based on 'finished' or 'cancelled')
                total_relevant_jobs = transcription_utils.count_jobs_in_range(
                    start, end, status__in=relevant_statuses_for_volume
                )
                metrics['jobs_submitted'][key] = total_relevant_jobs
                
                total_relevant_minutes = transcription_utils.sum_minutes_in_range(
                    start, end, status__in=relevant_statuses_for_volume
                )
                metrics['minutes_processed'][key] = round(total_relevant_minutes, 1)

                # API Distribution (based on 'finished' or 'cancelled')
                api_jobs = transcription_utils.get_api_distribution_in_range(
                    start, end, aggregate_minutes=False, status__in=relevant_statuses_for_volume
                )
                metrics['api_jobs_distribution'][key] = {api: api_jobs.get(api, 0) for api in supported_apis}

                api_minutes = transcription_utils.get_api_distribution_in_range(
                    start, end, aggregate_minutes=True, status__in=relevant_statuses_for_volume
                )
                metrics['api_minutes_distribution'][key] = {api: round(api_minutes.get(api, 0.0), 1) for api in supported_apis}

                # Language Distribution (remains based on 'finished' jobs)
                lang_dist = transcription_utils.get_language_distribution_in_range(start, end) # This already filters for status='finished'
                metrics['language_distribution'][key] = lang_dist

                # Feature Usage: Context Prompt (denominator based on 'finished' or 'cancelled' compatible jobs)
                compatible_apis_for_context = ['whisper', 'gpt-4o-transcribe', 'gpt-transcribe']
                total_compatible_relevant_jobs = transcription_utils.count_jobs_in_range(
                    start, end, status__in=relevant_statuses_for_volume, api_used__in=compatible_apis_for_context
                )
                context_used_count = transcription_utils.count_jobs_in_range(
                    start, end, status__in=relevant_statuses_for_volume, api_used__in=compatible_apis_for_context, context_prompt_used=True
                )
                metrics['context_prompt_usage'][key] = {
                    'used': context_used_count,
                    'total_compatible': total_compatible_relevant_jobs
                }

                # Feature Usage: Public API uploads (based on relevant jobs)
                public_api_count = transcription_utils.count_jobs_in_range(
                    start, end, status__in=relevant_statuses_for_volume, public_api_invocation=True
                )
                metrics['public_api_usage'][key] = {
                    'used': public_api_count,
                    'total_jobs': total_relevant_jobs
                }

                # Feature Usage: Downloads (denominator based on 'finished' jobs as per task)
                total_finished_jobs = transcription_utils.count_jobs_in_range(start, end, status='finished')
                downloaded_count = transcription_utils.count_jobs_in_range(start, end, status='finished', downloaded=True)
                metrics['download_usage'][key] = {
                    'downloaded': downloaded_count,
                    'total_finished': total_finished_jobs
                }

                # Feature Usage: Auto-Titles (denominator based on 'finished' jobs)
                title_success_count = transcription_utils.count_successful_title_generations_in_range(start, end) # Already filters for status='finished'
                metrics['auto_title_success'][key] = {
                    'count': title_success_count,
                    'total_finished': total_finished_jobs 
                }
                
                # Workflow Metrics (remains based on workflow_status)
                workflows_run_count = transcription_utils.count_jobs_in_range(start, end, llm_operation_status='finished') # Use llm_operation_status
                metrics['workflows_run'][key] = workflows_run_count
                model_dist = transcription_utils.get_workflow_model_distribution(start, end)
                metrics['workflow_model_distribution'][key] = {model: model_dist.get(model, 0) for model in supported_workflow_models}

        _cache_set('usage_analytics', metrics)
        logging.debug(f"{log_prefix} Retrieved usage analytics metrics.")
        return metrics

    except MySQLError as db_err:
        logging.error(f"{log_prefix} Database error retrieving usage analytics: {db_err}", exc_info=True)
        metrics['error'] = "Database error retrieving usage analytics."
        return metrics
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error retrieving usage analytics: {e}", exc_info=True)
        metrics['error'] = "Unexpected error retrieving usage analytics."
        return metrics

# --- User Insights Metrics ---
def get_user_insights_metrics() -> Dict[str, Any]:
    """
    Retrieves metrics about user signups and limits for the User Insights page.
    Requires app context for database access.
    Includes workflow limits check.
    (No changes needed for this function based on the task, as it doesn't directly use status filters from transcription_utils)
    """
    log_prefix = "[SERVICE:Admin:UserInsights]"
    cached = _cache_get('user_insights')
    if cached is not None:
        return cached
    metrics = {
        'new_signups': {},
        'users_hitting_limits': [],
        'error': None
    }
    time_periods = _get_time_periods()

    try:
        with current_app.app_context():
            for key, period in time_periods.items():
                start, end = period["start"], period["end"]
                metrics['new_signups'][key] = user_utils.count_new_users_in_range(start, end)

            metrics['users_hitting_limits'] = user_utils.get_users_hitting_limits()

        _cache_set('user_insights', metrics)
        logging.debug(f"{log_prefix} Retrieved user insights metrics.")
        return metrics

    except MySQLError as db_err:
        logging.error(f"{log_prefix} Database error retrieving user insights: {db_err}", exc_info=True)
        metrics['error'] = "Database error retrieving user insights."
        return metrics
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error retrieving user insights: {e}", exc_info=True)
        metrics['error'] = "Unexpected error retrieving user insights."
        return metrics

# --- Performance & Error Metrics ---
def get_performance_error_metrics() -> Dict[str, Any]:
    """
    Retrieves metrics about error rates and types for the Performance & Errors page.
    Requires app context for database access.
    Differentiates between transcription and workflow errors.
    Transcription error rates are calculated based on all relevant jobs (finished, cancelled, error).
    """
    log_prefix = "[SERVICE:Admin:PerformanceErrors]"
    cached = _cache_get('performance_errors')
    if cached is not None:
        return cached
    metrics = {
        'overall_transcription_error_rate': {},
        'api_transcription_error_rates': {},
        'common_transcription_errors': {},
        'overall_workflow_error_rate': {},
        'workflow_model_error_rates': {},
        'common_workflow_errors': {},
        'error': None
    }
    time_periods = _get_time_periods()
    supported_apis = ['gpt-transcribe', 'gpt-4o-transcribe', 'whisper', 'assemblyai']
    supported_workflow_models, workflow_display_map = _get_supported_llm_models()

    try:
        with current_app.app_context():
            metrics['workflow_model_display_map'] = workflow_display_map
            for key, period in time_periods.items():
                start, end = period["start"], period["end"]

                # Transcription Errors
                # Denominator for error rate: finished + cancelled + error
                total_transcription_jobs_for_error_rate = transcription_utils.count_jobs_in_range(
                    start, end, status__in=['finished', 'cancelled', 'error']
                )
                total_transcription_errors = transcription_utils.count_jobs_in_range(start, end, status='error')
                overall_transcription_rate = _safe_division(total_transcription_errors, total_transcription_jobs_for_error_rate) * 100
                metrics['overall_transcription_error_rate'][key] = round(overall_transcription_rate, 2)

                api_transcription_rates = {}
                for api in supported_apis:
                    # Denominator for API-specific error rate: all jobs for that API (finished, cancelled, error)
                    jobs_for_api = transcription_utils.count_jobs_in_range(
                        start, end, api_used=api, status__in=['finished', 'cancelled', 'error']
                    )
                    errors_for_api = transcription_utils.count_jobs_in_range(start, end, status='error', api_used=api)
                    api_rate = _safe_division(errors_for_api, jobs_for_api) * 100
                    api_transcription_rates[api] = round(api_rate, 2)
                metrics['api_transcription_error_rates'][key] = api_transcription_rates

                common_transcription = transcription_utils.get_common_error_messages_in_range(start, end, limit=10)
                metrics['common_transcription_errors'][key] = common_transcription

                # Workflow Errors
                # --- MODIFIED: Use llm_operation_status for workflow error calculations ---
                total_workflows_attempted = transcription_utils.count_jobs_in_range(start, end, llm_operation_status__ne='idle')
                total_workflow_errors = transcription_utils.count_jobs_in_range(start, end, llm_operation_status='error')
                # --- END MODIFIED ---
                overall_workflow_rate = _safe_division(total_workflow_errors, total_workflows_attempted) * 100
                metrics['overall_workflow_error_rate'][key] = round(overall_workflow_rate, 2)

                workflow_model_rates = {}
                model_attempt_dist = transcription_utils.get_workflow_model_distribution(start, end, include_attempted=True)
                for model in supported_workflow_models:
                    # --- MODIFIED: Use new function for counting errors by model ---
                    errors_for_model = transcription_utils.count_workflow_jobs_with_filters(
                        start_dt=start,
                        end_dt=end,
                        llm_operation_status='error',
                        llm_provider=model
                    )
                    # --- END MODIFIED ---
                    attempts_for_model = model_attempt_dist.get(model, 0)
                    model_rate = _safe_division(errors_for_model, attempts_for_model) * 100
                    workflow_model_rates[model] = round(model_rate, 2)
                metrics['workflow_model_error_rates'][key] = workflow_model_rates

                common_workflow = transcription_utils.get_common_workflow_error_messages(start, end, limit=10)
                metrics['common_workflow_errors'][key] = common_workflow

        _cache_set('performance_errors', metrics)
        logging.debug(f"{log_prefix} Retrieved performance and error metrics.")
        return metrics

    except MySQLError as db_err:
        logging.error(f"{log_prefix} Database error retrieving performance/error metrics: {db_err}", exc_info=True)
        metrics['error'] = "Database error retrieving performance/error metrics."
        return metrics
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error retrieving performance/error metrics: {e}", exc_info=True)
        metrics['error'] = "Unexpected error retrieving performance/error metrics."
        return metrics

def get_cost_analytics() -> Dict[str, Any]:
    """
    Retrieves cost analytics for the Costs page.
    """
    log_prefix = "[SERVICE:Admin:CostAnalytics]"
    cached = _cache_get('cost_analytics')
    if cached is not None:
        return cached
    metrics = {
        'by_component': {},
        'by_role': {},
        'error': None
    }
    time_periods = _get_time_periods()

    try:
        with current_app.app_context():
            for key, period in time_periods.items():
                start, end = period["start"], period["end"]
                
                # Costs by component
                component_costs = transcription_utils.get_cost_analytics_by_component(start, end)
                metrics['by_component'][key] = {
                    'transcriptions': component_costs.get('transcriptions', 0.0),
                    'title_generations': component_costs.get('title_generations', 0.0),
                    'workflows': component_costs.get('workflows', 0.0)
                }

                # Costs by role
                role_costs = transcription_utils.get_cost_analytics_by_role(start, end)
                metrics['by_role'][key] = {}
                for role, data in role_costs.items():
                    metrics['by_role'][key][role] = {
                        'total_cost': data.get('total_cost', 0.0),
                        'user_count': data.get('user_count', 0),
                        'cost_per_user': _safe_division(data.get('total_cost', 0.0), data.get('user_count', 1))
                    }

        _cache_set('cost_analytics', metrics)
        logging.debug(f"{log_prefix} Retrieved cost analytics.")
        return metrics

    except MySQLError as db_err:
        logging.error(f"{log_prefix} Database error retrieving cost analytics: {db_err}", exc_info=True)
        metrics['error'] = "Database error retrieving cost analytics."
        return metrics
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error retrieving cost analytics: {e}", exc_info=True)
        metrics['error'] = "Unexpected error retrieving cost analytics."
        return metrics

def get_user_usage_metrics(user_id: int) -> Dict[str, Any]:
    """
    Retrieves detailed usage metrics for a specific user for the admin user details page.
    """
    log_prefix = f"[SERVICE:Admin:UserUsageMetrics:{user_id}]"
    cache_key = f'user_usage_metrics:{user_id}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    metrics = {
        'costs': {},
        'transcriptions': {},
        'title_generations': {},
        'workflows': {},
        'audio_processed': {},
        'errors': {},
        'error': None
    }
    time_periods = _get_time_periods()

    try:
        with current_app.app_context():
            for key, period in time_periods.items():
                start, end = period["start"], period["end"]

                # Costs
                cost_data = transcription_utils.get_cost_analytics_by_component(start, end, user_id=user_id)
                total_cost = cost_data.get('transcriptions', 0.0) + cost_data.get('title_generations', 0.0) + cost_data.get('workflows', 0.0)
                metrics['costs'][key] = total_cost

                # Transcriptions
                metrics['transcriptions'][key] = transcription_utils.count_jobs_in_range(
                    start, end, user_id=user_id, status__in=['finished', 'cancelled']
                )

                # Title Generations
                metrics['title_generations'][key] = transcription_utils.count_successful_title_generations_in_range(
                    start, end, user_id=user_id
                )

                # Workflows
                metrics['workflows'][key] = transcription_utils.count_jobs_in_range(
                    start, end, user_id=user_id, llm_operation_status='finished'
                )

                # Audio Processed
                audio_minutes = transcription_utils.sum_minutes_in_range(
                    start, end, user_id=user_id, status__in=['finished', 'cancelled']
                )
                metrics['audio_processed'][key] = audio_minutes

                # Errors
                metrics['errors'][key] = transcription_utils.count_jobs_in_range(
                    start, end, user_id=user_id, status='error'
                )

        _cache_set(cache_key, metrics)
        logging.debug(f"{log_prefix} Retrieved user usage metrics.")
        return metrics

    except MySQLError as db_err:
        logging.error(f"{log_prefix} Database error retrieving user usage metrics: {db_err}", exc_info=True)
        metrics['error'] = "Database error retrieving user usage metrics."
        return metrics
    except Exception as e:
        logging.error(f"{log_prefix} Unexpected error retrieving user usage metrics: {e}", exc_info=True)
        metrics['error'] = "Unexpected error retrieving user usage metrics."
        return metrics
