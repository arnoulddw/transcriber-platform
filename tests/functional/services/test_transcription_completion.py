import uuid
from datetime import datetime

from app.models import transcription as transcription_model
from app.models.user import get_user_by_username


def _get_test_user():
    user = get_user_by_username("testuser_permissions")
    assert user is not None
    return user


def _create_job(app, user_id: int) -> str:
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
    return job_id


def _get_job(app, job_id: str, user_id: int):
    with app.app_context():
        job = transcription_model.get_transcription_by_id(job_id, user_id)
    assert job is not None
    return job


def test_transcriptions_schema_adds_completed_at_to_existing_table(
    app, logged_in_client_with_permissions
):
    with app.app_context():
        cursor = transcription_model.get_cursor()
        cursor.execute("SHOW COLUMNS FROM transcriptions LIKE 'completed_at'")
        if cursor.fetchone():
            cursor.execute("ALTER TABLE transcriptions DROP COLUMN completed_at")
            transcription_model.get_db().commit()

        transcription_model.init_db_command()

        cursor.execute(
            """
            SELECT DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'transcriptions'
              AND COLUMN_NAME = 'completed_at'
            """
        )
        column = cursor.fetchone()

    assert column is not None
    assert column["DATA_TYPE"] == "timestamp"
    assert column["IS_NULLABLE"] == "YES"
    assert column["COLUMN_DEFAULT"] is None


def test_completed_at_stays_null_for_non_terminal_states(
    app, logged_in_client_with_permissions
):
    user = _get_test_user()
    job_id = _create_job(app, user.id)

    assert _get_job(app, job_id, user.id)["completed_at"] is None

    with app.app_context():
        transcription_model.update_job_status(job_id, "processing")
    assert _get_job(app, job_id, user.id)["completed_at"] is None

    with app.app_context():
        transcription_model.update_job_status(job_id, "cancelling")
    assert _get_job(app, job_id, user.id)["completed_at"] is None


def test_completed_at_is_set_for_successful_transcription(
    app, logged_in_client_with_permissions
):
    user = _get_test_user()
    job_id = _create_job(app, user.id)

    with app.app_context():
        transcription_model.finalize_job_success(job_id, "Transcript text", "en")

    job = _get_job(app, job_id, user.id)
    assert job["status"] == "finished"
    assert isinstance(job["completed_at"], datetime)


def test_transcription_warning_flag_is_persisted(
    app, logged_in_client_with_permissions
):
    user = _get_test_user()

    with app.app_context():
        cursor = transcription_model.get_cursor()
        cursor.execute("SHOW COLUMNS FROM transcriptions LIKE 'has_transcription_warning'")
        if cursor.fetchone():
            cursor.execute("ALTER TABLE transcriptions DROP COLUMN has_transcription_warning")
            transcription_model.get_db().commit()
        transcription_model.init_db_command()

    job_id = _create_job(app, user.id)
    assert _get_job(app, job_id, user.id)["has_transcription_warning"] is False

    with app.app_context():
        transcription_model.finalize_job_success(
            job_id,
            "Transcript text",
            "en",
            has_transcription_warning=True,
        )

    assert _get_job(app, job_id, user.id)["has_transcription_warning"] is True


def test_completed_at_is_set_for_error(
    app, logged_in_client_with_permissions
):
    user = _get_test_user()
    job_id = _create_job(app, user.id)

    with app.app_context():
        transcription_model.set_job_error(job_id, "Transcription failed")

    job = _get_job(app, job_id, user.id)
    assert job["status"] == "error"
    assert isinstance(job["completed_at"], datetime)


def test_completed_at_is_set_when_a_job_is_cancelled(
    app, logged_in_client_with_permissions
):
    user = _get_test_user()
    job_id = _create_job(app, user.id)

    with app.app_context():
        transcription_model.update_job_status(job_id, "cancelling")
        transcription_model.update_job_status(job_id, "cancelled")

    job = _get_job(app, job_id, user.id)
    assert job["status"] == "cancelled"
    assert isinstance(job["completed_at"], datetime)


def test_completed_at_is_set_when_a_job_is_interrupted(
    app, logged_in_client_with_permissions
):
    user = _get_test_user()
    job_id = _create_job(app, user.id)

    with app.app_context():
        transcription_model.update_job_status(job_id, "processing")
        assert transcription_model.mark_active_jobs_interrupted([job_id]) == 1

    job = _get_job(app, job_id, user.id)
    assert job["status"] == "interrupted"
    assert isinstance(job["completed_at"], datetime)


def test_completed_at_is_write_once(
    app, logged_in_client_with_permissions
):
    user = _get_test_user()
    job_id = _create_job(app, user.id)
    original_completed_at = datetime(2020, 1, 2, 3, 4, 5)

    with app.app_context():
        cursor = transcription_model.get_cursor()
        cursor.execute(
            "UPDATE transcriptions SET completed_at = %s WHERE id = %s",
            (original_completed_at, job_id),
        )
        transcription_model.get_db().commit()
        transcription_model.finalize_job_success(job_id, "Transcript text", "en")

    job = _get_job(app, job_id, user.id)
    assert job["completed_at"] == original_completed_at
