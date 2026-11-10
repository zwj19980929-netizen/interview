from dataclasses import replace
import json
from pathlib import Path
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

from app.adapters.livekit_media import LiveKitConfiguration
from app.core.media_runtime import MediaRuntime
from app.operations.local_runtime import local_runtime_command


def local_configuration(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    monkeypatch.setenv("INTERVIEWER_LOCAL_MEDIA", "true")
    return LiveKitConfiguration.from_environment()


def test_configuration_identity_exposes_mode_and_drift_without_secrets(monkeypatch):
    local = local_configuration(monkeypatch)
    api = MediaRuntime(local, private_file_root="/private/SECRET-path").snapshot()
    worker = MediaRuntime(replace(local, local_media_enabled=False, recording_storage_backend="aliyun_oss"),
                          private_file_root="/private/SECRET-path").snapshot()
    assert api["recording_ready"] and not worker["recording_ready"]
    assert api["mode"] == "local" and worker["mode"] == "aliyun_oss"
    assert "private_oss_configuration_missing" in worker["issues"]
    assert api["configuration_sha256"] != worker["configuration_sha256"]
    assert "SECRET" not in json.dumps(api)
    assert local.api_secret not in json.dumps(api)
    assert local.url not in json.dumps(api)


def test_digest_detects_routing_root_and_encryption_drift(monkeypatch):
    local = local_configuration(monkeypatch)
    baseline = MediaRuntime(local, private_file_root="/one").snapshot()["configuration_sha256"]
    for changed in [replace(local, url="ws://127.0.0.1:9999"),
                    replace(local, local_egress_root="/another"),
                    replace(local, storage_sse_algorithm="KMS")]:
        assert MediaRuntime(changed, private_file_root="/one").snapshot()["configuration_sha256"] != baseline
    assert MediaRuntime(local, private_file_root="/two").snapshot()["configuration_sha256"] != baseline


def test_snapshot_is_frozen_and_does_not_hash_credential_values(monkeypatch):
    local = local_configuration(monkeypatch)
    runtime = MediaRuntime(local, private_file_root="/one")
    original = runtime.snapshot()
    monkeypatch.setenv("INTERVIEWER_LOCAL_MEDIA", "false")
    exposed = runtime.snapshot()
    exposed["issues"].append("injected")
    assert runtime.snapshot() == original
    changed = replace(local, api_secret="a-different-secret-value-at-least16")
    assert MediaRuntime(changed, private_file_root="/one").snapshot() == original


@pytest.mark.parametrize("runtime", ["production", "staging", "test"])
def test_local_launcher_rejects_non_development(runtime):
    with pytest.raises(ValueError):
        local_runtime_command("worker", {"INTERVIEWER_RUNTIME_ENV": runtime})


def test_api_and_worker_share_media_configuration_without_erasing_inherited_values():
    source = {"INTERVIEWER_OSS_ACCESS_KEY_SECRET": "synthetic-secret", "INTERVIEWER_LOCAL_MEDIA": "false"}
    api, api_environment = local_runtime_command("api", source)
    worker, worker_environment = local_runtime_command("worker", source)
    assert api_environment == worker_environment
    assert api_environment["INTERVIEWER_LOCAL_MEDIA"] == "true"
    assert api_environment["INTERVIEWER_OSS_ACCESS_KEY_SECRET"] == "synthetic-secret"
    assert source["INTERVIEWER_LOCAL_MEDIA"] == "false"
    assert Path(api[-1]).name == "main.py"
    assert "app.workers.celery_app:celery_app" in worker


def test_media_runtime_requires_admin_and_has_no_side_effects(monkeypatch):
    from app.main import create_app
    from app.repositories.provider import reset_store_for_tests
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.setenv("INTERVIEWER_ORGANIZATION_ID", "org_default")
    monkeypatch.setenv("INTERVIEWER_CONTACT_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("INTERVIEWER_CONTACT_LOOKUP_SECRET", "isolated-lookup-secret-at-least-thirty-two-characters")
    monkeypatch.setenv("INTERVIEWER_API_TOKENS_JSON", json.dumps({
        "isolated-admin": {"actor_id": "admin", "organization_id": "org_default", "roles": ["admin"]},
        "isolated-reviewer": {"actor_id": "reviewer", "organization_id": "org_default", "roles": ["reviewer"]},
    }))
    reset_store_for_tests()
    api = TestClient(create_app())
    route = "/api/v1/admin/media-runtime"
    assert api.get(route).status_code == 401
    assert api.get(route, headers={"Authorization": "Bearer isolated-reviewer"}).status_code == 403
    response = api.get(route, headers={"Authorization": "Bearer isolated-admin"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"schema", "mode", "configuration_sha256", "recording_ready", "issues"}
    assert api.get("/healthz").json() == {"status": "ok"}
    assert "isolated-admin" not in response.text
    assert "lookup-secret" not in response.text


@pytest.mark.parametrize("module", ["app.main", "app.workers.celery_app"])
def test_actual_bootstrap_freezes_configuration_before_any_query(module, tmp_path):
    script = """
import importlib, json, os, socket, sys
def no_network(*args, **kwargs):
    raise AssertionError('diagnostic attempted network access')
socket.create_connection = no_network
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
loaded = importlib.import_module(sys.argv[1])
from app.core.media_runtime import media_runtime_snapshot
before = media_runtime_snapshot()
os.environ['INTERVIEWER_LOCAL_MEDIA'] = 'false'
after = media_runtime_snapshot()
if sys.argv[1].endswith('celery_app'):
    assert loaded.media_runtime(None) == before
assert before == after
print(json.dumps(before, sort_keys=True))
"""
    completed = subprocess.run([sys.executable, "-B", "-c", script, module],
        cwd=Path(__file__).resolve().parents[1],
        env={"PYTHONDONTWRITEBYTECODE": "1", "INTERVIEWER_RUNTIME_ENV": "development",
             "INTERVIEWER_LOCAL_MEDIA": "true", "INTERVIEWER_DB_BACKEND": "memory",
             "INTERVIEWER_MEDIA_PATH": str(tmp_path / "media"),
             "INTERVIEWER_PRIVATE_FILE_ROOT": str(tmp_path / "private"),
             "INTERVIEWER_CELERY_BROKER_URL": "memory://", "INTERVIEWER_REDIS_URL": ""},
        capture_output=True, text=True, check=True, timeout=30)
    snapshot = json.loads(completed.stdout)
    assert snapshot["schema"] == "media-runtime.v1"
    assert snapshot["recording_ready"] and snapshot["mode"] == "local"
    assert snapshot["issues"] == []
    assert not (tmp_path / "private").exists()
