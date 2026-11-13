"""Idle realtime reads must not wait behind an unrelated SQLite writer."""

from contextlib import contextmanager

import pytest

from app.core.errors import ApiError
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.evidence_command_journal import EvidenceCommandJournal
from app.services.evidence_coordination import EvidenceOwnershipLost
from app.services.interviews import InterviewService
from test_evidence_command_journal import (
    INTERVIEW_ID, ORGANIZATION_ID, _grant, _session, _submission,
)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    return (InMemoryStore() if request.param == "memory"
            else SQLiteStore(str(tmp_path / "idle_reads.sqlite3")))


@pytest.mark.parametrize("operation", ["empty_claim", "get_interview"])
def test_sqlite_idle_reads_complete_while_another_writer_holds_its_transaction(tmp_path, monkeypatch, operation):
    store = SQLiteStore(str(tmp_path / "busy.sqlite3"))
    _session(store)
    grant = _grant(store)
    journal = EvidenceCommandJournal(store)
    interviews = InterviewService(store)
    original = store._connect
    statements = []

    def short_timeout_connection():
        connection = original()
        # A wrong BEGIN IMMEDIATE fails promptly rather than making a timing
        # assertion depend on machine load or blocking the suite for 5 seconds.
        connection.execute("PRAGMA busy_timeout = 25")
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store, "_connect", short_timeout_connection)
    writer = original()
    try:
        writer.execute("BEGIN IMMEDIATE")
        if operation == "empty_claim":
            assert journal.claim_next(grant.commit_fence(), ORGANIZATION_ID) is None
        else:
            assert interviews.get_interview(INTERVIEW_ID, ORGANIZATION_ID)["status"] == "in_progress"
        assert "BEGIN IMMEDIATE" not in statements
        assert "PRAGMA query_only = ON" in statements
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("change", [
    {"state": "released"},
    {"lease_expires_at": "2000-01-01T00:00:00Z"},
    {"ownership_epoch": 999},
    {"lease_id": "superseded_lease"},
    {"owner_instance_id": "new_owner"},
])
def test_empty_claim_still_rejects_invalid_owner(store, change):
    _session(store)
    grant = _grant(store)
    persistence = persistence_for(store)
    with persistence.transaction(ORGANIZATION_ID) as tx:
        owner = tx.evidence_ownerships.get(grant.ownership_id)
        tx.evidence_ownerships.update({**owner, **change}, expected_version=owner["version"])
    with pytest.raises(EvidenceOwnershipLost):
        EvidenceCommandJournal(store).claim_next(grant.commit_fence(), ORGANIZATION_ID)


@pytest.mark.parametrize("change", [
    {"ownership_epoch": 999},
    {"lease_expires_at": "2000-01-01T00:00:00Z"},
])
def test_owner_is_revalidated_when_entering_claim_write_transaction(store, change):
    _session(store)
    grant = _grant(store)
    persistence = persistence_for(store)
    receipt = EvidenceCommandJournal(store).submit(grant, _submission())
    modes = []

    class OwnershipChangesAfterRead:
        @contextmanager
        def transaction(self, organization_id, *, read_only=False):
            modes.append(read_only)
            with persistence.transaction(organization_id, read_only=read_only) as tx:
                yield tx
            if read_only:
                # A separate writer wins after discovery but before claiming.
                with persistence.transaction(organization_id) as tx:
                    owner = tx.evidence_ownerships.get(grant.ownership_id)
                    tx.evidence_ownerships.update(
                        {**owner, **change},
                        expected_version=owner["version"],
                    )

    journal = EvidenceCommandJournal(store, persistence=OwnershipChangesAfterRead())
    with pytest.raises(EvidenceOwnershipLost):
        journal.claim_next(grant.commit_fence(), ORGANIZATION_ID)
    assert modes == [True, False]
    with persistence.transaction(ORGANIZATION_ID, read_only=True) as tx:
        command = tx.evidence_commands.get(receipt.command_id)
        assert command["status"] == "pending"
        assert command["claim_id"] is None and command["attempt_count"] == 0


def test_command_claimed_after_discovery_is_not_claimed_twice(store):
    _session(store)
    grant = _grant(store)
    persistence = persistence_for(store)
    other_journal = EvidenceCommandJournal(store)
    receipt = other_journal.submit(grant, _submission())
    competing_claims = []

    class AnotherClaimWinsAfterRead:
        @contextmanager
        def transaction(self, organization_id, *, read_only=False):
            with persistence.transaction(organization_id, read_only=read_only) as tx:
                yield tx
            if read_only:
                competing_claims.append(other_journal.claim_next(grant.commit_fence(), organization_id))

    journal = EvidenceCommandJournal(store, persistence=AnotherClaimWinsAfterRead())
    assert journal.claim_next(grant.commit_fence(), ORGANIZATION_ID) is None
    assert len(competing_claims) == 1 and competing_claims[0].command_id == receipt.command_id
    with persistence.transaction(ORGANIZATION_ID, read_only=True) as tx:
        command = tx.evidence_commands.get(receipt.command_id)
        assert command["status"] == "running"
        assert command["claim_id"] == competing_claims[0].claim_id
        assert command["attempt_count"] == 1


def test_get_interview_remains_tenant_scoped_and_returns_detached_state(store):
    _session(store)
    interviews = InterviewService(store)
    found = interviews.get_interview(INTERVIEW_ID, ORGANIZATION_ID)
    found["status"] = "cancelled"
    assert interviews.get_interview(INTERVIEW_ID, ORGANIZATION_ID)["status"] == "in_progress"
    with pytest.raises(ApiError) as denied:
        interviews.get_interview(INTERVIEW_ID, "another_org")
    assert denied.value.code == "INTERVIEW_NOT_FOUND"
