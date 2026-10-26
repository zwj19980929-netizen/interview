"""Fresh media authorization reads must not materialize whole interviews."""
import json
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from app.persistence.interface import PersistenceTransaction
from app.persistence.memory import _MemoryTransactionBackend
from app.persistence.provider import persistence_for
from app.persistence.sqlite import _SQLiteTransactionBackend
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore


ORG = "org_realtime_projection"
IDENTITY = "interview_synthetic"


def _session():
    return {
        "id": IDENTITY, "organization_id": ORG, "status": "in_progress",
        "current_turn_id": "turn_1", "candidate_input_completed_at": None,
        "agent_runtime": {"floor": "agent", "active_performance_id": "performance_1",
                          "completion_emitted_at": None, "takeover": {"status": "released"},
                          "private_memory": "private memory"},
        "plan_snapshot": {"enterprise_skill_snapshot": {"skill_id": "synthetic"},
                          "question_pool": "private plan" * 10000},
        "turns": [{"question_snapshot": "private question" * 1000}],
        "answers": [{"text": "private answer" * 1000}],
        "agent_events": [
            {"event_id": "event_a", "type": "conversation.act.selected",
             "payload": {"act_id": "act_a", "approved": True, "text": "private speech"}},
            {"event_id": "event_b", "type": "conversation.act.selected",
             "payload": {"act_id": "act_b", "approved": True, "text": "private speech"}},
            {"event_id": "event_a_again", "type": "conversation.act.selected",
             "payload": {"act_id": "act_a", "approved": True, "text": "private speech"}},
            {"event_id": "other", "type": "avatar.performance.started", "payload": {"text": "private"}},
        ],
    }


@pytest.fixture(params=["memory", "sqlite"])
def persistence(request, tmp_path):
    store = InMemoryStore() if request.param == "memory" else SQLiteStore(str(tmp_path / "realtime.sqlite3"))
    persistence = persistence_for(store)
    with persistence.transaction(ORG) as tx:
        tx.interview_sessions.add(_session())
    return persistence


def _read(persistence, organization_id=ORG):
    with persistence.transaction(organization_id, read_only=True) as tx:
        return tx.interview_sessions.realtime_state(IDENTITY)


def test_projection_excludes_content_and_keeps_last_selection_without_full_get(persistence, monkeypatch):
    for backend in (_MemoryTransactionBackend, _SQLiteTransactionBackend):
        original = backend.get_document

        def guarded(self, collection, item_id, original=original):
            assert collection != "interviews", "Media guard decoded the full interview"
            return original(self, collection, item_id)

        monkeypatch.setattr(backend, "get_document", guarded)
    projected = _read(persistence)
    assert projected["id"] == IDENTITY and projected["version"] == 1
    assert "private" not in json.dumps(projected)
    assert "turns" not in projected and "answers" not in projected
    assert projected["agent_events"] == [{"type": "conversation.act.selected", "event_id": "event_a_again",
                                            "payload": {"act_id": "act_a", "approved": True}}]
    projected["agent_runtime"]["floor"] = "mutated"
    assert _read(persistence)["agent_runtime"]["floor"] == "agent"
    assert _read(persistence, "another_org") is None


@pytest.mark.parametrize("field,value", [
    ("status", "paused"), ("current_turn_id", "turn_2"),
    ("candidate_input_completed_at", "now"),
    ("agent_runtime", {"floor": "human", "active_performance_id": "new", "takeover": {"status": "active"}}),
    ("plan_snapshot", {"enterprise_skill_snapshot": {"skill_id": "replaced"}}),
    ("agent_events", [{"event_id": "revoked", "type": "conversation.act.selected",
                       "payload": {"act_id": "act_a", "approved": False}}]),
])
def test_each_read_observes_committed_revocation(persistence, field, value):
    before = _read(persistence)
    with persistence.transaction(ORG) as tx:
        session = tx.interview_sessions.get(IDENTITY)
        session[field] = deepcopy(value)
        tx.interview_sessions.update(session, expected_version=session["version"])
    after = _read(persistence)
    assert before[field] != after[field]
    assert after["version"] == 2


@pytest.mark.parametrize("approved", [True, False, 1, 0, "true", None])
def test_selection_does_not_coerce_approval_type(persistence, approved):
    with persistence.transaction(ORG) as tx:
        session = tx.interview_sessions.get(IDENTITY)
        session["agent_events"][-2]["payload"]["approved"] = approved
        tx.interview_sessions.update(session, expected_version=session["version"])
    actual = _read(persistence)["agent_events"][0]["payload"]["approved"]
    assert type(actual) is type(approved) and actual == approved


@pytest.mark.parametrize("container", ["plan_snapshot", "agent_runtime", "takeover"])
@pytest.mark.parametrize("malformed", [True, "unexpected", [1]])
def test_malformed_guard_containers_fail_closed(persistence, container, malformed):
    with persistence.transaction(ORG) as tx:
        session = tx.interview_sessions.get(IDENTITY)
        if container == "takeover":
            session["agent_runtime"]["takeover"] = malformed
        else:
            session[container] = malformed
        tx.interview_sessions.update(session, expected_version=session["version"])
    assert _read(persistence) is None


def test_repository_defensively_rejects_backend_scope_leak():
    class Backend:
        def get_interview_realtime_state(self, **kwargs):
            return {"id": IDENTITY, "organization_id": "wrong_org"}
    assert PersistenceTransaction(Backend(), ORG).interview_sessions.realtime_state(IDENTITY) is None


def test_empty_legacy_state_has_no_selected_expression(persistence):
    with persistence.transaction(ORG) as tx:
        session = tx.interview_sessions.get(IDENTITY)
        for key in ("plan_snapshot", "agent_runtime", "agent_events"):
            session.pop(key)
        tx.interview_sessions.update(session, expected_version=session["version"])
    projected = _read(persistence)
    assert projected["agent_events"] == [] and projected["agent_runtime"]["floor"] is None
    assert projected["plan_snapshot"]["enterprise_skill_snapshot"] is None
    assert projected["agent_runtime"]["calibration_status"] is None
    assert projected["agent_runtime"]["calibration_updated_at"] is None


@pytest.mark.parametrize("next_status", ["completed", "retrying"])
def test_warmup_guard_sees_explicit_confirm_or_retry_without_decoding_history(persistence, monkeypatch, next_status):
    with persistence.transaction(ORG) as tx:
        session = tx.interview_sessions.get(IDENTITY)
        session["current_turn_id"] = None
        session["agent_runtime"].update(calibration_status="awaiting_confirmation", calibration_updated_at="trial_1")
        tx.interview_sessions.update(session, expected_version=session["version"])
    before = _read(persistence)
    with persistence.transaction(ORG) as tx:
        session = tx.interview_sessions.get(IDENTITY)
        session["agent_runtime"].update(calibration_status=next_status, calibration_updated_at="trial_2")
        tx.interview_sessions.update(session, expected_version=session["version"])
    for backend in (_MemoryTransactionBackend, _SQLiteTransactionBackend):
        original = backend.get_document
        def guarded(self, collection, item_id, original=original):
            assert collection != "interviews", "Warmup PCM guard decoded whole history"
            return original(self, collection, item_id)
        monkeypatch.setattr(backend, "get_document", guarded)
    after = _read(persistence)
    assert before["agent_runtime"]["calibration_status"] == "awaiting_confirmation"
    assert after["agent_runtime"]["calibration_status"] == next_status
    assert after["agent_runtime"]["calibration_updated_at"] != before["agent_runtime"]["calibration_updated_at"]
    assert "private" not in json.dumps(after)


def test_postgresql_projection_is_parameterized_scoped_and_read_only():
    from app.persistence.postgresql import _PostgreSQLTransactionBackend
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = None
    backend = _PostgreSQLTransactionBackend(connection, read_only=True)
    assert backend.get_interview_realtime_state(organization_id=ORG, interview_id=IDENTITY) is None
    sql, params = connection.execute.call_args.args
    assert "jsonb_build_array" in sql and "WITH ORDINALITY" in sql
    assert "organization_id = %s" in sql and "id = %s" in sql
    assert "FOR UPDATE" not in sql
    assert "{agent_runtime,calibration_status}" in sql
    assert "{agent_runtime,calibration_updated_at}" in sql
    assert params == (IDENTITY, ORG)


def test_sqlite_guard_only_decodes_bounded_projection_while_writer_is_open(tmp_path, monkeypatch):
    store = SQLiteStore(str(tmp_path / "guard_load.sqlite3"))
    persistence = persistence_for(store)
    with persistence.transaction(ORG) as tx:
        item = _session()
        item["agent_events"] = item["agent_events"] * 20
        item["answers"] *= 6
        tx.interview_sessions.add(item)
    import app.persistence.sqlite as adapter
    original_loads = adapter.json.loads
    decoded_lengths = []

    def bounded_loads(value, *args, **kwargs):
        decoded_lengths.append(len(value))
        assert len(value) < 2000, "Guard decoded interview history instead of its projection"
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(adapter.json, "loads", bounded_loads)
    original_connect = store._connect

    def short_timeout_connect():
        connection = original_connect()
        connection.execute("PRAGMA busy_timeout = 25")
        return connection

    monkeypatch.setattr(store, "_connect", short_timeout_connect)
    writer = original_connect()
    try:
        writer.execute("BEGIN IMMEDIATE")
        assert _read(persistence)["status"] == "in_progress"
        assert len(decoded_lengths) == 2
    finally:
        writer.rollback()
        writer.close()
