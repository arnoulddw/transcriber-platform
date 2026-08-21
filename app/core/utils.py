# app/core/utils.py
# Contains core utility functions for the application.

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

def format_currency(value: float) -> str:
    """
    Formats a float value into a currency string with a dollar sign
    and two decimal places.
    """
    if value is None:
        return "$0.00"
    return f"${value:,.2f}"

def get_usage_period_starts(
    now: Optional[datetime] = None,
    tz_name: Optional[str] = None,
) -> Tuple[datetime, datetime, datetime]:
    """Return day/week/month period starts as UTC datetimes for quota buckets.

    Calendar boundaries follow the application timezone (``Config.TZ``, from
    the ``TZ`` environment variable) so a "day" of usage matches the user's
    local day; the returned instants are UTC because the ``user_usage.date``
    TIMESTAMP column stores UTC. Week starts on Monday.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if not tz_name:
        from flask import current_app
        tz_name = current_app.config.get('TZ') or 'UTC'

    try:
        from zoneinfo import ZoneInfo
        local_now = now.astimezone(ZoneInfo(str(tz_name)))
    except Exception:
        # Unknown timezone name: fall back to UTC boundaries.
        local_now = now.astimezone(timezone.utc)

    local_day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_week_start = local_day_start - timedelta(days=local_day_start.weekday())
    local_month_start = local_day_start.replace(day=1)

    return (
        local_day_start.astimezone(timezone.utc),
        local_week_start.astimezone(timezone.utc),
        local_month_start.astimezone(timezone.utc),
    )
