import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.persistence.provider import persistence_for
from app.repositories.provider import get_store, reset_store_for_tests
from app.services.interviews import InterviewService
from app.workers.outbox import OutboxWorker


def api_client() -> TestClient:
    reset_store_for_tests()
    return TestClient(create_app())


def create_plan(api: TestClient) -> tuple:
    question = api.post(
        "/api/v1/questions",
        json={
            "knowledge_base_id": "kb_aggregate",
            "title": "事务边界",
            "question_text": "为什么外部模型调用不能放在数据库事务里？",
            "standard_answer": "长事务会扩大锁竞争和失败面，应使用 Outbox 分阶段提交。",
            "key_points": ["避免长事务", "Outbox 分阶段提交"],
            "difficulty": "senior",
            "skills": ["architecture"],
        },
    )
    assert question.status_code == 200, question.text
    role = api.post(
        "/api/v1/role-requirements",
        json={
            "title": "架构师",
            "description": "负责 architecture 与可靠性设计。",
            "must_have_skills": ["architecture"],
            "seniority": "senior",
            "interview_duration_minutes": 20,
        },
    )
    assert role.status_code == 200, role.text
    plan = api.post(
        "/api/v1/interview-plans/generate",
        json={
            "role_requirement_id": role.json()["id"],
            "knowledge_base_ids": ["kb_aggregate"],
            "question_count": 1,
        },
    )
    assert plan.status_code == 200, plan.text
    return question.json(), role.json(), plan.json()


def test_approved_plan_snapshot_and_optimistic_concurrency() -> None:
    api = api_client()
    question, _, plan = create_plan(api)

    rejected = api.post(
        "/api/v1/interviews",
        json={"plan_id": plan["id"], "candidate": {"name": "候选人"}},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "INTERVIEW_PLAN_NOT_APPROVED"

    approved = api.patch(
        "/api/v1/interview-plans/%s" % plan["id"],
        json={"expected_version": plan["version"], "status": "approved"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["version"] == plan["version"] + 1

    stale = api.patch(
        "/api/v1/interview-plans/%s" % plan["id"],
        json={"expected_version": plan["version"], "status": "archived"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "PERSISTENCE_CONFLICT"

    interview = api.post(
        "/api/v1/interviews",
        json={"plan_id": plan["id"], "candidate": {"name": "候选人", "metadata": {"source": "test"}}},
    )
    assert interview.status_code == 200, interview.text
    session = interview.json()
    assert session["candidate"]["interview_id"] == session["id"]
    assert session["candidate"]["metadata"] == {"source": "test"}
    assert session["plan_snapshot"]["source_plan_version"] == approved.json()["version"]
    assert session["turns"][0]["question_snapshot"]["source_question_version"] == question["version"]

    persistence = persistence_for(get_store())
    with persistence.transaction("org_default") as transaction:
        current = transaction.questions.get(question["id"])
        current["question_text"] = "后来修改过的题干"
        transaction.questions.update(current, expected_version=current["version"])

    frozen = api.get("/api/v1/interviews/%s" % session["id"]).json()
    assert frozen["turns"][0]["question_snapshot"]["question_text"] == question["question_text"]
    assert frozen["turns"][0]["question_spoken_text"] == question["question_text"]
    assert frozen["plan_snapshot"]["assembly_policy"] == approved.json()["assembly_policy"]
    assert frozen["plan_snapshot"]["assembly_summary"] == approved.json()["assembly_summary"]
    assert get_store().candidates == {}
    assert get_store().turns == {}


def test_evaluation_and_report_revisions_are_append_only() -> None:
    api = api_client()
    _, _, plan = create_plan(api)
    approved = api.patch(
        "/api/v1/interview-plans/%s" % plan["id"],
        json={"expected_version": plan["version"], "status": "approved"},
    )
    assert approved.status_code == 200, approved.text
    interview = api.post(
        "/api/v1/interviews",
        json={"plan_id": plan["id"], "candidate": {"name": "修订链候选人"}},
    ).json()
    started = api.post("/api/v1/interviews/%s/start" % interview["id"])
    answer = api.post(
        "/api/v1/interviews/%s/answers" % interview["id"],
        json={
            "turn_id": started.json()["current_turn_id"],
            "final_transcript": "避免长事务，并用 Outbox 分阶段提交。",
        },
    )
    assert answer.status_code == 200, answer.text
    answer_id = answer.json()["answer"]["id"]
    first_evaluation_id = answer.json()["evaluation"]["id"]

    completed = api.post("/api/v1/interviews/%s/complete" % interview["id"])
    assert completed.status_code == 200, completed.text
    first_report_id = completed.json()["report"]["id"]

    regraded = api.post(
        "/api/v1/interviews/%s/answers/%s/regrade" % (interview["id"], answer_id)
    )
    assert regraded.status_code == 200, regraded.text
    assert regraded.json()["revision"] == 2
    assert regraded.json()["supersedes_evaluation_id"] == first_evaluation_id
    assert regraded.json()["current_report_id"] != first_report_id

    evaluations = api.get(
        "/api/v1/interviews/%s/answers/%s/evaluations" % (interview["id"], answer_id)
    ).json()["items"]
    reports = api.get("/api/v1/interviews/%s/reports" % interview["id"]).json()["items"]
    session = api.get("/api/v1/interviews/%s" % interview["id"]).json()

    assert [item["revision"] for item in evaluations] == [1, 2]
    assert [item["revision"] for item in reports] == [1, 2]
    assert reports[1]["supersedes_report_id"] == first_report_id
    assert session["answers"][0]["current_evaluation_id"] == evaluations[1]["id"]
    assert session["current_report_id"] == reports[1]["id"]
    assert all(item["status"] == "completed" for item in get_store().outbox_work_items.values())


def test_lifecycle_controls_and_durable_events_share_one_seam() -> None:
    api = api_client()
    _, _, plan = create_plan(api)
    approved = api.patch(
        "/api/v1/interview-plans/%s" % plan["id"],
        json={"expected_version": plan["version"], "status": "approved"},
    )
    assert approved.status_code == 200, approved.text
    interview = api.post(
        "/api/v1/interviews",
        json={"plan_id": plan["id"], "candidate": {"name": "恢复测试候选人"}},
    ).json()
    started = api.post("/api/v1/interviews/%s/start" % interview["id"])
    assert started.status_code == 200, started.text
    turn_id = started.json()["current_turn_id"]

    timed_out = api.post(
        "/api/v1/interviews/%s/timeout" % interview["id"],
        json={"reason": "heartbeat deadline exceeded"},
    )
    assert timed_out.status_code == 200, timed_out.text
    assert timed_out.json()["status"] == "paused"
    assert timed_out.json()["interruption"]["kind"] == "timeout"

    blocked_answer = api.post(
        "/api/v1/interviews/%s/answers" % interview["id"],
        json={"turn_id": turn_id, "final_transcript": "暂停期间不应接受回答"},
    )
    assert blocked_answer.status_code == 409
    assert blocked_answer.json()["error"]["code"] == "INTERVIEW_NOT_IN_PROGRESS"

    recovered = api.post(
        "/api/v1/interviews/%s/recover" % interview["id"],
        json={"reason": "candidate reconnected"},
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "in_progress"
    assert recovered.json()["current_turn_id"] == turn_id

    cancelled = api.post(
        "/api/v1/interviews/%s/cancel" % interview["id"],
        json={"reason": "candidate withdrew"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    events = api.get("/api/v1/interviews/%s/events" % interview["id"])
    assert events.status_code == 200, events.text
    items = events.json()["items"]
    assert [item["sequence"] for item in items] == list(range(1, len(items) + 1))
    assert [item["type"] for item in items] == [
        "interview.created",
        "interview.started",
        "interview.timed_out",
        "interview.recovered",
        "interview.cancelled",
    ]


def test_manual_completion_skips_open_turns_before_requesting_report() -> None:
    api = api_client()
    _, _, plan = create_plan(api)
    approved = api.patch(
        "/api/v1/interview-plans/%s" % plan["id"],
        json={"expected_version": plan["version"], "status": "approved"},
    )
    assert approved.status_code == 200, approved.text
    interview = api.post(
        "/api/v1/interviews",
        json={"plan_id": plan["id"], "candidate": {"name": "提前结束候选人"}},
    ).json()
    assert api.post("/api/v1/interviews/%s/start" % interview["id"]).status_code == 200

    completed = api.post("/api/v1/interviews/%s/complete" % interview["id"])
    assert completed.status_code == 200, completed.text
    session = completed.json()["interview"]
    assert session["status"] == "report_ready"
    assert session["current_turn_id"] is None
    assert {turn["status"] for turn in session["turns"]} == {"skipped"}
    event_types = [item["type"] for item in session["lifecycle_events"]]
    assert event_types[-3:] == ["interview.completed", "report.requested", "report.completed"]


def test_failed_report_work_reenters_lifecycle_before_worker_retry() -> None:
    api = api_client()
    _, _, plan = create_plan(api)
    approved = api.patch(
        "/api/v1/interview-plans/%s" % plan["id"],
        json={"expected_version": plan["version"], "status": "approved"},
    )
    assert approved.status_code == 200, approved.text
    interview = api.post(
        "/api/v1/interviews",
        json={"plan_id": plan["id"], "candidate": {"name": "报告恢复候选人"}},
    ).json()
    assert api.post("/api/v1/interviews/%s/start" % interview["id"]).status_code == 200

    service = InterviewService(get_store())

    def fail_report(*args, **kwargs):
        raise RuntimeError("report provider unavailable")

    service.reports.build_report = fail_report
    with pytest.raises(RuntimeError, match="report provider unavailable"):
        service.complete_interview(interview["id"])

    failed = service.get_interview(interview["id"])
    assert failed["status"] == "completed"
    assert failed["lifecycle_events"][-1]["type"] == "report.failed"

    result = asyncio.run(OutboxWorker(get_store()).run_once())
    assert result[-1]["status"] == "completed"
    recovered = service.get_interview(interview["id"])
    assert recovered["status"] == "report_ready"
    assert [item["type"] for item in recovered["lifecycle_events"]][-3:] == [
        "report.failed",
        "report.retry_started",
        "report.completed",
    ]


def test_failed_evaluation_work_reenters_lifecycle_before_worker_retry() -> None:
    api = api_client()
    _, _, plan = create_plan(api)
    approved = api.patch(
        "/api/v1/interview-plans/%s" % plan["id"],
        json={"expected_version": plan["version"], "status": "approved"},
    )
    assert approved.status_code == 200, approved.text
    interview = api.post(
        "/api/v1/interviews",
        json={"plan_id": plan["id"], "candidate": {"name": "评分恢复候选人"}},
    ).json()
    started = api.post("/api/v1/interviews/%s/start" % interview["id"]).json()

    service = InterviewService(get_store())

    async def fail_evaluation(*args, **kwargs):
        raise RuntimeError("evaluation provider unavailable")

    service.evaluation.evaluate_answer = fail_evaluation
    with pytest.raises(RuntimeError, match="evaluation provider unavailable"):
        asyncio.run(
            service.submit_answer(
                interview["id"],
                {"turn_id": started["current_turn_id"], "final_transcript": "等待恢复评分"},
            )
        )

    failed = service.get_interview(interview["id"])
    assert failed["answers"][0]["evaluation_status"] == "failed"
    assert failed["lifecycle_events"][-1]["type"] == "evaluation.failed"

    result = asyncio.run(OutboxWorker(get_store()).run_once())
    assert result[-1]["status"] == "completed"
    recovered = service.get_interview(interview["id"])
    assert recovered["status"] == "report_ready"
    event_types = [item["type"] for item in recovered["lifecycle_events"]]
    assert event_types.index("evaluation.failed") < event_types.index("evaluation.retry_started")
    assert event_types.index("evaluation.retry_started") < event_types.index("evaluation.completed")
