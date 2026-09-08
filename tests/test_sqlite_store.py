import asyncio
import json

from app.core.time import utc_now
from app.persistence.provider import persistence_for
from app.repositories import provider as repository_provider
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.catalog import CatalogService
from app.workers.outbox import OutboxWorker


def test_sqlite_store_persists_catalog_questions_and_speech_work(tmp_path) -> None:
    db_path = tmp_path / "interviewer.sqlite3"
    store = SQLiteStore(str(db_path))
    service = CatalogService(store)
    position = service.create_position({"code": "backend", "name": "后端工程师"})
    knowledge_base = service.create_knowledge_base(position["id"], {"name": "数据库题库"})

    question = asyncio.run(
        service.create_question(
            knowledge_base["id"],
            {
                "knowledge_base_id": knowledge_base["id"],
                "title": "B+ 树索引",
                "question_text": "B+ 树索引为什么适合范围查询？",
                "standard_answer": "B+ 树叶子节点有序并通过链表连接，适合范围扫描。",
                "key_points": ["叶子节点有序", "范围扫描", "磁盘 IO 友好"],
                "difficulty": "mid",
                "skills": ["database", "mysql"],
                "type": "open_ended",
                "role_families": ["backend_engineer"],
                "rubric": {"semantic_correctness": 1.0},
            }
        )
    )
    asyncio.run(OutboxWorker(store).run_once())
    question = service.get_question(question["id"])

    assert question["index_status"] == "not_required"
    assert question["speech_status"] == "ready"
    assert question["version"] == 2
    assert list(store.outbox_work_items.values())[0]["status"] == "completed"
    assert store.model_invocations

    reopened = SQLiteStore(str(db_path))
    assert question["id"] in reopened.questions
    assert reopened.questions[question["id"]]["title"] == "B+ 树索引"
    assert reopened.knowledge_bases[knowledge_base["id"]]["status"] == "ready"
    assert list(reopened.outbox_work_items.values())[0]["status"] == "completed"
    assert reopened.model_invocations


def test_reset_store_for_tests_never_resets_development_sqlite(tmp_path) -> None:
    db_path = tmp_path / "development.sqlite3"
    store = SQLiteStore(str(db_path))
    persisted = {
        **store.provider_connections["provider_conn_mock"],
        "id": "provider_conn_persisted",
        "display_name": "Must survive test reset",
    }
    store.provider_connections[persisted["id"]] = persisted
    store.save_item("provider_connections", persisted["id"], persisted)
    repository_provider._store = store

    test_store = repository_provider.reset_store_for_tests()

    assert isinstance(test_store, InMemoryStore)
    assert not isinstance(test_store, SQLiteStore)
    reopened = SQLiteStore(str(db_path))
    assert reopened.provider_connections[persisted["id"]]["display_name"] == "Must survive test reset"


def test_sqlite_realtime_transaction_refreshes_only_its_committed_delta(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "realtime-delta.sqlite3"
    store = SQLiteStore(str(db_path))
    now = utc_now()
    interview = {
        "id": "iv_realtime_delta",
        "organization_id": "org_default",
        "status": "in_progress",
        "agent_runtime": {"floor": "candidate"},
        "created_at": now,
        "updated_at": now,
        "version": 1,
    }
    store.save_item("interviews", interview["id"], interview)
    # Reproduce the local development shape that made every VAD command parse
    # ten thousand unrelated rows after commit.
    with store._connect() as connection:
        connection.executemany(
            "INSERT INTO documents(collection, id, data, updated_at) VALUES (?, ?, ?, ?)",
            [
                (
                    "audit_events",
                    "audit_history_%05d" % index,
                    json.dumps(
                        {
                            "id": "audit_history_%05d" % index,
                            "organization_id": "org_default",
                            "event_type": "historical.event",
                            "created_at": now,
                            "updated_at": now,
                            "version": 1,
                        }
                    ),
                    now,
                )
                for index in range(2_000)
            ],
        )
    store._load_from_db()

    parsed_rows = 0
    from app.persistence import sqlite as sqlite_persistence

    real_loads = sqlite_persistence.json.loads

    def counted_loads(value):
        nonlocal parsed_rows
        parsed_rows += 1
        return real_loads(value)

    monkeypatch.setattr(sqlite_persistence.json, "loads", counted_loads)
    persistence = persistence_for(store)
    with persistence.transaction("org_default") as transaction:
        current = transaction.interview_sessions.get(interview["id"])
        current["agent_runtime"]["last_sequence"] = 1
        current["updated_at"] = utc_now()
        updated = transaction.interview_sessions.update(
            current, expected_version=current["version"]
        )

    assert parsed_rows == 2  # one CAS read plus the explicit repository get
    assert updated["version"] == 2
    assert store.interviews[interview["id"]]["agent_runtime"]["last_sequence"] == 1
    assert len(store.audit_events) == 2_000
