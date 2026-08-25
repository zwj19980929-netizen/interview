import json

from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

from app.main import create_app
from app.persistence.provider import persistence_for
from app.repositories.provider import get_store, reset_store_for_tests


def test_production_bearer_rbac_and_request_audit(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.setenv("INTERVIEWER_ORGANIZATION_ID", "org_default")
    monkeypatch.setenv("INTERVIEWER_CONTACT_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("INTERVIEWER_CONTACT_LOOKUP_SECRET", "lookup-secret-at-least-thirty-two-characters")
    monkeypatch.setenv(
        "INTERVIEWER_API_TOKENS_JSON",
        json.dumps(
            {
                "interviewer-token": {
                    "actor_id": "interviewer_1",
                    "organization_id": "org_default",
                    "roles": ["interviewer"],
                },
                "admin-token": {
                    "actor_id": "admin_1",
                    "organization_id": "org_default",
                    "roles": ["admin"],
                },
            }
        ),
    )
    reset_store_for_tests()
    api = TestClient(create_app())
    assert api.get("/healthz").status_code == 200
    unauthenticated = api.get("/api/v1/job-positions")
    assert unauthenticated.status_code == 401
    forbidden = api.get(
        "/api/v1/admin/work-items", headers={"Authorization": "Bearer interviewer-token"}
    )
    assert forbidden.status_code == 403
    review_forbidden = api.get(
        "/api/v1/interviews/not-present/review",
        headers={"Authorization": "Bearer interviewer-token"},
    )
    assert review_forbidden.status_code == 403
    allowed = api.get(
        "/api/v1/job-positions", headers={"Authorization": "Bearer interviewer-token"}
    )
    assert allowed.status_code == 200
    admin = api.get(
        "/api/v1/admin/work-items", headers={"Authorization": "Bearer admin-token"}
    )
    assert admin.status_code == 200
    with persistence_for(get_store()).transaction("org_default") as transaction:
        audited = transaction.audit_events.list()
    assert any(
        item["action"] == "api.request"
        and item["actor_id"] == "interviewer_1"
        and item["resource_id"] == "/api/v1/job-positions"
        for item in audited
    )
    assert any(item["actor_id"] == "anonymous" and item["metadata"]["status_code"] == 401 for item in audited)


def test_provider_secrets_are_encrypted_at_the_repository_seam(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_PROVIDER_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    reset_store_for_tests()
    store = get_store()
    with persistence_for(store).transaction("org_default") as transaction:
        transaction.provider_secrets.replace("mpc_secret", {"api_key": "super-secret-value"})
    assert store.provider_secrets["mpc_secret"].get("sealed_v1")
    assert "super-secret-value" not in str(store.provider_secrets)
    with persistence_for(store).transaction("org_default") as transaction:
        assert transaction.provider_secrets.get("mpc_secret") == {"api_key": "super-secret-value"}


def test_audit_resource_redacts_url_bearer_tokens() -> None:
    from app.core.auth import _safe_route_resource

    secret = "opaque-secret-that-must-not-be-audited"
    assert _safe_route_resource(f"/api/v1/public/interview-invitations/{secret}/start") == (
        "/api/v1/public/interview-invitations/{token}/start"
    )
    assert _safe_route_resource(f"/api/v1/private-files/{secret}") == "/api/v1/private-files/{token}"
    assert secret not in _safe_route_resource(f"/api/v1/private-media/{secret}")
