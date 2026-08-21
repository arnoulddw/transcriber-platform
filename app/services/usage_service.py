# app/services/usage_service.py
# This file contains functions for calculating user usage statistics.

import logging
from datetime import datetime, timezone
from typing import Dict, Any

from app.core.utils import get_usage_period_starts
from app.database import get_cursor

def get_user_usage(user_id: int) -> Dict[str, Any]:
    """
    Calculates a user's usage for the last day, week, and month in a single query.

    Args:
        user_id: The ID of the user.

    Returns:
        A dictionary with the user's usage stats.
    """
    log_prefix = f"[UsageService:User:{user_id}]"
    cursor = get_cursor()

    today = datetime.now(timezone.utc)
    day_start, start_of_week, start_of_month = get_usage_period_starts(now=today)
    today = day_start
    # Use the earlier of the two boundaries so that weekly totals are correct
    # when the current week started in the previous month (e.g. today is the
    # 1st or 2nd of the month and Monday fell in the previous month).
    earliest = min(start_of_week, start_of_month)

    usage_stats = {
        'daily':   {'cost': 0, 'minutes': 0, 'workflows': 0, 'live_minutes': 0},
        'weekly':  {'cost': 0, 'minutes': 0, 'workflows': 0, 'live_minutes': 0},
        'monthly': {'cost': 0, 'minutes': 0, 'workflows': 0, 'live_minutes': 0},
    }

    try:
        cursor.execute(
            """
            SELECT
                SUM(CASE WHEN date = %s  THEN cost      ELSE 0 END) AS daily_cost,
                SUM(CASE WHEN date = %s  THEN minutes   ELSE 0 END) AS daily_minutes,
                SUM(CASE WHEN date = %s  THEN workflows ELSE 0 END) AS daily_workflows,
                SUM(CASE WHEN date = %s  THEN live_minutes ELSE 0 END) AS daily_live_minutes,
                SUM(CASE WHEN date >= %s THEN cost      ELSE 0 END) AS weekly_cost,
                SUM(CASE WHEN date >= %s THEN minutes   ELSE 0 END) AS weekly_minutes,
                SUM(CASE WHEN date >= %s THEN workflows ELSE 0 END) AS weekly_workflows,
                SUM(CASE WHEN date >= %s THEN live_minutes ELSE 0 END) AS weekly_live_minutes,
                SUM(CASE WHEN date >= %s THEN cost      ELSE 0 END) AS monthly_cost,
                SUM(CASE WHEN date >= %s THEN minutes   ELSE 0 END) AS monthly_minutes,
                SUM(CASE WHEN date >= %s THEN workflows ELSE 0 END) AS monthly_workflows,
                SUM(CASE WHEN date >= %s THEN live_minutes ELSE 0 END) AS monthly_live_minutes
            FROM user_usage
            WHERE user_id = %s AND date >= %s
            """,
            (
                today, today, today, today,
                start_of_week, start_of_week, start_of_week, start_of_week,
                start_of_month, start_of_month, start_of_month, start_of_month,
                user_id, earliest,
            )
        )
        row = cursor.fetchone()
        if row:
            usage_stats = {
                'daily': {
                    'cost':      float(row['daily_cost'] or 0),
                    'minutes':   int(row['daily_minutes'] or 0),
                    'workflows': int(row['daily_workflows'] or 0),
                    'live_minutes': float(row['daily_live_minutes'] or 0),
                },
                'weekly': {
                    'cost':      float(row['weekly_cost'] or 0),
                    'minutes':   int(row['weekly_minutes'] or 0),
                    'workflows': int(row['weekly_workflows'] or 0),
                    'live_minutes': float(row['weekly_live_minutes'] or 0),
                },
                'monthly': {
                    'cost':      float(row['monthly_cost'] or 0),
                    'minutes':   int(row['monthly_minutes'] or 0),
                    'workflows': int(row['monthly_workflows'] or 0),
                    'live_minutes': float(row['monthly_live_minutes'] or 0),
                },
            }
    except Exception as e:
        logging.error(f"{log_prefix} Error calculating usage stats: {e}", exc_info=True)
    finally:
        # The cursor is managed by the application context, so we don't close it here.
        pass

    return usage_stats