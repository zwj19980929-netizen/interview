import asyncio

from fastapi.testclient import TestClient

from app.main import create_app
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.persistence.interface import new_work_item
from app.persistence.provider import persistence_for
from app.repositories.provider import get_store, reset_store_for_tests
from app.workers.outbox import OutboxWorker
from app.workers.knowledge_base_speech import execute_work_item


def _question() -> dict:
    return {
        "title": "事务隔离",
        "question_text": "请解释事务隔离级别",
        "standard_answer": "隔离级别用于控制并发事务可见性",
        "key_points": [{"text": "并发可见性", "weight": 1}],
        "skills": ["database"],
        "difficulty": "mid",
        "type": "open_ended",
        "rubric": {
            "semantic_weight": 0.45,
            "key_point_weight": 0.35,
            "communication_weight": 0.2,
        },
    }


def _drain_worker(rounds: int = 4) -> None:
    worker = OutboxWorker(get_store())
    for _ in range(rounds):
        asyncio.run(worker.run_once())


def test_switching_bank_tts_voice_rebuilds_every_question_with_revision_guard() -> None:
    reset_store_for_tests()
    api = TestClient(create_app())
    position = api.post("/api/v1/job-positions", json={"code": "backend", "name": "后端"}).json()
    knowledge_base = api.post(
        f"/api/v1/job-positions/{position['id']}/knowledge-bases",
        json={"name": "后端核心题库"},
    ).json()
    model = api.post(
        "/api/v1/admin/model-configurations",
        json={
            "provider_connection_id": "provider_conn_mock",
            "model_type": "tts",
            "provider_model_id": "mock-tts",
            "display_name": "Mock 中文语音",
            "settings": {
                "default_voice": "voice_a",
                "voice_map": {"voice_a": "provider-a", "voice_b": "provider-b"},
            },
        },
    ).json()
    voices = api.get(f"/api/v1/admin/model-configurations/{model['id']}/voices")
    assert voices.status_code == 200
    assert {item["voice_profile_id"] for item in voices.json()["items"]} == {"voice_a", "voice_b"}

    created = api.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/questions",
        json=_question(),
    )
    assert created.status_code == 202
    _drain_worker()

    current = api.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}").json()
    first = api.put(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/speech-profile",
        headers={"Idempotency-Key": "profile-a"},
        json={
            "expected_version": current["version"],
            "model_configuration_id": model["id"],
            "voice_profile_id": "voice_a",
            "language": "zh-CN",
            "audio_format": "audio/wav",
            "speaking_rate": 1,
        },
    )
    assert first.status_code == 202, first.text
    assert first.json()["total"] == 1
    _drain_worker()

    current = api.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}").json()
    second = api.put(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/speech-profile",
        headers={"Idempotency-Key": "profile-b"},
        json={
            "expected_version": current["version"],
            "model_configuration_id": model["id"],
            "voice_profile_id": "voice_b",
            "language": "zh-CN",
            "audio_format": "audio/wav",
            "speaking_rate": 1,
        },
    )
    assert second.status_code == 202, second.text
    assert second.json()["speech_profile_revision"] == first.json()["speech_profile_revision"] + 1
    _drain_worker()

    detail = api.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}").json()
    question = api.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}/questions").json()["items"][0]
    builds = api.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}/speech-builds").json()["items"]
    assert detail["speech_profile"]["voice_profile_id"] == "voice_b"
    assert detail["speech_build_status"] == "ready"
    assert question["speech_status"] == "ready"
    assert question["speech_profile_revision"] == detail["speech_profile"]["revision"]
    assert builds[0]["status"] == "ready"
    assert builds[0]["ready"] == 1

    blocked = api.delete(
        f"/api/v1/admin/model-configurations/{model['id']}?expected_version={model['version']}"
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "MODEL_CONFIGURATION_IN_USE"


def test_switching_profile_absorbs_progress_version_and_cancels_old_revision_work() -> None:
    reset_store_for_tests()
    store = get_store()
    api = TestClient(create_app())
    position = api.post(
        "/api/v1/job-positions", json={"code": "switch-running-tts", "name": "切换语音"}
    ).json()
    bank = api.post(
        f"/api/v1/job-positions/{position['id']}/knowledge-bases",
        json={"name": "运行中切换模型题库"},
    ).json()
    model = api.post(
        "/api/v1/admin/model-configurations",
        json={
            "provider_connection_id": "provider_conn_mock",
            "model_type": "tts",
            "provider_model_id": "mock-tts",
            "display_name": "可切换 TTS",
            "settings": {
                "default_voice": "voice_a",
                "voice_map": {"voice_a": "provider-a", "voice_b": "provider-b"},
            },
        },
    ).json()
    api.post(f"/api/v1/knowledge-bases/{bank['id']}/questions", json=_question())
    _drain_worker()

    before_first = api.get(f"/api/v1/knowledge-bases/{bank['id']}").json()
    first = api.put(
        f"/api/v1/knowledge-bases/{bank['id']}/speech-profile",
        headers={"Idempotency-Key": "switch-running-first"},
        json={
            "expected_version": before_first["version"],
            "expected_speech_profile_revision": before_first["speech_profile"]["revision"],
            "model_configuration_id": model["id"],
            "voice_profile_id": "voice_a",
            "language": "zh-CN",
            "audio_format": "audio/wav",
            "speaking_rate": 1,
        },
    )
    assert first.status_code == 202, first.text
    # Fan out the first revision and leave its child in-flight.
    asyncio.run(OutboxWorker(store).run_item(first.json()["job_id"]))
    stale_snapshot = api.get(f"/api/v1/knowledge-bases/{bank['id']}").json()
    with persistence_for(store).transaction("org_default") as transaction:
        old_child = next(
            item
            for item in transaction.outbox.list()
            if item.get("payload", {}).get("parent_build_id") == first.json()["job_id"]
        )
        transaction.outbox.start(old_child["id"])
        # Simulate another background progress write after the form read.
        current_bank = transaction.knowledge_bases.get(bank["id"])
        current_bank["updated_at"] = "2026-08-31T12:00:00Z"
        progressed_bank = transaction.knowledge_bases.update(
            current_bank, expected_version=current_bank["version"]
        )

    switched = api.put(
        f"/api/v1/knowledge-bases/{bank['id']}/speech-profile",
        headers={"Idempotency-Key": "switch-running-second"},
        json={
            "expected_version": stale_snapshot["version"],
            "expected_speech_profile_revision": stale_snapshot["speech_profile"]["revision"],
            "model_configuration_id": model["id"],
            "voice_profile_id": "voice_b",
            "language": "zh-CN",
            "audio_format": "audio/wav",
            "speaking_rate": 1,
        },
    )
    assert progressed_bank["version"] > stale_snapshot["version"]
    assert switched.status_code == 202, switched.text
    assert switched.json()["speech_profile_revision"] == stale_snapshot["speech_profile"]["revision"] + 1

    with persistence_for(store).transaction("org_default") as transaction:
        cancelled_child = transaction.outbox.get(old_child["id"])
        new_parent = transaction.outbox.get(switched.json()["job_id"])
        audit = next(
            item
            for item in reversed(transaction.audit_events.list())
            if item.get("action") == "knowledge_base.speech_profile_changed"
        )
    assert cancelled_child["status"] == "running"
    assert cancelled_child["cancel_requested"]["reason"].startswith(
        "Superseded by speech profile revision"
    )
    assert new_parent["status"] == "pending"
    assert audit["metadata"]["superseded_work_count"] == 1
    old_projection = api.get(
        f"/api/v1/knowledge-bases/{bank['id']}/speech-builds/{first.json()['job_id']}"
    ).json()
    assert old_projection["status"] == "superseded"
    assert old_projection["superseded"] == 1

    # A genuinely concurrent profile change still fails closed.
    conflict = api.put(
        f"/api/v1/knowledge-bases/{bank['id']}/speech-profile",
        headers={"Idempotency-Key": "switch-running-stale-semantic"},
        json={
            "expected_version": stale_snapshot["version"],
            "expected_speech_profile_revision": stale_snapshot["speech_profile"]["revision"],
            "model_configuration_id": model["id"],
            "voice_profile_id": "voice_a",
            "language": "zh-CN",
            "audio_format": "audio/wav",
            "speaking_rate": 1,
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "PERSISTENCE_CONFLICT"


def test_new_bank_inherits_ready_question_speech_route_as_its_default_profile() -> None:
    reset_store_for_tests()
    store = get_store()
    with persistence_for(store).transaction("org_default") as transaction:
        connection = transaction.provider_connections.add(
            {
                "id": "provider_conn_default_tts",
                "organization_id": "org_default",
                "provider_id": "zhipuai",
                "display_name": "Default TTS connection",
                "enabled": True,
                "connection_config": {},
                "created_at": "2026-08-28T00:00:00Z",
                "updated_at": "2026-08-28T00:00:00Z",
            }
        )
        model = transaction.model_configurations.add(
            {
                "id": "model_cfg_default_tts",
                "organization_id": "org_default",
                "provider_connection_id": connection["id"],
                "provider_id": "zhipuai",
                "model_type": "tts",
                "provider_model_id": "glm-tts",
                "display_name": "Organization default TTS",
                "supported_capabilities": [cap.TTS_SYNTHESIZE],
                "settings": {"default_voice": "tongtong"},
                "default_parameters": {},
                "enabled": True,
                "status": "ready",
                "created_at": "2026-08-28T00:00:00Z",
                "updated_at": "2026-08-28T00:00:00Z",
            }
        )
        route = transaction.model_routes.add(
            {
                "id": "route_default_question_speech",
                "organization_id": "org_default",
                "capability": cap.TTS_SYNTHESIZE,
                "purpose": "question_speech_generation",
                "primary": {
                    "model_configuration_id": model["id"],
                    "timeout_s": 30,
                },
                "fallbacks": [],
                "policy": {"retry_count": 1},
                "enabled": True,
                "created_at": "2026-08-28T00:00:00Z",
                "updated_at": "2026-08-28T00:00:00Z",
            }
        )

    api = TestClient(create_app())
    position = api.post(
        "/api/v1/job-positions", json={"code": "route-default", "name": "默认语音"}
    ).json()
    knowledge_base = api.post(
        f"/api/v1/job-positions/{position['id']}/knowledge-bases",
        json={"name": "继承默认语音的题库"},
    ).json()

    assert knowledge_base["speech_profile"] == {
        "model_configuration_id": model["id"],
        "model_configuration_version": model["version"],
        "voice_profile_id": "tongtong",
        "language": "zh-CN",
        "audio_format": "audio/wav",
        "speaking_rate": 1.0,
        "revision": 1,
        "source": "model_route_default",
        "model_route_id": route["id"],
        "configured_by": "model_route_default",
        "configured_at": knowledge_base["speech_profile"]["configured_at"],
    }
    created = api.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/questions", json=_question()
    ).json()
    assert created["speech_configuration_required"] is False
    assert created["job_id"]
    with persistence_for(store).transaction("org_default") as transaction:
        work = transaction.outbox.get(created["job_id"])
    assert work["payload"]["speech_profile"]["model_configuration_id"] == model["id"]
    assert work["payload"]["speech_profile"]["voice_profile_id"] == "tongtong"


def test_bank_list_is_summary_only_and_detail_loads_questions_separately() -> None:
    reset_store_for_tests()
    api = TestClient(create_app())
    position = api.post("/api/v1/job-positions", json={"code": "qa", "name": "测试"}).json()
    knowledge_base = api.post(
        f"/api/v1/job-positions/{position['id']}/knowledge-bases",
        json={"name": "测试题库"},
    ).json()
    api.post(f"/api/v1/knowledge-bases/{knowledge_base['id']}/questions", json=_question())

    listed = api.get("/api/v1/knowledge-bases")
    assert listed.status_code == 200
    item = listed.json()["items"][0]
    assert item["question_count"] == 1
    assert "questions" not in item
    assert api.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}/questions").json()["items"]


def test_speech_options_explain_untested_tts_and_question_delete_is_recoverable() -> None:
    reset_store_for_tests()
    api = TestClient(create_app())
    connection = api.post(
        "/api/v1/admin/model-provider-connections",
        json={
            "provider_id": "zhipuai",
            "display_name": "智谱测试连接",
            "connection_config": {"base_url": "https://open.bigmodel.cn/api/paas/v4"},
            "credentials": {"api_key": "test-key"},
        },
    ).json()
    model = api.post(
        "/api/v1/admin/model-configurations",
        json={
            "provider_connection_id": connection["id"],
            "model_type": "tts",
            "provider_model_id": "glm-tts",
            "display_name": "GLM-TTS",
            "settings": {"default_voice": "tongtong"},
        },
    ).json()
    position = api.post("/api/v1/job-positions", json={"code": "crud", "name": "CRUD"}).json()
    knowledge_base = api.post(
        f"/api/v1/job-positions/{position['id']}/knowledge-bases", json={"name": "CRUD 题库"}
    ).json()

    options = api.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}/speech-options").json()
    candidate = next(item for item in options["candidates"] if item["id"] == model["id"])
    assert candidate["selectable"] is False
    assert candidate["unavailable_reason"] == "untested"
    assert candidate["voices"][0]["voice_profile_id"] == "tongtong"
    assert all(item["id"] != model["id"] for item in options["items"])

    created = api.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/questions", json=_question()
    ).json()
    deleted = api.delete(
        f"/api/v1/questions/{created['id']}?expected_version={created['version']}"
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    _drain_worker()
    assert api.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}/questions").json()["items"] == []
    archived = api.get(f"/api/v1/questions/{created['id']}").json()
    assert archived["status"] == "archived"
    assert archived["speech_status"] == "superseded"


def test_celery_task_executes_only_the_referenced_durable_work_item() -> None:
    reset_store_for_tests()
    api = TestClient(create_app())
    position = api.post("/api/v1/job-positions", json={"code": "ops", "name": "运维"}).json()
    knowledge_base = api.post(
        f"/api/v1/job-positions/{position['id']}/knowledge-bases",
        json={"name": "运维题库"},
    ).json()
    queued = api.post(f"/api/v1/knowledge-bases/{knowledge_base['id']}/questions", json=_question()).json()

    result = execute_work_item.apply(args=("org_default", queued["job_id"])).get()

    assert result["status"] == "completed"
    question = api.get(f"/api/v1/questions/{queued['id']}").json()
    assert question["speech_status"] == "ready"
    assert question["speech_preview"] == {
        "available": False,
        "reason": "development_mock_asset",
        "message": "当前是开发模拟语音，没有实际音频。请先配置已测试通过的语音模型和声音。",
    }
    preview = api.post(
        f"/api/v1/question-speech-assets/{question['speech_asset_id']}/content-url"
    )
    assert preview.status_code == 409
    assert preview.json()["error"]["code"] == "QUESTION_SPEECH_PREVIEW_UNAVAILABLE"
    assert "开发模拟模型" in preview.json()["error"]["message"]
    assert preview.json()["error"]["details"]["action"] == "configure_knowledge_base_speech"


def test_failed_bank_speech_retry_uses_current_question_version_and_new_work_identity() -> None:
    reset_store_for_tests()
    store = get_store()
    api = TestClient(create_app())
    position = api.post(
        "/api/v1/job-positions", json={"code": "retry-speech", "name": "语音重试"}
    ).json()
    knowledge_base = api.post(
        f"/api/v1/job-positions/{position['id']}/knowledge-bases",
        json={"name": "语音失败重试题库"},
    ).json()
    model = api.post(
        "/api/v1/admin/model-configurations",
        json={
            "provider_connection_id": "provider_conn_mock",
            "model_type": "tts",
            "provider_model_id": "mock-tts",
            "display_name": "Retry Mock TTS",
            "settings": {"default_voice": "voice_default_cn"},
        },
    ).json()
    created = api.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/questions", json=_question()
    ).json()
    _drain_worker()

    current = api.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}").json()
    configured = api.put(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/speech-profile",
        headers={"Idempotency-Key": "retry-profile"},
        json={
            "expected_version": current["version"],
            "model_configuration_id": model["id"],
            "voice_profile_id": "voice_default_cn",
            "language": "zh-CN",
            "audio_format": "audio/wav",
            "speaking_rate": 1,
        },
    )
    assert configured.status_code == 202, configured.text
    _drain_worker()
    parent_id = configured.json()["job_id"]

    with persistence_for(store).transaction("org_default") as transaction:
        question = transaction.questions.get(created["id"])
        profile = transaction.knowledge_bases.get(knowledge_base["id"])["speech_profile"]
        failed_work = transaction.outbox.enqueue(
            new_work_item(
                organization_id="org_default",
                kind="question.speech.generate",
                aggregate_id=question["id"],
                idempotency_key="forced-failed-question-speech",
                payload={
                    "owner_type": "question",
                    "owner_id": question["id"],
                    "source_version": question["version"],
                    "knowledge_base_id": knowledge_base["id"],
                    "speech_profile": profile,
                    "speech_profile_revision": profile["revision"],
                    "parent_build_id": parent_id,
                },
            )
        )
        started = transaction.outbox.start(failed_work["id"])
        transaction.outbox.fail(
            failed_work["id"],
            "forced provider failure",
            lease_token=started["lease_token"],
            error_code="FORCED_TTS_FAILURE",
            retryable=False,
        )
        question["speech_status"] = "failed"
        question["speech_error"] = "forced provider failure"
        failed_question = transaction.questions.update(
            question, expected_version=question["version"]
        )

    failed_build = api.get(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/speech-builds/{parent_id}"
    ).json()
    assert failed_build["failed_items"] == [
        {
            "question_id": created["id"],
            "error_code": "FORCED_TTS_FAILURE",
            "retryable": False,
        }
    ]
    latest_bank = api.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}").json()
    retry = api.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/speech-builds/{parent_id}/retry-failed",
        headers={"Idempotency-Key": "retry-command-1"},
        json={"expected_version": latest_bank["version"]},
    )
    assert retry.status_code == 202, retry.text
    assert retry.json()["total"] == 1
    replayed_retry = api.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/speech-builds/{parent_id}/retry-failed",
        headers={"Idempotency-Key": "retry-command-1"},
        json={"expected_version": latest_bank["version"]},
    )
    assert replayed_retry.status_code == 202, replayed_retry.text
    assert replayed_retry.json()["job_id"] == retry.json()["job_id"]

    with persistence_for(store).transaction("org_default") as transaction:
        retry_parent = transaction.outbox.get(retry.json()["job_id"])
    assert retry_parent["payload"]["question_manifest"] == [
        {"question_id": created["id"], "question_version": failed_question["version"]}
    ]

    _drain_worker()
    regenerated = api.get(f"/api/v1/questions/{created['id']}").json()
    assert regenerated["speech_status"] == "ready"
    with persistence_for(store).transaction("org_default") as transaction:
        retry_children = [
            item
            for item in transaction.outbox.list()
            if item.get("payload", {}).get("parent_build_id") == retry.json()["job_id"]
        ]
    assert len(retry_children) == 1
    assert retry_children[0]["status"] == "completed"
    assert retry_children[0]["idempotency_key"].startswith("question.speech.retry:")


def test_retryable_tts_failure_keeps_source_version_and_build_running_until_retry_succeeds() -> None:
    reset_store_for_tests()
    store = get_store()
    api = TestClient(create_app())
    position = api.post(
        "/api/v1/job-positions", json={"code": "durable-tts-retry", "name": "语音自动重试"}
    ).json()
    knowledge_base = api.post(
        f"/api/v1/job-positions/{position['id']}/knowledge-bases",
        json={"name": "供应商无关语音重试题库"},
    ).json()
    model = api.post(
        "/api/v1/admin/model-configurations",
        json={
            "provider_connection_id": "provider_conn_mock",
            "model_type": "tts",
            "provider_model_id": "mock-tts",
            "display_name": "Provider-neutral TTS",
            "settings": {"default_voice": "voice_default_cn"},
        },
    ).json()
    created = api.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/questions", json=_question()
    ).json()
    _drain_worker()
    current = api.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}").json()
    configured = api.put(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/speech-profile",
        headers={"Idempotency-Key": "provider-neutral-retry-profile"},
        json={
            "expected_version": current["version"],
            "model_configuration_id": model["id"],
            "voice_profile_id": "voice_default_cn",
            "language": "zh-CN",
            "audio_format": "audio/wav",
            "speaking_rate": 1,
        },
    ).json()

    worker = OutboxWorker(store)
    original_invoke = worker.catalog.gateway.invoke
    calls = 0

    async def fail_once_then_invoke(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError("provider_transient", "temporary provider failure", retryable=True)
        return await original_invoke(*args, **kwargs)

    worker.catalog.gateway.invoke = fail_once_then_invoke
    asyncio.run(worker.run_once())  # Parent build enqueues the speech child.
    asyncio.run(worker.run_once())  # First child attempt fails and remains claimable.

    retrying_question = api.get(f"/api/v1/questions/{created['id']}").json()
    retrying_build = api.get(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/speech-builds/{configured['job_id']}"
    ).json()
    with persistence_for(store).transaction("org_default") as transaction:
        retrying_child = next(
            item
            for item in transaction.outbox.list()
            if item.get("payload", {}).get("parent_build_id") == configured["job_id"]
        )
    assert retrying_question["version"] == retrying_child["payload"]["source_version"]
    assert retrying_question["speech_status"] == "pending"
    assert retrying_build["status"] == "running"
    assert retrying_build["pending"] == 1
    assert retrying_build["failed"] == 0
    assert retrying_build["failed_items"] == []

    asyncio.run(worker.run_once())
    completed_question = api.get(f"/api/v1/questions/{created['id']}").json()
    completed_build = api.get(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/speech-builds/{configured['job_id']}"
    ).json()
    assert completed_question["speech_status"] == "ready"
    assert completed_build["status"] == "ready"
    assert completed_build["ready"] == 1
    with persistence_for(store).transaction("org_default") as transaction:
        children = [
            item
            for item in transaction.outbox.list()
            if item.get("payload", {}).get("parent_build_id") == configured["job_id"]
        ]
    assert len(children) == 1
    assert children[0]["attempt_count"] == 2
    assert children[0]["status"] == "completed"
    assert children[0].get("result_status") != "superseded"


def test_failed_build_retry_recovers_legacy_self_superseded_child() -> None:
    reset_store_for_tests()
    store = get_store()
    api = TestClient(create_app())
    position = api.post(
        "/api/v1/job-positions", json={"code": "legacy-speech-retry", "name": "历史语音恢复"}
    ).json()
    knowledge_base = api.post(
        f"/api/v1/job-positions/{position['id']}/knowledge-bases",
        json={"name": "历史失败语音题库"},
    ).json()
    created = api.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/questions", json=_question()
    ).json()

    with persistence_for(store).transaction("org_default") as transaction:
        question = transaction.questions.get(created["id"])
        bank = transaction.knowledge_bases.get(knowledge_base["id"])
        profile = bank["speech_profile"]
        parent = transaction.outbox.enqueue(
            new_work_item(
                organization_id="org_default",
                kind="knowledge_base.speech.rebuild",
                aggregate_id=bank["id"],
                idempotency_key="legacy-self-superseded-parent",
                payload={
                    "knowledge_base_id": bank["id"],
                    "speech_profile": profile,
                    "speech_profile_revision": profile["revision"],
                    "question_manifest": [
                        {"question_id": question["id"], "question_version": question["version"]}
                    ],
                },
            )
        )
        child = transaction.outbox.enqueue(
            new_work_item(
                organization_id="org_default",
                kind="question.speech.generate",
                aggregate_id=question["id"],
                idempotency_key="legacy-self-superseded-child",
                payload={
                    "owner_type": "question",
                    "owner_id": question["id"],
                    "source_version": question["version"],
                    "knowledge_base_id": bank["id"],
                    "speech_profile": profile,
                    "speech_profile_revision": profile["revision"],
                    "parent_build_id": parent["id"],
                },
            )
        )
        started_parent = transaction.outbox.start(parent["id"])
        transaction.outbox.complete(parent["id"], lease_token=started_parent["lease_token"])
        started_child = transaction.outbox.start(child["id"])
        transaction.outbox.complete(
            child["id"], lease_token=started_child["lease_token"], result_status="superseded"
        )
        question["speech_status"] = "failed"
        question["speech_error"] = "Provider rate limited the request."
        transaction.questions.update(question, expected_version=question["version"])
        bank["speech_build_status"] = "failed"
        bank = transaction.knowledge_bases.update(bank, expected_version=bank["version"])

    legacy_projection = api.get(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/speech-builds/{parent['id']}"
    ).json()
    assert legacy_projection["status"] == "superseded"
    assert legacy_projection["ready"] == 0
    assert legacy_projection["superseded"] == 1

    retried = api.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/speech-builds/{parent['id']}/retry-failed",
        headers={"Idempotency-Key": "recover-legacy-self-superseded"},
        json={"expected_version": bank["version"]},
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["total"] == 1
