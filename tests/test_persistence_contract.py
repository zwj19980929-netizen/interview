from typing import Any, Dict, Tuple

import pytest

from app.persistence.errors import ConcurrencyConflict
from app.persistence.interface import Persistence, new_work_item
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore


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


def test_question_generation_batches_share_the_versioned_persistence_contract(persistence_bundle) -> None:
    _, persistence = persistence_bundle
    item = {
        "id": "question_gen_contract",
        "organization_id": "org_a",
        "knowledge_base_id": "kb_contract",
        "status": "reviewing",
        "drafts": [],
        "created_at": "2026-08-27T00:00:00Z",
        "updated_at": "2026-08-27T00:00:00Z",
    }
    with persistence.transaction("org_a") as transaction:
        saved = transaction.question_generation_batches.add(item)
    with persistence.transaction("org_b") as transaction:
        assert transaction.question_generation_batches.get(saved["id"]) is None
    with persistence.transaction("org_a") as transaction:
        current = transaction.question_generation_batches.get(saved["id"])
        current["status"] = "importing"
        updated = transaction.question_generation_batches.update(
            current, expected_version=current["version"]
        )
    assert updated["version"] == 2
    assert updated["status"] == "importing"


def test_versioned_document_and_provider_secret_delete_are_atomic(persistence_bundle) -> None:
    _, persistence = persistence_bundle
    connection = {
        "id": "provider_delete_contract",
        "organization_id": "org_a",
        "provider_id": "mock",
        "display_name": "Delete contract",
        "created_at": "2026-08-26T00:00:00Z",
        "updated_at": "2026-08-26T00:00:00Z",
    }
    with persistence.transaction("org_a") as transaction:
        saved = transaction.provider_connections.add(connection)
        transaction.provider_secrets.replace(saved["id"], {"api_key": "contract-secret"})

    with pytest.raises(ConcurrencyConflict):
        with persistence.transaction("org_a") as transaction:
            transaction.provider_connections.delete(saved["id"], expected_version=2)
            transaction.provider_secrets.delete(saved["id"])

    with persistence.transaction("org_a") as transaction:
        assert transaction.provider_connections.get(saved["id"]) is not None
        assert transaction.provider_secrets.get(saved["id"]) == {"api_key": "contract-secret"}
        transaction.provider_connections.delete(saved["id"], expected_version=1)
        transaction.provider_secrets.delete(saved["id"])

    with persistence.transaction("org_a") as transaction:
        assert transaction.provider_connections.get(saved["id"]) is None
        assert transaction.provider_secrets.get(saved["id"]) == {}


def test_question_catalog_search_is_filtered_inside_each_persistence_adapter(persistence_bundle) -> None:
    _, persistence = persistence_bundle

    def catalog_question(question_id: str, **overrides) -> Dict[str, Any]:
        item = {
            **question(question_id),
            "job_position_id": "position_a",
            "knowledge_base_id": "kb_a",
            "validation_status": "valid",
            "speech_status": "ready",
            "skills": ["architecture"],
        }
        item.update(overrides)
        return item

    with persistence.transaction("org_a") as transaction:
        transaction.questions.add(catalog_question("q_match"))
        transaction.questions.add(catalog_question("q_inactive", status="archived"))
        transaction.questions.add(catalog_question("q_invalid", validation_status="invalid"))
        transaction.questions.add(catalog_question("q_speech_pending", speech_status="pending"))
        transaction.questions.add(catalog_question("q_reused_position", job_position_id="position_b"))
        transaction.questions.add(catalog_question("q_other_kb", knowledge_base_id="kb_b"))
        transaction.questions.add(catalog_question("q_other_skill", skills=["database"]))
    with persistence.transaction("org_b") as transaction:
        transaction.questions.add(catalog_question("q_other_tenant", organization_id="org_b"))

    with persistence.transaction("org_a") as transaction:
        matches = transaction.questions.search_catalog(
            job_position_id="position_a",
            knowledge_base_ids=["kb_a"],
            skills=["architecture"],
            difficulties=["mid"],
            question_types=["open_ended"],
        )

    assert [item["id"] for item in matches] == ["q_match", "q_reused_position"]


def test_outbox_is_idempotent_and_commits_with_aggregate_result(persistence_bundle) -> None:
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

    with persistence.transaction("org_a") as transaction:
        current = transaction.questions.get(saved["id"])
        assert current is not None
        current["speech_status"] = "ready"
        updated = transaction.questions.update(current, expected_version=current["version"])
        completed = transaction.outbox.complete(first["id"], lease_token=running["lease_token"])

    assert updated["version"] == 2
    assert completed["status"] == "completed"
    with persistence.transaction("org_a") as transaction:
        assert transaction.questions.get(saved["id"])["speech_status"] == "ready"
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


def test_outbox_dead_letter_and_manual_replay(persistence_bundle) -> None:
    _, persistence = persistence_bundle
    work = new_work_item(
        organization_id="org_a",
        kind="contract.dead-letter",
        aggregate_id="aggregate_1",
        idempotency_key="contract.dead-letter:1",
    )
    work["max_attempts"] = 1
    with persistence.transaction("org_a") as transaction:
        saved = transaction.outbox.enqueue(work)
        running = transaction.outbox.start(saved["id"])
        failed = transaction.outbox.fail(
            saved["id"], "terminal failure", lease_token=running["lease_token"]
        )
    assert failed["status"] == "dead_letter"
    assert failed["dead_lettered_at"]
    with persistence.transaction("org_a") as transaction:
        replayed = transaction.outbox.replay(failed["id"], reason="operator fixed input", actor_id="admin_1")
    assert replayed["status"] == "pending"
    assert replayed["attempt_count"] == 0
    assert replayed["replay_count"] == 1
    assert replayed["last_replay"]["actor_id"] == "admin_1"


def test_non_retryable_outbox_failure_is_terminal_on_first_attempt(persistence_bundle) -> None:
    _, persistence = persistence_bundle
    item = new_work_item(
        organization_id="org_a",
        kind="contract.non-retryable",
        aggregate_id="aggregate_non_retryable",
        idempotency_key="contract.non-retryable:1",
    )
    item["max_attempts"] = 5
    with persistence.transaction("org_a") as transaction:
        saved = transaction.outbox.enqueue(item)
        running = transaction.outbox.start(saved["id"])
        failed = transaction.outbox.fail(
            saved["id"],
            "request cannot succeed unchanged",
            lease_token=running["lease_token"],
            error_code="provider_output_truncated",
            retryable=False,
        )

    assert failed["attempt_count"] == 1
    assert failed["status"] == "dead_letter"
    assert failed["error_retryable"] is False
    assert failed["dead_lettered_at"]
    with persistence.transaction("org_a") as transaction:
        assert saved["id"] not in {
            candidate["id"] for candidate in transaction.outbox.claimable()
        }
        replayed = transaction.outbox.replay(
            saved["id"], reason="operator changed the request", actor_id="admin_1"
        )
    assert replayed["status"] == "pending"
