import asyncio

from app.repositories.sqlite import SQLiteStore
from app.services.questions import QuestionService


def test_sqlite_store_persists_questions_and_vectors(tmp_path) -> None:
    db_path = tmp_path / "interviewer.sqlite3"
    store = SQLiteStore(str(db_path))
    service = QuestionService(store)

    question = asyncio.run(
        service.create_question(
            {
                "knowledge_base_id": "kb_backend",
                "title": "B+ 树索引",
                "question_text": "B+ 树索引为什么适合范围查询？",
                "standard_answer": "B+ 树叶子节点有序并通过链表连接，适合范围扫描。",
                "key_points": ["叶子节点有序", "范围扫描", "磁盘 IO 友好"],
                "difficulty": "mid",
                "skills": ["database", "mysql"],
                "type": "open_ended",
                "role_families": ["backend_engineer"],
                "rubric": {},
            }
        )
    )

    assert question["index_status"] == "indexed"
    assert question["version"] == 2
    assert len(store.vector_documents) == 4
    assert list(store.outbox_work_items.values())[0]["status"] == "completed"
    assert store.model_invocations

    reopened = SQLiteStore(str(db_path))
    assert question["id"] in reopened.questions
    assert reopened.questions[question["id"]]["title"] == "B+ 树索引"
    assert len(reopened.vector_documents) == 4
    assert list(reopened.outbox_work_items.values())[0]["status"] == "completed"
    assert reopened.model_invocations
