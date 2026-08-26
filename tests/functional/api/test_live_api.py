from unittest.mock import patch

import pytest


@pytest.fixture
def logged_in_client_with_gemini_only_permission(app, clean_db):
    """A logged-in client whose role has only use_api_google_gemini among API permissions."""
    with app.test_client() as client:
        with app.app_context():
            from app.services import auth_service
            from app.models import role as role_model

            permissions = {"use_api_google_gemini": True}
            role = role_model.create_role(
                "gemini_live_role", "A role with only the Gemini permission", permissions
            )
            assert role is not None
            auth_service.create_user(
                "testuser_gemini_live",
                "password123",
                "gemini_live@example.com",
                "gemini_live_role",
            )

        client.post(
            "/login",
            data='{"username": "testuser_gemini_live", "password": "password123"}',
            content_type="application/json",
            headers={"Accept": "application/json"},
        )

        yield client


def test_default_roles_enable_live_only_for_admin(app, clean_db):
    with app.app_context():
        from app.initialization import create_default_roles
        from app.models import role as role_model

        create_default_roles()

        assert role_model.get_role_by_name("admin").use_api_openai_live_transcribe is True
        assert (
            role_model.get_role_by_name("beta-tester").use_api_openai_live_transcribe
            is False
        )


def test_live_page_requires_permission(logged_in_client):
    response = logged_in_client.get("/live")

    assert response.status_code == 403


def test_live_page_renders_for_permitted_user(logged_in_client_with_permissions):
    logged_in_client_with_permissions.application.config["BUILD_TIMESTAMP"] = "test-build"
    response = logged_in_client_with_permissions.get("/live")

    assert response.status_code == 200
    assert b'id="liveWorkspace"' in response.data
    assert b'id="liveMicrophone"' in response.data
    assert b'id="liveFollowButton"' in response.data
    assert b"Back to live" in response.data
    assert b"live_transcription.js?v=test-build" in response.data
    assert b'href="/live"' in response.data


def test_live_page_renders_for_gemini_only_user(
    logged_in_client_with_gemini_only_permission,
):
    logged_in_client_with_gemini_only_permission.application.config[
        "BUILD_TIMESTAMP"
    ] = "test-build"
    response = logged_in_client_with_gemini_only_permission.get("/live")

    assert response.status_code == 200
    assert b'id="liveWorkspace"' in response.data


def test_live_session_endpoint_admits_gemini_only_user(
    logged_in_client_with_gemini_only_permission,
):
    with patch(
        "app.api.live.live_transcription_service.create_session",
        return_value={
            "answer_sdp": "",
            "session_token": "signed-token",
            "transport": "gemini-wss",
        },
    ):
        response = logged_in_client_with_gemini_only_permission.post(
            "/api/live/session",
            json={"sdp": "offer", "language_code": "auto"},
        )

    assert response.status_code == 200
    assert response.get_json() == {
        "answer_sdp": "",
        "session_token": "signed-token",
        "transport": "gemini-wss",
    }


def test_live_session_endpoint_requires_permission(logged_in_client):
    response = logged_in_client.post(
        "/api/live/session",
        json={"sdp": "offer", "language_code": "auto"},
    )

    assert response.status_code == 403


def test_live_session_endpoint_returns_negotiation_result(
    logged_in_client_with_permissions,
):
    with patch(
        "app.api.live.live_transcription_service.create_session",
        return_value={"answer_sdp": "answer", "session_token": "signed-token"},
    ):
        response = logged_in_client_with_permissions.post(
            "/api/live/session",
            json={
                "sdp": "offer",
                "language_code": "auto",
                "context_prompt": "",
            },
        )

    assert response.status_code == 200
    assert response.get_json() == {
        "answer_sdp": "answer",
        "session_token": "signed-token",
    }


def test_live_finalize_endpoint_returns_saved_history_link(
    logged_in_client_with_permissions,
):
    with patch(
        "app.api.live.live_transcription_service.finalize_session",
        return_value={"transcription_id": "live-id", "saved": True},
    ):
        response = logged_in_client_with_permissions.post(
            "/api/live/finalize",
            json={"session_token": "signed-token", "transcript": "Hello"},
        )

    assert response.status_code == 200
    assert response.get_json() == {
        "history_url": "/",
        "saved": True,
        "transcription_id": "live-id",
    }


def test_live_stop_endpoint_hangs_up_remote_session(
    logged_in_client_with_permissions,
):
    with patch(
        "app.api.live.live_transcription_service.hangup_session",
        return_value={"stopped": True},
    ) as hangup:
        response = logged_in_client_with_permissions.post(
            "/api/live/stop",
            json={"session_token": "signed-token"},
        )

    assert response.status_code == 200
    assert response.get_json() == {"stopped": True}
    assert hangup.call_args.args[1] == "signed-token"
