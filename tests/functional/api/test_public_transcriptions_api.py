import io

from app.models.user import get_user_by_username
from app.models import transcription as transcription_model
from app.models import role as role_model
from app.services import user_service
import app.api.transcriptions as transcriptions_api


def _generate_user_api_key(app):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
        key_data = user_service.generate_public_api_key(user.id)
    return user, key_data


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
