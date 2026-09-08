import logging

from app.core.access_log import (
    SensitiveMediaLogFilter,
    SensitiveQueryAccessLogFilter,
    install_sensitive_access_log_filter,
    redact_sensitive_query,
)


def test_native_media_error_retains_failure_category_but_not_jwt():
    synthetic = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.synthetic_signature"
    record = logging.LogRecord("livekit", logging.ERROR, __file__, 1,
                              "%s: invalid token: %s, token is not valid yet", ("room", synthetic), None)
    assert SensitiveMediaLogFilter().filter(record)
    assert synthetic not in record.getMessage()
    assert "[REDACTED_JWT]" in record.getMessage()
    assert "token is not valid yet" in record.getMessage()
    assert not record.args


def test_media_log_redaction_install_is_idempotent():
    install_sensitive_access_log_filter()
    install_sensitive_access_log_filter()
    assert sum(isinstance(item, SensitiveMediaLogFilter)
               for item in logging.getLogger("livekit").filters) == 1


def test_sensitive_query_values_are_redacted_without_losing_route_context() -> None:
    request_target = (
        "/api/v1/public/interviews/iv_1/avatar-model"
        "?grant=secret-grant&view=full&token=secret-token"
    )
    redacted = redact_sensitive_query(request_target)

    assert redacted.startswith(
        "/api/v1/public/interviews/iv_1/avatar-model?"
    )
    assert "view=full" in redacted
    assert "grant=REDACTED" in redacted
    assert "token=REDACTED" in redacted
    assert "secret-grant" not in redacted
    assert "secret-token" not in redacted


def test_sensitive_path_segments_are_redacted_without_losing_route_suffix() -> None:
    targets = (
        (
            "/api/v1/private-files/private-file-grant",
            "/api/v1/private-files/{token}",
            "private-file-grant",
        ),
        (
            "/api/v1/private-media/private-media-grant?download=1",
            "/api/v1/private-media/{token}?download=1",
            "private-media-grant",
        ),
        (
            "http://127.0.0.1:8000/api/v1/public/interview-invitations/"
            "invitation-token/readiness?view=full&grant=query-secret",
            "http://127.0.0.1:8000/api/v1/public/interview-invitations/"
            "{token}/readiness?view=full&grant=REDACTED",
            "invitation-token",
        ),
    )

    for request_target, expected, secret in targets:
        redacted = redact_sensitive_query(request_target)
        assert redacted == expected
        assert secret not in redacted


def test_uvicorn_access_record_filter_removes_credentials() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:1234",
            "GET",
            "/agent?ticket=secret-ticket&signature=secret-signature",
            "1.1",
            200,
        ),
        None,
    )

    assert SensitiveQueryAccessLogFilter().filter(record) is True
    message = record.getMessage()
    assert "GET /agent?" in message
    assert "ticket=REDACTED" in message
    assert "signature=REDACTED" in message
    assert "secret-ticket" not in message
    assert "secret-signature" not in message


def test_uvicorn_access_record_filter_removes_path_credentials() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:1234",
            "GET",
            "/api/v1/private-files/private-file-grant",
            "1.1",
            206,
        ),
        None,
    )

    assert SensitiveQueryAccessLogFilter().filter(record) is True
    message = record.getMessage()
    assert "GET /api/v1/private-files/{token}" in message
    assert "private-file-grant" not in message
