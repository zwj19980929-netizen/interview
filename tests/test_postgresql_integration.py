import os
from uuid import uuid4

import pytest

from app.persistence.errors import ConcurrencyConflict, RecordAlreadyExists
from app.persistence.interface import new_work_item
from app.persistence.postgresql import PostgreSQLPersistence
from app.repositories.postgresql import PostgreSQLStore


POSTGRES_DSN = os.getenv("INTERVIEWER_TEST_POSTGRES_DSN", "").strip()


@pytest.mark.skipif(not POSTGRES_DSN, reason="INTERVIEWER_TEST_POSTGRES_DSN is not configured")
def test_real_postgresql_runtime_role_is_transactional_and_tenant_scoped() -> None:
    persistence = PostgreSQLPersistence(PostgreSQLStore(POSTGRES_DSN))
    suffix = uuid4().hex
    question_id = "q_pg_%s" % suffix
    position_id = "position_pg_%s" % suffix
    knowledge_base_id = "kb_pg_%s" % suffix
    item = {
        "id": question_id,
        "organization_id": "org_pg_a",
        "job_position_id": position_id,
        "knowledge_base_id": knowledge_base_id,
        "title": "PostgreSQL integration",
        "question_text": "Does RLS isolate tenants?",
        "standard_answer": "Yes.",
        "key_points": [],
        "difficulty": "mid",
        "type": "open_ended",
        "skills": ["postgresql"],
        "role_families": [],
        "rubric": {},
        "status": "active",
        "validation_status": "valid",
        "speech_status": "ready",
        "index_status": "pending",
        "created_at": "2026-08-26T00:00:00Z",
        "updated_at": "2026-08-26T00:00:00Z",
    }

    with pytest.raises(RuntimeError):
        with persistence.transaction("org_pg_a") as transaction:
            transaction.questions.add({**item, "id": "%s_rollback" % question_id})
            raise RuntimeError("force rollback")

    with persistence.transaction("org_pg_a") as transaction:
        assert transaction.questions.get("%s_rollback" % question_id) is None
        saved = transaction.questions.add(item)
    assert saved["version"] == 1

    with persistence.transaction("org_pg_a") as transaction:
        current = transaction.questions.get(question_id)
        current["title"] = "updated"
        updated = transaction.questions.update(current, expected_version=1)
    assert updated["version"] == 2
    with pytest.raises(ConcurrencyConflict):
        with persistence.transaction("org_pg_a") as transaction:
            transaction.questions.update({**saved, "title": "stale"}, expected_version=1)

    with persistence.transaction("org_pg_a") as transaction:
        matches = transaction.questions.search_catalog(
            job_position_id=position_id,
            knowledge_base_ids=[knowledge_base_id],
            skills=["postgresql"],
            difficulties=["mid"],
            question_types=["open_ended"],
        )
    assert [existing["id"] for existing in matches] == [question_id]

    with persistence.transaction("org_pg_b") as transaction:
        assert transaction.questions.get(question_id) is None
        assert all(existing["id"] != question_id for existing in transaction.questions.list())


@pytest.mark.skipif(not POSTGRES_DSN, reason="INTERVIEWER_TEST_POSTGRES_DSN is not configured")
def test_real_postgresql_enforces_outbox_and_appointment_uniqueness() -> None:
    persistence = PostgreSQLPersistence(PostgreSQLStore(POSTGRES_DSN))
    suffix = uuid4().hex
    work = new_work_item(
        organization_id="org_pg_constraints",
        kind="validation.work",
        aggregate_id="aggregate_%s" % suffix,
        idempotency_key="validation:%s" % suffix,
    )
    with persistence.transaction("org_pg_constraints") as transaction:
        first = transaction.outbox.enqueue(work)
        duplicate = transaction.outbox.enqueue({**work, "id": "work_duplicate_%s" % suffix})
    assert duplicate["id"] == first["id"]

    session = {
        "id": "interview_pg_%s" % suffix,
        "organization_id": "org_pg_constraints",
        "appointment_id": "appointment_pg_%s" % suffix,
        "status": "in_progress",
        "question_selections": [],
        "turns": [],
        "answers": [],
        "created_at": "2026-08-26T00:00:00Z",
        "updated_at": "2026-08-26T00:00:00Z",
    }
    with persistence.transaction("org_pg_constraints") as transaction:
        transaction.interview_sessions.add(session)
    with pytest.raises(RecordAlreadyExists):
        with persistence.transaction("org_pg_constraints") as transaction:
            transaction.interview_sessions.add(
                {**session, "id": "interview_pg_duplicate_%s" % suffix}
            )
