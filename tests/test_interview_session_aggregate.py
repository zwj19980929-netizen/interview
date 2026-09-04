import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ApiError
from app.main import create_app
from app.persistence.provider import persistence_for
from app.repositories.provider import get_store, reset_store_for_tests
from app.services.interviews import InterviewService
from app.workers.outbox import OutboxWorker


@pytest.fixture(autouse=True)
def _enable_formal_local_media(monkeypatch: pytest.MonkeyPatch) -> None:
    """聚合 API 场景显式声明使用本地正式媒体配置。"""

    monkeypatch.setenv("INTERVIEWER_LOCAL_MEDIA", "true")


def api_client() -> TestClient:
    reset_store_for_tests()
    return TestClient(create_app())


def create_plan(api: TestClient) -> tuple:
    position = api.post(
        "/api/v1/job-positions",
        json={"code": "architect", "name": "架构师"},
    ).json()
    knowledge_base = api.post(
        "/api/v1/job-positions/%s/knowledge-bases" % position["id"],
        json={"name": "架构题库"},
    ).json()
    question = api.post(
        "/api/v1/knowledge-bases/%s/questions" % knowledge_base["id"],
        json={
            "knowledge_base_id": knowledge_base["id"],
            "title": "事务边界",
            "question_text": "为什么外部模型调用不能放在数据库事务里？",
            "standard_answer": "长事务会扩大锁竞争和失败面，应使用 Outbox 分阶段提交。",
            "key_points": ["避免长事务", "Outbox 分阶段提交"],
            "difficulty": "senior",
            "skills": ["architecture"],
            "rubric": {"semantic_correctness": 1.0},
        },
    )
    assert question.status_code == 202, question.text
    asyncio.run(OutboxWorker(get_store()).run_once())
    question = api.get("/api/v1/questions/%s" % question.json()["id"])
    role = api.post(
        "/api/v1/job-positions/%s/role-requirements" % position["id"],
        json={
            "title": "架构师",
            "description": "负责 architecture 与可靠性设计。",
            "must_have_skills": ["architecture"],
            "seniority": "senior",
            "interview_duration_minutes": 20,
        },
    )
    assert role.status_code == 200, role.text
    candidate = api.post(
        "/api/v1/candidate-profiles",
        json={"name": "聚合测试候选人", "email": "aggregate@example.com", "phone": "13800138002"},
    ).json()
    plan = api.post(
        "/api/v1/interview-plans/generate",
        json={
            "role_requirement_id": role.json()["id"],
            "job_position_id": position["id"],
            "candidate_profile_id": candidate["id"],
            "knowledge_base_ids": [knowledge_base["id"]],
            "question_count": 1,
        },
    )
    assert plan.status_code == 200, plan.text
    return question.json(), role.json(), plan.json()


def admit_plan(api: TestClient, plan: dict) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    appointment = api.post(
        "/api/v1/interview-appointments",
        json={
            "plan_id": plan["id"],
            "candidate_profile_id": plan["candidate_profile_id"],
            "job_position_id": plan["job_position_id"],
            "scheduled_start_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "scheduled_end_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "settings": {"record_audio": True},
        },
    )
    assert appointment.status_code == 200, appointment.text
    invitation = api.post(
        "/api/v1/interview-appointments/%s/invite" % appointment.json()["id"],
        json={"expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")},
    )
    assert invitation.status_code == 200, invitation.text
    token = invitation.json()["invitation_token"]
    notice = api.get("/api/v1/public/interview-invitations/%s" % token).json()["consent"]
    intake = api.post(
        "/api/v1/public/interview-invitations/%s/intake" % token,
        json={
            "name": "聚合测试候选人",
            "email": "aggregate@example.com",
            "phone": "13800138002",
            "consent": {
                "accepted": True,
                "version": notice["version"],
                "audio_recording": True,
            },
        },
    )
    assert intake.status_code == 200, intake.text
    incomplete_readiness = api.post(
        "/api/v1/public/interview-invitations/%s/readiness" % token,
        json={
            "browser_supported": True,
            "microphone_granted": True,
            "audio_content_type": "audio/webm",
        },
    )
    assert incomplete_readiness.status_code == 422
    readiness = api.post(
        "/api/v1/public/interview-invitations/%s/readiness" % token,
        json={
            "browser_supported": True,
            "microphone_granted": True,
            "camera_granted": True,
            "speaker_verified": True,
            "webrtc_supported": True,
            "audio_worklet_supported": True,
            "webgl_supported": True,
            "media_recorder_supported": True,
            "network_rtt_ms": 20,
            "network_jitter_ms": 3,
            "avatar_fps": 60,
            "audio_content_type": "audio/webm",
        },
    )
    assert readiness.status_code == 200, readiness.text
    started = api.post("/api/v1/public/interview-invitations/%s/start" % token)
    assert started.status_code == 200, started.text
    return api.get("/api/v1/interviews/%s" % started.json()["interview_id"]).json()


def audio_answer(api: TestClient, interview: dict, transcript: str):
    return asyncio.run(
        InterviewService(get_store()).submit_audio_answer(
            interview["id"],
            {
            "turn_id": interview["current_turn_id"],
            "audio_uri": "private-test://answer.webm",
            "content_type": "audio/webm",
            "development_transcript": transcript,
            "duration_seconds": 5,
            },
        )
    )


def test_approved_plan_snapshot_and_optimistic_concurrency() -> None:
    api = api_client()
    question, _, plan = create_plan(api)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    rejected = api.post(
        "/api/v1/interview-appointments",
        json={
            "plan_id": plan["id"],
            "candidate_profile_id": plan["candidate_profile_id"],
            "job_position_id": plan["job_position_id"],
            "scheduled_start_at": now.isoformat().replace("+00:00", "Z"),
            "scheduled_end_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        },
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

    session = admit_plan(api, approved.json())
    assert session["candidate"]["interview_id"] == session["id"]
    assert session["candidate"]["metadata"] == {"candidate_profile_id": plan["candidate_profile_id"]}
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
    interview = admit_plan(api, approved.json())
    answer = audio_answer(api, interview, "避免长事务，并用 Outbox 分阶段提交。")
    answer_id = answer["answer"]["id"]
    assert answer["evaluation"]["status"] == "pending"
    asyncio.run(OutboxWorker(get_store()).run_once())
    first_evaluation_id = api.get(
        "/api/v1/interviews/%s/answers/%s/evaluations" % (interview["id"], answer_id)
    ).json()["items"][0]["id"]

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
    work_items = list(get_store().outbox_work_items.values())
    assert all(
        item["status"] == "completed"
        for item in work_items
        if item["kind"] != "appointment.reminder.email"
    )
    assert next(item for item in work_items if item["kind"] == "appointment.reminder.email")["status"] == "cancelled"


def test_lifecycle_controls_and_durable_events_share_one_seam() -> None:
    api = api_client()
    _, _, plan = create_plan(api)
    approved = api.patch(
        "/api/v1/interview-plans/%s" % plan["id"],
        json={"expected_version": plan["version"], "status": "approved"},
    )
    assert approved.status_code == 200, approved.text
    interview = admit_plan(api, approved.json())
    turn_id = interview["current_turn_id"]

    timed_out = api.post(
        "/api/v1/interviews/%s/timeout" % interview["id"],
        json={"reason": "heartbeat deadline exceeded"},
    )
    assert timed_out.status_code == 200, timed_out.text
    assert timed_out.json()["status"] == "paused"
    assert timed_out.json()["interruption"]["kind"] == "timeout"

    with pytest.raises(ApiError) as blocked:
        asyncio.run(
            InterviewService(get_store()).submit_audio_answer(
                interview["id"],
                {
                    "turn_id": turn_id,
                    "audio_uri": "private-test://paused.webm",
                    "development_transcript": "暂停期间不应接受回答",
                },
            )
        )
    assert blocked.value.code == "INTERVIEW_NOT_IN_PROGRESS"

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
    interview = admit_plan(api, approved.json())

    completed = api.post("/api/v1/interviews/%s/complete" % interview["id"])
    assert completed.status_code == 200, completed.text
    session = completed.json()["interview"]
    assert session["status"] == "report_ready"
    assert session["current_turn_id"] is None
    assert {turn["status"] for turn in session["turns"]} == {"skipped"}
    event_types = [item["type"] for item in session["lifecycle_events"]]
    assert event_types[-3:] == ["interview.completed", "report.requested", "report.completed"]


def test_retryable_report_work_stays_generating_until_worker_retry() -> None:
    api = api_client()
    _, _, plan = create_plan(api)
    approved = api.patch(
        "/api/v1/interview-plans/%s" % plan["id"],
        json={"expected_version": plan["version"], "status": "approved"},
    )
    assert approved.status_code == 200, approved.text
    interview = admit_plan(api, approved.json())

    service = InterviewService(get_store())

    def fail_report(*args, **kwargs):
        raise RuntimeError("report provider unavailable")

    service.reports.build_report = fail_report
    with pytest.raises(RuntimeError, match="report provider unavailable"):
        service.complete_interview(interview["id"])

    retrying = service.get_interview(interview["id"])
    assert retrying["status"] == "report_generating"
    assert retrying["lifecycle_events"][-1]["type"] == "report.requested"

    result = asyncio.run(OutboxWorker(get_store()).run_once())
    assert result[-1]["status"] == "completed"
    recovered = service.get_interview(interview["id"])
    assert recovered["status"] == "report_ready"
    assert [item["type"] for item in recovered["lifecycle_events"]][-2:] == [
        "report.requested",
        "report.completed",
    ]


def test_retryable_evaluation_work_stays_pending_until_worker_retry() -> None:
    api = api_client()
    _, _, plan = create_plan(api)
    approved = api.patch(
        "/api/v1/interview-plans/%s" % plan["id"],
        json={"expected_version": plan["version"], "status": "approved"},
    )
    assert approved.status_code == 200, approved.text
    interview = admit_plan(api, approved.json())

    service = InterviewService(get_store())

    async def fail_evaluation(*args, **kwargs):
        raise RuntimeError("evaluation provider unavailable")

    accepted = asyncio.run(
        service.submit_audio_answer(
            interview["id"],
            {
                "turn_id": interview["current_turn_id"],
                "audio_uri": "private-test://failure.webm",
                "content_type": "audio/webm",
                "development_transcript": "等待恢复评分",
            },
        )
    )
    worker = OutboxWorker(get_store())
    worker.interviews.evaluation.evaluate_answer = fail_evaluation
    with pytest.raises(RuntimeError, match="evaluation provider unavailable"):
        asyncio.run(
            worker.run_item(accepted["evaluation_work_id"])
        )

    retrying = service.get_interview(interview["id"])
    assert retrying["answers"][0]["evaluation_status"] == "pending"
    assert "evaluation.failed" not in [item["type"] for item in retrying["lifecycle_events"]]

    result = asyncio.run(OutboxWorker(get_store()).run_once())
    assert result[-1]["status"] == "completed"
    recovered = service.get_interview(interview["id"])
    assert recovered["status"] == "in_progress"
    followup = next(item for item in recovered["turns"] if item["id"] == recovered["current_turn_id"])
    assert followup["is_followup"] is True
    event_types = [item["type"] for item in recovered["lifecycle_events"]]
    assert "evaluation.failed" not in event_types
    assert "evaluation.retry_started" not in event_types
    assert "evaluation.completed" in event_types

    asyncio.run(
        service.submit_audio_answer(
            interview["id"],
            {
                "turn_id": followup["id"],
                "audio_uri": "private-test://followup.webm",
                "content_type": "audio/webm",
                "development_transcript": "应避免长事务，并通过 Outbox 分阶段提交。",
            },
        )
    )
    asyncio.run(OutboxWorker(get_store()).run_once())
    assert service.get_interview(interview["id"])["status"] == "report_ready"
