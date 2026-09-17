"""Watchdog discovery reads candidates, not completed interview aggregates."""
import pytest

from app.persistence.errors import ReadOnlyViolation
from app.persistence.interface import PersistenceTransaction
from app.persistence.memory import _MemoryTransactionBackend
from app.persistence.provider import persistence_for
from app.persistence.sqlite import _SQLiteTransactionBackend
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore


ORGANIZATION_ID = "org_watchdog_synthetic"
FALSY_JSON = [None, False, 0, 0.0, "", [], {}]
TRUTHY_JSON = [True, 1, -1, "0", "false", "[]", "{}", " ", [0], {"x": False}]


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    return InMemoryStore() if request.param == "memory" else SQLiteStore(str(tmp_path / "watchdogs.sqlite3"))


def _item(identity, **fields):
    return {"id": identity, "organization_id": ORGANIZATION_ID, "status": "in_progress", **fields}


def _seed(store, items):
    for item in items:
        with persistence_for(store).transaction(item["organization_id"]) as tx:
            tx.interview_sessions.add(item)


def _forbid_history(monkeypatch):
    for cls in (_MemoryTransactionBackend, _SQLiteTransactionBackend):
        original = cls.list_documents

        def guarded(backend, collection, original=original):
            assert collection != "interviews", "Watchdog scanned all interview aggregates"
            return original(backend, collection)

        monkeypatch.setattr(cls, "list_documents", guarded)


def test_deadline_candidates_preserve_truthiness_status_and_legacy_dates(store, monkeypatch):
    items = [_item("missing"), _item("removed", list_removed_at="already removed")]
    items += [_item("false_%s" % index, candidate_input_completed_at=value) for index, value in enumerate(FALSY_JSON)]
    items += [_item("true_%s" % index, candidate_input_completed_at=value) for index, value in enumerate(TRUTHY_JSON)]
    items += [_item(status, status=status) for status in ["scheduled", "waiting", "paused", "cancelled", "report_ready", "completed"]]
    # Discovery cannot decide date validity, placement or actual expiry.
    items += [_item("future", scheduled_end_at="2999-01-01T00:00:00Z"),
              _item("invalid", scheduled_end_at="invalid"),
              _item("legacy", settings={"scheduled_end_at": "2000-01-01T00:00:00Z"}),
              _item("appointment", appointment_id="appointment_synthetic"),
              _item("other_org", organization_id="other_org")]
    _seed(store, items)
    _forbid_history(monkeypatch)
    expected = {"missing", "removed", "scheduled", "waiting", "paused", "future", "invalid", "legacy", "appointment"}
    expected.update("false_%s" % index for index in range(len(FALSY_JSON)))
    with persistence_for(store).transaction(ORGANIZATION_ID, read_only=True) as tx:
        result = tx.interview_sessions.watchdog_candidates("deadline")
        assert {item["id"] for item in result} == expected
        result[0]["status"] = "mutated"
        assert all(item["status"] != "mutated" for item in tx.interview_sessions.watchdog_candidates("deadline"))
        with pytest.raises(ReadOnlyViolation):
            tx.interview_sessions.add(_item("write_forbidden"))


def test_takeover_candidates_keep_damaged_leases_and_exclude_removed_sessions(store, monkeypatch):
    lease = {"lease_id": "lease_synthetic"}
    items = [_item("missing"), _item("empty", agent_runtime={"takeover": {}}),
             _item("valid", agent_runtime={"takeover": lease}),
             _item("expired", agent_runtime={"takeover": {"expires_at": "2000-01-01T00:00:00Z"}}),
             _item("invalid", agent_runtime={"takeover": {"expires_at": "not a date"}}),
             _item("future", agent_runtime={"takeover": {"expires_at": "2999-01-01T00:00:00Z"}}),
             _item("null_date", agent_runtime={"takeover": {"expires_at": None}}),
             _item("ended", status="report_ready", agent_runtime={"takeover": lease}),
             _item("other_org", organization_id="other_org", agent_runtime={"takeover": lease})]
    items += [_item("false_%s" % index, list_removed_at=value, agent_runtime={"takeover": lease}) for index, value in enumerate(FALSY_JSON)]
    items += [_item("true_%s" % index, list_removed_at=value, agent_runtime={"takeover": lease}) for index, value in enumerate(TRUTHY_JSON)]
    items += [_item("bad_runtime_%s" % index, agent_runtime=value) for index, value in enumerate([None, [], True, "value", 3])]
    items += [_item("bad_lease_%s" % index, agent_runtime={"takeover": value}) for index, value in enumerate([None, [], [1], True, "value", 3])]
    _seed(store, items)
    _forbid_history(monkeypatch)
    expected = {"valid", "expired", "invalid", "future", "null_date", "ended"}
    expected.update("false_%s" % index for index in range(len(FALSY_JSON)))
    with persistence_for(store).transaction(ORGANIZATION_ID, read_only=True) as tx:
        assert {item["id"] for item in tx.interview_sessions.watchdog_candidates("takeover")} == expected


@pytest.mark.parametrize("kind", ["deadline", "takeover"])
def test_repository_rejects_scope_leaks_and_invalid_backend_candidates(kind):
    eligible = _item("eligible", agent_runtime={"takeover": {"expires_at": "bad"}})
    rows = [eligible, {**eligible, "id": "other", "organization_id": "other_org"}]
    rows += [_item("ineligible", **({"status": "report_ready"} if kind == "deadline" else {"agent_runtime": {"takeover": {}}}))]
    rows += [_item("ineligible_flag", **({"candidate_input_completed_at": "done"} if kind == "deadline" else {"agent_runtime": eligible["agent_runtime"], "list_removed_at": "removed"}))]

    class LeakyBackend:
        def list_interview_watchdog_candidates(self, *, organization_id, kind):
            assert organization_id == ORGANIZATION_ID
            return rows

    repository = PersistenceTransaction(LeakyBackend(), ORGANIZATION_ID).interview_sessions
    result = repository.watchdog_candidates(kind)
    assert result == [{**eligible, "version": 1}]
    result[0]["agent_runtime"]["takeover"]["expires_at"] = "mutated"
    assert eligible["agent_runtime"]["takeover"]["expires_at"] == "bad"


def test_unknown_watchdog_kind_rejected_before_backend_query(store):
    with persistence_for(store).transaction(ORGANIZATION_ID, read_only=True) as tx:
        with pytest.raises(ValueError, match="watchdog"):
            tx.interview_sessions.watchdog_candidates("deadline' OR 1=1 --")


@pytest.mark.parametrize("kind", ["deadline", "takeover"])
def test_sqlite_query_uses_partial_index_and_only_decodes_candidates(tmp_path, monkeypatch, kind):
    store = SQLiteStore(str(tmp_path / "indexed.sqlite3"))
    with persistence_for(store).transaction(ORGANIZATION_ID) as tx:
        for index in range(2000):
            tx.interview_sessions.add(_item("history_%s" % index, status="report_ready", payload="x" * 1024))
        tx.interview_sessions.add(_item("candidate", agent_runtime={"takeover": {"expires_at": "invalid"}}))
    original_connect = store._connect
    statements = []

    def traced():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store, "_connect", traced)
    import app.persistence.sqlite as adapter
    original_loads = adapter.json.loads
    decoded_ids = []

    def counted_loads(value, *args, **kwargs):
        result = original_loads(value, *args, **kwargs)
        decoded_ids.append(result.get("id"))
        return result

    monkeypatch.setattr(adapter.json, "loads", counted_loads)
    _forbid_history(monkeypatch)
    with persistence_for(store).transaction(ORGANIZATION_ID, read_only=True) as tx:
        assert [item["id"] for item in tx.interview_sessions.watchdog_candidates(kind)] == ["candidate"]
    assert decoded_ids == ["candidate"]
    query = next(sql for sql in statements if sql.startswith("SELECT data") and "interviews" in sql)
    with original_connect() as connection:
        plan = connection.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    assert any("idx_interviews_watchdog_%s" % kind in row["detail"] for row in plan)
