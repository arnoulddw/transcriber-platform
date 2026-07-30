from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Flask
from flask_babel import Babel

from app.services import live_transcription_service as service


@pytest.fixture
def live_app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="unit-test-secret",
        DEPLOYMENT_MODE="single",
        OPENAI_API_KEY="server-only-openai-key",
        OPENAI_HTTP_TIMEOUT=5,
        LIVE_TRANSCRIPTION_MODEL="gpt-live-transcribe",
        LIVE_TRANSCRIPTION_SESSION_MAX_RETRIES=1,
        LIVE_TRANSCRIPTION_SESSION_RETRY_DELAY=0,
    )
    Babel(app)
    return app


def test_build_session_config_omits_languages_for_auto():
    config = service.build_session_config("gpt-live-transcribe", "auto", "")

    transcription = config["audio"]["input"]["transcription"]
    assert config["type"] == "transcription"
    assert transcription == {
        "model": "gpt-live-transcribe",
        "delay": "low",
    }
    assert config["audio"]["input"]["turn_detection"] is None


def test_build_session_config_adds_language_and_prompt():
    config = service.build_session_config(
        "gpt-live-transcribe",
        "fr",
        "An Adyen payments meeting.",
    )

    transcription = config["audio"]["input"]["transcription"]
    assert transcription["languages"] == ["fr"]
    assert transcription["prompt"] == "An Adyen payments meeting."


def test_create_session_posts_multipart_without_exposing_api_key(live_app, monkeypatch):
    user = SimpleNamespace(id=42)
    response = SimpleNamespace(
        status_code=200,
        text="answer-sdp",
        headers={"Location": "/v1/realtime/calls/rtc_test_call"},
    )
    post = MagicMock(return_value=response)
    monkeypatch.setattr(service, "_validate_settings", lambda *_: ("auto", ""))
    monkeypatch.setattr(service.httpx, "post", post)

    with live_app.app_context():
        result = service.create_session(user, "offer-sdp", "auto", "")

    assert result["answer_sdp"] == "answer-sdp"
    assert result["session_token"]
    call = post.call_args
    assert call.args[0] == "https://api.openai.com/v1/realtime/calls"
    assert call.kwargs["headers"]["Authorization"] == "Bearer server-only-openai-key"
    assert "server-only-openai-key" not in result["session_token"]
    assert call.kwargs["files"]["sdp"][1] == "offer-sdp"


def test_hangup_session_stops_the_openai_call(live_app, monkeypatch):
    user = SimpleNamespace(id=7)
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 7,
            "call_id": "rtc_test_call",
        },
    )
    post = MagicMock(return_value=SimpleNamespace(status_code=200))
    monkeypatch.setattr(service.httpx, "post", post)

    with live_app.app_context():
        result = service.hangup_session(user, "token")

    assert result == {"stopped": True}
    assert post.call_args.args[0].endswith(
        "/v1/realtime/calls/rtc_test_call/hangup"
    )


def test_call_id_is_read_from_openai_location_header():
    assert (
        service._call_id_from_location(
            "https://api.openai.com/v1/realtime/calls/rtc_123"
        )
        == "rtc_123"
    )


def test_finalize_session_saves_usage_and_normalizes_auto_language(live_app, monkeypatch):
    user = SimpleNamespace(id=7, enable_auto_title_generation=False)
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 7,
            "transcription_id": "live-job",
            "started_at": 1000.0,
            "language": "auto",
            "context_prompt_used": True,
        },
    )
    monkeypatch.setattr(service.time, "time", lambda: 1120.0)
    monkeypatch.setattr(service.transcription_model, "get_transcription_by_id", lambda *_: None)
    create_job = MagicMock()
    update_cost = MagicMock()
    finalize_job = MagicMock()
    disable_title = MagicMock()
    increment_usage = MagicMock()
    monkeypatch.setattr(service.transcription_model, "create_transcription_job", create_job)
    monkeypatch.setattr(service.transcription_model, "update_transcription_cost", update_cost)
    monkeypatch.setattr(service.transcription_model, "finalize_job_success", finalize_job)
    monkeypatch.setattr(service.transcription_model, "update_title_generation_status", disable_title)
    monkeypatch.setattr(service.role_model, "increment_usage", increment_usage)
    monkeypatch.setattr(service.pricing_service, "get_price", lambda *_: 0.25)

    with live_app.app_context():
        result = service.finalize_session(user, "token", "Hello from live mode.")

    assert result == {"transcription_id": "live-job", "saved": True}
    assert create_job.call_args.args[5] == pytest.approx(2.0)
    assert create_job.call_args.args[6] is True
    finalize_job.assert_called_once_with("live-job", "Hello from live mode.", "und")
    update_cost.assert_called_once_with("live-job", pytest.approx(0.5))
    increment_usage.assert_called_once_with(7, pytest.approx(0.5), pytest.approx(2.0))
    disable_title.assert_called_once_with("live-job", "disabled")


def test_finalize_session_caps_recorded_usage_at_120_minutes(
    live_app, monkeypatch
):
    user = SimpleNamespace(id=7, enable_auto_title_generation=False)
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 7,
            "transcription_id": "long-live-job",
            "started_at": 1000.0,
            "language": "en",
            "context_prompt_used": False,
        },
    )
    monkeypatch.setattr(service.time, "time", lambda: 1000.0 + (180 * 60))
    monkeypatch.setattr(
        service.transcription_model,
        "get_transcription_by_id",
        lambda *_: None,
    )
    create_job = MagicMock()
    monkeypatch.setattr(
        service.transcription_model,
        "create_transcription_job",
        create_job,
    )
    monkeypatch.setattr(
        service.transcription_model,
        "update_transcription_cost",
        MagicMock(),
    )
    monkeypatch.setattr(
        service.transcription_model,
        "finalize_job_success",
        MagicMock(),
    )
    monkeypatch.setattr(
        service.transcription_model,
        "update_title_generation_status",
        MagicMock(),
    )
    monkeypatch.setattr(service.role_model, "increment_usage", MagicMock())
    monkeypatch.setattr(service.pricing_service, "get_price", lambda *_: 0)

    with live_app.app_context():
        service.finalize_session(user, "token", "Long transcript")

    assert create_job.call_args.args[5] == 120


def test_finalize_session_is_idempotent(live_app, monkeypatch):
    user = SimpleNamespace(id=7)
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 7,
            "transcription_id": "existing-live-job",
            "started_at": 1000.0,
            "language": "en",
            "context_prompt_used": False,
        },
    )
    monkeypatch.setattr(
        service.transcription_model,
        "get_transcription_by_id",
        lambda *_: {
            "api_used": "gpt-live-transcribe",
            "status": "finished",
        },
    )
    create_job = MagicMock()
    monkeypatch.setattr(service.transcription_model, "create_transcription_job", create_job)

    with live_app.app_context():
        result = service.finalize_session(user, "token", "Duplicate request")

    assert result == {"transcription_id": "existing-live-job", "saved": True}
    create_job.assert_not_called()


def test_finalize_session_completes_existing_pending_record(live_app, monkeypatch):
    user = SimpleNamespace(id=7, enable_auto_title_generation=False)
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 7,
            "transcription_id": "pending-live-job",
            "started_at": 1000.0,
            "language": "nl",
            "context_prompt_used": False,
        },
    )
    monkeypatch.setattr(service.time, "time", lambda: 1060.0)
    monkeypatch.setattr(
        service.transcription_model,
        "get_transcription_by_id",
        lambda *_: {
            "api_used": "gpt-live-transcribe",
            "status": "pending",
        },
    )
    create_job = MagicMock()
    finalize_job = MagicMock()
    monkeypatch.setattr(service.transcription_model, "create_transcription_job", create_job)
    monkeypatch.setattr(service.transcription_model, "update_transcription_cost", MagicMock())
    monkeypatch.setattr(service.transcription_model, "finalize_job_success", finalize_job)
    monkeypatch.setattr(
        service.transcription_model, "update_title_generation_status", MagicMock()
    )
    monkeypatch.setattr(service.role_model, "increment_usage", MagicMock())
    monkeypatch.setattr(service.pricing_service, "get_price", lambda *_: None)

    with live_app.app_context():
        result = service.finalize_session(user, "token", "Recovered transcript")

    assert result == {"transcription_id": "pending-live-job", "saved": True}
    create_job.assert_not_called()
    finalize_job.assert_called_once_with(
        "pending-live-job", "Recovered transcript", "nl"
    )


def test_resolve_openai_key_falls_back_to_global_for_managed_users(
    live_app, monkeypatch
):
    live_app.config["DEPLOYMENT_MODE"] = "multi"
    user = SimpleNamespace(id=7)
    monkeypatch.setattr(service.user_service, "get_decrypted_api_key", lambda *_: None)
    monkeypatch.setattr(service, "check_permission", lambda *_: False)

    with live_app.app_context():
        assert service._resolve_openai_api_key(user) == "server-only-openai-key"


def test_create_session_maps_openai_transport_failure(live_app, monkeypatch):
    user = SimpleNamespace(id=42)
    monkeypatch.setattr(service, "_validate_settings", lambda *_: ("auto", ""))
    monkeypatch.setattr(
        service.httpx,
        "post",
        MagicMock(side_effect=service.httpx.ConnectError("offline")),
    )

    with live_app.app_context(), pytest.raises(service.LiveTranscriptionUpstreamError):
        service.create_session(user, "offer-sdp", "auto", "")


def test_create_session_retries_transient_gateway_failure(live_app, monkeypatch):
    user = SimpleNamespace(id=42)
    post = MagicMock(
        side_effect=[
            SimpleNamespace(status_code=504, text="gateway timeout"),
            SimpleNamespace(
                status_code=200,
                text="answer-sdp",
                headers={"Location": "/v1/realtime/calls/rtc_retry_call"},
            ),
        ]
    )
    monkeypatch.setattr(service, "_validate_settings", lambda *_: ("auto", ""))
    monkeypatch.setattr(service.httpx, "post", post)

    with live_app.app_context():
        result = service.create_session(user, "offer-sdp", "auto", "")

    assert result["answer_sdp"] == "answer-sdp"
    assert post.call_count == 2


def test_create_session_does_not_retry_non_transient_rejection(live_app, monkeypatch):
    user = SimpleNamespace(id=42)
    post = MagicMock(
        return_value=SimpleNamespace(status_code=400, text="invalid session")
    )
    monkeypatch.setattr(service, "_validate_settings", lambda *_: ("auto", ""))
    monkeypatch.setattr(service.httpx, "post", post)

    with live_app.app_context(), pytest.raises(service.LiveTranscriptionUpstreamError):
        service.create_session(user, "offer-sdp", "auto", "")

    assert post.call_count == 1


def test_finalize_session_dispatches_title_generation(live_app, monkeypatch):
    user = SimpleNamespace(id=7, enable_auto_title_generation=True)
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 7,
            "transcription_id": "titled-live-job",
            "started_at": 1000.0,
            "language": "en",
            "context_prompt_used": False,
        },
    )
    monkeypatch.setattr(service.time, "time", lambda: 1060.0)
    monkeypatch.setattr(service, "check_permission", lambda *_: True)
    monkeypatch.setattr(service.transcription_model, "get_transcription_by_id", lambda *_: None)
    monkeypatch.setattr(service.transcription_model, "create_transcription_job", MagicMock())
    monkeypatch.setattr(service.transcription_model, "update_transcription_cost", MagicMock())
    monkeypatch.setattr(service.transcription_model, "finalize_job_success", MagicMock())
    monkeypatch.setattr(service.role_model, "increment_usage", MagicMock())
    monkeypatch.setattr(service.pricing_service, "get_price", lambda *_: 0)
    thread = MagicMock()
    monkeypatch.setattr(service.threading, "Thread", thread)

    with live_app.app_context():
        service.finalize_session(user, "token", "Title this transcript")

    thread.assert_called_once()
    assert thread.call_args.kwargs["target"] is service.generate_title_task
    thread.return_value.start.assert_called_once()


def test_finalize_session_rejects_wrong_owner(live_app, monkeypatch):
    user = SimpleNamespace(id=8)
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 7,
            "transcription_id": "live-job",
            "started_at": 1000.0,
            "language": "en",
            "context_prompt_used": False,
        },
    )

    with live_app.app_context(), pytest.raises(service.LiveTranscriptionPermissionError):
        service.finalize_session(user, "token", "Text")
