import asyncio

from fastapi.testclient import TestClient

from app.main import create_app
from app.model_gateway import capabilities as cap
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
