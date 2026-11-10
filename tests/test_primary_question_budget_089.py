"""Combined-source limits exercise plan freezing and lifecycle enforcement."""
from copy import deepcopy
import hashlib
import json

import pytest

from app.core.errors import ApiError
from app.core.prompt.inquiry_units import validate_inquiry_units
from app.domain.adaptive_interview import (
    budget_summary, coverage_summary, freeze_assessment_contract, frozen_candidate,
    initialize_adaptive_snapshot, validate_assessment_contract,
)
from app.domain.interview_lifecycle import LifecycleCommand, LifecycleCommandType as Kind
from app.domain.primary_question_budget import primary_question_budget
from tests.test_adaptive_interview import LIFECYCLE, NOW, make_session, submit, end


def synthetic_pool():
    # 41 distinct competencies require 28 disjoint groups; 56 approved units
    # duplicate those mappings. Nine bank and three resume source documents.
    groups = [["c%d" % (index * 2), "c%d" % (index * 2 + 1)] for index in range(13)]
    groups += [["c%d" % index] for index in range(26, 41)]
    counts = [6, 6, 5, 5, 5, 5, 5, 5, 5, 3, 3, 3]
    candidates, cursor = [], 0
    for index, count in enumerate(counts):
        mappings = [groups[(cursor + offset) % 28] for offset in range(count)]
        cursor += count
        names = sorted({name for mapping in mappings for name in mapping})
        source = {"id": "source_%s" % index, "version": 1, "question_text": "请说明虚构系统的处理边界。",
                  "standard_answer": "应明确边界并验证输入。", "skills": names,
                  "key_points": [{"id": "p%s" % offset, "text": "核实边界"} for offset in range(count)],
                  "competency_ids": names}
        candidate = frozen_candidate(source, competency_ids=names,
            source_type="position_bank" if index < 9 else "resume_experience")
        data = {"questions": [{"question_id": source["id"], "units": [
            {"question_text": "你会怎样验证此项处理边界？", "assessed_rubric_point_ids": ["p%s" % offset],
             "answer_excerpt_start": "a1", "answer_excerpt_end": "a1", "competency_ids": mapping}
            for offset, mapping in enumerate(mappings)]}]}
        candidate["inquiry_units"] = validate_inquiry_units(data, [source])[source["id"]]
        candidates.append(candidate)
    return candidates


def bank_contract():
    return freeze_assessment_contract(role={"id": None, "version": None}, candidates=synthetic_pool(),
        dimension_weights={"c%d" % index: 1 for index in range(41)}, question_count=10, duration_minutes=45,
        policy={"min_root_questions": 1, "min_evidence_units_per_competency": 1},
        assessment_basis={"kind": "question_bank", "job_position_id": "position", "position_version": 1,
                          "knowledge_base_ids": ["bank"]})


def historical_contract():
    contract = bank_contract()
    # Historical fixture is explicitly v1; new preparation freezes a bounded
    # v2 session scope and must not be rewritten to simulate old storage.
    contract.pop("assessment_scope", None)
    contract.update(schema_version="interview_assessment.v1", scoring_policy="fixed_competencies.v1")
    contract["competencies"] = [{"id": "c%d" % index, "weight": 1 / 41,
        "required": True, "min_evidence_roots": 1} for index in range(41)]
    contract["budget"].update(min_root_questions=28, max_root_questions=28)
    contract["coverage_policy"] = "answered_roots.v1"
    body = {key: value for key, value in contract.items() if key != "contract_hash"}
    contract["contract_hash"] = "sha256:" + hashlib.sha256(json.dumps(body, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    validate_assessment_contract(contract)
    return contract


def session_with(contract):
    session = make_session()
    session["plan_snapshot"] = initialize_adaptive_snapshot({"assessment_contract": contract})
    return session


def choose(session, candidate, unit):
    return LIFECYCLE.execute(session, LifecycleCommand(Kind.SELECT_NEXT, {
        "decision_id": "choose_" + unit["id"], "expected_decision_revision": session.get("decision_revision", 0),
        "question_id": candidate["question_id"], "inquiry_unit_id": unit["id"]}), now=NOW)


def test_bank_preparation_never_inflates_ten_total_topics_to_cover_41_labels():
    contract = bank_contract()
    assert len(contract["assessment_scope"]["available_competency_ids"]) == 41
    assert sum(len(row["inquiry_units"]) for row in contract["candidate_questions"]) == 56
    assert contract["budget"]["min_root_questions"] == 10
    assert contract["budget"]["max_root_questions"] == 10
    assert contract["coverage_policy"] == "session_scope_answered_roots.v1"
    assert all(item["required"] for item in contract["competencies"])
    assert sum(item["weight"] for item in contract["competencies"]) == pytest.approx(1)


def _role_contract_with_pool(count, policy=None):
    candidates = [frozen_candidate({"id": "question_%s" % index, "version": 1,
        "question_text": "请说明你会怎样验证系统边界。", "standard_answer": "需要核实真实的边界。",
        "key_points": [{"id": "boundary", "text": "系统边界"}]},
        competency_ids=["python"], source_type="position_bank") for index in range(count)]
    return freeze_assessment_contract(role={"id": "role", "version": 1}, candidates=candidates,
        dimension_weights={"python": 1}, question_count=10, duration_minutes=45, policy=policy)


def test_role_based_generate_also_defaults_to_eight_to_ten_total_topics():
    budget = _role_contract_with_pool(12)["budget"]
    assert (budget["min_root_questions"], budget["max_root_questions"]) == (8, 10)


def test_small_material_pool_and_explicit_low_minimum_remain_compatible():
    few = _role_contract_with_pool(3)["budget"]
    explicit = _role_contract_with_pool(12, {"min_root_questions": 2})["budget"]
    assert (few["min_root_questions"], few["max_root_questions"]) == (3, 3)
    assert (explicit["min_root_questions"], explicit["max_root_questions"]) == (2, 10)


def test_legacy_contract_stays_frozen_mixed_sources_share_ten_and_followups_do_not_count():
    contract = historical_contract()
    session = session_with(contract)
    pool = contract["candidate_questions"]
    selections = [(row, row["inquiry_units"][0]) for row in pool[:7] + pool[9:]]
    for index, (candidate, unit) in enumerate(selections):
        decision = choose(session, candidate, unit)
        session = decision.session
        if index == 0:
            assert [event["type"] for event in decision.events].count("interview.question_budget_applied") == 1
        if index == 9:
            assert budget_summary(session, NOW)["exhausted"]
            with pytest.raises(ApiError):
                end(session, "budget_exhausted")  # Last answer must remain open.
        session = submit(session).session
        if index == 3:
            root = session["turns"][-1]
            session["turns"].extend([{**deepcopy(root), "id": "followup_%s" % number,
                                     "is_followup": True} for number in range(2)])
    assert len(session["turns"]) == 12
    assert budget_summary(session, NOW)["remaining_root_questions"] == 0
    assert session["plan_snapshot"]["assessment_contract"] == contract
    assert session["execution_question_budget"] == {
        "policy_version": "combined_primary_questions.v1", "max_root_questions": 10,
        "frozen_max_root_questions": 28, "applied_at": NOW}
    with pytest.raises(ApiError) as error:
        choose(session, pool[7], pool[7]["inquiry_units"][0])
    assert error.value.code == "ASSESSMENT_BUDGET_EXHAUSTED"
    ended = end(session, "budget_exhausted").session
    assert ended["plan_snapshot"]["assessment_contract"] == contract
    assert ended["assessment_coverage"]["sufficient"] is False
    assert ended["assessment_coverage"]["missing_required_competencies"]
    assert len(ended["answers"]) == 10


def test_old_completed_interview_is_not_reinterpreted_as_having_ten_question_budget():
    session = session_with(historical_contract())
    session.update(status="report_ready", candidate_input_completed_at=NOW)
    before = deepcopy(session)
    assert primary_question_budget(session)["max_root_questions"] == 28
    assert session == before


def test_active_legacy_fixed_plan_skips_only_unstarted_roots_after_tenth_answer():
    session = {"id": "fixed", "organization_id": "org_default", "status": "in_progress", "plan_snapshot": {}, "answers": [],
               "turns": [{"id": "root_%s" % index, "order": index, "is_followup": False,
                          "status": "answered", "started_at": NOW, "weight": 1 / 12}
                         for index in range(10)]}
    session["turns"] += [{"id": "pending_%s" % index, "order": index + 10, "is_followup": False,
                           "status": "pending", "started_at": None, "weight": 1 / 12}
                          for index in range(2)]
    session["turns"].append({"id": "followup", "order": 12, "is_followup": True, "status": "pending"})
    original_roots = deepcopy(session["turns"][:10])
    events = []
    following = LIFECYCLE._next_playable_turn(session, NOW, events)
    assert following["id"] == "followup"
    assert session["turns"][:10] == original_roots
    assert all(turn["skip_reason"] == "primary_question_budget_exhausted" for turn in session["turns"][10:12])
    assert all(turn["weight"] == 1 / 12 for turn in session["turns"][:12])
    assert [event["type"] for event in events].count("interview.question_budget_applied") == 1


@pytest.mark.parametrize("field", ["question_count", "adaptive_maximum"])
def test_new_plan_request_does_not_accept_more_than_ten(field):
    from pydantic import ValidationError
    from app.schemas.api import InterviewPlanGenerateRequest
    payload = {"role_requirement_id": "role", "job_position_id": "position", "candidate_profile_id": "candidate",
               "knowledge_base_ids": ["bank"], "question_count": 10, "execution_schema_version": 3}
    if field == "question_count":
        payload["question_count"] = 11
    else:
        payload["adaptive_policy"] = {"max_root_questions": 11}
    with pytest.raises(ValidationError):
        InterviewPlanGenerateRequest.model_validate(payload)


def test_execution_budget_changes_invalidate_prepared_planning_context():
    from app.services.interviewer_supervisor.context import context_fingerprint
    source = session_with(historical_contract())
    prior = context_fingerprint(source)
    source["execution_question_budget"] = {"policy_version": "combined_primary_questions.v1",
        "max_root_questions": 10, "frozen_max_root_questions": 28, "applied_at": NOW}
    assert context_fingerprint(source) != prior


def test_fixed_plan_budget_skip_is_unassessed_not_a_zero_or_candidate_refusal():
    from app.domain.scoring_quality import project_report
    from app.repositories.memory import InMemoryStore
    from app.services.reports import ReportService
    from tests.test_exit_report_notice_065 import _withdrawn_session
    source = _withdrawn_session(2)
    source["candidate_input_completion_reason"] = "budget_exhausted"
    for turn in source["turns"]:
        if turn["status"] == "skipped":
            turn["skip_reason"] = "primary_question_budget_exhausted"
    before = deepcopy(source)
    report = ReportService(InMemoryStore()).build_report(source, trigger_reason="budget_exhausted")
    assert report["overall_score"] is None
    assert report["question_evaluations"][0]["score"] == 91
    assert report["coverage_status"] == "insufficient_evidence"
    assert report["skipped_questions"][0]["counts_toward_score"] is False
    assert report["candidate_input_completion_reason"] == "budget_exhausted"
    assert all("主动" not in risk for risk in report["risks"])
    assert project_report(report, source)["overall_score"] is None
    assert source == before


def test_old_fixed_plan_reaches_limit_only_after_active_answer_is_submitted():
    from tests.test_interview_lifecycle import started_session, answer
    lifecycle, source = started_session(12)
    # Model an already running pre-release session, which has no override yet.
    source.pop("execution_question_budget", None)
    for index in range(10):
        active = source["current_turn_id"]
        assert active == "turn_%s" % (index + 1)
        source = lifecycle.execute(source, LifecycleCommand(Kind.ANSWER_SUBMITTED,
            {"answer": answer("answer_%s" % index, active)}), now=NOW).session
    assert source["current_turn_id"] is None
    assert len(source["answers"]) == 10
    assert all(turn["status"] == "skipped" for turn in source["turns"][10:])
    assert all(turn.get("started_at") is None for turn in source["turns"][10:])
    assert source["candidate_input_completed_at"] == NOW


def test_pending_fixed_blueprint_with_legacy_start_time_never_consumes_a_question():
    from tests.test_interview_lifecycle import started_session, answer
    lifecycle, source = started_session(2)
    # A copied legacy blueprint may retain metadata from its source question.
    source["turns"][1]["started_at"] = source["turns"][0]["started_at"]
    source = lifecycle.execute(source, LifecycleCommand(Kind.ANSWER_SUBMITTED,
        {"answer": answer("answer_first", source["current_turn_id"])}), now=NOW).session
    assert source["current_turn_id"] == "turn_2"
    assert not source.get("candidate_input_completed_at")
    assert source["turns"][1]["status"] == "asking"
    assert len(source["answers"]) == 1


def test_fixed_schema_uses_root_blueprints_not_an_unused_adaptive_contract():
    from tests.test_interview_lifecycle import started_session
    _, source = started_session(2)
    source["plan_snapshot"] = {"execution_schema_version": 2,
        "assessment_contract": {"budget": {"max_root_questions": 1}}}
    assert primary_question_budget(source)["max_root_questions"] == 2


@pytest.mark.anyio
async def test_new_fixed_plan_counts_resume_questions_inside_requested_total(monkeypatch):
    from app.repositories.memory import InMemoryStore
    from app.services.plan_assembly import InterviewPlanAssembly, PlanAssemblyRequest
    from tests.test_plan_assembly import create_scope, add_question
    store = InMemoryStore()
    catalog, position, bank, role, candidate = create_scope(store, "合计预算岗位", ["python"], 45)
    for index in range(8):
        await add_question(catalog, bank["id"], title="主题%s" % index, skill="python", difficulty="mid",
                           key_points=["边界%s" % index])
    assembly = InterviewPlanAssembly(store)
    monkeypatch.setattr(assembly, "_approved_experience_question_snapshots", lambda *args: [
        {"id": "resume_%s" % index, "version": 1, "order": index + 1} for index in range(3)])
    plan = await assembly.assemble(PlanAssemblyRequest(role_requirement_id=role["id"],
        job_position_id=position["id"], candidate_profile_id=candidate["id"], knowledge_base_ids=(bank["id"],),
        question_count=8, execution_schema_version=2))
    assert len(plan["bank_slots"]) == 5
    assert len(plan["experience_question_snapshots"]) == 3
    assert plan["assembly_summary"]["requested_question_count"] == 8
