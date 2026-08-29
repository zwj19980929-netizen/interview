import os
import stat

import pytest

from app.operations.production_config import (
    generate_production_config,
    inspect_production_config,
)


def test_generated_config_is_private_shell_safe_and_reports_external_gaps(tmp_path):
    path = tmp_path / ".env.production.local"

    report = generate_production_config(
        path,
        postgres_dsn="postgresql://runtime:p@ss@127.0.0.1:55432/interviewer",
        redis_url="redis://:secret@127.0.0.1:56379/0",
        organization_id="org_default",
        clamd_host="127.0.0.1",
    )

    assert not report.valid
    assert report.invalid == ()
    assert set(report.missing) == {
        "INTERVIEWER_OSS_ACCESS_KEY_ID",
        "INTERVIEWER_OSS_ACCESS_KEY_SECRET",
        "INTERVIEWER_OSS_BUCKET",
        "INTERVIEWER_OSS_ENDPOINT",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    contents = path.read_text(encoding="utf-8")
    assert "INTERVIEWER_API_TOKENS_JSON=" in contents
    assert "production_admin" in contents
    assert "p@ss" in contents


def test_generator_never_overwrites_existing_config(tmp_path):
    path = tmp_path / "existing.env"
    path.write_text("sentinel", encoding="utf-8")

    with pytest.raises(FileExistsError):
        generate_production_config(path, postgres_dsn="postgresql://db", redis_url="redis://cache")

    assert path.read_text(encoding="utf-8") == "sentinel"


def test_complete_generated_config_passes_static_validation(tmp_path):
    path = tmp_path / "complete.env"
    report = generate_production_config(
        path,
        postgres_dsn="postgresql://runtime:secret@db.example/interviewer",
        redis_url="rediss://:secret@cache.example/0",
        clamd_host="scanner.internal",
        oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        oss_bucket="interviewer-private",
        oss_access_key_id="key-id",
        oss_access_key_secret="key-secret",
    )

    assert report.valid
    assert inspect_production_config(path).valid


def test_inspector_does_not_export_config_to_process(tmp_path, monkeypatch):
    path = tmp_path / "config.env"
    generate_production_config(path, postgres_dsn="postgresql://db", redis_url="redis://cache")
    monkeypatch.delenv("INTERVIEWER_RUNTIME_ENV", raising=False)

    inspect_production_config(path)

    assert "INTERVIEWER_RUNTIME_ENV" not in os.environ
