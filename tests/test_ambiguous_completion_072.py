"""Conflicting completion output can clarify, but cannot gain authority."""
from copy import deepcopy

import pytest

from test_answer_declined_understanding import no_followup
from test_prepared_turn_decision import Gateway, _input, _service
from test_semantic_recovery_066 import REPLY, _clarification_context, unresolved_response


def _case(*, composite=True, intent="answer", reply=REPLY, delivered=False):
    text, before, data = unresolved_response(technical=True, reply=reply)
    data.update(intent=intent, followup_allowed=True)
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    turn["allow_followup"] = composite
    dialogue = {"input_context": {"version": "conversation_input_context.v1",
        "current_speech_start": 0, "current_speech": text, "responded_exchanges": []}}
    if delivered:
        dialogue["content_clarification"] = _clarification_context(before, reply)
    interview.update(_semantic_first=True, _interviewer_context=dialogue)
    return text, before, data, utterance, turn, interview


async def _prepare(case, *, correction=None):
    text, before, data, utterance, turn, interview = case
    def response(value):
        return {"understanding": value, "followup": no_followup()} if turn["allow_followup"] else value
    gateway = Gateway(response(data), response(data if correction is None else correction))
    service = _service(gateway)
    result, followup = await service.prepare_decision(utterance, turn, interview)
    return result, followup, gateway, service


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [True, False])
@pytest.mark.parametrize("intent", ["answer", "clarification_request"])
async def test_first_ambiguous_completion_retains_claims_and_requests_only_specific_clarification(composite, intent):
    case = _case(composite=composite, intent=intent)
    original = deepcopy(case[2])
    before_interview = deepcopy(case[5])
    result, followup, gateway, service = await _prepare(case)
    assert result.problem is None
    assert result.intent == "clarification_request" and result.turn_intent == "finish_answer"
    assert result.suggested_action == "clarify" and result.confidence < .65
    assert result.completion_basis is None and result.followup_allowed is False
    assert result.clarification_target.focus_quote == "蓝桥" and result.ambiguities == original["ambiguities"]
    assert result.claims[0].evidence_quote == "我记录确认位置。"
    assert result.covered_capability_points == ["确认序号"]
    assert not followup["selected"] and not service.can_complete_without_confirmation(result, case[0])
    assert len(gateway.requests) == 1 and case[2] == original and case[5] == before_interview


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [True, False])
async def test_delivered_clarification_is_not_reopened_and_fresh_unresolved_reply_can_finish(composite):
    case = _case(composite=composite, delivered=True)
    corrected = deepcopy(case[2])
    corrected.update(intent="answer_unresolved", followup_allowed=False)
    result, followup, gateway, service = await _prepare(case, correction=corrected)
    assert result.problem is None and result.intent == "answer_unresolved"
    assert result.completion_basis.evidence_quotes == [REPLY]
    assert result.ambiguities and result.claims and not followup["selected"]
    assert service.can_complete_without_confirmation(result, case[0]) and len(gateway.requests) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("mutation,reason", [
    (lambda data: data.update(clarification_target=None), "completion_has_unresolved_ambiguity"),
    (lambda data: data.update(completion_basis=None), "completion_basis_required"),
    (lambda data: data["clarification_target"].update(focus_quote="从未说出的词"), "clarification_focus_not_verbatim"),
    (lambda data: data["evidence_ids"].remove("E1"), "claim_evidence_undeclared"),
    (lambda data: data.update(claims=[]), "technical_content_claims_missing"),
    (lambda data: data.update(covered_point_ids=["P2"], missing_point_ids=["P2"]), "point_partition_invalid"),
    (lambda data: data.update(intent="answer_unresolved", followup_allowed=False), "unresolved_clarification_required"),
])
async def test_projection_does_not_repair_missing_evidence_or_grant_unresolved_completion(mutation, reason):
    case = _case(composite=False)
    mutation(case[2])
    result, followup, gateway, service = await _prepare(case)
    assert result.problem is not None and result.problem.reason_code == reason
    assert result.completion_basis is None and not followup["selected"]
    assert not service.can_complete_without_confirmation(result, case[0]) and len(gateway.requests) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("reply", ["我还没说完。", "让我再想想。", "我不想面了。", "他说：‘我已经说完了。’"])
async def test_conflicting_current_or_quoted_control_is_checked_before_dropping_completion_basis(reply):
    case = _case(composite=False, reply=reply)
    result, followup, gateway, service = await _prepare(case)
    assert result.problem is not None and result.problem.reason_code == "semantic_current_speech_conflict"
    assert result.completion_basis is None and not followup["selected"]
    assert not service.can_complete_without_confirmation(result, case[0])


@pytest.mark.anyio
@pytest.mark.parametrize("old", ["completion", "clarification"])
async def test_old_completion_and_already_answered_question_are_not_hidden_by_projection(old):
    case = _case(composite=False)
    text, before, data, _, _, interview = case
    context = interview["_interviewer_context"]["input_context"]
    context.update(current_speech_start=len(before), current_speech=REPLY)
    if old == "completion":
        data["completion_basis"]["evidence_ids"] = ["E3"]
    else:
        context["responded_exchanges"] = [{"request_text": before}]
    result, followup, gateway, service = await _prepare(case)
    assert result.problem is not None and result.problem.reason_code == "semantic_current_speech_conflict"
    assert not followup["selected"] and not service.can_complete_without_confirmation(result, text)


@pytest.mark.anyio
async def test_unambiguous_finish_is_understood_once_and_still_requires_runtime_confirmation():
    case = _case(composite=False)
    case[2].update(ambiguities=[], clarification_target=None)
    result, followup, gateway, service = await _prepare(case)
    assert result.problem is None and result.intent == "answer"
    assert result.completion_basis.evidence_quotes == [REPLY]
    assert result.suggested_action == "next" and result.followup_allowed
    assert not service.can_complete_without_confirmation(result, case[0]) and len(gateway.requests) == 1


@pytest.mark.parametrize("quote", ["从未说出的结束语", "   "])
def test_canonical_completion_source_is_checked_before_ambiguity_projection(quote):
    from app.core.prompt.semantic_turn import SemanticContractError, normalize_ambiguous_completion
    from app.core.prompt.understanding_references import resolve_understanding_references, understanding_references

    text, _, data, _, _, _ = _case()
    canonical = resolve_understanding_references(data, understanding_references(text, ["幂等键", "确认序号"]))
    canonical["completion_basis"]["evidence_quotes"] = [quote]
    with pytest.raises(SemanticContractError) as caught:
        normalize_ambiguous_completion(canonical, text)
    assert caught.value.reason_code == "semantic_completion_evidence_invalid"
    assert canonical["completion_basis"]["evidence_quotes"] == [quote]
