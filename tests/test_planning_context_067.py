"""Bound model input without silently shrinking the approved interview scope."""
from copy import deepcopy
import json
import time

import pytest

from app.core.errors import ApiError
from app.core.interaction_budget import interaction_deadline
from app.core.prompt.interviewer_supervisor import supervisor_contract
from app.domain.adaptive_interview import freeze_assessment_contract, frozen_candidate
from app.services.interviewer_supervisor.context import (
    PLANNING_MEMORY_MAX_CHARS, PLANNING_REQUEST_MAX_CHARS, planning_memory, planning_request_size,
)
from app.services.interviewer_supervisor.tools import (
    CATALOG_PAGE_ITEMS, TOOL_RESULT_MAX_CHARS, InterviewerTools, serialized_size,
)
from test_interviewer_supervisor import Gateway, decision, source_session, supervisor


def large_session(size=300):
    session = source_session()
    candidates = []
    for index in range(size):
        competency = "database" if index == size - 1 else "python"
        candidates.append(frozen_candidate({
            "id": f"q_{index:04d}", "version": 1, "title": f"Question {index}",
            "question_text": "Explain the technical trade-off " + str(index),
            "standard_answer": "Private scoring answer.", "key_points": [{"id": f"kp_{index}", "text": "Trade-off", "weight": 1.0}],
            "skills": [competency],
        }, competency_ids=[competency], source_type="position_bank"))
    session["plan_snapshot"]["assessment_contract"] = freeze_assessment_contract(
        role={"id": "role", "version": 1}, candidates=candidates,
        dimension_weights={"python": 0.5, "database": 0.5}, question_count=3, duration_minutes=10,
        policy={"min_root_questions": 2, "max_root_questions": 3})
    return session


def test_mock_expert_deduplicates_question_ids_across_inquiry_units():
    from types import SimpleNamespace
    from app.core.prompt.interviewer_supervisor import evidence_expert_contract, mock_supervisor_result
    from app.core.prompt.validation import validate_structured_response
    context = {"eligible_questions": [
        {"question_id": "q1", "inquiry_units": [{"id": "u1"}]},
        {"question_id": "q1", "inquiry_units": [{"id": "u2"}]},
        {"question_id": "q2", "inquiry_units": [{"id": "u3"}]},
    ]}
    contract = evidence_expert_contract(context)
    response = mock_supervisor_result(SimpleNamespace(
        messages=contract.messages, metadata={"prompt_version": contract.version}))
    validate_structured_response(response, contract.response_schema)
    assert response["question_ids"] == ["q1", "q2"]


def test_catalog_is_bounded_but_every_eligible_question_remains_pageable_and_selectable():
    session = large_session()
    original = deepcopy(session)
    tools = InterviewerTools(session)
    page = tools.catalog_page()
    assert len(page["items"]) <= CATALOG_PAGE_ITEMS
    assert page["items"][0]["question_id"] == "q_0299", "Scarce required coverage must not disappear behind hundreds of equivalent choices"
    assert page["total"] == len(tools.candidates) == 300
    seen = set()
    while True:
        assert serialized_size(page) <= TOOL_RESULT_MAX_CHARS
        seen.update(item["question_id"] for item in page["items"])
        if page["next_cursor"] is None:
            break
        page = tools.execute("questions.search", page["next_cursor"])
    assert seen == set(tools.candidates)
    tools.require_selection("q_0250", None)
    assert session == original


def test_shortlist_prioritizes_uncovered_evidence_and_budget_without_reopening_closed_topics():
    session = large_session()
    session["turns"] = [{"id": "answered", "question_id": "q_0000", "competency_ids": ["python"],
                          "current_understanding": {"turn_intent": "finish_topic"}}]
    session["answers"] = [{"id": "answer", "turn_id": "answered"}]
    tools = InterviewerTools(session)
    assert tools.catalog()[0]["question_id"] == "q_0299"
    assert "q_0000" not in tools.candidates
    session["plan_snapshot"]["assessment_contract"]["candidate_questions"][-1]["expected_minutes"] = 10
    constrained = InterviewerTools(session, remaining_seconds=300)
    assert constrained.catalog()[0]["expected_seconds"] <= 300
    assert "q_0299" in constrained.candidates


def test_inquiry_units_page_independently_and_already_asked_units_stay_excluded():
    session = source_session()
    session["plan_snapshot"]["assessment_contract"]["candidate_questions"][0]["inquiry_units"] = [
        {"id": f"unit_{index}", "question_text": f"Explain this approved scope {index}", "competency_ids": ["python"]}
        for index in range(40)]
    session["turns"] = [{"id": "past", "question_id": "q1", "inquiry_unit_id": "unit_0"}]
    tools = InterviewerTools(session)
    page, seen = tools.catalog_page("python"), set()
    while True:
        for item in page["items"]:
            seen.update(unit["id"] for unit in item["inquiry_units"])
        if page["next_cursor"] is None:
            break
        page = tools.execute("questions.search", page["next_cursor"])
    assert seen == {f"unit_{index}" for index in range(1, 40)}
    with pytest.raises(ValueError):
        tools.require_selection("q1", "unit_0")
    tools.require_selection("q1", "unit_39")


def test_large_read_is_losslessly_pageable_and_cursors_cannot_change_tool_permissions():
    session = source_session()
    question = session["plan_snapshot"]["assessment_contract"]["candidate_questions"][0]["frozen_question"]
    question["question_text"] = 'Long approved content with "quotes". ' * 1_000
    question["spoken_text"] = question["question_text"]
    tools = InterviewerTools(session)
    page = tools.execute("questions.read", "q1")
    fragments = []
    cursor = page["next_cursor"]
    with pytest.raises(ApiError):
        tools.execute("company.read_reference", cursor)
    while True:
        assert page["format"] == "json_text_page"
        assert serialized_size(page) <= TOOL_RESULT_MAX_CHARS
        fragments.append(page["content"])
        if page["next_cursor"] is None:
            break
        page = tools.execute("questions.read", page["next_cursor"])
    result = json.loads("".join(fragments))
    assert result["question"]["question_text"] == question["question_text"]
    assert "spoken_text" not in result["question"]
    assert "standard_answer" not in result["question"]
    fresh = InterviewerTools(session)
    with pytest.raises(ApiError):
        fresh.execute("questions.read", cursor)


@pytest.mark.anyio
async def test_large_pool_keeps_first_request_bounded_and_full_pool_selection_validation():
    session = large_session()
    gateway = Gateway(decision("q_0250"))
    result = await supervisor(gateway).propose_next(session)
    request = gateway.calls[0]
    context = json.loads(request.messages[-1].content)["context"]
    assert result.question_id == "q_0250"
    assert context["catalog"]["total"] == 300
    assert len(context["eligible_questions"]) <= CATALOG_PAGE_ITEMS
    assert request.metadata["planning_input_size"]["characters"] <= PLANNING_REQUEST_MAX_CHARS
    assert "enum" not in request.json_schema["properties"]["question_id"]
    assert "enum" not in request.json_schema["properties"]["inquiry_unit_id"]
    with pytest.raises(ApiError):
        await supervisor(Gateway(decision("foreign"))).propose_next(session)


@pytest.mark.anyio
async def test_repeated_tool_read_reuses_one_observation():
    read = decision(None, action="use_tool", tool_name="questions.read", argument="q1", reason_code="inspect_context")
    gateway = Gateway(read, read, decision())
    result = await supervisor(gateway).propose_next(source_session())
    assert result.model_calls == 3
    final_input = json.loads(gateway.calls[-1].messages[-1].content)
    assert len(final_input["tool_observations"]) == 1


@pytest.mark.anyio
async def test_specialist_foreign_reference_is_rejected_before_supervisor_reuse():
    gateway = Gateway(decision(None, action="use_tool", tool_name="specialists.consult", reason_code="inspect_context"),
                      {"question_ids": ["foreign"], "evidence_gap": "A gap."}, decision())
    with pytest.raises(ApiError):
        await supervisor(gateway).propose_next(source_session())
    assert len(gateway.calls) == 2


def test_planning_memory_drops_whole_old_observations_and_never_changes_source():
    session = source_session()
    session["turns"] = [{"id": f"turn_{index}", "question_id": f"question_{index}",
                         "question_spoken_text": "Approved question.", "utterances": [{"text": "technical evidence " * 500}],
                         "current_understanding": {}} for index in range(20)]
    original = deepcopy(session)
    memory = planning_memory(session)
    assert memory["projection_truncated"] is True
    assert serialized_size(memory) <= PLANNING_MEMORY_MAX_CHARS
    assert session == original


def test_uncompressible_authorized_context_fails_closed_instead_of_truncating_rules():
    contract = supervisor_contract({"skill": {"instructions": "important approved rule " * 3_000}}, [], tools=[])
    with pytest.raises(ApiError) as error:
        planning_request_size(contract)
    assert error.value.code == "INTERVIEW_PLANNING_CONTEXT_TOO_LARGE"


@pytest.mark.anyio
async def test_supervisor_shares_interaction_deadline_and_never_renews_expired_budget():
    gateway = Gateway(decision())
    with interaction_deadline(time.monotonic() + 0.5):
        await supervisor(gateway).propose_next(source_session())
    assert 0 < gateway.calls[0].execution_budget.timeout_s <= 0.5
    empty_gateway = Gateway(decision())
    with interaction_deadline(time.monotonic() - 1):
        with pytest.raises(ApiError) as error:
            await supervisor(empty_gateway).propose_next(source_session())
    assert error.value.code == "INTERVIEW_DECISION_BUDGET_EXHAUSTED"
    assert not empty_gateway.calls
