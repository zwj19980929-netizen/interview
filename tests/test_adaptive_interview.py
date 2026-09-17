from copy import deepcopy
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.core.errors import ApiError
from app.domain.adaptive_interview import (
    AdaptiveInterviewPolicy, budget_summary, coverage_summary, freeze_assessment_contract,
    frozen_candidate, initialize_adaptive_snapshot,
)
from app.domain.interview_lifecycle import InterviewSessionLifecycle, LifecycleCommand, LifecycleCommandType as Kind
from app.domain.scoring_quality import project_report
from app.repositories.memory import InMemoryStore
from app.services.reports import ReportService


NOW = "2026-09-10T10:00:00Z"
LIFECYCLE = InterviewSessionLifecycle()


def make_session(*, maximum=3, minimum=2):
    questions = []
    for index, dimension in enumerate(("python", "database", "python"), 1):
        questions.append(frozen_candidate({
            "id": "q%d" % index, "version": 1, "question_text": "说明%s的边界。" % dimension,
            "standard_answer": "说明边界与权衡。", "key_points": [{"id": "kp%d" % index, "text": "边界", "weight": 1.0}],
            "skills": [dimension], "speech_asset_id": "speech%d" % index,
        }, competency_ids=[dimension], source_type="position_bank"))
    contract = freeze_assessment_contract(role={"id": "role", "version": 1}, candidates=questions,
        dimension_weights={"python": .75, "database": .25}, question_count=maximum, duration_minutes=10,
        policy={"min_root_questions": minimum, "max_root_questions": maximum})
    source = {"id": "iv", "organization_id": "org_default", "plan_id": "plan", "status": "scheduled",
        "plan_snapshot": initialize_adaptive_snapshot({"assessment_contract": contract}),
        "turns": [], "turn_ids": [], "current_turn_id": None, "answers": [], "evaluation_revisions": [],
        "report_revisions": [], "current_report_id": None, "lifecycle_events": [], "created_at": NOW, "updated_at": NOW}
    created = LIFECYCLE.execute(source, LifecycleCommand(Kind.CREATE), now=NOW).session
    return LIFECYCLE.execute(created, LifecycleCommand(Kind.START), now=NOW).session


def select(source, question_id, **overrides):
    payload = {"decision_id": "choose_" + question_id, "question_id": question_id,
               "expected_decision_revision": source.get("decision_revision", 0), **overrides}
    return LIFECYCLE.execute(source, LifecycleCommand(Kind.SELECT_NEXT, payload), now=NOW)


def submit(source):
    turn = next(item for item in source["turns"] if item["id"] == source["current_turn_id"])
    answer = {"id": "answer_" + turn["question_id"], "interview_id": source["id"], "turn_id": turn["id"],
        "question_id": turn["question_id"], "question_snapshot_id": turn["question_snapshot_id"],
        "evaluation_status": "pending", "current_evaluation_id": None, "created_at": NOW, "updated_at": NOW,
        "stt_confidence": .35, "stt_confidence_source": "provider"}
    return LIFECYCLE.execute(source, LifecycleCommand(Kind.ANSWER_SUBMITTED, {"answer": answer}), now=NOW)


def scored(source, score, answer_id=None):
    answer = next(item for item in source["answers"] if item["id"] == answer_id) if answer_id else source["answers"][-1]
    revision = len([item for item in source["evaluation_revisions"] if item["answer_id"] == answer["id"]]) + 1
    evaluation = {"id": "eval_" + answer["id"] + str(revision), "answer_id": answer["id"], "interview_id": "iv",
        "question_id": answer["question_id"], "question_snapshot_id": answer["question_snapshot_id"],
        "revision": revision, "score": score, "confidence": .9, "review_flags": [],
        "covered_key_points": [{"key_point_id": "point", "evidence": "真实回答"}], "missing_key_points": [],
        "feedback": "已提供证据。", "created_at": NOW, "updated_at": NOW}
    return LIFECYCLE.execute(source, LifecycleCommand(Kind.EVALUATION_SUCCEEDED, {
        "answer_id": answer["id"], "revision": revision, "evaluation": evaluation}), now=NOW)


def end(source, reason="evidence_sufficient", **overrides):
    return LIFECYCLE.execute(source, LifecycleCommand(Kind.END_CANDIDATE_INPUT, {
        "decision_id": "end", "expected_decision_revision": source["decision_revision"], "reason": reason,
        **overrides}), now=NOW)


def test_v3_empty_queue_and_background_scoring_never_end_candidate_input():
    source = make_session()
    assert source["turns"] == []
    assert source["dialogue_state"] == "awaiting_next_decision"
    selected = select(source, "q1")
    assert source["turns"] == []  # Proposal execution does not mutate the source.
    assert len(selected.session["turns"]) == 1
    assert selected.session["turns"][0]["competency_ids"] == ["python"]
    assert selected.session["turns"][0]["assessed_rubric_point_ids"] == ["kp1"]
    submitted = submit(selected.session)
    assert submitted.effects[0]["type"] == "evaluation.requested"
    assert submitted.session["dialogue_state"] == "awaiting_next_decision"
    assert submitted.session.get("candidate_input_completed_at") is None
    evaluated = scored(submitted.session, 80)
    assert evaluated.session["status"] == "in_progress"
    assert evaluated.session.get("candidate_input_completed_at") is None
    assert evaluated.effects == []
    assert select(evaluated.session, "q2").session["current_turn_id"] is not None


def test_selections_are_scoped_versioned_and_idempotent():
    source = make_session()
    with pytest.raises(ApiError, match="frozen approved pool"):
        select(source, "foreign_question")
    selected = select(source, "q1")
    replay = select(selected.session, "q1", expected_decision_revision=0)
    assert replay.changed is False
    assert replay.events == []
    with pytest.raises(ApiError) as conflict:
        select(selected.session, "q2", decision_id="choose_q1", expected_decision_revision=0)
    assert conflict.value.code == "AGENT_DECISION_CONFLICT"
    with pytest.raises(ApiError) as stale:
        select(selected.session, "q2", expected_decision_revision=0)
    assert stale.value.code == "AGENT_DECISION_STALE"
    with pytest.raises(ApiError):
        select(selected.session, "q2")
    available = submit(selected.session).session
    assert len(select(available, "q2").session["turns"]) == 2  # Scoring need not finish first.
    with pytest.raises(ApiError):
        select(available, "q1", decision_id="new_q1")
    with pytest.raises(ApiError):
        LIFECYCLE.execute(available, LifecycleCommand(Kind.SELECT_NEXT, {"decision_id": "x", "question_id": "q2"}), now=NOW)


def test_normal_end_requires_real_coverage_and_runs_reports_only_after_scoring():
    source = make_session()
    with pytest.raises(ApiError):
        end(source)
    one = submit(select(source, "q1").session).session
    with pytest.raises(ApiError):
        end(one)
    two = submit(select(one, "q2").session).session
    assert coverage_summary(two)["sufficient"]
    ended = end(two)
    assert ended.session["candidate_input_completed_at"] == NOW
    assert ended.session["status"] == "in_progress"
    assert ended.effects == []
    replay = end(ended.session, expected_decision_revision=two["decision_revision"])
    assert not replay.changed
    with pytest.raises(ApiError):
        select(ended.session, "q3")
    first = scored(ended.session, 80, "answer_q1")
    assert first.effects == []
    second = scored(first.session, 60, "answer_q2")
    assert second.session["status"] == "report_generating"
    assert second.effects[0]["type"] == "report.requested"


def test_candidate_stop_respects_incomplete_capture_without_inventing_answer():
    source = submit(select(make_session(), "q1").session).session
    source = select(source, "q2").session
    ended = end(source, "candidate_requested")
    assert len(ended.session["answers"]) == 1
    assert ended.session["turns"][-1]["status"] == "skipped"
    assert ended.session["turns"][-1]["skip_reason"] == "candidate_requested"
    assert ended.session["assessment_coverage"]["missing_required_competencies"] == ["database"]
    with pytest.raises(ApiError):
        LIFECYCLE.execute(ended.session, LifecycleCommand(Kind.REPORT_REQUESTED), now=NOW)
    assert scored(ended.session, 90).effects[0]["type"] == "report.requested"


def test_budget_cannot_be_forged_and_last_selected_question_keeps_its_answer_time():
    source = make_session(maximum=2)
    with pytest.raises(ApiError):
        end(source, "budget_exhausted")
    source = submit(select(source, "q1").session).session
    source = select(source, "q2").session
    assert budget_summary(source, NOW)["exhausted"]
    with pytest.raises(ApiError):
        end(source, "budget_exhausted")
    source = submit(source).session
    assert end(source, "budget_exhausted").session["candidate_input_completion_reason"] == "budget_exhausted"
    with pytest.raises(ApiError):
        select(source, "q3")
    late = LIFECYCLE.execute(make_session(), LifecycleCommand(Kind.END_CANDIDATE_INPUT, {
        "decision_id": "late", "expected_decision_revision": 0, "reason": "budget_exhausted"}),
        now="2026-09-10T10:10:00Z")
    assert late.session["status"] == "report_generating"


def test_frozen_pool_and_budget_validate_without_trusting_self_reported_hashes():
    source = make_session()
    source["plan_snapshot"]["assessment_contract"]["budget"]["max_root_questions"] = 50
    with pytest.raises(ApiError):
        select(source, "q1")
    for invalid in ({"min_root_questions": 4, "max_root_questions": 2}, {"max_root_questions": True},
                    {"max_root_questions": 51}, {"tools": ["all"]}):
        with pytest.raises(ValidationError):
            AdaptiveInterviewPolicy.model_validate(invalid)


def completed_session(scores):
    source = make_session()
    for question_id, score in scores:
        source = submit(select(source, question_id).session).session
        source = scored(source, score).session
    return end(source, "evidence_sufficient" if coverage_summary(source)["sufficient"] else "candidate_requested").session


def test_fixed_competency_weights_ignore_variable_root_count_and_followup_weight():
    source = completed_session([("q1", 100), ("q2", 0), ("q3", 100)])
    report = ReportService(InMemoryStore()).build_report(source, trigger_reason="test")
    assert report["overall_score"] == 75  # .75 * python 100 + .25 * database 0, not 200/3.
    assert report["score_status"] == "available"
    assert report["coverage_status"] == "sufficient"
    assert report["question_evaluations"][0]["assessed_rubric_point_ids"] == ["kp1"]
    assert report["recognition_warning_answer_ids"]  # 027 remains advisory.
    assert report["dimension_scores"] == [
        {"dimension": "database", "score": 0, "weight": .25, "evidence_root_count": 1, "evidence_status": "sufficient"},
        {"dimension": "python", "score": 100, "weight": .75, "evidence_root_count": 2, "evidence_status": "sufficient"}]
    root = source["turns"][0]
    source["turns"].append({"id": "followup", "is_followup": True, "root_turn_id": root["id"], "weight": 0})
    child_answer = deepcopy(source["answers"][0])
    child_answer.update(id="child", turn_id="followup", current_evaluation_id="child_eval")
    child_eval = deepcopy(source["evaluation_revisions"][0])
    child_eval.update(id="child_eval", answer_id="child", score=0)
    source["answers"].append(child_answer)
    source["evaluation_revisions"].append(child_eval)
    after = ReportService(InMemoryStore()).build_report(source, trigger_reason="test")
    assert after["overall_score"] == 75
    assert len(after["question_evaluations"]) == 3
    newer = deepcopy(source["evaluation_revisions"][0])
    newer.update(id="root_current", revision=2, score=20)
    source["evaluation_revisions"].append(newer)
    source["answers"][0]["current_evaluation_id"] = newer["id"]
    latest = ReportService(InMemoryStore()).build_report(source, trigger_reason="regrade")
    assert latest["overall_score"] == 45
    assert latest["question_evaluations"][0]["evaluation_id"] == newer["id"]


def test_uncovered_competency_survives_report_projection_get_and_export():
    source = completed_session([("q1", 90)])
    store = InMemoryStore()
    service = ReportService(store)
    report = service.build_report(source, trigger_reason="test")
    assert report["overall_score"] is None
    assert report["score_status"] == "available"
    assert report["question_evaluations"][0]["score"] == 90
    assert report["job_fit_level"] == "insufficient_evidence"
    assert report["dimension_scores"][0]["score"] is None
    assert report["dimension_scores"][1]["score"] == 90
    source.update(report_revisions=[report], current_report_id=report["id"], status="report_ready")
    assert project_report(report, source)["overall_score"] is None
    with service.persistence.transaction("org_default") as transaction:
        transaction.interview_sessions.add(source)
    assert service.get_report("iv")["overall_score"] is None
    exported = service.export_report("iv", export_format="json", actor_id="reviewer")
    data = json.loads(exported["content"])
    assert data["overall_score"] is None
    assert data["question_evaluations"][0]["score"] == 90
    csv = service.export_report("iv", export_format="csv", actor_id="reviewer")["content"]
    assert "insufficient_evidence" in csv


@pytest.mark.anyio
async def test_plan_assembly_freezes_v3_contract_but_keeps_v2_default():
    from tests.test_plan_assembly import create_scope, add_question
    from app.services.plan_assembly import InterviewPlanAssembly, PlanAssemblyRequest
    from app.services.plans import InterviewPlanService
    from app.schemas.api import InterviewPlanGenerateRequest

    store = InMemoryStore()
    catalog, position, bank, role, candidate = create_scope(store, "Python Backend", ["python"], 20)
    await add_question(catalog, bank["id"], title="Python 并发", skill="python", difficulty="mid", key_points=["边界"])
    settings = dict(role_requirement_id=role["id"], job_position_id=position["id"],
                    candidate_profile_id=candidate["id"], knowledge_base_ids=(bank["id"],), question_count=1, approve=True)
    assembly = InterviewPlanAssembly(store)
    legacy = await assembly.assemble(PlanAssemblyRequest(**settings))
    assert legacy["execution_schema_version"] == 2
    adaptive = await assembly.assemble(PlanAssemblyRequest(**settings, execution_schema_version=3))
    assert adaptive["assessment_contract"]["competencies"] == [
        {"id": "python", "weight": 1.0, "required": True, "min_evidence_roots": 1}]
    assert adaptive["assessment_contract"]["candidate_questions"][0]["frozen_question"]["standard_answer"]
    assert InterviewPlanService(store).get_plan(adaptive["id"])["execution_schema_version"] == 3
    assert len(InterviewPlanService(store).list_plans()) == 2
    assert InterviewPlanGenerateRequest.model_validate(settings).execution_schema_version == 2
    with pytest.raises(ApiError):
        await assembly.assemble(PlanAssemblyRequest(**settings, adaptive_policy={"max_root_questions": 1}))


@pytest.mark.parametrize("already_scored", [False, True])
def test_v3_deadline_preserves_accepted_answers_and_durably_enqueues_report(already_scored):
    from app.services.interviews import InterviewService

    store = InMemoryStore()
    source = submit(select(make_session(), "q1").session).session
    if already_scored:
        source = scored(source, 80).session
    source = select(source, "q2").session
    # Exercise the supported settings fallback as well as the lifecycle's
    # strict frozen deadline validation.
    source["settings"] = {"scheduled_end_at": "2026-09-10T10:10:00Z"}
    service = InterviewService(store, clock=lambda: datetime(2026, 9, 10, 10, 11, tzinfo=timezone.utc))
    with service.persistence.transaction("org_default") as transaction:
        transaction.interview_sessions.add(source)
    expired = service.expire_overdue_interviews()
    assert len(expired) == 1
    current = expired[0]
    assert current["candidate_input_completion_reason"] == "appointment_window_expired"
    assert current["candidate_input_completed_at"] == "2026-09-10T10:11:00Z"
    assert len(current["answers"]) == 1
    assert current["turns"][-1]["skip_reason"] == "appointment_window_expired"
    assert current["status"] == ("report_generating" if already_scored else "in_progress")
    with service.persistence.transaction("org_default") as transaction:
        reports = [work for work in transaction.outbox.list() if work["kind"] == "interview.report.generate"]
        assert len(reports) == int(already_scored)
        assert len([audit for audit in transaction.audit_events.list()
                    if audit["action"] == "interview.appointment_window_expired"]) == 1
    assert service.expire_overdue_interviews() == []
    if already_scored:
        report = service._process_report_work(reports[0]["id"], "org_default")
        assert report["overall_score"] is None
        assert report["question_evaluations"][0]["score"] == 80


def test_legacy_candidate_stop_preserves_scores_and_marks_the_unfinished_fixed_plan():
    source = scored(submit(select(make_session(), "q1").session).session, 100).session
    source = select(source, "q2").session
    source["plan_snapshot"]["execution_schema_version"] = 2
    with pytest.raises(ApiError):
        end(source, "evidence_sufficient")
    stopped = end(source, "candidate_requested")
    assert stopped.session["status"] == "report_generating"
    assert stopped.session["turns"][-1]["status"] == "skipped"
    assert stopped.session["candidate_input_completion_reason"] == "candidate_requested"
    assert stopped.effects[0]["type"] == "report.requested"
    report = ReportService(InMemoryStore()).build_report(stopped.session, trigger_reason="candidate_requested")
    assert report["overall_score"] is None
    assert report["question_evaluations"][0]["score"] == 100
    assert report["coverage_status"] == "insufficient_evidence"
    assert report["job_fit_level"] == "insufficient_evidence"
    assert project_report(report, stopped.session)["overall_score"] is None
    assert report["assessment_coverage"]["planned_root_count"] == 2


def test_review_projects_frozen_scope_and_reason_before_report_is_ready():
    from app.services.review import EnterpriseReviewService

    source = submit(select(make_session(), "q1").session).session
    source = end(source, "candidate_requested").session
    source["candidate"] = {"id": "candidate", "name": "合成候选人"}
    source["agent_runtime"] = {"last_planning": {"decision_id": "safe", "reason_code": "coverage_gap",
        "tools_used": ["questions.search"], "model_calls": 1, "prompt_version": "interviewer_supervisor.v1",
        "raw_prompt": "sensitive input", "full_model_response": "unapproved model output"}}
    store = InMemoryStore()
    service = EnterpriseReviewService(store)
    with service.persistence.transaction("org_default") as transaction:
        transaction.interview_sessions.add(source)
    review = service.get_review(source["id"])
    assert review["execution_schema_version"] == 3
    assert review["candidate_input_completion_reason"] == "candidate_requested"
    assert review["turns"][0]["competency_ids"] == ["python"]
    assert review["turns"][0]["assessed_rubric_point_ids"] == ["kp1"]
    assert review["turns"][0]["presented_unit_ids"] == ["complete_question"]
    assert review["planning"]["last_decision"]["reason_code"] == "coverage_gap"
    assert "raw_prompt" not in review["planning"]["last_decision"]
    assert "full_model_response" not in review["planning"]["last_decision"]
