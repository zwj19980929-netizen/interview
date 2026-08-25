from fastapi.testclient import TestClient

from app.core.rate_limit import public_rate_limiter
from app.main import create_app
from app.repositories.provider import reset_store_for_tests


def test_public_invitation_rate_limit_is_generic_and_audited(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_PUBLIC_RATE_LIMIT_MAX", "2")
    monkeypatch.setenv("INTERVIEWER_PUBLIC_RATE_LIMIT_WINDOW_SECONDS", "60")
    public_rate_limiter.reset()
    reset_store_for_tests()
    api = TestClient(create_app())
    path = "/api/v1/public/interview-invitations/not-a-real-token"
    assert api.get(path).status_code == 404
    assert api.get(path).status_code == 404
    limited = api.get(path)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    events = api.get("/api/v1/admin/audit-events").json()["items"]
    assert any(
        item["resource_id"] == "/api/v1/public/interview-invitations/{token}"
        and item["metadata"]["status_code"] == 429
        for item in events
    )

    public_rate_limiter.reset()
    candidate_path = "/api/v1/public/interviews/not-a-real-session"
    assert api.get(candidate_path).status_code == 422
    assert api.get(candidate_path).status_code == 422
    candidate_limited = api.get(candidate_path)
    assert candidate_limited.status_code == 429
    assert candidate_limited.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
