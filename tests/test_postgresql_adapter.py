from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.persistence.postgresql import MIGRATIONS, PostgreSQLPersistence
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


@pytest.mark.parametrize("read_only", [False, True])
def test_postgresql_read_only_lookup_avoids_row_locks_and_retains_tenant_context(monkeypatch, read_only):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = None
    persistence = object.__new__(PostgreSQLPersistence)
    monkeypatch.setattr(persistence, "_connect", lambda: connection)
    with persistence.transaction("org_reader", read_only=read_only) as transaction:
        assert transaction.questions.get("q_missing") is None
        assert transaction.outbox.get("work_missing") is None
    calls = connection.execute.call_args_list
    statements = [call.args[0] for call in calls]
    lookups = [statement for statement in statements if statement.startswith("SELECT data")]
    assert len(lookups) == 2
    assert all(("FOR UPDATE" not in statement) == read_only for statement in lookups)
    tenant_call = next(call for call in calls if "set_config" in call.args[0])
    assert tenant_call.args[1] == ("org_reader",)
    if read_only:
        assert statements[0] == "SET TRANSACTION READ ONLY"
    else:
        assert all("READ ONLY" not in statement for statement in statements)
