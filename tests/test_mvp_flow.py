from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.provider import reset_store_for_tests


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


def test_web_console_and_static_assets() -> None:
    api = client()

    console = api.get("/")
    assert console.status_code == 200
    assert "text/html" in console.headers["content-type"]
    assert "Interviewer" in console.text

    stylesheet = api.get("/web/styles.css")
    assert stylesheet.status_code == 200
    assert "text/css" in stylesheet.headers["content-type"]

    script = api.get("/web/app.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]

    icon_library = api.get("/web/vendor/lucide.min.js")
    assert icon_library.status_code == 200
    assert "javascript" in icon_library.headers["content-type"]

    avatar = api.get("/web/assets/digital-interviewer.png")
    assert avatar.status_code == 200
    assert avatar.headers["content-type"] == "image/png"


def test_provider_config_updates_require_current_version() -> None:
    api = client()
    created = api.post(
        "/api/v1/admin/model-provider-configs",
        json={
            "provider_id": "mock",
            "display_name": "Versioned mock",
            "credentials": {"api_key": "never-return-this"},
        },
    )
    assert created.status_code == 200, created.text
    config = created.json()
    assert config["version"] == 1
    assert "credentials" not in config

    updated = api.patch(
        "/api/v1/admin/model-provider-configs/%s" % config["id"],
        json={"expected_version": config["version"], "display_name": "Updated mock"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2

    stale = api.patch(
        "/api/v1/admin/model-provider-configs/%s" % config["id"],
        json={"expected_version": config["version"], "enabled": False},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "PERSISTENCE_CONFLICT"


def test_provider_manifest_and_route_invariants_are_enforced_on_write() -> None:
    api = client()

    missing_config = api.post(
        "/api/v1/admin/model-provider-configs",
        json={
            "provider_id": "openai_compatible",
            "display_name": "Invalid OpenAI",
            "config": {},
            "credentials": {"api_key": "test-key"},
        },
    )
    assert missing_config.status_code == 400
    assert missing_config.json()["error"]["code"] == "MODEL_PROVIDER_CONFIG_INVALID"
    assert missing_config.json()["error"]["details"]["fields"] == ["base_url"]

    missing_secret = api.post(
        "/api/v1/admin/model-provider-configs",
        json={
            "provider_id": "openai_compatible",
            "display_name": "Invalid credentials",
            "config": {"base_url": "https://models.example.com/v1"},
            "credentials": {},
        },
    )
    assert missing_secret.status_code == 400
    assert missing_secret.json()["error"]["details"]["fields"] == ["api_key"]

    configured = api.post(
        "/api/v1/admin/model-provider-configs",
        json={
            "provider_id": "openai_compatible",
            "display_name": "Configured OpenAI",
            "config": {"base_url": "https://models.example.com/v1"},
            "credentials": {"api_key": "test-key"},
        },
    )
    assert configured.status_code == 200, configured.text

    incompatible_route = api.post(
        "/api/v1/admin/model-routes",
        json={
            "capability": "avatar.speak",
            "purpose": "interview_question_delivery",
            "primary": {
                "provider_config_id": configured.json()["id"],
                "model": "chat-model",
                "timeout_s": 10,
            },
        },
    )
    assert incompatible_route.status_code == 409
    assert incompatible_route.json()["error"]["code"] == "MODEL_PROVIDER_CAPABILITY_MISSING"


def test_question_to_report_mvp_flow() -> None:
    api = client()

    q1 = api.post(
        "/api/v1/questions",
        json={
            "knowledge_base_id": "kb_backend",
            "title": "Python GIL",
            "question_text": "请解释 Python GIL 对 CPU 密集型多线程程序的影响。",
            "standard_answer": "GIL 会限制同一进程内多个线程同时执行 Python 字节码，CPU 密集任务可考虑多进程。",
            "key_points": [
                {"text": "GIL 限制同一进程内多个线程同时执行 Python 字节码", "weight": 0.6},
                {"text": "CPU 密集任务可考虑多进程", "weight": 0.4},
            ],
            "difficulty": "senior",
            "skills": ["python", "concurrency"],
        },
    )
    assert q1.status_code == 200, q1.text

    q2 = api.post(
        "/api/v1/questions",
        json={
            "knowledge_base_id": "kb_backend",
            "title": "Redis 缓存击穿",
            "question_text": "如何处理 Redis 缓存击穿？",
            "standard_answer": "可使用互斥锁、逻辑过期、热点 key 保护等方式。",
            "key_points": ["互斥锁", "逻辑过期", "热点 key 保护"],
            "difficulty": "mid",
            "skills": ["redis", "cache"],
        },
    )
    assert q2.status_code == 200, q2.text

    search = api.post(
        "/api/v1/questions/search",
        json={
            "query": "资深 Python 后端，需要熟悉并发和 Redis",
            "filters": {"skills": ["python", "redis"], "knowledge_base_ids": ["kb_backend"]},
            "limit": 2,
            "include_answer": True,
        },
    )
    assert search.status_code == 200, search.text
    assert len(search.json()["items"]) == 2

    role = api.post(
        "/api/v1/role-requirements",
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

    plan = api.post(
        "/api/v1/interview-plans/generate",
        json={
            "role_requirement_id": role_id,
            "knowledge_base_ids": ["kb_backend"],
            "question_count": 2,
            "strategy": {"allow_followups": True},
        },
    )
    assert plan.status_code == 200, plan.text
    plan_body = plan.json()
    assert len(plan_body["items"]) == 2

    plans = api.get("/api/v1/interview-plans")
    assert plans.status_code == 200
    assert [item["id"] for item in plans.json()["items"]] == [plan_body["id"]]

    approved = api.patch(
        f"/api/v1/interview-plans/{plan_body['id']}",
        json={"expected_version": plan_body["version"], "status": "approved"},
    )
    assert approved.status_code == 200, approved.text
    plan_body = approved.json()

    interview = api.post(
        "/api/v1/interviews",
        json={
            "plan_id": plan_body["id"],
            "candidate": {"name": "候选人 A", "email": "candidate@example.com"},
            "settings": {"allow_text_fallback": True},
        },
    )
    assert interview.status_code == 200, interview.text
    interview_id = interview.json()["id"]

    interviews = api.get("/api/v1/interviews")
    assert interviews.status_code == 200
    assert [item["id"] for item in interviews.json()["items"]] == [interview_id]
    assert len(interviews.json()["items"][0]["turns"]) == 2
    assert interviews.json()["items"][0]["plan_snapshot"]["source_plan_version"] == plan_body["version"]

    started = api.post(f"/api/v1/interviews/{interview_id}/start")
    assert started.status_code == 200, started.text
    current_turn_id = started.json()["current_turn_id"]
    assert current_turn_id
    active_turn = next(
        item
        for item in api.get(f"/api/v1/interviews/{interview_id}").json()["turns"]
        if item["id"] == current_turn_id
    )

    answer = api.post(
        f"/api/v1/interviews/{interview_id}/answers",
        json={
            "turn_id": current_turn_id,
            "final_transcript": active_turn["question_snapshot"]["standard_answer"],
            "duration_seconds": 20,
        },
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["evaluation"]["score"] >= 80

    complete = api.post(f"/api/v1/interviews/{interview_id}/complete")
    assert complete.status_code == 200, complete.text
    assert complete.json()["report"]["overall_score"] > 0

    report = api.get(f"/api/v1/interviews/{interview_id}/report")
    assert report.status_code == 200, report.text
    assert report.json()["recommendation"] in {"strong_advance", "advance", "hold", "reject"}
