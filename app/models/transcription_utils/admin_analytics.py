"""
Admin-facing analytics helpers for transcription data.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from mysql.connector import Error as MySQLError

from app.database import get_cursor
from app.services import display_mapping_service
from .filtering import _build_filter_sql_and_params


def count_transcriptions_since(cutoff_datetime: datetime) -> int:
    """Counts transcriptions created since a specific datetime (including hidden)."""
    sql = "SELECT COUNT(*) as count FROM transcriptions WHERE created_at >= %s"
    cutoff_iso = cutoff_datetime.isoformat(timespec='seconds')
    cursor = get_cursor()
    count = 0
    try:
        cursor.execute(sql, (cutoff_iso,))
        result = cursor.fetchone()
        cursor.fetchall()
        count = result['count'] if result else 0
        logging.debug(
            "[DB:Admin] Counted %s transcriptions since %s (including hidden).",
            count,
            cutoff_iso,
        )
    except MySQLError as err:
        logging.error(
            "[DB:Admin] Error counting transcriptions since %s: %s",
            cutoff_iso,
            err,
            exc_info=True,
        )
    return count


def count_errors_since(cutoff_datetime: datetime) -> int:
    """Counts transcriptions with status 'error' created since a specific datetime."""
    sql = "SELECT COUNT(*) as count FROM transcriptions WHERE status = 'error' AND created_at >= %s"
    cutoff_iso = cutoff_datetime.isoformat(timespec='seconds')
    cursor = get_cursor()
    count = 0
    try:
        cursor.execute(sql, (cutoff_iso,))
        result = cursor.fetchone()
        cursor.fetchall()
        count = result['count'] if result else 0
        logging.debug(
            "[DB:Admin] Counted %s errors since %s (including hidden).",
            count,
            cutoff_iso,
        )
    except MySQLError as err:
        logging.error(
            "[DB:Admin] Error counting errors since %s: %s",
            cutoff_iso,
            err,
            exc_info=True,
        )
    return count


def count_jobs_in_range(
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    **filters: Any,
) -> int:
    """
    Counts transcription jobs within a date range, optionally applying filters (includes hidden).
    Supports equality filters and suffixed filters like 'status__in', 'status__not_in', 'status__ne'.
    """
    base_sql = "SELECT COUNT(*) as count FROM transcriptions WHERE 1=1"
    sql, params = _build_filter_sql_and_params(base_sql, start_dt, end_dt, **filters)

    cursor = get_cursor()
    count = 0
    try:
        cursor.execute(sql, tuple(params))
        result = cursor.fetchone()
        cursor.fetchall()
        count = result['count'] if result else 0
    except MySQLError as err:
        logging.error(
            "[DB:AdminUtils] Error counting jobs in range (%s - %s) with filters %s: %s",
            start_dt,
            end_dt,
            filters,
            err,
            exc_info=True,
        )
    return count


def sum_minutes_in_range(
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    **filters: Any,
) -> float:
    """
    Sums audio_length_minutes within a date range, optionally applying filters (includes hidden).
    """
    base_sql = (
        "SELECT SUM(audio_length_minutes) as total_minutes "
        "FROM transcriptions WHERE audio_length_minutes IS NOT NULL"
    )
    sql, params = _build_filter_sql_and_params(base_sql, start_dt, end_dt, **filters)

    cursor = get_cursor()
    total_minutes = 0.0
    try:
        cursor.execute(sql, tuple(params))
        result = cursor.fetchone()
        cursor.fetchall()
        total_minutes = result['total_minutes'] if result and result['total_minutes'] is not None else 0.0
        return float(total_minutes)
    except MySQLError as err:
        logging.error(
            "[DB:AdminUtils] Error summing minutes in range (%s - %s) with filters %s: %s",
            start_dt,
            end_dt,
            filters,
            err,
            exc_info=True,
        )
        return 0.0


def get_api_distribution_in_range(
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    aggregate_minutes: bool = False,
    **filters: Any,
) -> Dict[str, float]:
    """Get usage grouped by canonical transcription model identity.

    Historical rows may store a bare model code or a provider-level identifier,
    while newer rows use ``provider:model``. Resolve those aliases before
    aggregating so one model produces one analytics row.
    """
    aggregate_column = "SUM(audio_length_minutes)" if aggregate_minutes else "COUNT(*)"
    result_column_name = "total_value"

    base_sql = (
        f"SELECT api_used, api_model, {aggregate_column} as {result_column_name} "
        "FROM transcriptions WHERE 1=1"
    )
    if aggregate_minutes:
        base_sql += " AND audio_length_minutes IS NOT NULL"

    sql, params = _build_filter_sql_and_params(base_sql, start_dt, end_dt, **filters)
    sql += " GROUP BY api_used, api_model"

    cursor = get_cursor()
    distribution: Dict[str, float] = {}
    aliases = display_mapping_service.get_transcription_model_aliases()
    try:
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        for row in rows:
            api = display_mapping_service.resolve_transcription_model_key(
                row.get("api_used"),
                row.get("api_model"),
                aliases,
            )
            value = row[result_column_name]
            if aggregate_minutes:
                distribution[api] = distribution.get(api, 0.0) + (float(value) if value is not None else 0.0)
            else:
                distribution[api] = distribution.get(api, 0) + (int(value) if value is not None else 0)
    except MySQLError as err:
        logging.error(
            "[DB:AdminUtils] Error getting API %s distribution with filters %s: %s",
            "minute" if aggregate_minutes else "job",
            filters,
            err,
            exc_info=True,
        )
    return distribution


def get_api_error_rate_distribution_in_range(
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> Dict[str, float]:
    """Get API error rates grouped by canonical transcription model identity."""
    base_sql = """
        SELECT
            api_used,
            api_model,
            COUNT(*) AS total_count,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count
        FROM transcriptions
        WHERE status IN ('finished', 'cancelled', 'error')
    """
    sql, params = _build_filter_sql_and_params(base_sql, start_dt, end_dt)
    sql += " GROUP BY api_used, api_model"

    cursor = get_cursor()
    totals: Dict[str, int] = {}
    errors: Dict[str, int] = {}
    aliases = display_mapping_service.get_transcription_model_aliases()
    try:
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        for row in rows:
            api = display_mapping_service.resolve_transcription_model_key(
                row.get("api_used"),
                row.get("api_model"),
                aliases,
            )
            totals[api] = totals.get(api, 0) + int(row.get("total_count") or 0)
            errors[api] = errors.get(api, 0) + int(row.get("error_count") or 0)
    except MySQLError as err:
        logging.error(
            "[DB:AdminUtils] Error getting API error-rate distribution: %s",
            err,
            exc_info=True,
        )
        return {}

    return {
        api: round((errors.get(api, 0) * 100) / total, 2) if total else 0.0
        for api, total in totals.items()
    }


def get_language_distribution_in_range(
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> Dict[str, int]:
    """Gets job counts per detected language within a date range (finished jobs only)."""
    sql = "SELECT detected_language, COUNT(*) as count FROM transcriptions WHERE status = 'finished'"
    params: List[Any] = []
    if start_dt:
        sql += " AND created_at >= %s"
        params.append(start_dt.isoformat(timespec='seconds'))
    if end_dt:
        sql += " AND created_at < %s"
        params.append(end_dt.isoformat(timespec='seconds'))

    sql += " GROUP BY detected_language"

    cursor = get_cursor()
    distribution: Dict[str, int] = {}
    try:
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        for row in rows:
            lang = row['detected_language'] or 'Unknown'
            distribution[lang] = int(row['count']) if row['count'] is not None else 0
    except MySQLError as err:
        logging.error(
            "[DB:AdminUtils] Error getting language distribution: %s",
            err,
            exc_info=True,
        )
    return distribution


def get_common_error_messages_in_range(
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Gets the most common transcription error messages and their counts within a date range."""
    sql = """
        SELECT error_message, COUNT(*) as count
        FROM transcriptions
        WHERE status = 'error' AND error_message IS NOT NULL AND error_message != ''
    """
    params: List[Any] = []
    if start_dt:
        sql += " AND created_at >= %s"
        params.append(start_dt.isoformat(timespec='seconds'))
    if end_dt:
        sql += " AND created_at < %s"
        params.append(end_dt.isoformat(timespec='seconds'))

    sql += " GROUP BY error_message ORDER BY count DESC LIMIT %s"
    params.append(limit)

    cursor = get_cursor()
    errors: List[Dict[str, Any]] = []
    try:
        cursor.execute(sql, tuple(params))
        errors = cursor.fetchall()
    except MySQLError as err:
        logging.error(
            "[DB:AdminUtils] Error getting common transcription error messages: %s",
            err,
            exc_info=True,
        )
    return errors


def get_workflow_model_distribution(
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    include_attempted: bool = False,
) -> Dict[str, int]:
    """
    Gets workflow counts per model used within a date range.
    Queries from 'llm_operations' where operation_type = 'workflow'.
    """
    status_filter = (
        "lo.status != 'idle'"
        if include_attempted
        else "lo.status = 'finished'"
    )
    date_column = "lo.completed_at" if not include_attempted else "lo.created_at"

    sql = f"""
        SELECT
            COALESCE(lo.model, lo.provider) AS llm_operation_model_used,
            COUNT(lo.id) AS count
        FROM llm_operations lo
        WHERE
            lo.operation_type = 'workflow'
            AND {status_filter}
    """
    params: List[Any] = []

    if start_dt:
        sql += f" AND {date_column} >= %s"
        params.append(start_dt.isoformat(timespec='seconds'))
    if end_dt:
        sql += f" AND {date_column} < %s"
        params.append(end_dt.isoformat(timespec='seconds'))

    sql += " GROUP BY COALESCE(lo.model, lo.provider)"

    cursor = get_cursor()
    distribution: Dict[str, int] = {}
    try:
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        for row in rows:
            model = row['llm_operation_model_used'] or 'Unknown'
            distribution[model] = int(row['count']) if row['count'] is not None else 0
    except MySQLError as err:
        logging.error(
            "[DB:AdminUtils] MySQL Error getting workflow model distribution (Attempted=%s): %s (%s) - %s",
            include_attempted,
            err.errno,
            err.sqlstate,
            err.msg,
            exc_info=True,
        )
        if err.errno == 1054:
            logging.warning(
                "[DB:AdminUtils] Error getting workflow model distribution: A column might be missing. %s",
                err.msg,
            )
        return {}
    except Exception as exc:
        logging.error(
            "[DB:AdminUtils] Unexpected error getting workflow model distribution (Attempted=%s): %s",
            include_attempted,
            exc,
            exc_info=True,
        )
        return {}
    return distribution


def get_common_workflow_error_messages(
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """
    Gets the most common workflow error messages and their counts within a date range (from llm_operations).
    """
    sql = """
        SELECT error AS llm_operation_error, COUNT(*) as count
        FROM llm_operations
        WHERE operation_type = 'workflow' AND status = 'error' AND error IS NOT NULL AND error != ''
    """
    params: List[Any] = []
    if start_dt:
        sql += " AND created_at >= %s"
        params.append(start_dt.isoformat(timespec='seconds'))
    if end_dt:
        sql += " AND created_at < %s"
        params.append(end_dt.isoformat(timespec='seconds'))

    sql += " GROUP BY error ORDER BY count DESC LIMIT %s"
    params.append(limit)

    cursor = get_cursor()
    errors: List[Dict[str, Any]] = []
    try:
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        for row in rows:
            errors.append({'error_message': row['llm_operation_error'], 'count': row['count']})
    except MySQLError as err:
        logging.error(
            "[DB:AdminUtils] Error getting common workflow error messages: %s",
            err,
            exc_info=True,
        )
    return errors


def count_successful_title_generations_in_range(
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    user_id: Optional[int] = None,
) -> int:
    """Counts finished jobs with successful title generation within a date range."""
    filters: Dict[str, Any] = {'status': 'finished', 'title_generation_status': 'success'}
    if user_id is not None:
        filters['user_id'] = user_id
    return count_jobs_in_range(start_dt, end_dt, **filters)


def count_workflow_jobs_with_filters(
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    transcription_status_in: Optional[Tuple[str, ...]] = None,
    llm_operation_status: Optional[str] = None,
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
) -> int:
    """
    Counts transcription jobs that have associated workflow operations matching the given criteria.
    Joins transcriptions with llm_operations on transcription_id.
    """
    sql = """
        SELECT COUNT(DISTINCT lo.transcription_id) as count
        FROM llm_operations lo
        JOIN transcriptions t ON lo.transcription_id = t.id
        WHERE lo.operation_type = 'workflow'
    """
    params: List[Any] = []
    log_prefix = "[DB:AdminUtils:CountWorkflowJobs]"

    if start_dt:
        sql += " AND t.created_at >= %s"
        params.append(start_dt.isoformat(timespec='seconds'))
    if end_dt:
        sql += " AND t.created_at < %s"
        params.append(end_dt.isoformat(timespec='seconds'))

    if transcription_status_in:
        if not isinstance(transcription_status_in, (list, tuple)) or not transcription_status_in:
            logging.warning(
                "%s Invalid transcription_status_in: %s. Skipping filter.",
                log_prefix,
                transcription_status_in,
            )
        else:
            placeholders = ', '.join(['%s'] * len(transcription_status_in))
            sql += f" AND t.status IN ({placeholders})"
            params.extend(list(transcription_status_in))

    if llm_operation_status:
        sql += " AND lo.status = %s"
        params.append(llm_operation_status)

    if llm_provider:
        sql += " AND lo.provider = %s"
        params.append(llm_provider)
    if llm_model:
        sql += " AND COALESCE(lo.model, lo.provider) = %s"
        params.append(llm_model)

    cursor = get_cursor()
    count = 0
    try:
        cursor.execute(sql, tuple(params))
        result = cursor.fetchone()
        if result:
            count = result['count']
        logging.debug(
            "%s Counted %s workflow jobs with filters: status_in=%s, op_status=%s, provider=%s",
            log_prefix,
            count,
            transcription_status_in,
            llm_operation_status,
            llm_provider,
        )
    except MySQLError as err:
        logging.error(
            "%s Error counting workflow jobs with filters: %s",
            log_prefix,
            err,
            exc_info=True,
        )
    return count


def get_cost_analytics_by_component(
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    user_id: Optional[int] = None,
) -> Dict[str, float]:
    """
    Calculates the total cost for transcriptions, title generations, and workflows within a date range.
    Can be filtered by a specific user_id.
    """
    log_prefix = f"[DB:AdminUtils:CostAnalytics:User:{user_id or 'All'}]"
    costs = {'transcriptions': 0.0, 'title_generations': 0.0, 'workflows': 0.0}

    date_filter_sql = ""
    params: List[Any] = []
    if start_dt:
        date_filter_sql += " AND created_at >= %s"
        params.append(start_dt.isoformat(timespec='seconds'))
    if end_dt:
        date_filter_sql += " AND created_at < %s"
        params.append(end_dt.isoformat(timespec='seconds'))

    user_filter_sql = ""
    user_params: List[Any] = []
    if user_id is not None:
        user_filter_sql = " AND user_id = %s"
        user_params.append(user_id)

    cursor = get_cursor()
    try:
        sql_transcriptions = (
            "SELECT SUM(cost) as total_cost FROM transcriptions "
            f"WHERE cost IS NOT NULL{date_filter_sql}{user_filter_sql}"
        )
        cursor.execute(sql_transcriptions, tuple(params + user_params))
        result = cursor.fetchone()
        if result and result.get('total_cost') is not None:
            try:
                costs['transcriptions'] = float(result['total_cost'])
            except Exception:
                logging.warning(
                    "%s Unable to cast transcriptions total_cost '%s' to float.",
                    log_prefix,
                    result.get('total_cost'),
                )
        else:
            logging.debug("%s Transcriptions SUM(cost) returned NULL or 0.", log_prefix)

        sql_llm = (
            "SELECT operation_type, SUM(cost) as total_cost FROM llm_operations "
            f"WHERE cost IS NOT NULL{date_filter_sql}{user_filter_sql} GROUP BY operation_type"
        )
        cursor.execute(sql_llm, tuple(params + user_params))
        rows = cursor.fetchall()

        for row in rows or []:
            op_type = row.get('operation_type')
            total_cost = row.get('total_cost')
            if total_cost is None:
                continue
            if op_type == 'title_generation':
                costs['title_generations'] = float(total_cost)
            elif op_type == 'workflow':
                costs['workflows'] = float(total_cost)

        logging.debug(
            "%s Aggregated component costs => transcriptions=%s, title_generations=%s, workflows=%s",
            log_prefix,
            costs['transcriptions'],
            costs['title_generations'],
            costs['workflows'],
        )

    except MySQLError as err:
        logging.error(
            "%s Error calculating cost analytics by component: %s",
            log_prefix,
            err,
            exc_info=True,
        )
    return costs


def get_cost_analytics_by_role(
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> Dict[str, Dict[str, Any]]:
    """Calculates the total cost and user count per role within a date range."""
    log_prefix = "[DB:AdminUtils:CostAnalyticsByRole]"
    costs_by_role: Dict[str, Dict[str, Any]] = {}

    date_filter_sql = ""
    params: List[Any] = []
    if start_dt:
        date_filter_sql += " AND t.created_at >= %s"
        params.append(start_dt.isoformat(timespec='seconds'))
    if end_dt:
        date_filter_sql += " AND t.created_at < %s"
        params.append(end_dt.isoformat(timespec='seconds'))

    cursor = get_cursor()
    try:
        sql = f"""
            SELECT r.name as role_name, SUM(total_cost) as total_cost, COUNT(DISTINCT u.id) as user_count
            FROM roles r
            JOIN users u ON r.id = u.role_id
            LEFT JOIN (
                SELECT user_id, cost as total_cost, created_at FROM transcriptions WHERE cost IS NOT NULL
                UNION ALL
                SELECT user_id, cost as total_cost, created_at FROM llm_operations WHERE cost IS NOT NULL
            ) as costs ON u.id = costs.user_id
            WHERE costs.total_cost IS NOT NULL{date_filter_sql.replace('created_at', 'costs.created_at')}
            GROUP BY r.name
        """
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        for row in rows:
            costs_by_role[row['role_name']] = {
                'total_cost': float(row['total_cost']),
                'user_count': int(row['user_count']),
            }

    except MySQLError as err:
        logging.error(
            "%s Error calculating cost analytics by role: %s",
            log_prefix,
            err,
            exc_info=True,
        )
    return costs_by_role


__all__ = [
    "count_transcriptions_since",
    "count_errors_since",
    "count_jobs_in_range",
    "sum_minutes_in_range",
    "get_api_distribution_in_range",
    "get_api_error_rate_distribution_in_range",
    "get_language_distribution_in_range",
    "get_common_error_messages_in_range",
    "get_workflow_model_distribution",
    "get_common_workflow_error_messages",
    "count_successful_title_generations_in_range",
    "count_workflow_jobs_with_filters",
    "get_cost_analytics_by_component",
    "get_cost_analytics_by_role",
]
