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
        "idx_evidence_commands_unsettled",
        "idx_interviews_watchdog_takeover",
        "idx_interviews_watchdog_deadline",
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


@pytest.mark.parametrize("read_only", [False, True])
def test_postgresql_unsettled_query_scopes_candidates_before_fetching_and_relocks_on_get(monkeypatch, read_only):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    item = {"id": "command", "organization_id": "org_reader", "interview_id": "interview_reader", "status": "pending"}
    connection.execute.return_value.fetchall.return_value = [{"data": item}]
    connection.execute.return_value.fetchone.return_value = {"data": item}
    persistence = object.__new__(PostgreSQLPersistence)
    monkeypatch.setattr(persistence, "_connect", lambda: connection)
    with persistence.transaction("org_reader", read_only=read_only) as tx:
        assert tx.evidence_commands.list_unsettled("interview_reader") == [item]
        assert tx.evidence_commands.get("command")["id"] == "command"
    reads = [call for call in connection.execute.call_args_list if call.args[0].startswith("SELECT data")]
    assert len(reads) == 2
    query, parameters = reads[0].args
    assert "organization_id = %s" in query and "data->>'interview_id' = %s" in query
    assert "data->>'status' IN ('pending', 'running')" in query
    assert parameters == ("org_reader", "interview_reader")
    assert "FOR UPDATE" not in query
    assert ("FOR UPDATE" not in reads[1].args[0]) == read_only


def test_postgresql_startup_requires_command_poll_index(monkeypatch):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = {"documents": "documents", "evidence_command_poll_index": None}
    persistence = object.__new__(PostgreSQLPersistence)
    monkeypatch.setattr(persistence, "_connect", lambda: connection)
    with pytest.raises(RuntimeError, match="evidence_command_poll_index missing"):
        persistence._verify_schema()


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("kind", ["takeover", "deadline"])
def test_postgresql_watchdog_query_has_tenant_predicates_and_no_discovery_lock(monkeypatch, read_only, kind):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    item = {"id": "interview_synthetic", "organization_id": "org_reader", "status": "in_progress",
            "agent_runtime": {"takeover": {"expires_at": "invalid"}}, "version": 1}
    connection.execute.return_value.fetchall.return_value = [{"data": item}]
    connection.execute.return_value.fetchone.return_value = {"data": item}
    persistence = object.__new__(PostgreSQLPersistence)
    monkeypatch.setattr(persistence, "_connect", lambda: connection)
    with persistence.transaction("org_reader", read_only=read_only) as tx:
        assert tx.interview_sessions.watchdog_candidates(kind) == [item]
        assert tx.interview_sessions.get("interview_synthetic") == item
    reads = [call for call in connection.execute.call_args_list if call.args[0].startswith("SELECT data")]
    query, parameters = reads[0].args
    assert "collection = 'interviews'" in query and "organization_id = %s" in query
    assert parameters == ("org_reader",)
    assert "FOR UPDATE" not in query and "expires_at" not in query and "scheduled_end_at" not in query
    if kind == "takeover":
        assert "jsonb_typeof(data#>'{agent_runtime,takeover}') = 'object'" in query
        assert "data#>'{agent_runtime,takeover}' <> '{}'::jsonb" in query
        assert "data->'list_removed_at'" in query
    else:
        assert "data->>'status' IN ('scheduled', 'waiting', 'in_progress', 'paused')" in query
        assert "data->'candidate_input_completed_at'" in query
        assert "list_removed_at" not in query
    assert "'null'::jsonb, 'false'::jsonb, '0'::jsonb, '\"\"'::jsonb, '[]'::jsonb, '{}'::jsonb" in query
    assert ("FOR UPDATE" not in reads[1].args[0]) == read_only
    migration = next(path for path in MIGRATIONS if path.name == "007_interview_watchdog_candidates.sql")
    # Migration and discovery must use the same candidate predicate to keep
    # the org-scoped partial index eligible without broad history scans.
    predicate = query.split("AND organization_id = %s AND ", 1)[1]
    assert predicate in " ".join(migration.read_text().split())


@pytest.mark.parametrize("missing", ["interview_takeover_watchdog_index", "interview_deadline_watchdog_index"])
def test_postgresql_startup_requires_watchdog_indexes(monkeypatch, missing):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = {"documents": "documents", missing: None}
    persistence = object.__new__(PostgreSQLPersistence)
    monkeypatch.setattr(persistence, "_connect", lambda: connection)
    with pytest.raises(RuntimeError, match=missing + " missing"):
        persistence._verify_schema()
    query = connection.execute.call_args.args[0]
    assert "to_regclass('public.idx_interviews_watchdog_takeover')" in query
    assert "to_regclass('public.idx_interviews_watchdog_deadline')" in query
