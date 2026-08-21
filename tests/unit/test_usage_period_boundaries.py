"""Usage quota period boundaries follow the TZ environment variable."""

from datetime import datetime, timezone

from flask import Flask

from app.core.utils import get_usage_period_starts


def _starts(now, tz_name):
    return get_usage_period_starts(now=now, tz_name=tz_name)


def test_utc_boundaries_match_previous_behavior():
    now = datetime(2026, 8, 22, 15, 30, tzinfo=timezone.utc)  # a Saturday
    day_start, week_start, month_start = _starts(now, "UTC")

    assert day_start == datetime(2026, 8, 22, tzinfo=timezone.utc)
    # Monday of that week.
    assert week_start == datetime(2026, 8, 17, tzinfo=timezone.utc)
    assert month_start == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_madrid_day_boundary_shifts_to_local_midnight():
    # 00:30 UTC on Aug 22 is 02:30 in Madrid (CEST): still the same local day.
    now = datetime(2026, 8, 22, 0, 30, tzinfo=timezone.utc)
    day_start, _, month_start = _starts(now, "Europe/Madrid")

    assert day_start == datetime(2026, 8, 21, 22, 0, tzinfo=timezone.utc)
    assert month_start == datetime(2026, 7, 31, 22, 0, tzinfo=timezone.utc)


def test_madrid_week_boundary_uses_local_monday():
    # Sunday 2026-08-16 23:00 UTC is Monday 01:00 in Madrid: local week already
    # started, so the boundary is that day's local midnight.
    now = datetime(2026, 8, 16, 23, 0, tzinfo=timezone.utc)
    _, week_start, _ = _starts(now, "Europe/Madrid")

    assert week_start == datetime(2026, 8, 16, 22, 0, tzinfo=timezone.utc)


def test_unknown_timezone_falls_back_to_utc():
    now = datetime(2026, 8, 22, 15, 30, tzinfo=timezone.utc)
    day_start, week_start, month_start = _starts(now, "Not/AZone")

    assert day_start == datetime(2026, 8, 22, tzinfo=timezone.utc)
    assert week_start == datetime(2026, 8, 17, tzinfo=timezone.utc)
    assert month_start == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_defaults_to_configured_app_timezone():
    app = Flask(__name__)
    app.config["TZ"] = "Europe/Madrid"
    now = datetime(2026, 8, 22, 0, 30, tzinfo=timezone.utc)

    with app.app_context():
        day_start, _, _ = get_usage_period_starts(now=now)

    assert day_start == datetime(2026, 8, 21, 22, 0, tzinfo=timezone.utc)
