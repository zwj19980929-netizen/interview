"""The scheduler discovers durable work without competing for the writer lock."""

import pytest

from app.persistence.errors import ConcurrencyConflict
from app.persistence.interface import new_work_item
from app.persistence.provider import persistence_for
from app.repositories.sqlite import SQLiteStore
from app.workers import dispatcher


def test_dispatcher_reads_committed_work_while_another_writer_is_active(tmp_path, monkeypatch):
    store = SQLiteStore(str(tmp_path / "dispatch.sqlite3"))
    persistence = persistence_for(store)
    item = new_work_item(
        organization_id="org_default", kind="contract.dispatch", aggregate_id="committed",
        idempotency_key="contract.dispatch:committed",
    )
    with persistence.transaction("org_default") as transaction:
        transaction.outbox.enqueue(item)
    with persistence.transaction("org_other") as transaction:
        transaction.outbox.enqueue({**item, "id": "other_tenant", "organization_id": "org_other"})

    connect = store._connect

    def short_wait_connection():
        connection = connect()
        # This is a deterministic lock regression, not a wall-clock benchmark:
        # BEGIN IMMEDIATE would fail as soon as it competes with the writer.
        connection.execute("PRAGMA busy_timeout = 0")
        return connection

    monkeypatch.setattr(store, "_connect", short_wait_connection)
    monkeypatch.setattr(dispatcher, "get_store", lambda: store)
    monkeypatch.setenv("INTERVIEWER_ORGANIZATION_ID", "org_default")
    published = []

    def publish(organization_id, work_item_id):
        # Publication happens only after the read snapshot has closed.
        with store._connect() as connection:
            connection.execute("BEGIN EXCLUSIVE")
            connection.rollback()
        published.append((organization_id, work_item_id))

    # Keep a real second connection's writer reservation open. The scheduler
    # must still read committed data, including before this writer commits.
    writer = connect()
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO outbox_work_items SELECT 'uncommitted', organization_id, "
        "'uncommitted', status, replace(data, ?, 'uncommitted'), created_at, updated_at "
        "FROM outbox_work_items WHERE id = ?", (item["id"], item["id"]),
    )
    monkeypatch.setattr(dispatcher.execute_work_item, "delay", lambda *args: published.append(args))
    try:
        assert dispatcher.dispatch_due_work() == {"organization_id": "org_default", "dispatched": 1}
        assert published == [("org_default", item["id"])]
    finally:
        writer.rollback()
        writer.close()

    # Read-only dispatch did not acquire a claim or spend an attempt. Once the
    # writer is gone, publication can acquire an exclusive lock: no leaked read
    # transaction remains to block the actual executor.
    published.clear()
    monkeypatch.setattr(dispatcher.execute_work_item, "delay", publish)
    assert dispatcher.dispatch_due_work()["dispatched"] == 1
    with persistence.transaction("org_default") as transaction:
        queued = transaction.outbox.get(item["id"])
        assert queued["status"] == "pending"
        assert queued["attempt_count"] == 0
        running = transaction.outbox.start(item["id"])
    assert running["status"] == "running"
    with pytest.raises(ConcurrencyConflict):
        with persistence.transaction("org_default") as transaction:
            transaction.outbox.start(item["id"])


def test_empty_dispatch_does_not_take_a_writer_reservation(tmp_path, monkeypatch):
    store = SQLiteStore(str(tmp_path / "empty-dispatch.sqlite3"))
    writer = store._connect()
    writer.execute("BEGIN IMMEDIATE")
    connect = store._connect

    def short_wait_connection():
        connection = connect()
        connection.execute("PRAGMA busy_timeout = 0")
        return connection

    monkeypatch.setattr(store, "_connect", short_wait_connection)
    monkeypatch.setattr(dispatcher, "get_store", lambda: store)
    monkeypatch.setattr(dispatcher.execute_work_item, "delay", lambda *args: pytest.fail("No work is due"))
    monkeypatch.setenv("INTERVIEWER_ORGANIZATION_ID", "org_default")
    try:
        for _ in range(5):
            assert dispatcher.dispatch_due_work()["dispatched"] == 0
    finally:
        writer.rollback()
        writer.close()
