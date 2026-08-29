import asyncio

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.model_gateway.errors import ProviderError
from app.repositories.provider import get_store, reset_store_for_tests
from app.workers.outbox import OutboxWorker


def _setup() -> tuple[TestClient, dict, dict]:
    reset_store_for_tests()
    api = TestClient(create_app())
    position = api.post(
        "/api/v1/job-positions",
        json={"code": "ai-authoring", "name": "后端工程师", "description": "分布式系统开发"},
    ).json()
    bank = api.post(
        f"/api/v1/job-positions/{position['id']}/knowledge-bases",
        json={
            "name": "后端核心题库",
            "positioning": "考察后端工程师分析和解决生产问题的能力",
            "tags": ["Python", "数据库"],
        },
    ).json()
    model = api.post(
        "/api/v1/admin/model-configurations",
        json={
            "provider_connection_id": "provider_conn_mock",
            "model_type": "llm",
            "provider_model_id": "mock-json",
            "display_name": "Mock 结构化生题模型",
        },
    ).json()
    return api, bank, model


def _drain(rounds: int = 1) -> None:
    worker = OutboxWorker(get_store())
    for _ in range(rounds):
        asyncio.run(worker.run_once())


class TruncatingGateway:
    def __init__(self) -> None:
        self.calls = 0
        self.requests = []
        self.routes = []

    async def invoke(self, capability, request, *, route=None):
        self.calls += 1
        self.requests.append(request)
        self.routes.append(route)
        raise ProviderError(
            "provider_output_truncated",
            "Provider output reached the configured token limit before completing JSON.",
            retryable=False,
            details={
                "finish_reason": "length",
                "requested_max_output_tokens": request.max_output_tokens,
                "input_tokens": 900,
                "output_tokens": request.max_output_tokens,
                "total_tokens": 900 + request.max_output_tokens,
                "reasoning_tokens": 3000,
                "content_length": 1370,
            },
        )


def test_question_generation_requires_review_before_idempotent_import() -> None:
    api, bank, model = _setup()
    options = api.get(
        f"/api/v1/knowledge-bases/{bank['id']}/question-generation-options"
    )
    assert options.status_code == 200
    assert options.json()["positioning"].startswith("考察后端")
    assert options.json()["tags"] == ["python", "数据库"]
    assert options.json()["items"][0]["id"] == model["id"]

    queued = api.post(
        f"/api/v1/knowledge-bases/{bank['id']}/question-generation-batches",
        headers={"Idempotency-Key": "generate-1"},
        json={
            "model_configuration_id": model["id"],
            "target_count": 3,
            "positioning": options.json()["positioning"],
            "tags": options.json()["tags"],
            "requirements": "每题关注一次真实故障定位",
        },
    )
    assert queued.status_code == 202, queued.text
    assert queued.json()["status"] == "queued"
    assert queued.json()["work"]["retry_base_seconds"] >= 5
    assert api.get(f"/api/v1/knowledge-bases/{bank['id']}/questions").json()["items"] == []

    duplicate = api.post(
        f"/api/v1/knowledge-bases/{bank['id']}/question-generation-batches",
        headers={"Idempotency-Key": "generate-1"},
        json={
            "model_configuration_id": model["id"],
            "target_count": 3,
            "positioning": "不会覆盖已有批次",
            "tags": ["other"],
        },
    )
    assert duplicate.json()["id"] == queued.json()["id"]

    _drain(4)
    batch = api.get(f"/api/v1/question-generation-batches/{queued.json()['id']}").json()
    assert batch["status"] == "reviewing"
    assert len(batch["drafts"]) == 3
    assert api.get(f"/api/v1/knowledge-bases/{bank['id']}/questions").json()["items"] == []

    first = batch["drafts"][0]
    patched = api.patch(
        f"/api/v1/question-generation-batches/{batch['id']}/drafts/{first['id']}",
        json={
            "expected_version": batch["version"],
            "title": "人工修改后的故障定位题",
            "skills": ["Python", "可观测性"],
            "rubric": {
                "semantic_weight": 0.45,
                "key_point_weight": 0.35,
                "communication_weight": 0.2,
            },
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["drafts"][0]["title"] == "人工修改后的故障定位题"
    assert patched.json()["drafts"][0]["skills"] == ["python", "可观测性"]

    removed = patched.json()["drafts"][1]
    deleted = api.delete(
        f"/api/v1/question-generation-batches/{batch['id']}/drafts/{removed['id']}",
        params={"expected_version": patched.json()["version"]},
    )
    assert deleted.status_code == 200, deleted.text
    assert len(deleted.json()["drafts"]) == 2

    imported = api.post(
        f"/api/v1/question-generation-batches/{batch['id']}/import",
        headers={"Idempotency-Key": "import-1"},
        json={"expected_version": deleted.json()["version"]},
    )
    assert imported.status_code == 202, imported.text
    assert imported.json()["status"] == "importing"
    assert api.get(f"/api/v1/knowledge-bases/{bank['id']}/questions").json()["items"] == []

    _drain(2)
    completed = api.get(f"/api/v1/question-generation-batches/{batch['id']}").json()
    questions = api.get(f"/api/v1/knowledge-bases/{bank['id']}/questions").json()["items"]
    assert completed["status"] == "imported"
    assert len(completed["imported_question_ids"]) == 2
    assert len(questions) == 2
    assert {item["generation_batch_id"] for item in questions} == {batch["id"]}
    assert "人工修改后的故障定位题" in {item["title"] for item in questions}

    replay = api.post(
        f"/api/v1/question-generation-batches/{batch['id']}/import",
        headers={"Idempotency-Key": "import-1"},
        json={"expected_version": deleted.json()["version"]},
    )
    assert replay.status_code == 202
    _drain()
    assert len(api.get(f"/api/v1/knowledge-bases/{bank['id']}/questions").json()["items"]) == 2


def test_single_draft_import_keeps_other_drafts_reviewable_and_is_idempotent() -> None:
    api, bank, model = _setup()
    queued = api.post(
        f"/api/v1/knowledge-bases/{bank['id']}/question-generation-batches",
        headers={"Idempotency-Key": "generate-single-import"},
        json={
            "model_configuration_id": model["id"],
            "target_count": 3,
            "positioning": "后端故障分析",
            "tags": ["python", "数据库"],
        },
    )
    assert queued.status_code == 202, queued.text
    _drain(4)
    batch = api.get(
        f"/api/v1/question-generation-batches/{queued.json()['id']}"
    ).json()
    draft = batch["drafts"][0]

    submitted = api.post(
        f"/api/v1/question-generation-batches/{batch['id']}/drafts/{draft['id']}/import",
        headers={"Idempotency-Key": "single-import-1"},
        json={"expected_version": batch["version"], "expected_draft_version": draft["version"]},
    )
    assert submitted.status_code == 202, submitted.text
    assert submitted.json()["status"] == "reviewing"
    assert submitted.json()["drafts"][0]["import_status"] == "importing"
    assert len(submitted.json()["drafts"]) == 3

    second_draft = batch["drafts"][1]
    consecutive = api.post(
        f"/api/v1/question-generation-batches/{batch['id']}/drafts/{second_draft['id']}/import",
        headers={"Idempotency-Key": "single-import-2"},
        json={"expected_version": batch["version"]},
    )
    assert consecutive.status_code == 202, consecutive.text
    assert consecutive.json()["version"] == submitted.json()["version"] + 1
    assert consecutive.json()["drafts"][1]["import_status"] == "importing"

    replay = api.post(
        f"/api/v1/question-generation-batches/{batch['id']}/drafts/{draft['id']}/import",
        headers={"Idempotency-Key": "single-import-1"},
        json={"expected_version": batch["version"], "expected_draft_version": draft["version"]},
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["drafts"][0]["import_status"] == "importing"

    _drain()
    completed = api.get(
        f"/api/v1/question-generation-batches/{batch['id']}"
    ).json()
    imported = next(item for item in completed["drafts"] if item["id"] == draft["id"])
    assert completed["status"] == "reviewing"
    assert imported["import_status"] == "imported"
    assert imported["imported_question_id"] in completed["imported_question_ids"]
    assert len(api.get(f"/api/v1/knowledge-bases/{bank['id']}/questions").json()["items"]) == 2

    frozen_edit = api.patch(
        f"/api/v1/question-generation-batches/{batch['id']}/drafts/{draft['id']}",
        json={"expected_version": completed["version"], "title": "不应被修改"},
    )
    assert frozen_edit.status_code == 409
    frozen_delete = api.delete(
        f"/api/v1/question-generation-batches/{batch['id']}/drafts/{draft['id']}",
        params={"expected_version": completed["version"]},
    )
    assert frozen_delete.status_code == 409

    editable = next(
        item for item in completed["drafts"] if item.get("import_status") == "pending"
    )
    edited = api.patch(
        f"/api/v1/question-generation-batches/{batch['id']}/drafts/{editable['id']}",
        json={"expected_version": completed["version"], "title": "并发修改后的候选题"},
    )
    assert edited.status_code == 200, edited.text
    stale_target = api.post(
        f"/api/v1/question-generation-batches/{batch['id']}/drafts/{editable['id']}/import",
        headers={"Idempotency-Key": "stale-draft-import"},
        json={
            "expected_version": completed["version"],
            "expected_draft_version": editable["version"],
        },
    )
    assert stale_target.status_code == 409
    assert stale_target.json()["error"]["code"] == "PERSISTENCE_CONFLICT"

    remaining = api.post(
        f"/api/v1/question-generation-batches/{batch['id']}/import",
        headers={"Idempotency-Key": "import-remaining"},
        json={"expected_version": edited.json()["version"]},
    )
    assert remaining.status_code == 202, remaining.text
    assert len(remaining.json()["import_work"]["payload"]["questions"]) == 1
    _drain()
    final = api.get(f"/api/v1/question-generation-batches/{batch['id']}").json()
    assert final["status"] == "imported"
    assert len(final["imported_question_ids"]) == 3


def test_question_generation_rejects_non_structured_or_unready_model() -> None:
    api, bank, _ = _setup()
    tts = api.post(
        "/api/v1/admin/model-configurations",
        json={
            "provider_connection_id": "provider_conn_mock",
            "model_type": "tts",
            "provider_model_id": "mock-tts",
            "display_name": "TTS only",
        },
    ).json()
    response = api.post(
        f"/api/v1/knowledge-bases/{bank['id']}/question-generation-batches",
        headers={"Idempotency-Key": "wrong-model"},
        json={
            "model_configuration_id": tts["id"],
            "target_count": 2,
            "positioning": "后端",
            "tags": ["Python"],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MODEL_CONFIGURATION_CAPABILITY_MISMATCH"


def test_ten_questions_are_planned_then_generated_in_bounded_worker_chunks() -> None:
    api, bank, model = _setup()
    queued = api.post(
        f"/api/v1/knowledge-bases/{bank['id']}/question-generation-batches",
        headers={"Idempotency-Key": "fanout-10"},
        json={
            "model_configuration_id": model["id"],
            "target_count": 10,
            "positioning": "后端生产系统故障分析",
            "tags": ["Python", "数据库", "可观测性"],
        },
    ).json()

    _drain()
    planned = api.get(f"/api/v1/question-generation-batches/{queued['id']}").json()
    assert planned["phase"] == "generating"
    assert len(planned["blueprints"]) == 10
    assert len(planned["generation_chunks"]) == 5
    assert all(len(chunk["slot_ids"]) <= 2 for chunk in planned["generation_chunks"])
    assert planned["generation_progress"] == {
        "phase": "generating",
        "planned_count": 10,
        "completed_chunks": 0,
        "total_chunks": 5,
        "completed_slots": 0,
        "target_count": 10,
        "accepted_count": 0,
        "rejected_count": 0,
        "refill_round": 0,
    }

    _drain(2)
    completed = api.get(f"/api/v1/question-generation-batches/{queued['id']}").json()
    assert completed["status"] == "reviewing"
    assert len(completed["drafts"]) == 10
    assert len({item["question_text"] for item in completed["drafts"]}) == 10
    assert completed["generation_progress"]["completed_chunks"] == 5
    assert completed["generation_progress"]["accepted_count"] == 10
    assert completed["refill_round"] == 0


def test_two_slot_truncation_is_split_into_single_slot_work_without_restarting_batch() -> None:
    api, bank, model = _setup()
    queued = api.post(
        f"/api/v1/knowledge-bases/{bank['id']}/question-generation-batches",
        headers={"Idempotency-Key": "adaptive-split"},
        json={
            "model_configuration_id": model["id"],
            "target_count": 2,
            "positioning": "结构化输出截断恢复",
            "tags": ["可靠性"],
        },
    ).json()
    _drain()
    store = get_store()
    initial = store.question_generation_batches[queued["id"]]
    original = initial["generation_chunks"][0]
    gateway = TruncatingGateway()
    worker = OutboxWorker(store)
    worker.question_generation.gateway = gateway

    result = asyncio.run(worker.run_item(original["work_item_id"]))

    assert result["status"] == "completed"
    assert result["result_status"] == "split_into_single_slot_chunks"
    assert gateway.calls == 1
    assert gateway.requests[0].max_output_tokens == 8000
    assert gateway.routes[0]["policy"]["retry_count"] == 0
    recovered = api.get(
        f"/api/v1/question-generation-batches/{queued['id']}"
    ).json()
    original_projection = next(
        chunk for chunk in recovered["generation_chunks"] if chunk["id"] == original["id"]
    )
    replacements = [
        chunk
        for chunk in recovered["generation_chunks"]
        if chunk["id"] != original["id"]
    ]
    assert original_projection["status"] == "superseded"
    assert original_projection["recovery"]["reason"] == "provider_output_truncated"
    assert original_projection["recovery"]["diagnostics"]["finish_reason"] == "length"
    assert len(replacements) == 2
    assert all(len(chunk["slot_ids"]) == 1 for chunk in replacements)
    assert recovered["generation_progress"]["total_chunks"] == 2
    assert recovered["status"] == "generating"

    _drain(2)
    completed = api.get(
        f"/api/v1/question-generation-batches/{queued['id']}"
    ).json()
    assert completed["status"] == "reviewing"
    assert len(completed["drafts"]) == 2
    assert completed["generation_progress"]["completed_slots"] == 2
    assert completed["prompt_versions"]["generation"] == "question_blueprint_generation.v2"


def test_single_slot_truncation_is_terminal_until_an_explicit_retry() -> None:
    api, bank, model = _setup()
    queued = api.post(
        f"/api/v1/knowledge-bases/{bank['id']}/question-generation-batches",
        headers={"Idempotency-Key": "single-slot-truncation"},
        json={
            "model_configuration_id": model["id"],
            "target_count": 1,
            "positioning": "单槽位截断失败边界",
            "tags": ["可靠性"],
        },
    ).json()
    _drain()
    store = get_store()
    batch = store.question_generation_batches[queued["id"]]
    chunk = batch["generation_chunks"][0]
    gateway = TruncatingGateway()
    worker = OutboxWorker(store)
    worker.question_generation.gateway = gateway

    with pytest.raises(ProviderError, match="token limit"):
        asyncio.run(worker.run_item(chunk["work_item_id"]))

    failed = api.get(f"/api/v1/question-generation-batches/{queued['id']}").json()
    failed_chunk = failed["generation_chunks"][0]
    failed_task = next(
        task for task in failed["tasks"] if task.get("chunk_id") == failed_chunk["id"]
    )
    assert gateway.calls == 1
    assert gateway.requests[0].max_output_tokens == 4000
    assert failed["status"] == "failed"
    assert failed_chunk["status"] == "failed"
    assert len(failed["generation_chunks"]) == 1
    assert failed_task["work_status"] == "dead_letter"
    assert failed_task["attempt_count"] == 1
    assert failed_task["error"]["code"] == "provider_output_truncated"
    assert failed_task["error"]["retryable"] is False
    assert "retry_failed" in failed["available_actions"]


def test_expired_generation_lease_resumes_a_generating_batch() -> None:
    api, bank, model = _setup()
    queued = api.post(
        f"/api/v1/knowledge-bases/{bank['id']}/question-generation-batches",
        headers={"Idempotency-Key": "crash-recovery"},
        json={
            "model_configuration_id": model["id"],
            "target_count": 2,
            "positioning": "崩溃恢复测试",
            "tags": ["可靠性"],
        },
    ).json()
    store = get_store()
    batch = store.question_generation_batches[queued["id"]]
    batch["status"] = "generating"
    batch["version"] += 1
    work = store.outbox_work_items[queued["generation_work_item_id"]]
    work["status"] = "running"
    work["attempt_count"] = 1
    work["lease_token"] = "abandoned-lease"
    work["lease_expires_at"] = "2020-01-01T00:00:00Z"

    asyncio.run(OutboxWorker(store).run_item(work["id"]))
    _drain(3)

    recovered = api.get(f"/api/v1/question-generation-batches/{batch['id']}").json()
    assert recovered["status"] == "reviewing"
    assert len(recovered["drafts"]) == 2
    assert recovered["work"]["status"] == "completed"


def test_generation_batch_can_stop_and_resume_without_reusing_cancelled_work() -> None:
    api, bank, model = _setup()
    queued = api.post(
        f"/api/v1/knowledge-bases/{bank['id']}/question-generation-batches",
        headers={"Idempotency-Key": "stop-resume"},
        json={
            "model_configuration_id": model["id"],
            "target_count": 2,
            "positioning": "任务停止恢复测试",
            "tags": ["可靠性"],
        },
    ).json()

    stopped = api.post(
        f"/api/v1/question-generation-batches/{queued['id']}/stop",
        headers={"Idempotency-Key": "stop-1"},
        json={"expected_version": queued["version"], "reason": "人工检查"},
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "stopped"
    assert stopped.json()["execution_revision"] == 2
    assert stopped.json()["tasks"][0]["status"] == "cancelled"
    assert stopped.json()["available_actions"] == ["resume"]

    resumed = api.post(
        f"/api/v1/question-generation-batches/{queued['id']}/resume",
        headers={"Idempotency-Key": "resume-1"},
        json={"expected_version": stopped.json()["version"], "reason": "继续生成"},
    )
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["status"] == "queued"
    assert resumed.json()["execution_revision"] == 3
    assert len(resumed.json()["control_history"]) == 2

    _drain(4)
    completed = api.get(
        f"/api/v1/question-generation-batches/{queued['id']}"
    ).json()
    assert completed["status"] == "reviewing"
    assert len(completed["drafts"]) == 2
    assert any(task["execution_revision"] == 3 for task in completed["tasks"])


def test_failed_generation_chunk_can_be_retried_without_redoing_completed_work() -> None:
    api, bank, model = _setup()
    queued = api.post(
        f"/api/v1/knowledge-bases/{bank['id']}/question-generation-batches",
        headers={"Idempotency-Key": "retry-chunk"},
        json={
            "model_configuration_id": model["id"],
            "target_count": 4,
            "positioning": "分片失败恢复测试",
            "tags": ["分布式系统"],
        },
    ).json()
    _drain()
    store = get_store()
    batch = store.question_generation_batches[queued["id"]]
    first, failed = batch["generation_chunks"]
    first_work = store.outbox_work_items[first["work_item_id"]]
    asyncio.run(OutboxWorker(store).run_item(first_work["id"]))
    batch = store.question_generation_batches[queued["id"]]
    first, failed = batch["generation_chunks"]
    failed_work = store.outbox_work_items[failed["work_item_id"]]
    failed["status"] = "failed"
    failed["last_error"] = "Provider request exceeded the invocation timeout."
    failed_work["status"] = "dead_letter"
    failed_work["last_error"] = failed["last_error"]
    failed_work["last_error_code"] = "provider_timeout"
    batch["status"] = "failed"
    batch["phase"] = "failed"
    batch["last_error"] = failed["last_error"]
    batch["version"] += 1

    snapshot = api.get(
        f"/api/v1/question-generation-batches/{queued['id']}"
    ).json()
    failed_task = next(task for task in snapshot["tasks"] if task["chunk_id"] == failed["id"])
    assert failed_task["retryable"] is True
    retried = api.post(
        f"/api/v1/question-generation-batches/{queued['id']}/chunks/{failed['id']}/retry",
        headers={"Idempotency-Key": "retry-only-failed"},
        json={"expected_version": snapshot["version"], "reason": "单分片超时"},
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["status"] == "generating"
    assert first["status"] == "completed"
    assert len(retried.json()["generation_chunks"]) == 3

    _drain(2)
    completed = api.get(
        f"/api/v1/question-generation-batches/{queued['id']}"
    ).json()
    assert completed["status"] == "reviewing"
    assert len(completed["drafts"]) == 4
    assert sum(task["type"] == "generate_chunk" for task in completed["tasks"]) == 3


def test_stop_request_supersedes_an_expired_inflight_generation_work() -> None:
    api, bank, model = _setup()
    queued = api.post(
        f"/api/v1/knowledge-bases/{bank['id']}/question-generation-batches",
        headers={"Idempotency-Key": "stop-running"},
        json={
            "model_configuration_id": model["id"],
            "target_count": 1,
            "positioning": "在途停止测试",
            "tags": ["任务控制"],
        },
    ).json()
    store = get_store()
    work = store.outbox_work_items[queued["generation_work_item_id"]]
    work["status"] = "running"
    work["attempt_count"] = 1
    work["lease_token"] = "inflight"
    work["lease_expires_at"] = "2099-01-01T00:00:00Z"

    stopping = api.post(
        f"/api/v1/question-generation-batches/{queued['id']}/stop",
        headers={"Idempotency-Key": "stop-running-1"},
        json={"expected_version": queued["version"], "reason": "立即停止"},
    ).json()
    assert stopping["status"] == "stopping"
    work = store.outbox_work_items[queued["generation_work_item_id"]]
    assert work["cancel_requested"]["reason"] == "立即停止"

    work["lease_expires_at"] = "2020-01-01T00:00:00Z"
    asyncio.run(OutboxWorker(store).run_item(work["id"]))
    stopped = api.get(
        f"/api/v1/question-generation-batches/{queued['id']}"
    ).json()
    assert stopped["status"] == "stopped"
    assert stopped["work"]["result_status"] == "superseded"
    assert stopped["drafts"] == []
