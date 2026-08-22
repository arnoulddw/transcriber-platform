from datetime import datetime, timezone
from unittest.mock import Mock, patch

from app.services import usage_service


def test_usage_service_preserves_fractional_minutes_for_all_periods():
    cursor = Mock()
    cursor.fetchone.return_value = {
        "daily_cost": 1.25,
        "daily_minutes": 9.5,
        "daily_workflows": 1,
        "daily_live_minutes": 10.0,
        "weekly_cost": 2.5,
        "weekly_minutes": 19.5,
        "weekly_workflows": 2,
        "weekly_live_minutes": 11.0,
        "monthly_cost": 3.75,
        "monthly_minutes": 29.5,
        "monthly_workflows": 3,
        "monthly_live_minutes": 12.0,
    }
    starts = (
        datetime(2026, 8, 22, tzinfo=timezone.utc),
        datetime(2026, 8, 17, tzinfo=timezone.utc),
        datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    with patch.object(usage_service, "get_cursor", return_value=cursor), patch.object(
        usage_service, "get_usage_period_starts", return_value=starts
    ):
        usage = usage_service.get_user_usage(7)

    assert usage["daily"]["minutes"] == 9.5
    assert usage["weekly"]["minutes"] == 19.5
    assert usage["monthly"]["minutes"] == 29.5
    assert usage["daily"]["live_minutes"] == 10.0
