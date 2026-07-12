import uuid
from unittest.mock import patch

from app.models import transcription as transcription_model
from app.models import transcription_utils
from app.models.user import get_user_by_username


def _create_transcription(app, user_id, status="finished"):
    job_id = str(uuid.uuid4())
    with app.app_context():
        transcription_model.create_transcription_job(
            job_id=job_id,
            user_id=user_id,
            filename="audio.mp3",
            api_used="whisper",
            file_size_mb=1.0,
            audio_length_minutes=1.2,
            context_prompt_used=False,
        )
        if status == "finished":
            transcription_model.finalize_job_success(
                job_id, "Transcript text", "en"
            )
        else:
            transcription_model.update_job_status(job_id, status)
    return job_id


def test_get_progress_finished_job(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
    job_id = _create_transcription(app, user.id)
    with app.app_context():
        cursor = transcription_model.get_cursor()
        cursor.execute(
            """
            UPDATE transcriptions
            SET title_generation_status='processing'
            WHERE id=%s
            """,
            (job_id,),
        )
        transcription_model.get_db().commit()

    response = logged_in_client_with_permissions.get(
        f"/api/progress/{job_id}"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["job_id"] == job_id
    assert payload["finished"] is True
    assert payload["should_poll_title"] is True
    assert payload["result"]["status"] == "finished"


def test_get_progress_not_found(logged_in_client_with_permissions):
    response = logged_in_client_with_permissions.get(
        "/api/progress/missing"
    )
    assert response.status_code == 404


def test_get_active_transcriptions_returns_unfinished_jobs(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
    assert user is not None
    pending_job_id = _create_transcription(app, user.id, status="pending")
    processing_job_id = _create_transcription(app, user.id, status="processing")
    _create_transcription(app, user.id, status="finished")

    response = logged_in_client_with_permissions.get("/api/transcriptions/active")

    assert response.status_code == 200
    payload = response.get_json()
    assert [job["job_id"] for job in payload] == [processing_job_id, pending_job_id]
    assert all(job["status"] in {"pending", "processing", "cancelling"} for job in payload)


def test_interrupted_job_is_a_finished_error_state(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
    assert user is not None
    job_id = _create_transcription(app, user.id, status="interrupted")
    with app.app_context():
        cursor = transcription_model.get_cursor()
        cursor.execute(
            "UPDATE transcriptions SET error_message=%s WHERE id=%s",
            ("WORKER_INTERRUPTED: restart", job_id),
        )
        transcription_model.get_db().commit()

    response = logged_in_client_with_permissions.get(f"/api/progress/{job_id}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["finished"] is True
    assert payload["status"] == "interrupted"
    assert payload["error_message"].startswith("WORKER_INTERRUPTED")

    active_response = logged_in_client_with_permissions.get("/api/transcriptions/active")
    assert active_response.status_code == 200
    assert job_id in [job["job_id"] for job in active_response.get_json()]


def test_marks_abandoned_jobs_interrupted(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
    assert user is not None
    job_id = _create_transcription(app, user.id, status="processing")

    with app.app_context():
        count = transcription_model.mark_active_jobs_interrupted([job_id])
        job = transcription_model.get_transcription_by_id(job_id, user.id)

    assert count >= 1
    assert job is not None
    assert job["status"] == "interrupted"
    assert "WORKER_INTERRUPTED" in job["error_message"]


def test_cancel_transcription_success(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
    job_id = _create_transcription(app, user.id, status="processing")

    response = logged_in_client_with_permissions.delete(
        f"/api/transcribe/{job_id}"
    )

    assert response.status_code == 200
    with app.app_context():
        job = transcription_model.get_transcription_by_id(job_id, user.id)
        assert job["status"] == "cancelling"


def test_cancel_transcription_invalid_status(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
    job_id = _create_transcription(app, user.id, status="finished")

    response = logged_in_client_with_permissions.delete(
        f"/api/transcribe/{job_id}"
    )

    assert response.status_code == 400


def test_get_transcriptions_list(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
    _create_transcription(app, user.id)
    _create_transcription(app, user.id)

    response = logged_in_client_with_permissions.get("/api/transcriptions")

    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, list)
    assert len(data) >= 2
    assert all("transcription_text" not in item for item in data)
    assert all("transcription_preview" in item for item in data)


def test_get_transcription_content_loads_full_text_on_demand(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
    job_id = _create_transcription(app, user.id)

    response = logged_in_client_with_permissions.get(
        f"/api/transcriptions/{job_id}/content"
    )

    assert response.status_code == 200
    assert response.get_json()["transcription_text"] == "Transcript text"


def test_get_transcription_content_hides_deleted_items(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
    job_id = _create_transcription(app, user.id)
    assert logged_in_client_with_permissions.delete(
        f"/api/transcriptions/{job_id}"
    ).status_code == 200

    response = logged_in_client_with_permissions.get(
        f"/api/transcriptions/{job_id}/content"
    )
    assert response.status_code == 404


def test_delete_transcription_success(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
    job_id = _create_transcription(app, user.id)

    response = logged_in_client_with_permissions.delete(f"/api/transcriptions/{job_id}")

    assert response.status_code == 200
    with app.app_context():
        job = transcription_model.get_transcription_by_id(job_id, user.id)
        assert job is not None
        assert job["is_hidden_from_user"] is True
        assert job["hidden_reason"] == "USER_DELETED"


def test_delete_transcription_not_found(logged_in_client_with_permissions):
    response = logged_in_client_with_permissions.delete(
        "/api/transcriptions/does-not-exist"
    )
    assert response.status_code == 404


def test_restore_transcription_success(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
    job_id = _create_transcription(app, user.id)

    delete_response = logged_in_client_with_permissions.delete(
        f"/api/transcriptions/{job_id}"
    )
    assert delete_response.status_code == 200

    restore_response = logged_in_client_with_permissions.post(
        f"/api/transcriptions/{job_id}/restore"
    )
    assert restore_response.status_code == 200

    with app.app_context():
        job = transcription_model.get_transcription_by_id(job_id, user.id)
        assert job is not None
        assert job["is_hidden_from_user"] is False
        assert job["hidden_reason"] is None


def test_restore_transcription_already_visible(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
    job_id = _create_transcription(app, user.id)

    response = logged_in_client_with_permissions.post(
        f"/api/transcriptions/{job_id}/restore"
    )

    assert response.status_code == 200

    with app.app_context():
        job = transcription_model.get_transcription_by_id(job_id, user.id)
        assert job is not None
        assert job["is_hidden_from_user"] is False


def test_restore_transcription_not_found(logged_in_client_with_permissions):
    response = logged_in_client_with_permissions.post(
        "/api/transcriptions/missing/restore"
    )
    assert response.status_code == 404


def test_clear_transcriptions(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
    _create_transcription(app, user.id)
    _create_transcription(app, user.id)

    response = logged_in_client_with_permissions.delete(
        "/api/transcriptions/clear"
    )

    assert response.status_code == 200
    with app.app_context():
        transcriptions = transcription_model.get_all_transcriptions(user.id)
        assert transcriptions == []


def test_log_download_requires_permission(app, logged_in_client):
    with app.app_context():
        user = get_user_by_username("testuser")
        from app.models import role as role_model

        role = role_model.get_role_by_id(user.role_id)
        role_model.update_role(role.id, {"allow_download_transcript": False})

        job_id = _create_transcription(app, user.id)

    response = logged_in_client.post(f"/api/transcriptions/{job_id}/log_download")
    assert response.status_code == 403


def test_log_download_success(app, logged_in_client_with_permissions):
    user_id = _get_permissions_user_id(logged_in_client_with_permissions)
    with patch("app.api.transcriptions.check_permission", return_value=True), patch(
        "app.api.transcriptions.transcription_model.mark_transcription_as_downloaded",
        return_value=True,
    ) as mock_mark:
        response = logged_in_client_with_permissions.post(
            "/api/transcriptions/job-123/log_download"
        )

    assert response.status_code == 200
    assert "deleted successfully" not in response.get_json().get("message", "")
    mock_mark.assert_called_once_with("job-123", user_id)


def test_get_title_status_generated(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
    job_id = _create_transcription(app, user.id)
    with app.app_context():
        cursor = transcription_model.get_cursor()
        cursor.execute(
            """
            UPDATE transcriptions
            SET title_generation_status='success',
                generated_title='My Title'
            WHERE id=%s
            """,
            (job_id,),
        )
        transcription_model.get_db().commit()

    response = logged_in_client_with_permissions.get(
        f"/api/transcriptions/{job_id}/title"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "generated"
    assert payload["title"] == "My Title"


def test_get_workflow_details(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
    job_id = _create_transcription(app, user.id)
    with app.app_context():
        cursor = transcription_model.get_cursor()
        cursor.execute(
            """
            UPDATE transcriptions
            SET llm_operation_id=12,
                llm_operation_status='finished',
                llm_operation_result='Result text',
                llm_operation_error=NULL,
                llm_operation_ran_at=NOW(),
                pending_workflow_prompt_text='Pending text',
                pending_workflow_prompt_title='Pending title',
                pending_workflow_prompt_color='#123456',
                pending_workflow_origin_prompt_id=3
            WHERE id=%s
            """,
            (job_id,),
        )
        transcription_model.get_db().commit()

    response = logged_in_client_with_permissions.get(
        f"/api/transcriptions/{job_id}/workflow-details"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["llm_operation_id"] == 12
    assert payload["pending_workflow_prompt_text"] == "Pending text"
def _get_permissions_user_id(client):
    with client.application.app_context():
        return get_user_by_username("testuser_permissions").id


def test_history_body_search_uses_fulltext_and_returns_preview(app, logged_in_client_with_permissions):
    with app.app_context():
        user = get_user_by_username("testuser_permissions")
        assert user is not None
        matching_id = _create_transcription(app, user.id)
        other_id = _create_transcription(app, user.id)
        cursor = transcription_model.get_cursor()
        cursor.execute(
            "UPDATE transcriptions SET transcription_text=%s WHERE id=%s",
            ("A quasarvelocity marker appears in this transcript.", matching_id),
        )
        cursor.execute(
            "UPDATE transcriptions SET transcription_text=%s WHERE id=%s",
            ("Completely unrelated content.", other_id),
        )
        transcription_model.get_db().commit()

        assert transcription_utils.count_visible_user_transcriptions(
            user.id, search_query="quasarvelocity"
        ) == 1
        results = transcription_utils.get_paginated_transcriptions(
            user.id, 1, 10, search_query="quasarvelocity"
        )

    assert [item["id"] for item in results] == [matching_id]
    assert results[0]["transcription_preview"].startswith("A quasarvelocity")
    assert "transcription_text" not in results[0]
