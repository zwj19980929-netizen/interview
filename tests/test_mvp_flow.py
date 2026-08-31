import asyncio

from fastapi.testclient import TestClient
from datetime import datetime, timedelta, timezone

from app.main import create_app
from app.repositories.provider import get_store, reset_store_for_tests
from app.workers.outbox import OutboxWorker


def client() -> TestClient:
    reset_store_for_tests()
    return TestClient(create_app())


def test_healthz_and_model_catalog() -> None:
    api = client()

    health = api.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    catalog = api.get("/api/v1/admin/model-providers/catalog")
    assert catalog.status_code == 200
    provider_ids = {item["provider_id"] for item in catalog.json()["items"]}
    assert "mock" in provider_ids
    assert "openai_compatible" in provider_ids
    assert "deepseek" in provider_ids
    assert "zhipuai" in provider_ids
    assert "dashscope" in provider_ids


def test_web_console_and_static_assets() -> None:
    api = client()

    console = api.get("/")
    assert console.status_code == 200
    assert "text/html" in console.headers["content-type"]
    assert "Interviewer" in console.text

    stylesheet = api.get("/web/styles.css")
    assert stylesheet.status_code == 200
    assert "text/css" in stylesheet.headers["content-type"]

    assert "/web/bundles/" in console.text
    web_console = api.get("/web/")
    assert web_console.status_code == 200
    assert "Interviewer" in web_console.text
    assert api.get("/web/app.js").status_code == 404

    icon_library = api.get("/web/vendor/lucide.min.js")
    assert icon_library.status_code == 200
    assert "javascript" in icon_library.headers["content-type"]

    avatar = api.get("/web/assets/digital-interviewer.png")
    assert avatar.status_code == 200
    assert avatar.headers["content-type"] == "image/png"


def test_provider_connection_updates_require_current_version() -> None:
    api = client()
    created = api.post(
        "/api/v1/admin/model-provider-connections",
        json={
            "provider_id": "mock",
            "display_name": "Versioned mock",
            "credentials": {},
        },
    )
    assert created.status_code == 200, created.text
    connection = created.json()
    assert connection["version"] == 1
    assert "credentials" not in connection

    updated = api.patch(
        "/api/v1/admin/model-provider-connections/%s" % connection["id"],
        json={"expected_version": connection["version"], "display_name": "Updated mock"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2

    stale = api.patch(
        "/api/v1/admin/model-provider-connections/%s" % connection["id"],
        json={"expected_version": connection["version"], "enabled": False},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "PERSISTENCE_CONFLICT"


def test_provider_manifest_and_route_invariants_are_enforced_on_write() -> None:
    api = client()

    unknown_field = api.post(
        "/api/v1/admin/model-provider-connections",
        json={"provider_id": "mock", "display_name": "Invalid", "connection_config": {"base_url": "nope"}},
    )
    assert unknown_field.status_code == 400
    assert unknown_field.json()["error"]["code"] == "MODEL_CONFIGURATION_INVALID"

    configured = api.post(
        "/api/v1/admin/model-provider-connections",
        json={"provider_id": "mock", "display_name": "Configured mock", "credentials": {}},
    )
    assert configured.status_code == 200, configured.text
    model = api.post(
        "/api/v1/admin/model-configurations",
        json={
            "provider_connection_id": configured.json()["id"],
            "model_type": "llm",
            "provider_model_id": "mock-json",
            "display_name": "Scoring model",
        },
    )
    assert model.status_code == 200, model.text

    incompatible_route = api.post(
        "/api/v1/admin/model-routes",
        json={
            "capability": "avatar.speak",
            "purpose": "interview_question_delivery",
            "primary": {"model_configuration_id": model.json()["id"], "timeout_s": 10},
        },
    )
    assert incompatible_route.status_code == 409
    assert incompatible_route.json()["error"]["code"] == "MODEL_CAPABILITY_MISMATCH"

    invalid_policy = api.post(
        "/api/v1/admin/model-routes",
        json={
            "capability": "llm.chat_json",
            "purpose": "answer_evaluation",
            "primary": {"model_configuration_id": model.json()["id"], "timeout_s": 10},
            "policy": {"max_retries": 1},
        },
    )
    assert invalid_policy.status_code == 422

    route_payload = {
        "capability": "llm.chat_json",
        "purpose": "answer_evaluation",
        "primary": {"model_configuration_id": model.json()["id"], "timeout_s": 10},
        "policy": {"retry_count": 1},
    }
    created_route = api.post("/api/v1/admin/model-routes", json=route_payload)
    assert created_route.status_code == 200, created_route.text
    duplicate_route = api.post("/api/v1/admin/model-routes", json=route_payload)
    assert duplicate_route.status_code == 409
    assert duplicate_route.json()["error"]["code"] == "MODEL_ROUTE_CONFLICT"


def test_provider_defaults_and_predefined_model_catalog_are_enforced() -> None:
    api = client()
    configured = api.post(
        "/api/v1/admin/model-provider-connections",
        json={
            "provider_id": "deepseek",
            "display_name": "DeepSeek production",
            "connection_config": {},
            "credentials": {"api_key": "test-key"},
        },
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["connection_config"]["base_url"] == "https://api.deepseek.com"
    catalog = api.get(
        "/api/v1/admin/model-provider-connections/%s/model-catalog" % configured.json()["id"]
    )
    assert catalog.status_code == 200
    assert {item["model_id"] for item in catalog.json()["models"]} >= {"deepseek-chat"}

    invalid_model = api.post(
        "/api/v1/admin/model-configurations",
        json={
            "provider_connection_id": configured.json()["id"],
            "model_type": "llm",
            "provider_model_id": "not-a-deepseek-model",
            "display_name": "Invalid model",
        },
    )
    assert invalid_model.status_code == 409
    assert invalid_model.json()["error"]["code"] == "MODEL_PROVIDER_MODEL_UNAVAILABLE"

    valid_model = api.post(
        "/api/v1/admin/model-configurations",
        json={
            "provider_connection_id": configured.json()["id"],
            "model_type": "llm",
            "provider_model_id": "deepseek-chat",
            "display_name": "DeepSeek Chat",
        },
    )
    assert valid_model.status_code == 200, valid_model.text
    assert valid_model.json()["status"] == "untested"


def test_model_configuration_enforces_type_specific_predefined_models() -> None:
    api = client()
    configured = api.post(
        "/api/v1/admin/model-provider-connections",
        json={
            "provider_id": "zhipuai",
            "display_name": "Zhipu multi-capability",
            "connection_config": {},
            "credentials": {"api_key": "test-key"},
        },
    )
    assert configured.status_code == 200, configured.text

    wrong_model = api.post(
        "/api/v1/admin/model-configurations",
        json={
            "provider_connection_id": configured.json()["id"],
            "model_type": "tts",
            "provider_model_id": "glm-5.2",
            "display_name": "Wrong TTS",
        },
    )
    assert wrong_model.status_code == 409
    assert wrong_model.json()["error"]["code"] == "MODEL_PROVIDER_MODEL_UNAVAILABLE"

    tts = api.post(
        "/api/v1/admin/model-configurations",
        json={
            "provider_connection_id": configured.json()["id"],
            "model_type": "tts",
            "provider_model_id": "glm-tts",
            "display_name": "GLM TTS",
        },
    )
    assert tts.status_code == 200, tts.text
    assert tts.json()["supported_capabilities"] == ["tts.synthesize"]


def test_mock_model_configuration_can_be_tested() -> None:
    api = client()
    configured = api.post(
        "/api/v1/admin/model-provider-connections",
        json={"provider_id": "mock", "display_name": "Mock connection test"},
    )
    assert configured.status_code == 200, configured.text
    model = api.post(
        "/api/v1/admin/model-configurations",
        json={
            "provider_connection_id": configured.json()["id"],
            "model_type": "llm",
            "provider_model_id": "mock-json",
            "display_name": "Mock JSON",
        },
    )
    assert model.status_code == 200, model.text

    tested = api.post(
        "/api/v1/admin/model-configurations/%s/test" % model.json()["id"], json={}
    )
    assert tested.status_code == 200, tested.text
    assert tested.json()["provider"]["provider_id"] == "mock"


def test_question_to_report_mvp_flow() -> None:
    api = client()
    position = api.post(
        "/api/v1/job-positions",
        json={"code": "backend-senior", "name": "资深 Python 后端工程师"},
    ).json()
    knowledge_base = api.post(
        "/api/v1/job-positions/%s/knowledge-bases" % position["id"],
        json={"name": "后端题库"},
    ).json()

    q1 = api.post(
        "/api/v1/knowledge-bases/%s/questions" % knowledge_base["id"],
        json={
            "knowledge_base_id": knowledge_base["id"],
            "title": "Python GIL",
            "question_text": "请解释 Python GIL 对 CPU 密集型多线程程序的影响。",
            "standard_answer": "GIL 会限制同一进程内多个线程同时执行 Python 字节码，CPU 密集任务可考虑多进程。",
            "key_points": [
                {"text": "GIL 限制同一进程内多个线程同时执行 Python 字节码", "weight": 0.6},
                {"text": "CPU 密集任务可考虑多进程", "weight": 0.4},
            ],
            "difficulty": "senior",
            "skills": ["python", "concurrency"],
            "rubric": {"semantic_correctness": 1.0},
        },
    )
    assert q1.status_code == 202, q1.text

    q2 = api.post(
        "/api/v1/knowledge-bases/%s/questions" % knowledge_base["id"],
        json={
            "knowledge_base_id": knowledge_base["id"],
            "title": "Redis 缓存击穿",
            "question_text": "如何处理 Redis 缓存击穿？",
            "standard_answer": "可使用互斥锁、逻辑过期、热点 key 保护等方式。",
            "key_points": ["互斥锁", "逻辑过期", "热点 key 保护"],
            "difficulty": "mid",
            "skills": ["redis", "cache"],
            "rubric": {"semantic_correctness": 1.0},
        },
    )
    assert q2.status_code == 202, q2.text
    asyncio.run(OutboxWorker(get_store()).run_once())

    search = api.post(
        "/api/v1/questions/search",
        json={
            "job_position_id": position["id"],
            "knowledge_base_ids": [knowledge_base["id"]],
            "query": "资深 Python 后端，需要熟悉并发和 Redis",
            "filters": {"skills": ["python", "redis"]},
            "limit": 2,
            "include_answer": True,
        },
    )
    assert search.status_code == 200, search.text

    role = api.post(
        "/api/v1/job-positions/%s/role-requirements" % position["id"],
        json={
            "title": "资深 Python 后端工程师",
            "description": "负责高并发 Python 服务、Redis 缓存和线上排障。",
            "must_have_skills": ["python", "redis"],
            "nice_to_have_skills": ["debugging"],
            "seniority": "senior",
            "interview_duration_minutes": 30,
        },
    )
    assert role.status_code == 200, role.text
    role_id = role.json()["id"]

    roles = api.get("/api/v1/role-requirements")
    assert roles.status_code == 200
    assert [item["id"] for item in roles.json()["items"]] == [role_id]
    candidate = api.post(
        "/api/v1/candidate-profiles",
        json={"name": "候选人 A", "email": "candidate@example.com", "phone": "13800138004"},
    ).json()

    plan = api.post(
        "/api/v1/interview-plans/generate",
        json={
            "role_requirement_id": role_id,
            "job_position_id": position["id"],
            "candidate_profile_id": candidate["id"],
            "knowledge_base_ids": [knowledge_base["id"]],
            "question_count": 2,
            "strategy": {"allow_followups": True},
        },
    )
    assert plan.status_code == 200, plan.text
    plan_body = plan.json()
    assert len(plan_body["bank_slots"]) == 2
    assert "items" not in plan_body

    plans = api.get("/api/v1/interview-plans")
    assert plans.status_code == 200
    assert [item["id"] for item in plans.json()["items"]] == [plan_body["id"]]

    approved = api.patch(
        f"/api/v1/interview-plans/{plan_body['id']}",
        json={"expected_version": plan_body["version"], "status": "approved"},
    )
    assert approved.status_code == 200, approved.text
    plan_body = approved.json()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    appointment = api.post(
        "/api/v1/interview-appointments",
        json={
            "plan_id": plan_body["id"],
            "candidate_profile_id": candidate["id"],
            "job_position_id": position["id"],
            "scheduled_start_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "scheduled_end_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "settings": {"record_audio": True},
        },
    )
    assert appointment.status_code == 200, appointment.text
    invitation = api.post(
        f"/api/v1/interview-appointments/{appointment.json()['id']}/invite",
        json={"expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")},
    )
    assert invitation.status_code == 200, invitation.text
    token = invitation.json()["invitation_token"]
    notice = api.get(f"/api/v1/public/interview-invitations/{token}").json()["consent"]
    intake = api.post(
        f"/api/v1/public/interview-invitations/{token}/intake",
        json={
            "name": "候选人 A",
            "email": "candidate@example.com",
            "phone": "13800138004",
            "consent": {"accepted": True, "version": notice["version"], "recording_accepted": True},
        },
    )
    assert intake.status_code == 200, intake.text
    readiness = api.post(
        f"/api/v1/public/interview-invitations/{token}/readiness",
        json={"browser_supported": True, "microphone_granted": True, "audio_content_type": "audio/webm"},
    )
    assert readiness.status_code == 200, readiness.text
    started = api.post(f"/api/v1/public/interview-invitations/{token}/start")
    assert started.status_code == 200, started.text
    interview_id = started.json()["interview_id"]

    interviews = api.get("/api/v1/interviews")
    assert interviews.status_code == 200
    assert [item["id"] for item in interviews.json()["items"]] == [interview_id]
    assert len(interviews.json()["items"][0]["turns"]) == 2
    assert interviews.json()["items"][0]["plan_snapshot"]["source_plan_version"] == plan_body["version"]

    current_turn_id = started.json()["current_turn_id"]
    assert current_turn_id
    active_turn = next(
        item
        for item in api.get(f"/api/v1/interviews/{interview_id}").json()["turns"]
        if item["id"] == current_turn_id
    )

    answer = api.post(
        f"/api/v1/interviews/{interview_id}/audio-answers",
        json={
            "turn_id": current_turn_id,
            "audio_uri": "private-test://mvp.webm",
            "content_type": "audio/webm",
            "development_transcript": active_turn["question_snapshot"]["standard_answer"],
            "duration_seconds": 20,
        },
    )
    assert answer.status_code == 200, answer.text
    answer_body = answer.json()
    assert answer_body["evaluation"]["status"] == "pending"
    assert answer_body["evaluation_work_id"]
    worker_results = asyncio.run(OutboxWorker(get_store()).run_once())
    assert any(
        item["kind"] == "answer.evaluate" and item["status"] == "completed"
        for item in worker_results
    )
    evaluations = api.get(
        f"/api/v1/interviews/{interview_id}/answers/{answer_body['answer']['id']}/evaluations"
    )
    assert evaluations.status_code == 200, evaluations.text
    assert evaluations.json()["items"][0]["score"] >= 80

    complete = api.post(f"/api/v1/interviews/{interview_id}/complete")
    assert complete.status_code == 200, complete.text
    assert complete.json()["report"]["overall_score"] > 0

    report = api.get(f"/api/v1/interviews/{interview_id}/report")
    assert report.status_code == 200, report.text
    assert report.json()["job_fit_level"] in {
        "strong_match",
        "match",
        "partial_match",
        "insufficient_evidence",
        "manual_review",
    }
    assert report.json()["human_decision_required"] is True
