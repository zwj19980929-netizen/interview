"""Optional user Skill/company context → admission → interview → audited report."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.appointments import AppointmentService
from app.services.interviews import InterviewService
from app.services.interview_skills import InterviewSkillService
from app.services.plan_assembly import InterviewPlanAssembly, PlanAssemblyRequest, PlanAssemblyPolicy
from test_plan_assembly import create_scope, add_question
from test_interview_skills import approved, package


PERSONAL_SKILL = {
    "schema_version": "interview_skill.v2", "name": "我的面试练习习惯",
    "instructions": "# 我的面试习惯\n\n先聊我做过的项目。答不出时换一个方向，适当留时间思考。\n\n讨论具体例子，避免把全部问题一次念完。",
}
COMPANY_CONTEXT = "独立企业资料039：这家公司的项目是离线数据同步平台，重视可恢复任务。"


@pytest.mark.anyio
@pytest.mark.parametrize("context_mode", ["none", "skill", "company", "both", "legacy_skill", "bank"])
async def test_enterprise_plan_real_admission_autonomous_selection_scoring_and_http_report(monkeypatch, context_mode):
    monkeypatch.setenv("INTERVIEWER_LOCAL_MEDIA", "true")
    store = InMemoryStore()
    catalog, position, kb, role, candidate = create_scope(store, "自主后端", ["python", "database"], 15)
    for skill, point in (("python", "线程边界"), ("database", "事务隔离")):
        await add_question(catalog, kb["id"], title=point, skill=skill, difficulty="mid", key_points=[point])
    skills = InterviewSkillService(store)
    uses_skill = context_mode in {"skill", "both", "legacy_skill"}
    company = COMPANY_CONTEXT if context_mode in {"company", "both"} else None
    if context_mode == "legacy_skill":
        selected_skill = approved(skills, "org_default")
    elif uses_skill:
        selected_skill = skills.create({"package": deepcopy(PERSONAL_SKILL)}, actor_id="author", organization_id="org_default")
        assert selected_skill["status"] == "active"
        assert selected_skill["package"]["instructions"] == PERSONAL_SKILL["instructions"]
    else:
        selected_skill = None
        def unexpected_skill_access(*args, **kwargs):
            pytest.fail("An interview without a Skill must not contact the Skill service")
        for method in ("freeze_snapshot", "verify_current_authorization", "load_compiled"):
            monkeypatch.setattr(InterviewSkillService, method, unexpected_skill_access)
    if context_mode == "bank":
        store.role_requirements.clear()
        plan = await InterviewPlanAssembly(store).prepare(job_position_id=position["id"],
            candidate_profile_id=candidate["id"], knowledge_base_ids=[kb["id"]], duration_minutes=15)
    else:
        plan = await InterviewPlanAssembly(store).assemble(PlanAssemblyRequest(
            role_requirement_id=role["id"], job_position_id=position["id"], candidate_profile_id=candidate["id"],
            knowledge_base_ids=(kb["id"],), question_count=2, approve=False, execution_schema_version=3,
            skill_id=selected_skill["id"] if selected_skill and context_mode != "legacy_skill" else None,
            enterprise_skill_id=selected_skill["id"] if context_mode == "legacy_skill" else None,
            company_context=company, adaptive_policy={"min_root_questions": 2, "max_root_questions": 2},
            policy=PlanAssemblyPolicy(allow_followups=False),
        ))
    frozen_skill = plan.get("enterprise_skill_snapshot")
    if selected_skill:
        assert frozen_skill["revision_id"] == selected_skill["revision_id"]
        assert "instructions" not in frozen_skill
    else:
        assert frozen_skill is None
    assert plan.get("company_context") == company
    assert plan["status"] == ("approved" if context_mode == "bank" else "draft")
    if selected_skill:
        revised_package = package(style="concise") if context_mode == "legacy_skill" else {
            **PERSONAL_SKILL, "instructions": "# 新的习惯\n\n先谈一个项目，再谈工程取舍。"}
        newer = skills.revise(selected_skill["id"], {"package": revised_package},
            expected_version=selected_skill["version"], actor_id="author", organization_id="org_default")
        if context_mode == "legacy_skill":
            newer = skills.validate(newer["id"], expected_version=newer["version"], actor_id="reviewer", organization_id="org_default")
            skills.approve(newer["id"], expected_version=newer["version"], actor_id="reviewer",
                reason="Synthetic version approval for the regression fixture.", review_confirmed=True, organization_id="org_default")
        else:
            assert newer["status"] == "active" and newer["revision_id"] != frozen_skill["revision_id"]
    plan = InterviewPlanAssembly(store).patch_plan(plan["id"], {"expected_version": plan["version"], "status": "approved"})
    assert plan.get("enterprise_skill_snapshot") == frozen_skill

    service = InterviewService(store)
    appointments = AppointmentService(store, interviews=service)
    now = datetime.now(timezone.utc)
    appointment = appointments.create({"plan_id": plan["id"], "job_position_id": position["id"],
        "candidate_profile_id": candidate["id"], "scheduled_start_at": (now - timedelta(minutes=1)).isoformat(),
        "scheduled_end_at": (now + timedelta(minutes=15)).isoformat()})
    assert appointment["readiness"]["can_invite"], appointment["readiness"]["checks"]
    invitation = appointments.invite(appointment["id"], {"expires_at": (now + timedelta(minutes=15)).isoformat()})
    token = invitation["invitation_token"]
    appointments.intake(token, {"name": "候选人", "email": "candidate@example.com", "phone": "13800138000",
        "consent": {"accepted": True, "version": "v1", "audio_recording": True, "video_recording": False}})
    ready = appointments.readiness(token, {"browser_supported": True, "microphone_granted": True,
        "camera_granted": True, "speaker_verified": True, "webrtc_supported": True, "audio_worklet_supported": True,
        "webgl_supported": True, "media_recorder_supported": True, "audio_content_type": "audio/webm;codecs=opus",
        "network_rtt_ms": 20, "network_jitter_ms": 3, "avatar_fps": 60})
    assert ready["can_start"] is True
    started = appointments.start(token)
    interview_id = started["interview_id"]
    session = service.get_interview(interview_id)
    if context_mode == "bank":
        assert session["plan_snapshot"]["role_requirement"] is None
        assert session["plan_snapshot"]["assessment_basis"] == plan["assessment_basis"]
    assert session["turns"] == [] and session["dialogue_state"] == "awaiting_next_decision"
    assert session["plan_snapshot"].get("enterprise_skill_snapshot") == frozen_skill
    assert session["plan_snapshot"].get("company_context") == company
    assert appointments.start(token)["interview_id"] == interview_id

    for index in range(2):
        session = await service.advance_adaptive_interview(interview_id)
        turn = next(t for t in session["turns"] if t["id"] == session["current_turn_id"])
        assert len(session["turns"]) == index + 1
        result = await service.submit_audio_answer(interview_id, {
            "turn_id": turn["id"], "audio_uri": "/media/synthetic-enterprise-flow.webm", "duration_seconds": 10,
            "development_transcript": turn["question_snapshot"]["standard_answer"],
        })
        assert result["accepted"] is True and result["answer"]["transcript_source"] == "server_batch"
        await service.process_outbox_work(result["evaluation_work_id"])
        scoring_audit = [item for item in store.model_invocations if item["purpose"] == "answer_evaluation"]
        assert len(scoring_audit) == index + 1
        assert all(item["prompt_version"] == "answer_evaluation.v6" for item in scoring_audit)
        current = service.get_interview(interview_id)
        assert not current.get("candidate_input_completed_at")
        assert current["dialogue_state"] == "awaiting_next_decision"
    session = await service.advance_adaptive_interview(interview_id)
    assert session["candidate_input_completed_at"]
    assert session["candidate_input_completion_reason"] in {"evidence_sufficient", "budget_exhausted"}
    with persistence_for(store).transaction("org_default") as tx:
        work = [item for item in tx.outbox.list() if item.get("kind") == "interview.report.generate"]
    assert len(work) == 1
    await service.process_outbox_work(work[0]["id"])
    monkeypatch.setattr("app.transport.service_locator.get_store", lambda: store)
    api = TestClient(create_app())
    report = api.get(f"/api/v1/interviews/{interview_id}/report")
    assert report.status_code == 200, report.text
    value = report.json()
    assert value["human_decision_required"] is True
    assert value["execution_schema_version"] == 3
    assert len(value["dimension_scores"]) == 2
    exported = api.get(f"/api/v1/interviews/{interview_id}/report/export?format=json")
    assert exported.status_code == 200
    assert exported.json()["overall_score"] == value["overall_score"]
    review = api.get(f"/api/v1/interviews/{interview_id}/review")
    assert review.status_code == 200
    assert len(review.json()["turns"]) == 2
    public = service.get_candidate_interview(interview_id, service._candidate_session_token(service.get_interview(interview_id)))
    assert "assessment_contract" not in public and "plan_snapshot" not in public
    assert "enterprise_skill_snapshot" not in public
    assert "company_context" not in public and "skill" not in public
    serialized = json.dumps(public, ensure_ascii=False)
    assert COMPANY_CONTEXT not in serialized and PERSONAL_SKILL["instructions"] not in serialized
