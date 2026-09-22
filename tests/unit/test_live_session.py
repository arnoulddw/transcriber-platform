from unittest.mock import Mock

from app.models import live_session


def test_claim_chunk_sequence_distinguishes_in_flight_from_empty_replay(monkeypatch):
    cursor = Mock(rowcount=0)
    database = Mock()
    monkeypatch.setattr(live_session, "get_cursor", lambda: cursor)
    monkeypatch.setattr(live_session, "get_db", lambda: database)

    monkeypatch.setattr(
        live_session,
        "get_session",
        lambda _session_id: {
            "status": "active",
            "last_sequence": 4,
            "last_transcript": None,
        },
    )
    in_flight = live_session.claim_chunk_sequence("session", 4)

    monkeypatch.setattr(
        live_session,
        "get_session",
        lambda _session_id: {
            "status": "active",
            "last_sequence": 4,
            "last_transcript": "",
        },
    )
    completed_empty = live_session.claim_chunk_sequence("session", 4)

    assert in_flight == {
        "claimed": False,
        "duplicate": True,
        "in_progress": True,
        "transcript": "",
    }
    assert completed_empty == {
        "claimed": False,
        "duplicate": True,
        "in_progress": False,
        "transcript": "",
    }
    assert database.rollback.call_count == 2


def test_claim_chunk_sequence_requires_previous_result_before_advancing(monkeypatch):
    cursor = Mock(rowcount=0)
    database = Mock()
    monkeypatch.setattr(live_session, "get_cursor", lambda: cursor)
    monkeypatch.setattr(live_session, "get_db", lambda: database)
    monkeypatch.setattr(
        live_session,
        "get_session",
        lambda _session_id: {
            "status": "active",
            "last_sequence": 0,
            "last_transcript": None,
        },
    )

    result = live_session.claim_chunk_sequence("session", 1)

    assert result == {
        "claimed": False,
        "duplicate": False,
        "out_of_order": True,
        "last_sequence": 0,
    }
    query, params = cursor.execute.call_args_list[0].args
    assert "last_transcript IS NOT NULL" in query
    assert params == (1, "session", 0)
    database.rollback.assert_called_once_with()


def test_record_chunk_result_persists_when_hangup_is_already_closing(monkeypatch):
    cursor = Mock(rowcount=1)
    database = Mock()
    monkeypatch.setattr(live_session, "get_cursor", lambda: cursor)
    monkeypatch.setattr(live_session, "get_db", lambda: database)

    assert live_session.record_chunk_result("session", 0, "hello") is True

    query, params = cursor.execute.call_args.args
    assert "status IN ('active', 'closing')" in query
    assert params == ("hello", "session", 0)
    database.commit.assert_called_once_with()


def test_begin_finalize_does_not_claim_while_chunk_is_in_flight(monkeypatch):
    cursor = Mock(rowcount=0)
    database = Mock()
    monkeypatch.setattr(live_session, "get_cursor", lambda: cursor)
    monkeypatch.setattr(live_session, "get_db", lambda: database)
    monkeypatch.setattr(
        live_session,
        "get_session",
        lambda _session_id: {
            "status": "active",
            "last_sequence": 0,
            "last_transcript": None,
        },
    )

    assert live_session.begin_finalize("session") == "active"

    query, params = cursor.execute.call_args_list[0].args
    assert "last_transcript IS NOT NULL" in query
    assert params == ("session",)
    database.rollback.assert_called_once_with()


def test_release_chunk_sequence_can_release_after_hangup_starts(monkeypatch):
    cursor = Mock(rowcount=1)
    database = Mock()
    monkeypatch.setattr(live_session, "get_cursor", lambda: cursor)
    monkeypatch.setattr(live_session, "get_db", lambda: database)

    assert live_session.release_chunk_sequence("session", 3) is True

    query, params = cursor.execute.call_args.args
    assert "status IN ('active', 'closing')" in query
    assert params == (2, "session", 3)
    database.commit.assert_called_once_with()


def test_purge_expired_sessions_uses_bounded_batch_and_retention(monkeypatch):
    cursor = Mock(rowcount=3)
    database = Mock()
    monkeypatch.setattr(live_session, "get_cursor", lambda: cursor)
    monkeypatch.setattr(live_session, "get_db", lambda: database)

    deleted = live_session.purge_expired_sessions(
        retention_seconds=100,
        max_duration_seconds=200,
        now=1000,
        batch_size=25,
    )

    assert deleted == 3
    query, params = cursor.execute.call_args.args
    assert "DELETE FROM live_transcription_sessions" in query
    assert params == (900, 700, 25)
    database.commit.assert_called_once_with()
