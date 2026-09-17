"""巡检空转不抢写锁；候选发现不能绕过最终生命周期校验。"""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.persistence.provider import persistence_for
from app.repositories.sqlite import SQLiteStore
from app.services.interviews import InterviewService
from app.services.interview_agent import InterviewAgentRuntime
from test_interview_agent_contracts import _runtime_session

ORG = 'org_default'


def future():
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def past():
    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def setup(tmp_path, **changes):
    store = SQLiteStore(str(tmp_path / 'watchdog.sqlite3'))
    session = _runtime_session(store)
    persistence = persistence_for(store)
    with persistence.transaction(ORG) as tx:
        item = tx.interview_sessions.get(session['id'])
        item.update(changes)
        tx.interview_sessions.update(item, expected_version=item['version'])
    return store, session['id']


def short_timeout(store, monkeypatch):
    original = store._connect
    def connect():
        connection = original()
        connection.execute('PRAGMA busy_timeout = 20')
        return connection
    monkeypatch.setattr(store, '_connect', connect)
    return original


@pytest.mark.parametrize('sweep', ['takeover', 'deadline', 'single_takeover', 'lease_delay'])
def test_idle_watchdogs_do_not_acquire_database_writer_lock(tmp_path, monkeypatch, sweep):
    store, identity = setup(tmp_path, scheduled_end_at=future())
    runtime = InterviewAgentRuntime(store)
    original = short_timeout(store, monkeypatch)
    lock = original()
    lock.execute('BEGIN IMMEDIATE')
    try:
        if sweep == 'takeover':
            assert asyncio.run(runtime.sweep_expired_takeovers()) == 0
        elif sweep == 'deadline':
            assert runtime.interviews.expire_overdue_interviews() == []
        elif sweep == 'lease_delay':
            assert runtime._takeover_seconds_remaining(identity, ORG, lease_id='none') is None
        else:
            assert runtime._expire_takeover_if_due(identity, ORG) is None
    finally:
        lock.rollback()
        lock.close()


@pytest.mark.parametrize('change', [
    {'scheduled_end_at': future()},
    {'candidate_input_completed_at': past()},
    {'status': 'report_ready'},
])
def test_deadline_candidate_is_rechecked_inside_mutating_transaction(tmp_path, change):
    store, identity = setup(tmp_path, scheduled_end_at=past())
    service = InterviewService(store)
    persistence = service.persistence
    fired = []
    class ChangedBetweenTransactions:
        @contextmanager
        def transaction(self, organization_id, *, read_only=False):
            if not read_only and not fired:
                fired.append(True)
                with persistence.transaction(organization_id) as tx:
                    item = tx.interview_sessions.get(identity)
                    item.update(change)
                    tx.interview_sessions.update(item, expected_version=item['version'])
            with persistence.transaction(organization_id, read_only=read_only) as tx:
                yield tx
    service.persistence = ChangedBetweenTransactions()
    assert service.expire_overdue_interviews() == []
    assert fired  # a real due candidate was found before the concurrent change
    with persistence.transaction(ORG, read_only=True) as tx:
        item = tx.interview_sessions.get(identity)
        assert all(item[k] == v for k, v in change.items())
        assert not tx.audit_events.list()


def test_renewed_takeover_not_consumed_from_outdated_read(tmp_path):
    store, identity = setup(tmp_path)
    runtime = InterviewAgentRuntime(store)
    persistence = runtime.persistence
    with persistence.transaction(ORG) as tx:
        item = tx.interview_sessions.get(identity)
        item['agent_runtime']['takeover'] = {'lease_id': 'kept', 'expires_at': past()}
        tx.interview_sessions.update(item, expected_version=item['version'])
    fired = []
    class RenewedBetweenTransactions:
        @contextmanager
        def transaction(self, organization_id, *, read_only=False):
            if not read_only and not fired:
                fired.append(True)
                with persistence.transaction(organization_id) as tx:
                    item = tx.interview_sessions.get(identity)
                    item['agent_runtime']['takeover']['expires_at'] = future()
                    tx.interview_sessions.update(item, expected_version=item['version'])
            with persistence.transaction(organization_id, read_only=read_only) as tx:
                yield tx
    runtime.persistence = RenewedBetweenTransactions()
    assert runtime._expire_takeover_if_due(identity, ORG) is None
    assert fired
    with persistence.transaction(ORG, read_only=True) as tx:
        assert tx.interview_sessions.get(identity)['agent_runtime']['takeover']['lease_id'] == 'kept'
        assert not tx.audit_events.list()
