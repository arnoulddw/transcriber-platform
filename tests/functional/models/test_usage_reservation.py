import threading
from concurrent.futures import ThreadPoolExecutor

from app.database import get_cursor
from app.models import role as role_model
from app.services import auth_service
from app.models.user import get_user_by_username


def test_concurrent_quota_reservations_cannot_overspend(app, clean_db):
    with app.app_context():
        role = role_model.create_role(
            'quota-role',
            'Role with one workflow per day',
            {'limit_daily_workflows': 1},
        )
        assert role is not None
        auth_service.create_user(
            'quota-user', 'password123', 'quota@example.com', role.name
        )
        user = get_user_by_username('quota-user')
        assert user is not None
        user_id = user.id
        role_id = role.id

    barrier = threading.Barrier(2)

    def reserve_once():
        with app.app_context():
            worker_role = role_model.get_role_by_id(role_id)
            assert worker_role is not None
            barrier.wait(timeout=5)
            return role_model.reserve_usage_if_allowed(
                user_id, worker_role, workflows_to_add=1
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: reserve_once(), range(2)))

    assert sorted(result[0] for result in results) == [False, True]
    assert any('fair use limit' in reason for allowed, reason in results if not allowed)

    with app.app_context():
        cursor = get_cursor()
        cursor.execute(
            'SELECT COALESCE(SUM(workflows), 0) AS total FROM user_usage WHERE user_id=%s',
            (user_id,),
        )
        assert int(cursor.fetchone()['total']) == 1


def test_usage_minutes_are_decimal(app, clean_db):
    with app.app_context():
        cursor = get_cursor()
        cursor.execute("SHOW COLUMNS FROM user_usage LIKE 'minutes'")
        column = cursor.fetchone()
        assert str(column['Type']).lower().startswith('decimal')


def test_user_usage_date_is_date_type(app, clean_db):
    with app.app_context():
        cursor = get_cursor()
        cursor.execute("SHOW COLUMNS FROM user_usage LIKE 'date'")
        column = cursor.fetchone()
        assert str(column['Type']).lower() == 'date'

