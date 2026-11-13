"""Approved short questions never inherit unasked long-question penalties."""
from copy import deepcopy

import pytest

from app.core.errors import ApiError
from app.core.prompt.contracts import prompt_contract
from app.core.prompt.inquiry_units import inquiry_units_contract, validate_inquiry_units
from app.core.prompt.validation import validate_structured_response
from app.domain.adaptive_interview import (
    budget_summary, coverage_summary, freeze_assessment_contract, frozen_candidate,
    initialize_adaptive_snapshot,
)
from app.domain.interview_lifecycle import LifecycleCommand, LifecycleCommandType as Kind
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.repositories.memory import InMemoryStore
from app.services.evaluation import EvaluationService
from app.services.reports import ReportService
from tests.test_adaptive_interview import NOW, LIFECYCLE, make_session, scored, end


SOURCE = {
    "id": "long_question", "version": 1,
    "question_text": "请设计大规模后台任务系统，并说明幂等、重试、资源控制、并行模式、监控告警和故障恢复；提供所有参数与代码示例。",
    "standard_answer": "使用幂等键避免重复执行。对暂时故障采用指数退避重试。通过有界并发限制资源使用。",
    "key_points": [{"id": "p1", "text": "幂等键", "weight": 1}, {"id": "p2", "text": "指数退避", "weight": 1},
                   {"id": "p3", "text": "并发上限", "weight": 1}],
    "rubric": {"semantic_correctness": 1, "followup_probes": ["继续列出所有监控参数。"]},
    "skills": ["python"], "speech_asset_id": "original_long_audio",
    "competency_ids": ["python"],
}
PROPOSAL = {"questions": [{"question_id": "long_question", "units": [
    {"question_text": "任务重复投递时，你如何避免重复执行？", "assessed_rubric_point_ids": ["p1"],
     "answer_excerpt_start": "a1", "answer_excerpt_end": "a1"},
    {"question_text": "调用暂时失败时，你会怎样安排重试？", "assessed_rubric_point_ids": ["p2"],
     "answer_excerpt_start": "a2", "answer_excerpt_end": "a2"},
    {"question_text": "大量任务积压时，你怎样控制资源使用？", "assessed_rubric_point_ids": ["p3"],
     "answer_excerpt_start": "a3", "answer_excerpt_end": "a3"},
]}]}
for _unit in PROPOSAL["questions"][0]["units"]:
    _unit["competency_ids"] = ["python"]


def unit_session():
    units = validate_inquiry_units(PROPOSAL, [SOURCE])[SOURCE["id"]]
    candidate = frozen_candidate(SOURCE, competency_ids=["python"], source_type="position_bank", expected_minutes=9)
    candidate["inquiry_units"] = units
    contract = freeze_assessment_contract(role={"id": "role", "version": 1}, candidates=[candidate],
        dimension_weights={"python": 1}, question_count=3, duration_minutes=10,
        policy={"min_root_questions": 2, "max_root_questions": 3})
    session = make_session()
    session["plan_snapshot"] = initialize_adaptive_snapshot({"assessment_contract": contract})
    return session, units


def select_unit(session, unit):
    return LIFECYCLE.execute(session, LifecycleCommand(Kind.SELECT_NEXT, {
        "decision_id": "select_" + unit["id"], "expected_decision_revision": session["decision_revision"],
        "question_id": "long_question", "inquiry_unit_id": unit["id"]}), now=NOW).session


def submit_unit(session):
    turn = next(turn for turn in session["turns"] if turn["id"] == session["current_turn_id"])
    answer = {"id": "answer_" + turn["inquiry_unit_id"], "interview_id": session["id"], "turn_id": turn["id"],
        "question_id": turn["question_id"], "question_snapshot_id": turn["question_snapshot_id"],
        "evaluation_status": "pending", "current_evaluation_id": None, "created_at": NOW, "updated_at": NOW,
        "final_transcript": "我会用幂等键防止重复执行。"}
    return LIFECYCLE.execute(session, LifecycleCommand(Kind.ANSWER_SUBMITTED, {"answer": answer}), now=NOW).session


def test_short_unit_snapshot_has_only_presented_scoring_scope_and_no_long_audio():
    session, units = unit_session()
    with pytest.raises(ApiError):
        LIFECYCLE.execute(session, LifecycleCommand(Kind.SELECT_NEXT, {
            "decision_id": "missing_unit", "expected_decision_revision": 0, "question_id": "long_question"}), now=NOW)
    selected = select_unit(session, units[0])
    snapshot = selected["turns"][0]["question_snapshot"]
    assert snapshot["spoken_text"] == "任务重复投递时，你如何避免重复执行？"
    assert snapshot["spoken_text"] != SOURCE["question_text"]
    assert snapshot["speech_asset_id"] is None
    assert snapshot["key_points"] == SOURCE["key_points"][:1]
    assert snapshot["standard_answer"] == "使用幂等键避免重复执行。"
    assert snapshot["rubric"] == {"semantic_correctness": 1}
    assert snapshot["followup_probes"] == []
    assert snapshot["not_assessed_rubric_point_ids"] == ["p2", "p3"]
    assert snapshot["presented_unit_ids"] == [units[0]["id"]]
    assert selected["turns"][0]["expected_minutes"] == 3
    assert selected["plan_snapshot"]["assessment_contract"]["competencies"][0]["min_evidence_roots"] == 2


def test_units_are_individually_selectable_but_a_closed_source_topic_stays_closed():
    session, units = unit_session()
    first = submit_unit(select_unit(session, units[0]))
    with pytest.raises(ApiError):
        LIFECYCLE.execute(first, LifecycleCommand(Kind.SELECT_NEXT, {
            "decision_id": "again", "expected_decision_revision": first["decision_revision"],
            "question_id": "long_question", "inquiry_unit_id": units[0]["id"]}), now=NOW)
    second = select_unit(first, units[1])
    assert len(second["turns"]) == 2
    assert second["turns"][0]["question_id"] == second["turns"][1]["question_id"]
    assert second["turns"][0]["question_snapshot_id"] != second["turns"][1]["question_snapshot_id"]
    first["turns"][0]["current_understanding"] = {"turn_intent": "decline_topic", "intent": "answer_declined"}
    with pytest.raises(ApiError):
        select_unit(first, units[1])
    assert budget_summary(first, NOW)["remaining_eligible_units"] == 0
    assert end(first, "budget_exhausted").session["candidate_input_completion_reason"] == "budget_exhausted"


def test_one_unit_cannot_claim_complete_question_or_sufficient_competency_evidence():
    session, units = unit_session()
    first = scored(submit_unit(select_unit(session, units[0])), 90).session
    assert not coverage_summary(first)["sufficient"]
    with pytest.raises(ApiError):
        end(first, "evidence_sufficient")
    interrupted = end(first, "candidate_requested").session
    report = ReportService(InMemoryStore()).build_report(interrupted, trigger_reason="test")
    assert report["overall_score"] is None
    assert report["question_evaluations"][0]["score"] == 90
    assert report["question_evaluations"][0]["not_assessed_rubric_point_ids"] == ["p2", "p3"]
    assert report["source_question_scopes"] == [{
        "source_question_id": "long_question", "assessed_rubric_point_ids": ["p1"],
        "not_assessed_rubric_point_ids": ["p2", "p3"], "assessed_point_count": 1, "approved_point_count": 3,
        "scope_status": "partial"}]
    second = scored(submit_unit(select_unit(first, units[1])), 70).session
    finished = end(second, "evidence_sufficient").session
    complete = ReportService(InMemoryStore()).build_report(finished, trigger_reason="test")
    assert complete["overall_score"] == 80
    assert complete["source_question_scopes"][0]["not_assessed_rubric_point_ids"] == ["p3"]
    assert complete["source_question_scopes"][0]["scope_status"] == "partial"
    assert "只评价实际呈现" in complete["scope_notice"]


def test_declining_a_followup_closes_every_remaining_unit_of_its_source_question():
    from app.services.interviews import InterviewService

    session, units = unit_session()
    session = submit_unit(select_unit(session, units[0]))
    session["followup_policy"] = {"max_total": 3, "max_per_root": 1, "max_depth": 1}
    root = session["turns"][0]
    child = InterviewService(InMemoryStore())._build_followup_turn(session, root, session["answers"][0], {
        "root_turn_id": root["id"], "question_text": "你会怎样选择幂等键？",
        "target_key_points": ["幂等键"], "reason": "missing_key_points"}, now=NOW)
    session = LIFECYCLE.execute(session, LifecycleCommand(Kind.FOLLOWUP_REQUESTED, {
        "root_turn_id": root["id"], "followup_turn": child}), now=NOW).session
    session["turns"][-1]["current_understanding"] = {"turn_intent": "decline_topic", "intent": "answer_declined"}
    answer = {**deepcopy(session["answers"][0]), "id": "followup_answer", "turn_id": child["id"],
              "question_id": child["question_id"], "question_snapshot_id": child["question_snapshot_id"],
              "final_transcript": "这部分我不会，我们换个话题吧。"}
    session = LIFECYCLE.execute(session, LifecycleCommand(Kind.ANSWER_SUBMITTED, {"answer": answer}), now=NOW).session
    assert budget_summary(session, NOW)["remaining_eligible_units"] == 0
    with pytest.raises(ApiError):
        select_unit(session, units[1])
    assert end(session, "budget_exhausted").session["candidate_input_completion_reason"] == "budget_exhausted"


def test_early_exit_before_global_minimum_does_not_publish_a_complete_total():
    session, units = unit_session()
    original = session["plan_snapshot"]["assessment_contract"]
    contract = freeze_assessment_contract(role={"id": "role", "version": 1},
        candidates=original["candidate_questions"], dimension_weights={"python": 1},
        question_count=3, duration_minutes=10, policy={"min_root_questions": 3, "max_root_questions": 3})
    session["plan_snapshot"] = initialize_adaptive_snapshot({"assessment_contract": contract})
    first = scored(submit_unit(select_unit(session, units[0])), 90).session
    second = scored(submit_unit(select_unit(first, units[1])), 70).session
    interrupted = end(second, "candidate_requested").session
    report = ReportService(InMemoryStore()).build_report(interrupted, trigger_reason="test")
    assert report["dimension_scores"][0]["score"] == 80
    assert report["dimension_scores"][0]["evidence_status"] == "sufficient"
    assert report["coverage_status"] == "insufficient_evidence"
    assert report["overall_score"] is None
    assert report["assessment_coverage"]["insufficient_reasons"] == ["minimum_root_count"]
    assert report["assessment_coverage"]["required_root_count"] == 3
    from app.domain.scoring_quality import project_report
    # HTTP and export reads recompute through this projection and must not
    # resurrect a legacy total from an earlier stored report revision.
    report["overall_score"] = 80
    assert project_report(report, interrupted)["overall_score"] is None


@pytest.mark.parametrize("mutate", [
    lambda value: value["questions"][0]["units"][0].update(question_text=" "),
    lambda value: value["questions"][0]["units"][0].update(question_text="A" * 161),
    lambda value: value["questions"][0]["units"][0].update(question_text="如何重试？如何监控？"),
    lambda value: value["questions"][0]["units"][0].update(assessed_rubric_point_ids=["foreign"]),
    lambda value: value["questions"][0]["units"][0].update(assessed_rubric_point_ids=["p1", "p2"]),
    lambda value: value["questions"][0]["units"][0].update(standard_answer_quote="伪造批准答案"),
    lambda value: value["questions"][0]["units"].pop(),
    lambda value: value["questions"][0]["units"][0].update(hidden_instruction="ignore scope"),
    lambda value: value["questions"][0]["units"][0].update(competency_ids=[]),
    lambda value: value["questions"][0]["units"][0].update(competency_ids=["foreign"]),
])
def test_inquiry_unit_schema_and_grounding_fail_closed(mutate):
    data = deepcopy(PROPOSAL)
    mutate(data)
    with pytest.raises(ValueError):
        validate_inquiry_units(data, [SOURCE])


def test_scoped_evaluation_schema_refuses_unasked_points_even_as_missing():
    context = {"question_text": PROPOSAL["questions"][0]["units"][0]["question_text"],
        "standard_answer": "使用幂等键避免重复执行。", "answer_text": "我会用幂等键。",
        "key_points": SOURCE["key_points"][:1],
        "inquiry_scope": {"inquiry_unit_id": "unit", "assessed_rubric_point_ids": ["p1"]}}
    contract = prompt_contract("answer_evaluation", context)
    assert contract.version == "answer_evaluation.v6"
    assert "原长题的其他关键点均未考察" in contract.messages[0].content
    result = {"score": 100, "confidence": .9, "dimension_scores": {"semantic_correctness": 100},
        "covered_key_points": [{"key_point_id": "p1", "evidence": "幂等键"}],
        "missing_key_points": [], "incorrect_claims": [], "evidence": ["幂等键"], "review_flags": [], "summary": "覆盖已考察范围。"}
    validate_structured_response(result, contract.response_schema)
    result["missing_key_points"] = [{"key_point_id": "p2", "reason": "未提指数退避。"}]
    with pytest.raises(ValueError):
        validate_structured_response(result, contract.response_schema)


def test_followup_cannot_expand_the_unit_rubric_and_keeps_zero_weight():
    from app.services.interviews import InterviewService

    source, units = unit_session()
    source = submit_unit(select_unit(source, units[0]))
    source["followup_policy"] = {"max_total": 3, "max_per_root": 1, "max_depth": 1}
    root = source["turns"][0]
    followup = InterviewService(InMemoryStore())._build_followup_turn(source, root, source["answers"][0], {
        "root_turn_id": root["id"], "question_text": "你会怎样选择幂等键？",
        "target_key_points": ["幂等键"], "reason": "missing_key_points"}, now=NOW)
    wrong = deepcopy(followup)
    wrong["question_snapshot"]["key_points"].append(SOURCE["key_points"][1])
    with pytest.raises(ApiError):
        LIFECYCLE.execute(source, LifecycleCommand(Kind.FOLLOWUP_REQUESTED, {
            "root_turn_id": root["id"], "followup_turn": wrong}), now=NOW)
    accepted = LIFECYCLE.execute(source, LifecycleCommand(Kind.FOLLOWUP_REQUESTED, {
        "root_turn_id": root["id"], "followup_turn": followup}), now=NOW).session
    child = accepted["turns"][-1]
    assert child["weight"] == 0
    assert child["inquiry_unit_id"] == root["inquiry_unit_id"]
    assert child["question_snapshot"]["assessed_rubric_point_ids"] == ["p1"]
    assert child["question_snapshot"]["not_assessed_rubric_point_ids"] == ["p2", "p3"]
    assert not coverage_summary(accepted)["sufficient"]


@pytest.mark.parametrize("newer_answered", [False, True])
def test_late_followup_cannot_reopen_an_older_turn(newer_answered):
    from app.services.interviews import InterviewService

    session, units = unit_session()
    session = submit_unit(select_unit(session, units[0]))
    root = session["turns"][0]
    child = InterviewService(InMemoryStore())._build_followup_turn(session, root, session["answers"][0], {
        "root_turn_id": root["id"], "question_text": "你会怎样选择幂等键？",
        "target_key_points": ["幂等键"], "reason": "missing_key_points"}, now=NOW)
    newer = select_unit(session, units[1])
    if newer_answered:
        newer = submit_unit(newer)
    with pytest.raises(ApiError, match="late follow-up"):
        LIFECYCLE.execute(newer, LifecycleCommand(Kind.FOLLOWUP_REQUESTED, {
            "root_turn_id": root["id"], "followup_turn": child}), now=NOW)
    assert len(newer["turns"]) == 2


@pytest.mark.parametrize("per_root,total,allowed", [(3, 3, 3), (3, 2, 2), (2, 3, 2)])
def test_v3_followups_honor_frozen_budgets_instead_of_legacy_caps(per_root, total, allowed):
    from app.services.interviews import InterviewService

    session, units = unit_session()
    contract = freeze_assessment_contract(role={"id": "role", "version": 1},
        candidates=session["plan_snapshot"]["assessment_contract"]["candidate_questions"],
        dimension_weights={"python": 1}, question_count=3, duration_minutes=10,
        policy={"max_followups_per_root": per_root, "max_total_followups": total})
    session["plan_snapshot"] = initialize_adaptive_snapshot({"assessment_contract": contract})
    session = submit_unit(select_unit(session, units[0]))
    root_id = session["turns"][0]["id"]
    service = InterviewService(InMemoryStore())
    for index in range(allowed + 1):
        parent, answer = session["turns"][-1], session["answers"][-1]
        child = service._build_followup_turn(session, parent, answer, {
            "root_turn_id": root_id, "question_text": "你会怎样选择幂等键？",
            "target_key_points": ["幂等键"], "reason": "missing_key_points"}, now=NOW)
        command = LifecycleCommand(Kind.FOLLOWUP_REQUESTED, {"root_turn_id": root_id, "followup_turn": child})
        if index == allowed:
            with pytest.raises(ApiError, match="budget"):
                LIFECYCLE.execute(session, command, now=NOW)
            break
        session = LIFECYCLE.execute(session, command, now=NOW).session
        accepted_answer = {**deepcopy(answer), "id": "child_answer_%s" % index, "turn_id": child["id"],
            "question_id": child["question_id"], "question_snapshot_id": child["question_snapshot_id"]}
        session = LIFECYCLE.execute(session, LifecycleCommand(Kind.ANSWER_SUBMITTED,
            {"answer": accepted_answer}), now=NOW).session
    assert len([turn for turn in session["turns"] if turn.get("is_followup")]) == allowed
    assert all(turn["weight"] == 0 for turn in session["turns"][1:])


def overlapping_competency_candidate():
    source = deepcopy(SOURCE)
    source["competency_ids"] = list("abcdef")
    proposal = deepcopy(PROPOSAL)
    for unit, mapping in zip(proposal["questions"][0]["units"], ["abcd", "abe", "cdf"]):
        unit["competency_ids"] = list(mapping)
    candidate = frozen_candidate(source, competency_ids=source["competency_ids"], source_type="position_bank")
    candidate["inquiry_units"] = validate_inquiry_units(proposal, [source])[source["id"]]
    return candidate


def test_coverage_budget_uses_the_true_minimum_for_overlapping_competencies():
    candidate = overlapping_competency_candidate()
    settings = dict(role={"id": "role", "version": 1}, candidates=[candidate],
                    dimension_weights={name: 1 for name in "abcdef"}, question_count=2, duration_minutes=10)
    contract = freeze_assessment_contract(**settings,
        policy={"max_root_questions": 2, "min_evidence_units_per_competency": 1})
    # The largest unit covers abcd, but selecting abe + cdf covers all six
    # competencies in two roots. A greedy estimate would incorrectly need three.
    assert contract["budget"]["min_root_questions"] == 2
    with pytest.raises(ApiError) as error:
        freeze_assessment_contract(**settings,
            policy={"max_root_questions": 1, "min_evidence_units_per_competency": 1})
    assert error.value.code == "ASSESSMENT_COVERAGE_BUDGET_INVALID"
    assert error.value.details["minimum_required_root_questions"] == 2


def test_bounded_coverage_search_does_not_claim_infeasibility_when_exhausted(monkeypatch):
    monkeypatch.setattr("app.domain.adaptive_interview._COVERAGE_MAX_SEARCH_NODES", 0)
    with pytest.raises(ApiError) as error:
        freeze_assessment_contract(role={"id": "role", "version": 1}, candidates=[overlapping_competency_candidate()],
            dimension_weights={name: 1 for name in "abcdef"}, question_count=2, duration_minutes=10,
            policy={"max_root_questions": 2, "min_evidence_units_per_competency": 1})
    assert error.value.code == "ASSESSMENT_COVERAGE_SEARCH_LIMIT"
    assert "minimum_required_root_questions" not in error.value.details


def test_minimum_coverage_matches_exhaustive_small_multicover_cases():
    from itertools import combinations
    from app.domain.adaptive_interview import _minimum_evidence_units

    mappings = [("a",), ("b",), ("c",), ("a", "b"), ("a", "c"), ("b", "c"), ("a", "b", "c")]
    for distinct in combinations(mappings, 4):
        groups = [*distinct, distinct[0]]  # Preserve separate units with equal competency mappings.
        for minimum in (1, 2):
            needs = {name: min(minimum, sum(name in group for group in groups)) for name in "abc"}
            if not all(needs.values()):
                continue
            expected = next(size for size in range(1, len(groups) + 1)
                            if any(all(sum(name in group for group in selection) >= need
                                       for name, need in needs.items()) for selection in combinations(groups, size)))
            actual = _minimum_evidence_units([{"id": name, "min_evidence_roots": need} for name, need in needs.items()],
                [{"inquiry_units": [{"competency_ids": list(group)} for group in groups]}])
            assert actual == expected


def test_units_do_not_credit_other_competencies_from_the_source_question_tags():
    source = deepcopy(SOURCE)
    source["competency_ids"] = ["python", "database"]
    response = deepcopy(PROPOSAL)
    response["questions"][0]["units"][2]["competency_ids"] = ["database"]
    units = validate_inquiry_units(response, [source])[source["id"]]
    candidate = frozen_candidate(source, competency_ids=source["competency_ids"], source_type="position_bank")
    candidate["inquiry_units"] = units
    contract = freeze_assessment_contract(role={"id": "role", "version": 1}, candidates=[candidate],
        dimension_weights={"python": .75, "database": .25}, question_count=3, duration_minutes=10)
    assert {item["id"]: item["min_evidence_roots"] for item in contract["competencies"]} == {"database": 1, "python": 2}
    assert contract["budget"]["min_root_questions"] == 3
    session = make_session()
    session["plan_snapshot"] = initialize_adaptive_snapshot({"assessment_contract": contract})
    one = scored(submit_unit(select_unit(session, units[0])), 100).session
    assert one["turns"][0]["competency_ids"] == ["python"]
    assert one["plan_snapshot"]["question_snapshots"][0]["competency_ids"] == ["python"]
    assert "database" in coverage_summary(one)["missing_required_competencies"]
    two = scored(submit_unit(select_unit(one, units[1])), 80).session
    partial = ReportService(InMemoryStore()).build_report(end(two, "candidate_requested").session, trigger_reason="test")
    assert partial["overall_score"] is None
    assert partial["dimension_scores"][0]["dimension"] == "database"
    assert partial["dimension_scores"][0]["score"] is None
    three = scored(submit_unit(select_unit(two, units[2])), 10).session
    final = ReportService(InMemoryStore()).build_report(end(three, "evidence_sufficient").session, trigger_reason="test")
    assert final["overall_score"] == 70
    assert final["dimension_scores"][0]["score"] == 10
    assert final["dimension_scores"][1]["score"] == 90


def test_inquiry_unit_cannot_borrow_another_sources_competency_in_a_batch():
    other = deepcopy(SOURCE)
    other.update(id="security_source", competency_ids=["security"])
    data = deepcopy(PROPOSAL)
    duplicate = deepcopy(PROPOSAL["questions"][0])
    duplicate["question_id"] = other["id"]
    for unit in duplicate["units"]:
        unit["competency_ids"] = ["security"]
    data["questions"].append(duplicate)
    data["questions"][0]["units"][0]["competency_ids"] = ["security"]
    # security is legal in the batch Schema; owning-source validation must
    # still prevent it from appearing on the Python question's unit.
    validate_structured_response(data, inquiry_units_contract([SOURCE, other]).response_schema)
    with pytest.raises(ValueError, match="original approved source"):
        validate_inquiry_units(data, [SOURCE, other])


@pytest.mark.anyio
async def test_evaluation_only_receives_scoped_reference_and_contract():
    class Gateway:
        request = None

        async def invoke(self, capability, request):
            self.request = request
            data = {"score": 100, "confidence": .9, "dimension_scores": {"semantic_correctness": 100},
                "covered_key_points": [{"key_point_id": "p1", "evidence": "幂等键"}], "missing_key_points": [],
                "incorrect_claims": [], "evidence": ["幂等键"], "review_flags": [], "summary": "已覆盖当前单元。"}
            validate_structured_response(data, request.json_schema)
            return ChatJSONResponse(data=data, usage=Usage(), provider=ProviderMeta(
                provider_id="synthetic", model="synthetic", request_id="request", latency_ms=0))

    session, units = unit_session()
    selected = submit_unit(select_unit(session, units[0]))
    gateway = Gateway()
    evaluation = await EvaluationService(InMemoryStore(), gateway=gateway).evaluate_answer(
        selected["answers"][0], selected["turns"][0]["question_snapshot"])
    assert gateway.request.metadata["prompt_version"] == "answer_evaluation.v6"
    assert "指数退避" not in gateway.request.messages[-1].content
    assert "并发上限" not in gateway.request.messages[-1].content
    assert evaluation["score"] == 100
    assert evaluation["missing_key_points"] == []
    assert evaluation["not_assessed_rubric_point_ids"] == ["p2", "p3"]


@pytest.mark.anyio
@pytest.mark.parametrize("point_count", [3, 10])
async def test_two_long_sources_can_supply_six_units_and_budget_errors_explain_required_minimum(point_count):
    from tests.test_plan_assembly import create_scope, add_question
    from app.services.plan_assembly import InterviewPlanAssembly, PlanAssemblyRequest

    store = InMemoryStore()
    catalog, position, bank, role, candidate = create_scope(store, "Python Units", ["python"], 20)
    for number in range(2):
        await add_question(catalog, bank["id"], title="任务系统%s" % number, skill="python", difficulty="mid",
                           key_points=["任务边界%s_%s" % (number, point) for point in range(point_count)])
    settings = dict(role_requirement_id=role["id"], job_position_id=position["id"], candidate_profile_id=candidate["id"],
        knowledge_base_ids=(bank["id"],), question_count=6, execution_schema_version=3,
        adaptive_policy={"min_root_questions": 2, "max_root_questions": 6}, approve=True)
    assembly = InterviewPlanAssembly(store)
    plan = await assembly.assemble(PlanAssemblyRequest(**settings))
    assert len(plan["assessment_contract"]["candidate_questions"]) == 2
    assert plan["assessment_contract"]["budget"]["max_root_questions"] == 6
    assert plan["assembly_summary"]["inquiry_unit_count"] == point_count * 2
    assert not any("候选池只有" in warning for warning in plan["assembly_summary"]["warnings"])
    settings["adaptive_policy"] = {"min_root_questions": 1, "max_root_questions": 1}
    with pytest.raises(ApiError) as raised:
        await assembly.assemble(PlanAssemblyRequest(**settings))
    assert raised.value.code == "ASSESSMENT_COVERAGE_BUDGET_INVALID"
    assert raised.value.details["minimum_required_root_questions"] == 2
    assert "至少需要 2 个考察单元" in raised.value.message
