"""Stuck LLM operation recovery sweeps."""

from unittest.mock import MagicMock, patch

from app.models import llm_operation


def test_mark_stale_operations_interrupted_all_when_no_threshold():
    cursor = MagicMock()
    cursor.rowcount = 3
    with patch.object(llm_operation, "get_cursor", return_value=cursor), patch.object(
        llm_operation, "get_db"
    ) as db:
        count = llm_operation.mark_stale_operations_interrupted(stale_seconds=None)

    assert count == 3
    executed_sql = cursor.execute.call_args.args[0]
    assert "INTERVAL" not in executed_sql
    assert "status IN ('pending', 'processing')" in executed_sql
    db.return_value.commit.assert_called_once()


def test_mark_stale_operations_interrupted_with_age_threshold():
    cursor = MagicMock()
    cursor.rowcount = 1
    with patch.object(llm_operation, "get_cursor", return_value=cursor), patch.object(
        llm_operation, "get_db"
    ):
        count = llm_operation.mark_stale_operations_interrupted(stale_seconds=1800)

    assert count == 1
    executed_sql = cursor.execute.call_args.args[0]
    assert "created_at < (NOW() - INTERVAL %s SECOND)" in executed_sql
    assert cursor.execute.call_args.args[1] == (1800,)


def test_mark_stale_operations_interrupted_ignores_non_positive_threshold():
    with patch.object(llm_operation, "get_cursor") as get_cursor:
        assert llm_operation.mark_stale_operations_interrupted(stale_seconds=0) == 0
        assert llm_operation.mark_stale_operations_interrupted(stale_seconds=-5) == 0
        get_cursor.assert_not_called()


def test_mark_stale_operations_interrupted_survives_db_errors():
    from mysql.connector import Error as MySQLError

    cursor = MagicMock()
    cursor.execute.side_effect = MySQLError("boom")
    with patch.object(llm_operation, "get_cursor", return_value=cursor), patch.object(
        llm_operation, "get_db"
    ) as db:
        count = llm_operation.mark_stale_operations_interrupted(stale_seconds=None)

    assert count == 0
    db.return_value.rollback.assert_called_once()
