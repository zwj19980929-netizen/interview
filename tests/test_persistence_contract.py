import asyncio
from typing import Any, Dict, Tuple

import pytest

from app.model_gateway.errors import ProviderError
from app.persistence.errors import ConcurrencyConflict
from app.persistence.interface import Persistence, new_work_item
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.questions import QuestionService
from app.workers.outbox import OutboxWorker


@pytest.fixture(params=["memory", "sqlite"])
def persistence_bundle(request, tmp_path) -> Tuple[InMemoryStore, Persistence]:
    if request.param == "sqlite":
        store: InMemoryStore = SQLiteStore(str(tmp_path / "contract.sqlite3"))
    else:
        store = InMemoryStore()
    return store, persistence_for(store)


def question(question_id: str = "q_contract", organization_id: str = "org_a") -> Dict[str, Any]:
    return {
        "id": question_id,
        "organization_id": organization_id,
        "knowledge_base_id": "kb_contract",
        "title": "Persistence contract",
        "question_text": "What must a persistence seam guarantee?",
        "standard_answer": "Atomic writes, tenant isolation, and explicit concurrency.",
        "key_points": [],
        "difficulty": "mid",
        "type": "open_ended",
        "skills": ["architecture"],
        "role_families": [],
        "rubric": {},
        "status": "active",
        "index_status": "pending",
        "created_at": "2026-08-23T00:00:00Z",
        "updated_at": "2026-08-23T00:00:00Z",
    }


def work(question_id: str = "q_contract", organization_id: str = "org_a") -> Dict[str, Any]:
    return new_work_item(
        organization_id=organization_id,
        kind="question.index",
        aggregate_id=question_id,
        idempotency_key="question.index:%s:1" % question_id,
        payload={"question_id": question_id},
    )


def test_transaction_is_atomic_and_tenant_scoped(persistence_bundle) -> None:
    _, persistence = persistence_bundle

    with pytest.raises(RuntimeError):
        with persistence.transaction("org_a") as transaction:
            transaction.questions.add(question("q_rollback"))
            transaction.outbox.enqueue(work("q_rollback"))
            raise RuntimeError("roll back")

    with persistence.transaction("org_a") as transaction:
        assert transaction.questions.get("q_rollback") is None
        assert transaction.outbox.list() == []
        saved = transaction.questions.add(question())
        queued = transaction.outbox.enqueue(work())

    assert saved["version"] == 1
    assert queued["status"] == "pending"
    with persistence.transaction("org_b") as transaction:
        assert transaction.questions.get(saved["id"]) is None
        assert transaction.questions.list() == []
        assert transaction.outbox.list() == []


def test_version_conflicts_are_explicit(persistence_bundle) -> None:
    _, persistence = persistence_bundle
    with persistence.transaction("org_a") as transaction:
        stale = transaction.questions.add(question())

    with persistence.transaction("org_a") as transaction:
        current = transaction.questions.get(stale["id"])
        assert current is not None
        current["title"] = "first writer"
        updated = transaction.questions.update(current, expected_version=1)
    assert updated["version"] == 2

    stale["title"] = "stale writer"
    with pytest.raises(ConcurrencyConflict):
        with persistence.transaction("org_a") as transaction:
            transaction.questions.update(stale, expected_version=1)

    with persistence.transaction("org_a") as transaction:
        current = transaction.questions.get(stale["id"])
    assert current is not None
    assert current["title"] == "first writer"
    assert current["version"] == 2


def test_outbox_is_idempotent_and_commits_with_index_result(persistence_bundle) -> None:
    _, persistence = persistence_bundle
    queued = work()
    with persistence.transaction("org_a") as transaction:
        saved = transaction.questions.add(question())
        first = transaction.outbox.enqueue(queued)
        duplicate = transaction.outbox.enqueue(work())
    assert duplicate["id"] == first["id"]

    with persistence.transaction("org_a") as transaction:
        running = transaction.outbox.start(first["id"])
    assert running["status"] == "running"
    assert running["attempt_count"] == 1

    vector = {
        "id": "vec_contract",
        "organization_id": "org_a",
        "knowledge_base_id": "kb_contract",
        "question_id": saved["id"],
        "doc_type": "question_doc",
        "text": saved["question_text"],
        "vector": [1.0, 0.0],
        "metadata": {},
        "created_at": "2026-08-23T00:00:00Z",
        "updated_at": "2026-08-23T00:00:00Z",
    }
    with persistence.transaction("org_a") as transaction:
        current = transaction.questions.get(saved["id"])
        assert current is not None
        current["index_status"] = "indexed"
        updated = transaction.questions.update(current, expected_version=current["version"])
        transaction.vector_documents.replace_for_question(saved["id"], [vector])
        completed = transaction.outbox.complete(first["id"], lease_token=running["lease_token"])

    assert updated["version"] == 2
    assert completed["status"] == "completed"
    with persistence.transaction("org_a") as transaction:
        assert transaction.questions.get(saved["id"])["index_status"] == "indexed"
        assert transaction.vector_documents.list() == [vector]
        assert transaction.outbox.list("completed")[0]["id"] == first["id"]


def test_expired_outbox_lease_can_be_reclaimed(persistence_bundle) -> None:
    _, persistence = persistence_bundle
    abandoned = work("q_abandoned")
    abandoned["status"] = "running"
    abandoned["attempt_count"] = 1
    abandoned["lease_token"] = "lease_abandoned"
    abandoned["lease_expires_at"] = "2020-01-01T00:00:00Z"
    with persistence.transaction("org_a") as transaction:
        queued = transaction.outbox.enqueue(abandoned)
        reclaimed = transaction.outbox.start(queued["id"])
    assert reclaimed["status"] == "running"
    assert reclaimed["attempt_count"] == 2

    with persistence.transaction("org_a") as transaction:
        with pytest.raises(ConcurrencyConflict):
            transaction.outbox.start(queued["id"])


def test_stale_worker_cannot_complete_reclaimed_work(persistence_bundle) -> None:
    _, persistence = persistence_bundle
    with persistence.transaction("org_a") as transaction:
        queued = transaction.outbox.enqueue(work("q_lease_owner"))
        first_lease = transaction.outbox.start(queued["id"])
        transaction.outbox.fail(
            queued["id"],
            "first worker stopped",
            lease_token=first_lease["lease_token"],
        )
        second_lease = transaction.outbox.start(queued["id"])

    with pytest.raises(ConcurrencyConflict):
        with persistence.transaction("org_a") as transaction:
            transaction.outbox.complete(queued["id"], lease_token=first_lease["lease_token"])

    with persistence.transaction("org_a") as transaction:
        completed = transaction.outbox.complete(
            queued["id"],
            lease_token=second_lease["lease_token"],
        )
    assert completed["status"] == "completed"


class FailingEmbeddingGateway:
    async def invoke(self, capability, request, *, route=None):
        raise ProviderError("provider_timeout", "embedding timed out", retryable=True)


def test_question_index_failure_is_durable(persistence_bundle) -> None:
    store, persistence = persistence_bundle
    service = QuestionService(store, gateway=FailingEmbeddingGateway(), persistence=persistence)

    saved = asyncio.run(
        service.create_question(
            {
                "knowledge_base_id": "kb_contract",
                "title": "Failure recovery",
                "question_text": "What happens after a provider timeout?",
                "standard_answer": "The question remains saved and work remains retryable.",
                "key_points": ["question remains saved"],
                "difficulty": "mid",
                "skills": ["reliability"],
            },
            organization_id="org_a",
        )
    )

    assert saved["index_status"] == "failed"
    assert saved["version"] == 2
    with persistence.transaction("org_a") as transaction:
        persisted = transaction.questions.get(saved["id"])
        failed = transaction.outbox.list("failed")
    assert persisted is not None
    assert persisted["index_status"] == "failed"
    assert len(failed) == 1
    assert failed[0]["last_error"] == "embedding timed out"


def test_outbox_worker_recovers_failed_question_index(persistence_bundle) -> None:
    store, persistence = persistence_bundle
    service = QuestionService(store, gateway=FailingEmbeddingGateway(), persistence=persistence)
    saved = asyncio.run(
        service.create_question(
            {
                "knowledge_base_id": "kb_contract",
                "title": "Recoverable work",
                "question_text": "Can a failed index job be retried?",
                "standard_answer": "Yes, from durable outbox state.",
                "key_points": ["durable outbox state"],
                "difficulty": "mid",
                "skills": ["reliability"],
            },
            organization_id="org_a",
        )
    )
    assert saved["index_status"] == "failed"

    results = asyncio.run(OutboxWorker(store, persistence=persistence).run_once("org_a"))
    assert results[0]["status"] == "completed"
    with persistence.transaction("org_a") as transaction:
        recovered = transaction.questions.get(saved["id"])
        completed = transaction.outbox.list("completed")
    assert recovered["index_status"] == "indexed"
    assert recovered["version"] == 3
    assert len(completed) == 1
