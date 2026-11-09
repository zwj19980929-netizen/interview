"""A healthy old process must not masquerade as a newly deployed release."""
import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.repositories.provider import reset_store_for_tests


def test_runtime_identity_requires_admin_and_keeps_health_contract(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.setenv("INTERVIEWER_ORGANIZATION_ID", "org_default")
    monkeypatch.setenv("INTERVIEWER_CONTACT_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("INTERVIEWER_CONTACT_LOOKUP_SECRET", "isolated-lookup-secret-at-least-thirty-two-characters")
    monkeypatch.setenv("INTERVIEWER_API_TOKENS_JSON", json.dumps({
        "isolated-admin": {"actor_id": "admin", "organization_id": "org_default", "roles": ["admin"]},
        "isolated-interviewer": {"actor_id": "interviewer", "organization_id": "org_default", "roles": ["interviewer"]},
    }))
    reset_store_for_tests()
    api = TestClient(create_app())
    route = "/api/v1/admin/runtime-release"
    assert api.get("/healthz").json() == {"status": "ok"}
    assert api.get(route).status_code == 401
    assert api.get(route, headers={"Authorization": "Bearer isolated-interviewer"}).status_code == 403
    headers = {"Authorization": "Bearer isolated-admin"}
    response = api.get(route, headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert set(data) == {"schema", "process", "source"}
    assert data["schema"] == "runtime-release.v1"
    assert data["process"]["role"] == "api"
    assert data["source"]["status"] == "available"
    assert len(data["source"]["sha256"]) == 64
    assert "isolated-admin" not in response.text
    assert "lookup-secret" not in response.text
    assert api.get(route, headers=headers).json() == data


def test_source_diagnostic_unavailable_does_not_change_health(monkeypatch):
    from app.api.routers import system

    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    monkeypatch.setattr(system, "runtime_release_snapshot", lambda role: {
        "schema": "runtime-release.v1",
        "process": {"instance_id": "isolated-process", "pid": 1, "started_at": "2026-11-09T00:00:00Z", "role": role},
        "source": {"status": "unavailable", "sha256": None, "file_count": 0},
    })
    reset_store_for_tests()
    api = TestClient(create_app())
    assert api.get("/healthz").json() == {"status": "ok"}
    response = api.get("/api/v1/admin/runtime-release")
    assert response.status_code == 200
    assert response.json()["source"]["status"] == "unavailable"


@pytest.mark.parametrize("module,role", [
    ("app.main", "api"), ("app.workers.celery_app", "worker"),
])
def test_actual_bootstrap_in_new_interpreter_matches_offline_manifest(tmp_path, module, role):
    from app.core.runtime_release import source_manifest

    script = """
import importlib
import json
import socket
import sys

def no_network(*args, **kwargs):
    raise AssertionError('bootstrap attempted network access')

socket.create_connection = no_network
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
importlib.import_module(sys.argv[1])
from app.core.runtime_release import runtime_release_snapshot
print(json.dumps(runtime_release_snapshot(sys.argv[2]), sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script, module, role],
        cwd=Path(__file__).resolve().parents[1],
        env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "INTERVIEWER_RUNTIME_ENV": "test",
            "INTERVIEWER_DB_BACKEND": "memory",
            "INTERVIEWER_MEDIA_PATH": str(tmp_path / "media"),
            "INTERVIEWER_LOCAL_MEDIA": "false",
            "INTERVIEWER_CELERY_BROKER_URL": "memory://",
            "INTERVIEWER_REDIS_URL": "",
        },
        capture_output=True, text=True, check=True, timeout=30,
    )
    snapshot = json.loads(completed.stdout)
    assert snapshot["schema"] == "runtime-release.v1"
    assert snapshot["source"] == source_manifest()
    assert snapshot["source"]["status"] == "available"
    assert snapshot["process"]["role"] == role
    assert snapshot["process"]["pid"] != os.getpid()


def test_runtime_logging_handler_failure_does_not_prevent_api_lifespan(monkeypatch):
    import app.main as application

    events = []

    class FailingHandler(logging.Handler):
        def emit(self, record):
            events.append("log_attempted")
            raise RuntimeError("synthetic diagnostic handler failure")

    logger = logging.Logger("isolated-runtime-release-test", level=logging.INFO)
    logger.propagate = False
    logger.addHandler(FailingHandler())
    monkeypatch.setattr(application, "logging", SimpleNamespace(getLogger=lambda name: logger))
    monkeypatch.setattr(application, "install_sensitive_access_log_filter", lambda: None)
    monkeypatch.setattr(application, "configure_speech_diagnostics", lambda: None)
    monkeypatch.setattr(application, "start_tts_http_pools", lambda: events.append("pools_started"))
    monkeypatch.setattr(application, "start_fixed_speech_preparation",
                        lambda *args, **kwargs: events.append("speech_started"))
    monkeypatch.setattr(application, "get_store", lambda: object())
    monkeypatch.setattr(application, "ModelGateway", lambda store: object())

    async def watchdog(*args, **kwargs):
        try:
            events.append("watchdog_started")
            await asyncio.Event().wait()
        finally:
            events.append("watchdog_stopped")

    async def closed(name):
        events.append(name)

    monkeypatch.setattr(application, "run_takeover_lease_watchdog", watchdog)
    monkeypatch.setattr(application, "run_interview_deadline_watchdog", watchdog)
    monkeypatch.setattr(application, "shutdown_livekit_evidence_supervisors",
                        lambda: closed("supervisors_closed"))
    monkeypatch.setattr(application, "close_fixed_speech_preparation", lambda: closed("speech_closed"))
    monkeypatch.setattr(application, "close_tts_http_pools", lambda: closed("pools_closed"))
    monkeypatch.setattr(application, "realtime_event_bus", lambda: SimpleNamespace(
        enabled=False, close=lambda: closed("bus_closed"),
    ))
    monkeypatch.setattr(application, "public_rate_limiter",
                        SimpleNamespace(close=lambda: closed("limiter_closed")))

    async def run():
        app = SimpleNamespace(state=SimpleNamespace())
        async with application.lifespan(app):
            await asyncio.sleep(0)
            events.append("lifespan_entered")
            assert not app.state.takeover_lease_watchdog.done()
            assert not app.state.interview_deadline_watchdog.done()
        assert app.state.takeover_lease_watchdog.cancelled()
        assert app.state.interview_deadline_watchdog.cancelled()

    asyncio.run(run())
    assert events.count("log_attempted") == 1
    assert events.count("watchdog_started") == events.count("watchdog_stopped") == 2
    assert {"pools_started", "speech_started", "lifespan_entered", "supervisors_closed",
            "speech_closed", "pools_closed", "bus_closed", "limiter_closed"}.issubset(events)
