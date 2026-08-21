from app.models.role import Role


def test_role_live_minutes_defaults_to_zero():
    role = Role(id=1, name="member")
    assert role.limit_daily_live_minutes == 0
    assert role.limit_weekly_live_minutes == 0
    assert role.limit_monthly_live_minutes == 0


def test_role_live_minutes_coerces_strings_like_other_limits():
    role = Role(
        id=1,
        name="member",
        limit_daily_live_minutes="30",
        limit_weekly_live_minutes="150",
        limit_monthly_live_minutes="600",
    )
    assert role.limit_daily_live_minutes == 30
    assert role.limit_weekly_live_minutes == 150
    assert role.limit_monthly_live_minutes == 600
