from app.api.transcriptions import _client_safe_error_message


def test_client_error_details_redact_credentials_and_tracebacks():
    message = (
        "Provider error 401; api_key=sk-secretvalue123456; "
        "Authorization: Bearer another-secret\n"
        "Traceback (most recent call last):\nsecret stack"
    )

    safe = _client_safe_error_message(message)

    assert "401" in safe
    assert "[REDACTED]" in safe
    assert "secretvalue" not in safe
    assert "another-secret" not in safe
    assert "Traceback" not in safe
