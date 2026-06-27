import io
import uuid

from app.models.user import get_user_by_username
from app.models import transcription as transcription_model
from app.models import role as role_model
from app.services import auth_service
from app.services import user_service
import app.api.transcriptions as transcriptions_api


def _generate_user_api_key(app):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
        key_data = user_service.generate_public_api_key(user.id)
    return user, key_data


def _create_transcription(app, user_id, status="processing"):
    job_id = str(uuid.uuid4())
    with app.app_context():
        transcription_model.create_transcription_job(
            job_id=job_id,
            user_id=user_id,
            filename="audio.mp3",
            api_used="gpt-4o-transcribe",
            file_size_mb=1.0,
            audio_length_minutes=0.89,
            context_prompt_used=False,
            public_api_invocation=True,
        )
        transcription_model.update_job_progress(job_id, "File uploaded")
        transcription_model.update_job_progress(job_id, "Transcription started")
        if status == "finished":
            transcription_model.finalize_job_success(job_id, "Transcript text", "en")
        elif status == "error":
            transcription_model.set_job_error(job_id, "Transcription failed")
        else:
            transcription_model.update_job_status(job_id, status)
    return job_id


def test_public_transcribe_creates_job(app, logged_in_client_with_permissions, monkeypatch):
    user, key_data = _generate_user_api_key(app)
    monkeypatch.setattr(transcriptions_api, "submit_transcription_job", lambda *args, **kwargs: None)

    data = {
        "audio_file": (io.BytesIO(b"fake audio bytes"), "sample.wav"),
    }
    response = logged_in_client_with_permissions.post(
        "/api/v1/transcribe",
        data=data,
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {key_data['api_key']}"},
    )

    assert response.status_code == 202
    job_id = response.get_json().get("job_id")
    assert job_id

    with app.app_context():
        job = transcription_model.get_transcription_by_id(job_id, user.id)
        assert job is not None
        assert job.get("public_api_invocation") is True


def test_public_api_keys_can_be_named_and_revoked_individually(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")

    first = logged_in_client_with_permissions.post(
        "/api/user/public-api-key",
        data={"name": "Production uploads"},
    )
    second = logged_in_client_with_permissions.post(
        "/api/user/public-api-key",
        data={"name": "Local testing"},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    first_payload = first.get_json()
    second_payload = second.get_json()
    assert first_payload["name"] == "Production uploads"
    assert second_payload["name"] == "Local testing"

    status_response = logged_in_client_with_permissions.get("/api/user/keys")
    assert status_response.status_code == 200
    keys = status_response.get_json()["public_api"]["keys"]
    assert {key["name"] for key in keys} == {"Production uploads", "Local testing"}

    revoke_response = logged_in_client_with_permissions.delete(
        f"/api/user/public-api-key/{first_payload['id']}"
    )
    assert revoke_response.status_code == 200

    with app.app_context():
        assert user_service.authenticate_public_api_key(first_payload["api_key"]) is None
        authenticated_user = user_service.authenticate_public_api_key(second_payload["api_key"])
        assert authenticated_user is not None
        assert authenticated_user.id == user.id

    status_response = logged_in_client_with_permissions.get("/api/user/keys")
    keys = status_response.get_json()["public_api"]["keys"]
    assert [key["name"] for key in keys] == ["Local testing"]


def test_public_transcribe_requires_permission(app, logged_in_client_with_permissions, monkeypatch):
    user, key_data = _generate_user_api_key(app)
    with app.app_context():
        role = role_model.get_role_by_id(user.role_id)
        role_model.update_role(role.id, {"allow_public_api_access": 0})

    monkeypatch.setattr(transcriptions_api, "submit_transcription_job", lambda *args, **kwargs: None)

    data = {
        "audio_file": (io.BytesIO(b"fake audio bytes"), "sample.wav"),
    }
    response = logged_in_client_with_permissions.post(
        "/api/v1/transcribe",
        data=data,
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {key_data['api_key']}"},
    )

    assert response.status_code == 403


def test_public_get_transcription_status_processing(app, logged_in_client_with_permissions):
    user, key_data = _generate_user_api_key(app)
    job_id = _create_transcription(app, user.id, status="processing")

    response = logged_in_client_with_permissions.get(
        f"/api/v1/transcribe/{job_id}",
        headers={"Authorization": f"Bearer {key_data['api_key']}"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["job_id"] == job_id
    assert payload["status"] == "processing"
    assert payload["finished"] is False
    assert payload["progress"][-2:] == ["File uploaded", "Transcription started"]
    assert payload["audio_length_minutes"] == 0.89
    assert payload["filename"] == "audio.mp3"
    assert payload["api_used"] == "gpt-4o-transcribe"
    assert "result" not in payload


def test_public_get_transcription_status_finished(app, logged_in_client_with_permissions):
    user, key_data = _generate_user_api_key(app)
    job_id = _create_transcription(app, user.id, status="finished")

    with app.test_client() as public_client:
        response = public_client.get(
            f"/api/v1/transcriptions/{job_id}",
            headers={"Authorization": f"Bearer {key_data['api_key']}"},
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "finished"
    assert payload["finished"] is True
    assert payload["result"]["transcription_text"] == "Transcript text"
    assert payload["result"]["detected_language"] == "en"
    assert payload["result"]["filename"] == "audio.mp3"
    assert payload["result"]["api_used"] == "gpt-4o-transcribe"
    assert payload["result"]["audio_length_minutes"] == 0.89
    assert payload["result"]["created_at"]


def test_public_get_transcription_status_error(app, logged_in_client_with_permissions):
    user, key_data = _generate_user_api_key(app)
    job_id = _create_transcription(app, user.id, status="error")

    response = logged_in_client_with_permissions.get(
        f"/api/v1/transcribe/{job_id}",
        headers={"Authorization": f"Bearer {key_data['api_key']}"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "error"
    assert payload["finished"] is True
    assert payload["error_message"] == "Transcription failed"
    assert "result" not in payload


def test_public_get_transcription_status_enforces_ownership(app, logged_in_client_with_permissions):
    user, key_data = _generate_user_api_key(app)
    with app.app_context():
        role = role_model.get_role_by_id(user.role_id)
        auth_service.create_user("other_user", "password123", "other@example.com", role.name)
        other_user = get_user_by_username("other_user")
    other_job_id = _create_transcription(app, other_user.id, status="finished")

    response = logged_in_client_with_permissions.get(
        f"/api/v1/transcribe/{other_job_id}",
        headers={"Authorization": f"Bearer {key_data['api_key']}"},
    )

    assert response.status_code == 403


def test_public_get_transcription_status_requires_api_key(logged_in_client_with_permissions):
    response = logged_in_client_with_permissions.get("/api/v1/transcribe/missing")

    assert response.status_code == 401
