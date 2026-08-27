"""
Filtering utilities shared across transcription analytics functions.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# Define a set of actual database column names that can be filtered on.
# These are the columns in the 'transcriptions' table.
VALID_TRANSCRIPTION_COLUMNS_FOR_FILTERING = {
    'id', 'user_id', 'filename', 'generated_title', 'title_generation_status',
    'file_size_mb', 'audio_length_minutes', 'detected_language',
    'transcription_text', 'api_used', 'created_at', 'status',
    'progress_log', 'error_message', 'context_prompt_used', 'downloaded',
    'is_hidden_from_user', 'hidden_date', 'hidden_reason',
    'pending_workflow_prompt_text', 'pending_workflow_prompt_title',
    'pending_workflow_prompt_color', 'pending_workflow_origin_prompt_id',
    'public_api_invocation', 'cost'
}


def _build_filter_sql_and_params(
    base_sql: str,
    start_dt: Optional[datetime],
    end_dt: Optional[datetime],
    **filters: Any,
) -> Tuple[str, List[Any]]:
    """Helper to build SQL WHERE clauses and parameters for filtering."""
    sql = base_sql
    params: List[Any] = []

    if start_dt:
        sql += " AND created_at >= %s"
        params.append(start_dt.isoformat(timespec='seconds'))
    if end_dt:
        sql += " AND created_at < %s"
        params.append(end_dt.isoformat(timespec='seconds'))

    column_mapping = {
        'workflow_status': 'llm_operation_status',
        # Add other mappings here as needed.
    }

    for key, value in filters.items():
        if value is None:
            continue

        actual_column_name = key
        operator = "="
        is_list_filter = False

        if key.endswith('__in') and isinstance(value, (list, tuple)):
            actual_column_name = key[:-4]
            operator = "IN"
            is_list_filter = True
            if not value:
                continue
        elif key.endswith('__not_in') and isinstance(value, (list, tuple)):
            actual_column_name = key[:-8]
            operator = "NOT IN"
            is_list_filter = True
            if not value:
                continue
        elif key.endswith('__ne') and not isinstance(value, (list, tuple)):
            actual_column_name = key[:-4]
            operator = "!="

        if key in column_mapping:
            actual_column_name = column_mapping[key]
        elif actual_column_name in column_mapping:
            actual_column_name = column_mapping[actual_column_name]

        if actual_column_name in VALID_TRANSCRIPTION_COLUMNS_FOR_FILTERING:
            if is_list_filter:
                placeholders = ', '.join(['%s'] * len(value))
                sql += f" AND {actual_column_name} {operator} ({placeholders})"
                params.extend(list(value))
            else:
                sql += f" AND {actual_column_name} {operator} %s"
                params.append(value)
        else:
            logging.warning(
                "[DB:AdminUtils] Ignored invalid or unmapped filter key: '%s' (resolved to '%s')",
                key,
                actual_column_name,
            )

    return sql, params


__all__ = [
    "VALID_TRANSCRIPTION_COLUMNS_FOR_FILTERING",
    "_build_filter_sql_and_params",
]
