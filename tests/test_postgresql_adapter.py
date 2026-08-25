from pathlib import Path

import pytest

from app.persistence.postgresql import MIGRATIONS
from app.repositories.provider import _create_store


def test_postgresql_migration_contains_required_constraints_and_tenant_rls() -> None:
    sql = "\n".join(Path(path).read_text(encoding="utf-8") for path in MIGRATIONS)
    required_fragments = (
        "UNIQUE (organization_id, idempotency_key)",
        "uq_interview_appointment_session",
        "ck_interview_unique_selection_slots",
        "ENABLE ROW LEVEL SECURITY",
        "FORCE ROW LEVEL SECURITY",
        "current_setting('app.organization_id', true)",
        "PRIMARY KEY (organization_id, provider_connection_id)",
    )
    for fragment in required_fragments:
        assert fragment in sql


def test_postgresql_backend_fails_fast_without_a_dsn(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_DB_BACKEND", "postgresql")
    monkeypatch.delenv("INTERVIEWER_POSTGRES_DSN", raising=False)
    with pytest.raises(RuntimeError, match="INTERVIEWER_POSTGRES_DSN"):
        _create_store()
