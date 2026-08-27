import base64
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


@pytest.fixture(autouse=True)
def allow_live_reservation(monkeypatch):
    """Default live-minutes reservation to success; individual tests override."""
    monkeypatch.setattr(
        service.role_model,
        "reserve_usage_if_allowed",
        MagicMock(return_value=(True, "")),
    )


def test_openrouter_model_slugs_default_to_openrouter_transport(live_app):
    live_app.config.update(LIVE_TRANSCRIPTION_PROVIDERS={})

    with live_app.app_context():
        assert service._resolve_provider(None, "openai/whisper-1") == "openrouter"
        assert service._resolve_provider(None, "gpt-live-transcribe") == "openai"


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
    user = SimpleNamespace(id=42, role=SimpleNamespace(name="member"))
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


def test_create_session_routes_openrouter_to_sse_without_webrtc(live_app, monkeypatch):
    live_app.config.update(
        LIVE_TRANSCRIPTION_MODELS=["openai/whisper-1"],
        LIVE_TRANSCRIPTION_PROVIDERS={"openai/whisper-1": "openrouter"},
        OPENROUTER_API_KEY="server-only-openrouter-key",
    )
    user = SimpleNamespace(id=42, role=SimpleNamespace(name="member"))
    post = MagicMock()
    monkeypatch.setattr(service, "_validate_settings", lambda *_: ("auto", ""))
    monkeypatch.setattr(service.httpx, "post", post)

    with live_app.app_context():
        result = service.create_session(
            user, "", "auto", "", requested_model="openai/whisper-1"
        )

    assert result["transport"] == "openrouter-sse"
    assert result["answer_sdp"] == ""
    post.assert_not_called()


def test_create_session_openrouter_token_carries_context_prompt(live_app, monkeypatch):
    live_app.config.update(
        LIVE_TRANSCRIPTION_MODELS=["openai/whisper-1"],
        LIVE_TRANSCRIPTION_PROVIDERS={"openai/whisper-1": "openrouter"},
        OPENROUTER_API_KEY="server-only-openrouter-key",
    )
    monkeypatch.setattr(
        service, "_validate_settings", lambda *_: ("en", "Project Falcon budget")
    )
    monkeypatch.setattr(service.httpx, "post", MagicMock())

    with live_app.app_context():
        result = service.create_session(
            SimpleNamespace(id=42, role=SimpleNamespace(name="member")),
            "",
            "en",
            "Project Falcon budget",
            requested_model="openai/whisper-1",
        )
        payload = service._serializer().loads(result["session_token"])

    assert payload["context_prompt"] == "Project Falcon budget"


def test_live_model_from_config_is_allowed_for_openrouter(live_app, monkeypatch):
    live_app.config.update(
        LIVE_TRANSCRIPTION_MODELS=["openai/whisper-1"],
        LIVE_TRANSCRIPTION_PROVIDERS={"openai/whisper-1": "openrouter"},
        OPENROUTER_API_KEY="server-only-openrouter-key",
    )
    monkeypatch.setattr(service, "_validate_settings", lambda *_: ("auto", ""))
    with live_app.app_context():
        assert service._resolve_live_model(
            SimpleNamespace(id=42), "openai/whisper-1"
        ) == "openai/whisper-1"


def test_canonical_live_model_reference_returns_provider_local_model(live_app):
    live_app.config.update(
        LIVE_TRANSCRIPTION_MODELS=["openai:gpt-live-transcribe"],
        LIVE_TRANSCRIPTION_PROVIDERS={"openai:gpt-live-transcribe": "openai"},
    )

    with live_app.app_context():
        assert service._resolve_live_model(
            SimpleNamespace(id=42), "openai:gpt-live-transcribe"
        ) == "gpt-live-transcribe"


def test_canonical_openrouter_live_model_reference_returns_slug(live_app):
    live_app.config.update(
        LIVE_TRANSCRIPTION_MODELS=["openrouter:openai/whisper-1"],
        LIVE_TRANSCRIPTION_PROVIDERS={"openrouter:openai/whisper-1": "openrouter"},
        OPENROUTER_API_KEY="server-only-openrouter-key",
    )

    with live_app.app_context():
        assert service._resolve_live_model(
            SimpleNamespace(id=42), "openrouter:openai/whisper-1"
        ) == "openai/whisper-1"


def test_live_model_rejects_openrouter_slug_not_listed_as_stt(live_app):
    live_app.config.update(
        LIVE_TRANSCRIPTION_MODELS=["google/gemini-3.7-flash"],
        LIVE_TRANSCRIPTION_PROVIDERS={"google/gemini-3.7-flash": "openrouter"},
    )

    with live_app.app_context(), pytest.raises(service.LiveTranscriptionValidationError):
        service._resolve_live_model(SimpleNamespace(id=42), "google/gemini-3.7-flash")


def test_configured_openrouter_stt_model_can_be_enabled_explicitly(live_app):
    live_app.config.update(
        LIVE_TRANSCRIPTION_MODELS=["vendor/new-stt"],
        LIVE_TRANSCRIPTION_PROVIDERS={"vendor/new-stt": "openrouter"},
        OPENROUTER_LIVE_TRANSCRIPTION_MODELS=["vendor/new-stt"],
    )

    with live_app.app_context():
        assert service._resolve_live_model(SimpleNamespace(id=42), "vendor/new-stt") == "vendor/new-stt"


def test_openrouter_live_chunk_ignores_sse_comments_and_accumulates_text(live_app, monkeypatch):
    class FakeStreamResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_lines(self):
            return [
                ": OPENROUTER PROCESSING",
                'data: {"choices":[{"delta":{"content":"Hello "}}]}',
                "",
                'data: {"choices":[{"delta":{"content":"world"}}]}',
                "",
                "data: [DONE]",
            ]

    live_app.config.update(
        DEPLOYMENT_MODE="single",
        OPENROUTER_API_KEY="server-only-openrouter-key",
        OPENROUTER_BASE_URL="https://openrouter.ai/api/v1",
    )
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 42,
            "transcription_id": "openrouter-live-job",
            "started_at": 1000.0,
            "context_prompt_used": False,
            "provider": "openrouter",
            "model": "openai/whisper-1",
            "language": "en",
        },
    )
    stream = MagicMock(return_value=FakeStreamResponse())
    monkeypatch.setattr(service.httpx, "stream", stream)
    audio = base64.b64encode(b"RIFF-test-wav").decode("ascii")

    with live_app.app_context():
        result = service.transcribe_openrouter_chunk(
            SimpleNamespace(id=42), "signed-token", audio, "wav", 3
        )

    assert result == {"sequence": 3, "transcript": "Hello world"}
    assert stream.call_args.args[:2] == (
        "POST",
        "https://openrouter.ai/api/v1/chat/completions",
    )
    assert stream.call_args.kwargs["json"]["stream"] is True
    assert stream.call_args.kwargs["json"]["messages"][0]["content"][1]["input_audio"]["format"] == "wav"


def test_openrouter_live_chunk_rejects_non_openrouter_session(live_app, monkeypatch):
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 42,
            "provider": "openai",
            "model": "gpt-live-transcribe",
        },
    )

    with live_app.app_context(), pytest.raises(service.LiveTranscriptionValidationError):
        service.transcribe_openrouter_chunk(
            SimpleNamespace(id=42), "signed-token", "cmFuZG9t", "wav", 0
        )


def test_openrouter_live_chunk_applies_context_prompt(live_app, monkeypatch):
    class FakeStreamResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_lines(self):
            return [
                'data: {"choices":[{"delta":{"content":"Hi"}}]}',
                "",
                "data: [DONE]",
            ]

    live_app.config.update(
        DEPLOYMENT_MODE="single",
        OPENROUTER_API_KEY="server-only-openrouter-key",
        OPENROUTER_BASE_URL="https://openrouter.ai/api/v1",
    )
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 42,
            "transcription_id": "openrouter-live-job",
            "started_at": 1000.0,
            "context_prompt_used": True,
            "context_prompt": "Project Falcon budget",
            "provider": "openrouter",
            "model": "openai/whisper-1",
            "language": "auto",
        },
    )
    stream = MagicMock(return_value=FakeStreamResponse())
    monkeypatch.setattr(service.httpx, "stream", stream)
    audio = base64.b64encode(b"RIFF-test-wav").decode("ascii")

    with live_app.app_context():
        service.transcribe_openrouter_chunk(
            SimpleNamespace(id=42), "signed-token", audio, "wav", 0
        )

    text_part = stream.call_args.kwargs["json"]["messages"][0]["content"][0]["text"]
    assert "Project Falcon budget" in text_part


def test_openrouter_live_chunk_without_prompt_keeps_plain_instruction(live_app, monkeypatch):
    class FakeStreamResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_lines(self):
            return ["data: [DONE]"]

    live_app.config.update(
        DEPLOYMENT_MODE="single",
        OPENROUTER_API_KEY="server-only-openrouter-key",
        OPENROUTER_BASE_URL="https://openrouter.ai/api/v1",
    )
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 42,
            "transcription_id": "openrouter-live-job",
            "started_at": 1000.0,
            "context_prompt_used": False,
            "provider": "openrouter",
            "model": "openai/whisper-1",
            "language": "auto",
        },
    )
    stream = MagicMock(return_value=FakeStreamResponse())
    monkeypatch.setattr(service.httpx, "stream", stream)
    audio = base64.b64encode(b"RIFF-test-wav").decode("ascii")

    with live_app.app_context():
        service.transcribe_openrouter_chunk(
            SimpleNamespace(id=42), "signed-token", audio, "wav", 0
        )

    text_part = stream.call_args.kwargs["json"]["messages"][0]["content"][0]["text"]
    assert text_part == (
        "Transcribe only the spoken words in this audio. Return only the transcript."
    )


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


def test_finalize_session_bills_only_minutes_beyond_reservation(live_app, monkeypatch):
    """A 25-minute live session bills only the 15 minutes past the reservation."""
    user = SimpleNamespace(id=7, enable_auto_title_generation=False)
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 7,
            "transcription_id": "over-reservation-live-job",
            "started_at": 1000.0,
            "language": "auto",
            "context_prompt_used": False,
        },
    )
    monkeypatch.setattr(service.time, "time", lambda: 1000.0 + (25 * 60))
    monkeypatch.setattr(service.transcription_model, "get_transcription_by_id", lambda *_: None)
    increment_usage = MagicMock()
    monkeypatch.setattr(service.transcription_model, "create_transcription_job", MagicMock())
    monkeypatch.setattr(service.transcription_model, "update_transcription_cost", MagicMock())
    monkeypatch.setattr(service.transcription_model, "finalize_job_success", MagicMock())
    monkeypatch.setattr(
        service.transcription_model, "update_title_generation_status", MagicMock()
    )
    monkeypatch.setattr(service.role_model, "increment_usage", increment_usage)
    monkeypatch.setattr(service.pricing_service, "get_price", lambda *_: 0)

    with live_app.app_context():
        service.finalize_session(user, "token", "Long live transcript.")

    assert increment_usage.call_args.kwargs["live_minutes_processed"] == pytest.approx(15.0)


def test_finalize_session_short_bills_zero_extra_live_minutes(live_app, monkeypatch):
    """Sessions within the reservation bill no additional live minutes."""
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
    finalize_job.assert_called_once_with("live-job", "Hello from live mode.", "unknown")
    update_cost.assert_called_once_with("live-job", pytest.approx(0.5))
    increment_usage.assert_called_once_with(
        7, pytest.approx(0.5), pytest.approx(2.0), live_minutes_processed=pytest.approx(0.0)
    )
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
    user = SimpleNamespace(id=42, role=SimpleNamespace(name="member"))
    monkeypatch.setattr(service, "_validate_settings", lambda *_: ("auto", ""))
    monkeypatch.setattr(
        service.httpx,
        "post",
        MagicMock(side_effect=service.httpx.ConnectError("offline")),
    )

    with live_app.app_context(), pytest.raises(service.LiveTranscriptionUpstreamError):
        service.create_session(user, "offer-sdp", "auto", "")


def test_create_session_retries_transient_gateway_failure(live_app, monkeypatch):
    user = SimpleNamespace(id=42, role=SimpleNamespace(name="member"))
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
    user = SimpleNamespace(id=42, role=SimpleNamespace(name="member"))
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


def test_resolve_saved_language_prefers_detected_language():
    assert service._resolve_saved_language("auto", "FR") == "fr"
    assert service._resolve_saved_language("en", "nl") == "nl"


def test_resolve_saved_language_ignores_invalid_detected_language():
    assert service._resolve_saved_language("auto", "  ") == "unknown"
    assert service._resolve_saved_language("auto", "not a language value!") == "unknown"
    assert service._resolve_saved_language("auto", None) == "unknown"
    assert service._resolve_saved_language("auto", "a/b") == "unknown"


def test_resolve_saved_language_falls_back_to_requested():
    assert service._resolve_saved_language("nl", None) == "nl"
    assert service._resolve_saved_language("nl", "") == "nl"


def test_finalize_session_stores_detected_language(live_app, monkeypatch):
    user = SimpleNamespace(id=7, enable_auto_title_generation=False)
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 7,
            "transcription_id": "live-job",
            "started_at": 1000.0,
            "language": "auto",
            "context_prompt_used": False,
        },
    )
    monkeypatch.setattr(service.time, "time", lambda: 1120.0)
    monkeypatch.setattr(service.transcription_model, "get_transcription_by_id", lambda *_: None)
    finalize_job = MagicMock()
    monkeypatch.setattr(service.transcription_model, "create_transcription_job", MagicMock())
    monkeypatch.setattr(service.transcription_model, "update_transcription_cost", MagicMock())
    monkeypatch.setattr(service.transcription_model, "finalize_job_success", finalize_job)
    monkeypatch.setattr(service.transcription_model, "update_title_generation_status", MagicMock())
    monkeypatch.setattr(service.role_model, "increment_usage", MagicMock())
    monkeypatch.setattr(service.pricing_service, "get_price", lambda *_: None)

    with live_app.app_context():
        service.finalize_session(
            user, "token", "Bonjour", detected_language="fr"
        )

    finalize_job.assert_called_once_with("live-job", "Bonjour", "fr")


def test_finalize_session_stores_unknown_when_no_language_reported(live_app, monkeypatch):
    user = SimpleNamespace(id=7, enable_auto_title_generation=False)
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 7,
            "transcription_id": "live-job",
            "started_at": 1000.0,
            "language": "auto",
            "context_prompt_used": False,
        },
    )
    monkeypatch.setattr(service.time, "time", lambda: 1120.0)
    monkeypatch.setattr(service.transcription_model, "get_transcription_by_id", lambda *_: None)
    finalize_job = MagicMock()
    monkeypatch.setattr(service.transcription_model, "create_transcription_job", MagicMock())
    monkeypatch.setattr(service.transcription_model, "update_transcription_cost", MagicMock())
    monkeypatch.setattr(service.transcription_model, "finalize_job_success", finalize_job)
    monkeypatch.setattr(service.transcription_model, "update_title_generation_status", MagicMock())
    monkeypatch.setattr(service.role_model, "increment_usage", MagicMock())
    monkeypatch.setattr(service.pricing_service, "get_price", lambda *_: None)

    with live_app.app_context():
        service.finalize_session(user, "token", "Hello", detected_language=None)

    finalize_job.assert_called_once_with("live-job", "Hello", "unknown")


def test_create_session_reserves_live_minutes_before_openai_call(live_app, monkeypatch):
    user = SimpleNamespace(id=42, role=SimpleNamespace(name="member"))
    reserve = MagicMock(return_value=(True, ""))
    monkeypatch.setattr(service.role_model, "reserve_usage_if_allowed", reserve)
    monkeypatch.setattr(service, "_validate_settings", lambda *_: ("auto", ""))
    post = MagicMock(
        return_value=SimpleNamespace(
            status_code=200,
            text="answer-sdp",
            headers={"Location": "/v1/realtime/calls/rtc_reserve"},
        )
    )
    monkeypatch.setattr(service.httpx, "post", post)

    with live_app.app_context():
        service.create_session(user, "offer-sdp", "auto", "")

    assert post.call_count == 1
    assert reserve.call_count == 1
    assert reserve.call_args.kwargs["live_minutes_to_add"] == 10.0
    assert reserve.call_args.args[0] == 42


def test_create_session_reserves_before_openrouter_token(live_app, monkeypatch):
    live_app.config.update(
        LIVE_TRANSCRIPTION_MODELS=["openai/whisper-1"],
        LIVE_TRANSCRIPTION_PROVIDERS={"openai/whisper-1": "openrouter"},
        OPENROUTER_API_KEY="server-only-openrouter-key",
    )
    user = SimpleNamespace(id=42, role=SimpleNamespace(name="member"))
    reserve = MagicMock(return_value=(True, ""))
    monkeypatch.setattr(service.role_model, "reserve_usage_if_allowed", reserve)
    monkeypatch.setattr(service, "_validate_settings", lambda *_: ("auto", ""))

    with live_app.app_context():
        result = service.create_session(
            user, "", "auto", "", requested_model="openai/whisper-1"
        )

    assert result["transport"] == "openrouter-sse"
    assert reserve.call_count == 1
    assert reserve.call_args.kwargs["live_minutes_to_add"] == 10.0


def test_create_session_rejects_when_live_quota_exhausted(live_app, monkeypatch):
    user = SimpleNamespace(id=42, role=SimpleNamespace(name="member"))
    monkeypatch.setattr(
        service.role_model,
        "reserve_usage_if_allowed",
        MagicMock(return_value=(False, "You have reached your fair use limit.")),
    )
    monkeypatch.setattr(service, "_validate_settings", lambda *_: ("auto", ""))
    post = MagicMock()
    monkeypatch.setattr(service.httpx, "post", post)

    with live_app.app_context(), pytest.raises(
        service.LiveTranscriptionPermissionError
    ):
        service.create_session(user, "offer-sdp", "auto", "")

    post.assert_not_called()


def test_create_session_fails_closed_when_reservation_errors(live_app, monkeypatch):
    user = SimpleNamespace(id=42, role=SimpleNamespace(name="member"))
    monkeypatch.setattr(
        service.role_model,
        "reserve_usage_if_allowed",
        MagicMock(side_effect=RuntimeError("db down")),
    )
    monkeypatch.setattr(service, "_validate_settings", lambda *_: ("auto", ""))
    post = MagicMock()
    monkeypatch.setattr(service.httpx, "post", post)

    with live_app.app_context(), pytest.raises(
        service.LiveTranscriptionUpstreamError
    ):
        service.create_session(user, "offer-sdp", "auto", "")

    post.assert_not_called()


def test_finalize_session_records_actual_live_minutes(live_app, monkeypatch):
    user = SimpleNamespace(id=7, enable_auto_title_generation=False)
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 7,
            "transcription_id": "live-job",
            "started_at": 1000.0,
            "language": "auto",
            "context_prompt_used": False,
        },
    )
    monkeypatch.setattr(service.time, "time", lambda: 1120.0)
    monkeypatch.setattr(service.transcription_model, "get_transcription_by_id", lambda *_: None)
    monkeypatch.setattr(service.transcription_model, "create_transcription_job", MagicMock())
    monkeypatch.setattr(service.transcription_model, "update_transcription_cost", MagicMock())
    monkeypatch.setattr(service.transcription_model, "finalize_job_success", MagicMock())
    monkeypatch.setattr(service.transcription_model, "update_title_generation_status", MagicMock())
    increment_usage = MagicMock()
    monkeypatch.setattr(service.role_model, "increment_usage", increment_usage)
    monkeypatch.setattr(service.pricing_service, "get_price", lambda *_: None)

    with live_app.app_context():
        service.finalize_session(user, "token", "Hello from live mode.")

    assert increment_usage.call_count == 1
    assert increment_usage.call_args.kwargs.get("live_minutes_processed") == pytest.approx(0.0)


# --- Gemini Live WebSocket transport ---------------------------------------


GEMINI_LIVE_CONFIG = {
    "LIVE_TRANSCRIPTION_MODELS": ["gemini-3.5-transcribe-live"],
    "LIVE_TRANSCRIPTION_PROVIDERS": {"gemini-3.5-transcribe-live": "gemini"},
    "GEMINI_API_KEY": "server-only-gemini-key",
}


def _gemini_fake_client(monkeypatch, token_name="tokens/gemini-ephemeral"):
    fake_client = MagicMock()
    fake_client.auth_tokens.create.return_value.name = token_name
    gemini_client = MagicMock(return_value=fake_client)
    monkeypatch.setattr(service, "_gemini_client", gemini_client)
    return gemini_client, fake_client


def test_resolve_provider_prefix_rule_maps_gemini_models(live_app):
    live_app.config.update(LIVE_TRANSCRIPTION_PROVIDERS={})

    with live_app.app_context():
        assert service._resolve_provider(None, "gemini-3.5-transcribe-live") == "gemini"
        assert service._resolve_provider(None, "gpt-live-transcribe") == "openai"
        assert service._resolve_provider(None, "openai/whisper-1") == "openrouter"


def test_configured_provider_beats_gemini_prefix_rule(live_app):
    live_app.config.update(
        LIVE_TRANSCRIPTION_PROVIDERS={"gemini-3.5-transcribe-live": "openrouter"}
    )

    with live_app.app_context():
        assert service._resolve_provider(None, "gemini-3.5-transcribe-live") == "openrouter"


def test_build_live_connect_constraints_auto_language_has_empty_language_codes():
    constraints = service.build_live_connect_constraints("gemini-3.5-transcribe-live", "auto", "")

    config = constraints["config"]
    assert constraints["model"] == "gemini-3.5-transcribe-live"
    assert config["response_modalities"] == ["TEXT"]
    assert config["input_audio_transcription"] == {"language_codes": []}
    assert config["session_resumption"] == {}


def test_build_live_connect_constraints_adds_explicit_language_code():
    constraints = service.build_live_connect_constraints("gemini-3.5-transcribe-live", "es", "")

    language_codes = constraints["config"]["input_audio_transcription"]["language_codes"]
    assert language_codes == ["es"]


def test_build_live_connect_constraints_dedupes_prompt_vocabulary():
    prompt = "alpha, beta ,\n alpha\nbeta\ngamma,,gamma"
    constraints = service.build_live_connect_constraints("m", "auto", prompt)

    vocab = constraints["config"]["input_audio_transcription"]["custom_vocabulary"]
    assert vocab == ["alpha", "beta", "gamma"]


def test_build_live_connect_constraints_dedupes_prompt_vocabulary_case_insensitively():
    constraints = service.build_live_connect_constraints("m", "auto", "Alpha, alpha ,ALPHA")

    vocab = constraints["config"]["input_audio_transcription"]["custom_vocabulary"]
    assert vocab == ["Alpha"]


def test_build_live_connect_constraints_caps_custom_vocabulary_at_1000_terms():
    prompt = ", ".join(f"term{i}" for i in range(1200))
    constraints = service.build_live_connect_constraints("m", "auto", prompt)

    vocab = constraints["config"]["input_audio_transcription"]["custom_vocabulary"]
    assert len(vocab) == 1000


def test_build_live_connect_constraints_omits_vocabulary_for_empty_prompt():
    constraints = service.build_live_connect_constraints("m", "auto", "")

    assert "custom_vocabulary" not in constraints["config"]["input_audio_transcription"]


def test_create_session_gemini_mints_ephemeral_token_without_webrtc(live_app, monkeypatch):
    live_app.config.update(**GEMINI_LIVE_CONFIG)
    user = SimpleNamespace(id=42, role=SimpleNamespace(name="member"))
    monkeypatch.setattr(
        service, "_validate_settings", lambda *_: ("fr", "Falcon budget")
    )
    post = MagicMock()
    monkeypatch.setattr(service.httpx, "post", post)
    gemini_client, fake_client = _gemini_fake_client(monkeypatch, "tokens/live-42")

    with live_app.app_context():
        result = service.create_session(
            user,
            "",
            "fr",
            "Falcon budget",
            requested_model="gemini-3.5-transcribe-live",
        )
        payload = service._serializer().loads(result["session_token"])

    assert result["transport"] == "gemini-wss"
    assert result["answer_sdp"] == ""
    assert result["ephemeral_token"] == "tokens/live-42"
    assert result["ws_url"].startswith("wss://generativelanguage.googleapis.com/")
    assert result["session_token"]
    post.assert_not_called()
    assert payload["provider"] == "gemini"
    assert payload["transport"] == "gemini-wss"
    assert payload["model"] == "gemini-3.5-transcribe-live"
    assert payload["context_prompt"] == "Falcon budget"
    assert gemini_client.call_args.args[0] == "server-only-gemini-key"
    token_config = fake_client.auth_tokens.create.call_args.kwargs["config"]
    assert token_config["uses"] == 1
    constraints = token_config["live_connect_constraints"]
    assert constraints["model"] == "gemini-3.5-transcribe-live"
    assert constraints["config"]["session_resumption"] == {}
    assert constraints["config"]["input_audio_transcription"]["custom_vocabulary"] == [
        "Falcon budget"
    ]
    fake_client.close.assert_called_once()


def test_create_session_gemini_requires_an_api_key(live_app, monkeypatch):
    live_app.config.update(**GEMINI_LIVE_CONFIG)
    live_app.config.update(LIVE_TRANSCRIPTION_PROVIDERS={}, GEMINI_API_KEY=None)
    user = SimpleNamespace(id=42, role=SimpleNamespace(name="member"))
    monkeypatch.setattr(service, "_validate_settings", lambda *_: ("auto", ""))

    with live_app.app_context(), pytest.raises(service.MissingApiKeyError):
        service.create_session(
            user, "", "auto", "", requested_model="gemini-3.5-transcribe-live"
        )


def test_create_session_gemini_wraps_upstream_failures(live_app, monkeypatch):
    live_app.config.update(**GEMINI_LIVE_CONFIG)
    user = SimpleNamespace(id=42, role=SimpleNamespace(name="member"))
    monkeypatch.setattr(service, "_validate_settings", lambda *_: ("auto", ""))
    _, fake_client = _gemini_fake_client(monkeypatch)
    fake_client.auth_tokens.create.side_effect = RuntimeError("token endpoint down")

    with live_app.app_context(), pytest.raises(service.LiveTranscriptionUpstreamError):
        service.create_session(
            user, "", "auto", "", requested_model="gemini-3.5-transcribe-live"
        )

    fake_client.close.assert_called_once()


def test_create_session_gemini_wraps_client_construction_failures(live_app, monkeypatch):
    live_app.config.update(**GEMINI_LIVE_CONFIG)
    user = SimpleNamespace(id=42, role=SimpleNamespace(name="member"))
    monkeypatch.setattr(service, "_validate_settings", lambda *_: ("auto", ""))
    monkeypatch.setattr(
        service,
        "_gemini_client",
        MagicMock(side_effect=RuntimeError("client initialization failed")),
    )

    with live_app.app_context(), pytest.raises(service.LiveTranscriptionUpstreamError):
        service.create_session(
            user, "", "auto", "", requested_model="gemini-3.5-transcribe-live"
        )


def test_refresh_session_token_mints_fresh_gemini_token_without_new_reservation(
    live_app, monkeypatch
):
    live_app.config.update(**GEMINI_LIVE_CONFIG)
    user = SimpleNamespace(id=42, role=SimpleNamespace(name="member"))
    monkeypatch.setattr(service, "_validate_settings", lambda *_: ("fr", "Falcon budget"))
    reserve = MagicMock(return_value=(True, ""))
    monkeypatch.setattr(service.role_model, "reserve_usage_if_allowed", reserve)
    gemini_client, fake_client = _gemini_fake_client(monkeypatch, "tokens/refreshed")

    with live_app.app_context():
        created = service.create_session(
            user,
            "",
            "fr",
            "Falcon budget",
            requested_model="gemini-3.5-transcribe-live",
        )
        refreshed = service.refresh_session_token(user, created["session_token"])

    assert refreshed == {
        "ephemeral_token": "tokens/refreshed",
        "ws_url": service.GEMINI_WS_URL,
    }
    # One reservation per logical session: taken at create only.
    assert reserve.call_count == 1
    assert gemini_client.call_count == 2
    assert fake_client.close.call_count == 2


def test_refresh_session_token_rejects_non_gemini_sessions(live_app, monkeypatch):
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 42,
            "transcription_id": "live-job",
            "started_at": service.time.time(),
            "language": "auto",
            "context_prompt_used": False,
            "provider": "openai",
            "model": "gpt-live-transcribe",
            "transport": "openai-webrtc",
        },
    )

    with live_app.app_context(), pytest.raises(
        service.LiveTranscriptionValidationError, match="cannot be refreshed"
    ):
        service.refresh_session_token(SimpleNamespace(id=42), "token")


def test_refresh_session_token_rejects_sessions_past_max_duration(live_app, monkeypatch):
    started_at = 1000.0
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 42,
            "transcription_id": "long-gemini-job",
            "started_at": started_at,
            "language": "auto",
            "context_prompt_used": False,
            "provider": "gemini",
            "model": "gemini-3.5-transcribe-live",
            "transport": "gemini-wss",
        },
    )
    monkeypatch.setattr(
        service.time,
        "time",
        lambda: started_at + (service.MAX_SESSION_DURATION_MINUTES * 60) + 1,
    )

    with live_app.app_context(), pytest.raises(
        service.LiveTranscriptionValidationError, match="maximum duration"
    ):
        service.refresh_session_token(SimpleNamespace(id=42), "token")


def test_hangup_session_stops_gemini_websocket_sessions_without_http_call(
    live_app, monkeypatch
):
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 7,
            "transcription_id": "gemini-live-job",
            "started_at": 1000.0,
            "language": "auto",
            "context_prompt_used": False,
            "provider": "gemini",
            "model": "gemini-3.5-transcribe-live",
            "transport": "gemini-wss",
        },
    )
    post = MagicMock()
    monkeypatch.setattr(service.httpx, "post", post)

    with live_app.app_context():
        assert service.hangup_session(SimpleNamespace(id=7), "token") == {"stopped": True}

    post.assert_not_called()


def test_hangup_session_stops_openrouter_transport_without_http_call(live_app, monkeypatch):
    monkeypatch.setattr(
        service,
        "_decode_session_token",
        lambda _token: {
            "user_id": 7,
            "transcription_id": "sse-live-job",
            "started_at": 1000.0,
            "language": "auto",
            "context_prompt_used": False,
            "provider": "openrouter",
            "model": "openai/whisper-1",
            "transport": "openrouter-sse",
        },
    )
    post = MagicMock()
    monkeypatch.setattr(service.httpx, "post", post)

    with live_app.app_context():
        assert service.hangup_session(SimpleNamespace(id=7), "token") == {"stopped": True}

    post.assert_not_called()
