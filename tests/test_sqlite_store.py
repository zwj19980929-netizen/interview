import asyncio

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
