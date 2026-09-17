"""The cognitive preflight respects the same frozen budget as the lifecycle."""
from copy import deepcopy

import pytest

from app.core.errors import ApiError
from app.domain.adaptive_interview import freeze_assessment_contract, frozen_candidate, initialize_adaptive_snapshot
from app.repositories.memory import InMemoryStore
from app.services.interviews import InterviewService
from test_prepared_turn_decision import _input


def scope_fixture(existing, *, per_root=3, total=8):
    utterance, root, session = _input()
    root["question_snapshot"]["key_points"] = [{"text": "知识点%d" % number} for number in range(1, 5)]
    question = {**root["question_snapshot"], "id": "question", "version": 1, "skills": ["engineering"]}
    contract = freeze_assessment_contract(role={"id": "role", "version": 1},
        candidates=[frozen_candidate(question, competency_ids=["engineering"], source_type="position_bank")],
        dimension_weights={"engineering": 1.0}, question_count=1, duration_minutes=20,
        policy={"min_root_questions": 1, "max_root_questions": 1,
                "max_followups_per_root": per_root, "max_total_followups": total})
    session["plan_snapshot"] = initialize_adaptive_snapshot({"assessment_contract": contract})
    # An obsolete cached v2 policy cannot override the frozen v3 agreement.
    session["followup_policy"] = {"max_total": 1, "max_depth": 1, "max_per_root": 1}
    for index in range(1, existing + 1):
        child = {**deepcopy(root), "id": "child%d" % index, "is_followup": True,
                 "followup_depth": index, "target_key_points": ["知识点%d" % index]}
        session["turns"].append(child)
    return utterance, session["turns"][-1], session


@pytest.mark.parametrize("existing,allowed", [(2, True), (3, False)])
def test_third_frozen_followup_is_allowed_and_fourth_is_denied(existing, allowed):
    service = InterviewService(InMemoryStore())
    utterance, turn, session = scope_fixture(existing)
    scope = service.conversation._followup_scope(session, turn, utterance)
    assert ("targets" in scope) is allowed
    assert scope["policy"]["max_depth"] == scope["policy"]["max_per_root"] == 3
    assert scope["policy"]["max_total"] == 8
    assembled = service._followup_policy({"execution_schema_version": 3,
        "assessment_contract": session["plan_snapshot"]["assessment_contract"]})
    assert assembled["max_depth"] == assembled["max_per_root"] == 3 and assembled["max_total"] == 8


def test_frozen_total_and_hash_cannot_be_bypassed_by_cached_followup_policy():
    service = InterviewService(InMemoryStore())
    utterance, turn, session = scope_fixture(2, total=2)
    session["followup_policy"] = {"max_total": 30, "max_depth": 30, "max_per_root": 30}
    scope = service.conversation._followup_scope(session, turn, utterance)
    assert scope["reason"] == "session_budget_exhausted"
    session["plan_snapshot"]["assessment_contract"]["budget"]["max_total_followups"] = 30
    with pytest.raises(ApiError):
        service.conversation._followup_scope(session, turn, utterance)


def test_legacy_v2_keeps_existing_depth_and_per_root_caps():
    service = InterviewService(InMemoryStore())
    utterance, turn, session = scope_fixture(2)
    session.pop("plan_snapshot")
    scope = service.conversation._followup_scope(session, turn, utterance)
    assert scope["reason"] == "depth_budget_exhausted"
