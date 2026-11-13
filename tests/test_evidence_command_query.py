"""Idle command polling must not deserialize completed organization history."""
import pytest

from app.persistence.memory import _MemoryTransactionBackend
from app.persistence.interface import EvidenceCommandRepository
from app.persistence.provider import persistence_for
from app.persistence.sqlite import _SQLiteTransactionBackend
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.evidence_command_journal import EvidenceCommandJournal
from test_evidence_command_journal import _session, _grant, _submission, INTERVIEW_ID, ORGANIZATION_ID


@pytest.fixture(params=['memory', 'sqlite'])
def store(request, tmp_path):
    return InMemoryStore() if request.param == 'memory' else SQLiteStore(str(tmp_path / 'commands.sqlite3'))


def forbid_history_scan(monkeypatch):
    for cls in (_MemoryTransactionBackend, _SQLiteTransactionBackend):
        original = cls.list_documents

        def guarded(backend, collection, original=original):
            assert collection != 'evidence_commands', 'Realtime polling scanned completed command history'
            return original(backend, collection)

        monkeypatch.setattr(cls, 'list_documents', guarded)


def test_idle_poll_and_claim_ignore_thousands_of_completed_commands(store, monkeypatch):
    _session(store)
    grant = _grant(store)
    persistence = persistence_for(store)
    with persistence.transaction(ORGANIZATION_ID) as tx:
        for index in range(2500):
            tx.evidence_commands.add({'id': 'old_%s' % index, 'organization_id': ORGANIZATION_ID,
                'interview_id': INTERVIEW_ID, 'status': 'completed', 'outcome': {'padding': 'x' * 512}})
    forbid_history_scan(monkeypatch)
    journal = EvidenceCommandJournal(store)
    assert journal.claim_next(grant.commit_fence(), ORGANIZATION_ID) is None
    assert not journal.has_unsettled(INTERVIEW_ID, ORGANIZATION_ID)
    receipt = journal.submit(grant, _submission())
    assert journal.has_unsettled(INTERVIEW_ID, ORGANIZATION_ID)
    claimed = journal.claim_next(grant.commit_fence(), ORGANIZATION_ID)
    assert claimed is not None and claimed.command_id == receipt.command_id
    # The live claim is not immediately claimable again; persisted history and
    # idempotency remain authoritative, rather than being deleted for speed.
    assert journal.claim_next(grant.commit_fence(), ORGANIZATION_ID) is None
    with persistence.transaction(ORGANIZATION_ID, read_only=True) as tx:
        assert tx.evidence_commands.get('old_2499')['status'] == 'completed'


def test_unsettled_query_is_tenant_session_status_scoped_and_read_only(store, monkeypatch):
    persistence = persistence_for(store)
    for org, interview, status, identity in [
        (ORGANIZATION_ID, INTERVIEW_ID, 'pending', 'pending'),
        (ORGANIZATION_ID, INTERVIEW_ID, 'running', 'running'),
        (ORGANIZATION_ID, INTERVIEW_ID, 'failed', 'failed'),
        (ORGANIZATION_ID, 'other_interview', 'pending', 'other_interview'),
        ('other_org', INTERVIEW_ID, 'pending', 'other_org'),
    ]:
        with persistence.transaction(org) as tx:
            tx.evidence_commands.add({'id': identity, 'organization_id': org, 'interview_id': interview, 'status': status})
    forbid_history_scan(monkeypatch)
    with persistence.transaction(ORGANIZATION_ID, read_only=True) as tx:
        found = tx.evidence_commands.list_unsettled(INTERVIEW_ID)
        assert {item['id'] for item in found} == {'pending', 'running'}
        found[0]['status'] = 'tampered'
        assert {item['status'] for item in tx.evidence_commands.list_unsettled(INTERVIEW_ID)} == {'pending', 'running'}


def test_repository_defensively_rejects_backend_scope_leaks():
    class LeakyBackend:
        def list_unsettled_evidence_commands(self, *, organization_id, interview_id):
            assert organization_id == ORGANIZATION_ID and interview_id == INTERVIEW_ID
            return [
                {'organization_id': ORGANIZATION_ID, 'interview_id': INTERVIEW_ID, 'status': 'pending'},
                {'organization_id': 'other_org', 'interview_id': INTERVIEW_ID, 'status': 'pending'},
                {'organization_id': ORGANIZATION_ID, 'interview_id': 'other_interview', 'status': 'pending'},
                {'organization_id': ORGANIZATION_ID, 'interview_id': INTERVIEW_ID, 'status': 'completed'},
            ]

    repository = EvidenceCommandRepository(LeakyBackend(), ORGANIZATION_ID)
    assert repository.list_unsettled(INTERVIEW_ID) == [
        {'organization_id': ORGANIZATION_ID, 'interview_id': INTERVIEW_ID, 'status': 'pending'},
    ]


@pytest.mark.parametrize('change, expected', [
    ({'available_at': '2999-01-01T00:00:00Z'}, 'pending'),
    ({'available_at': 'invalid-date'}, 'running'),
    ({'available_at': '2999-01-01T00:00:00Z', 'deadline_at': '2000-01-01T00:00:00Z'}, 'expired'),
    ({'available_at': '2999-01-01T00:00:00Z', 'control_generation': 0}, 'rejected'),
])
def test_query_does_not_bypass_due_time_deadline_or_control_checks(store, monkeypatch, change, expected):
    _session(store)
    grant = _grant(store)
    journal = EvidenceCommandJournal(store)
    receipt = journal.submit(grant, _submission())
    with persistence_for(store).transaction(ORGANIZATION_ID) as tx:
        command = tx.evidence_commands.get(receipt.command_id)
        command.update(change)
        tx.evidence_commands.update(command, expected_version=command['version'])
    forbid_history_scan(monkeypatch)
    claimed = journal.claim_next(grant.commit_fence(), ORGANIZATION_ID)
    assert (claimed is not None) == (expected == 'running')
    with persistence_for(store).transaction(ORGANIZATION_ID, read_only=True) as tx:
        assert tx.evidence_commands.get(receipt.command_id)['status'] == expected


def test_sqlite_unsettled_query_uses_partial_index_without_decoding_history(tmp_path, monkeypatch):
    store = SQLiteStore(str(tmp_path / 'indexed.sqlite3'))
    persistence = persistence_for(store)
    statements = []
    original = store._connect

    def traced():
        connection = original()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store, '_connect', traced)
    with persistence.transaction(ORGANIZATION_ID, read_only=True) as tx:
        assert tx.evidence_commands.list_unsettled(INTERVIEW_ID) == []
    query = next(sql for sql in statements if sql.startswith('SELECT data') and 'evidence_commands' in sql)
    with original() as connection:
        plan = connection.execute('EXPLAIN QUERY PLAN ' + query).fetchall()
    assert any('idx_evidence_commands_unsettled' in row['detail'] for row in plan)
