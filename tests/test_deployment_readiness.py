import asyncio

from fastapi.testclient import TestClient

from app.core.readiness import deployment_readiness
from app.main import create_app
from app.repositories.memory import InMemoryStore
from app.repositories.provider import reset_store_for_tests


def test_development_readiness_uses_local_adapters(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    monkeypatch.delenv("INTERVIEWER_REDIS_URL", raising=False)

    result = asyncio.run(deployment_readiness(InMemoryStore()))

    assert result["ready"] is True
    assert result["runtime_environment"] == "development"


def test_production_readiness_reports_missing_dependencies_without_secrets(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    for name in (
        "INTERVIEWER_REDIS_URL",
        "INTERVIEWER_API_TOKENS_JSON",
        "INTERVIEWER_CONTACT_ENCRYPTION_KEY",
        "INTERVIEWER_CONTACT_LOOKUP_SECRET",
        "INTERVIEWER_PROVIDER_SECRET_ENCRYPTION_KEY",
        "INTERVIEWER_CANDIDATE_TOKEN_SECRET",
        "INTERVIEWER_FILE_SIGNING_SECRET",
        "INTERVIEWER_OSS_ENDPOINT",
        "INTERVIEWER_OSS_BUCKET",
        "INTERVIEWER_OSS_ACCESS_KEY_ID",
        "INTERVIEWER_OSS_ACCESS_KEY_SECRET",
        "INTERVIEWER_FILE_SCANNER_COMMAND",
    ):
        monkeypatch.delenv(name, raising=False)

    result = asyncio.run(deployment_readiness(InMemoryStore()))
    checks = {item["name"]: item["ready"] for item in result["checks"]}

    assert result["ready"] is False
    assert checks["database"] is True
    assert checks["database_backend"] is False
    assert checks["redis"] is False
    assert checks["security_secrets"] is False
    assert checks["object_storage"] is False
    assert checks["malware_scanner"] is False


def test_readyz_exposes_probe_without_backend_auth(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    monkeypatch.delenv("INTERVIEWER_REDIS_URL", raising=False)
    reset_store_for_tests()

    with TestClient(create_app()) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
