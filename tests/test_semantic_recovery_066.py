"""Fragmented speech, clear completion and content failures remain distinct."""
from copy import deepcopy

import pytest

from app.core.prompt.semantic_turn import SemanticContractError, validate_semantic_turn
from app.core.prompt.understanding_references import resolve_understanding_references, understanding_references
from app.services.conversation_understanding import _log_validation_rejection


def _canonical():
    return {
        "intent": "answer", "answer_content": "partial", "turn_intent": "finish_answer",
        "answer_summary": "记录确认位置。", "claims": [{"claim": "记录确认位置", "evidence_quote": "我记录位置。"}],
        "evidence_quotes": ["我记录位置。", "我说完了。"], "covered_capability_points": ["确认序号"],
        "missing_capability_points": ["幂等键"], "ambiguities": [], "contradictions": [],
        "confidence": .95, "suggested_action": "next", "followup_allowed": True,
        "completion_basis": {"source": "explicit_server_intent", "evidence_quotes": ["我说完了。"]},
        "clarification_target": None,
    }


@pytest.mark.parametrize("change,reason,path", [
    ({"completion_basis": None}, "completion_basis_required", "$.completion_basis"),
    ({"turn_intent": "thinking"}, "completion_basis_unexpected", "$.completion_basis"),
    ({"answer_content": "none"}, "content_none_has_technical_evidence", "$.answer_content"),
    ({"claims": []}, "technical_content_claims_missing", "$.claims"),
    ({"intent": "answer_declined"}, "declined_answer_has_technical_content", "$.intent"),
    ({"confidence": .74}, "completion_confidence_insufficient", "$.confidence"),
    ({"suggested_action": "accept"}, "completion_action_invalid", "$.suggested_action"),
    ({"followup_allowed": False}, "completion_followup_permission_invalid", "$.followup_allowed"),
    ({"ambiguities": ["具体技术含义仍不明。"]}, "completion_has_unresolved_ambiguity", "$.ambiguities"),
    ({"intent": "not_finished"}, "completion_intent_content_mismatch", "$.intent"),
    ({"completion_basis": {"source": "explicit_server_intent", "evidence_quotes": ["编造完成信号"]}},
     "semantic_completion_evidence_invalid", "$.completion_basis.evidence_quotes"),
])
def test_each_semantic_failure_has_a_fixed_reason_without_transcript(change, reason, path, caplog):
    data = {**_canonical(), **change}
    with pytest.raises(SemanticContractError) as caught:
        validate_semantic_turn(data, "我记录位置。我说完了。")
    assert caught.value.reason_code == reason and caught.value.schema_path == path
    _log_validation_rejection("synthetic_rejection", caught.value, stage="understanding_content", attempt=1)
    assert f"reason={reason}" in caplog.text and f"path={path}" in caplog.text
    assert "具体技术含义" not in caplog.text and "编造完成信号" not in caplog.text
    assert "我记录位置" not in caplog.text and "understanding_content_invalid" not in caplog.text


def test_reference_focus_rejection_is_distinguishable_from_model_schema_error(caplog):
    wire = {"clarification_target": {"evidence_id": "E1", "focus_quote": "未说出的名字"}}
    with pytest.raises(SemanticContractError) as caught:
        resolve_understanding_references(wire, understanding_references("合成模糊片段。", []))
    assert caught.value.reason_code == "clarification_focus_not_verbatim"
    _log_validation_rejection("synthetic_rejection", caught.value, stage="understanding_references", attempt=1)
    assert "reason=clarification_focus_not_verbatim" in caplog.text
    assert "path=$.clarification_target.focus_quote" in caplog.text
    assert "合成模糊片段" not in caplog.text and "未说出的名字" not in caplog.text

from app.core.prompt.semantic_turn import (
    CurrentSpeechContractError, validate_current_speech, validate_unresolved_completion,
)
from app.core.prompt.contracts import prompt_contract
from app.core.prompt.validation import validate_structured_response
from app.services.conversation_understanding import ConversationUnderstandingService
from test_prepared_turn_decision import Gateway, _input, _service
from test_answer_declined_understanding import no_followup


FRAGMENT = "任务转到蓝桥，蓝桥。我的介绍完毕。"
REPLY = "我已经说完了。"


def _clarification_context(request=FRAGMENT, reply=REPLY):
    return {"version": "content_clarification.v1", "request_text": request,
            "focus_quote": "蓝桥", "reply_text": reply, "question_act_id": "act_synthetic_clarification",
            "delivered": True}


def unresolved_response(*, technical=False, reply=REPLY):
    before = ("我记录确认位置。" if technical else "") + FRAGMENT
    text = before + reply
    refs = understanding_references(text, ["幂等键", "确认序号"])
    last = list(refs["evidence"])[-2 if reply.startswith("他说") else -1]
    focus = "E2" if technical else "E1"
    data = {"intent": "answer_unresolved", "answer_content": "partial" if technical else "none",
        "turn_intent": "finish_answer", "answer_summary": "候选人明确结束本次回答，部分技术转写待核实。",
        "claims": [{"claim": "记录确认位置", "evidence_id": "E1"}] if technical else [],
        "evidence_ids": list(refs["evidence"]),
        "covered_point_ids": ["P2"] if technical else [], "missing_point_ids": ["P1"] if technical else ["P1", "P2"],
        "ambiguities": ["蓝桥的实际含义无法核实。"], "contradictions": [], "confidence": .95,
        "suggested_action": "next", "followup_allowed": False,
        "completion_basis": {"source": "explicit_server_intent", "evidence_ids": [last]},
        "clarification_target": {"evidence_id": focus, "focus_quote": "蓝桥"}}
    return text, before, data


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [True, False])
@pytest.mark.parametrize("technical", [True, False])
async def test_fragmented_content_can_finish_after_a_real_clarification_without_becoming_a_decline(composite, technical):
    text, before, data = unresolved_response(technical=technical)
    data["clarification_target"] = {"resolution": "same_focus_unresolved", "source_ref": "C1"}
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    turn["allow_followup"] = composite
    interview.update(_semantic_first=True, _interviewer_context={"content_clarification": _clarification_context(before)})
    gateway = Gateway({"understanding": data, "followup": no_followup()} if composite else data)
    service = _service(gateway)
    understanding, followup = await service.prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.intent == "answer_unresolved"
    assert understanding.turn_intent == "finish_answer" and understanding.followup_allowed is False
    assert understanding.ambiguities and understanding.completion_basis.evidence_quotes == [REPLY]
    assert understanding.answer_content == ("partial" if technical else "none")
    assert bool(understanding.claims) is technical and not followup["selected"]
    if technical:
        assert understanding.claims[0].evidence_quote == "我记录确认位置。"
    assert service.can_complete_without_confirmation(understanding, text)
    assert len(gateway.requests) == 1 and utterance.text == text


@pytest.mark.anyio
@pytest.mark.parametrize("technical", [False, True])
async def test_first_unclear_answer_and_clear_finish_have_a_valid_specific_clarification(technical):
    text, _, data = unresolved_response(technical=technical)
    data.update(intent="answer" if technical else "clarification_request", turn_intent="clarify",
                completion_basis=None, confidence=.6, suggested_action="clarify")
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    interview.update(_semantic_first=True, _interviewer_context={"input_context": {
        "version": "conversation_input_context.v1", "current_speech_start": 0,
        "current_speech": text, "responded_exchanges": []}})
    gateway = Gateway({"understanding": data, "followup": no_followup()})
    understanding, followup = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.suggested_action == "clarify"
    assert understanding.clarification_target.focus_quote == "蓝桥"
    assert understanding.completion_basis is None and bool(understanding.claims) is technical
    assert not followup["selected"] and len(gateway.requests) == 1


@pytest.mark.parametrize("mutation,reason", [
    (lambda c: c.clear(), "unresolved_clarification_required"),
    (lambda c: c.update(delivered=False), "unresolved_clarification_required"),
    (lambda c: c.update(delivered="true"), "unresolved_clarification_required"),
    (lambda c: c.update(question_act_id=""), "unresolved_clarification_required"),
    (lambda c: c.update(extra_permission=True), "unresolved_clarification_required"),
    (lambda c: c.update(request_text="别场候选人说过。"), "unresolved_clarification_source_invalid"),
    (lambda c: c.update(reply_text="旧回复。"), "unresolved_clarification_source_invalid"),
    (lambda c: c.update(focus_quote="未出现"), "unresolved_clarification_source_invalid"),
])
def test_unresolved_completion_requires_matching_delivered_clarification_and_fresh_reply(mutation, reason):
    text, before, data = unresolved_response()
    data = resolve_understanding_references(data, understanding_references(text, ["幂等键", "确认序号"]))
    context = _clarification_context(before)
    mutation(context)
    with pytest.raises(SemanticContractError) as caught:
        validate_unresolved_completion(data, text, context)
    assert caught.value.reason_code == reason


def test_unresolved_completion_cannot_reuse_pre_question_finish_evidence():
    text, before, data = unresolved_response()
    data = resolve_understanding_references(data, understanding_references(text, ["幂等键", "确认序号"]))
    data["completion_basis"]["evidence_quotes"] = ["我的介绍完毕。"]
    with pytest.raises(CurrentSpeechContractError):
        validate_unresolved_completion(data, text, _clarification_context(before))


@pytest.mark.parametrize("reply", ["我还没说完。", "让我再想想。", "我们结束面试吧。", "他说：‘我已经说完了。’"])
def test_retraction_wait_full_withdrawal_and_quoted_finish_cannot_be_unresolved_answer_completion(reply):
    text, before, data = unresolved_response(reply=reply)
    data = resolve_understanding_references(data, understanding_references(text, ["幂等键", "确认序号"]))
    with pytest.raises(CurrentSpeechContractError):
        validate_unresolved_completion(data, text, _clarification_context(before, reply))


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [True, False])
async def test_repeated_missing_clarification_proof_has_precise_safe_failure_and_no_permission(composite, caplog):
    text, before, data = unresolved_response()
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    interview["_semantic_first"] = True
    turn["allow_followup"] = composite
    response = {"understanding": data, "followup": no_followup()} if composite else data
    gateway = Gateway(response, response)
    service = _service(gateway)
    result, followup = await service.prepare_decision(utterance, turn, interview)
    assert result.problem.reason_code == "unresolved_clarification_required"
    assert result.problem.attempts == 2 and result.completion_basis is None
    assert not service.can_complete_without_confirmation(result, text) and not followup["selected"]
    assert len(gateway.requests) == 2 and text not in caplog.text and "蓝桥" not in caplog.text
    correction = next(message.content for message in gateway.requests[1].messages
                      if message.content.startswith("上一轮结果未通过合同校验"))
    assert "unresolved_clarification_required" in correction and "$.content_clarification" in correction
    assert "不能输出answer_unresolved+finish_answer" in correction and "clarification_request+finish_answer+clarify" in correction
    assert "不能为规避澄清条件将完成本题改为退出整场" in correction


@pytest.mark.anyio
async def test_low_acoustic_confidence_revokes_even_model_clear_unresolved_completion():
    text, before, data = unresolved_response()
    data["clarification_target"] = {"resolution": "same_focus_unresolved", "source_ref": "C1"}
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text, "stt_confidence": .6})
    turn["allow_followup"] = False
    interview.update(_semantic_first=True, _interviewer_context={"content_clarification": _clarification_context(before)})
    service = _service(Gateway(data))
    understanding, followup = await service.prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.completion_basis is None
    assert understanding.intent == "clarification_request" and not followup["selected"]
    assert not service.can_complete_without_confirmation(understanding, text)


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [True, False])
@pytest.mark.parametrize("technical", [True, False])
async def test_unclear_technical_content_does_not_block_an_exit_proposal_or_authorize_unconfirmed_exit(composite, technical):
    text, _, data = unresolved_response(technical=technical, reply="我不想面了。")
    data["turn_intent"] = "stop_interview"
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    turn["allow_followup"] = composite
    interview["_semantic_first"] = True
    gateway = Gateway({"understanding": data, "followup": no_followup()} if composite else data)
    service = _service(gateway)
    understanding, followup = await service.prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.intent == "answer_unresolved"
    assert understanding.turn_intent == "stop_interview" and understanding.ambiguities
    assert understanding.completion_basis.evidence_quotes == ["我不想面了。"]
    assert bool(understanding.claims) is technical and not followup["selected"]
    assert not service.can_complete_without_confirmation(understanding, text)
    assert len(gateway.requests) == 1


@pytest.mark.anyio
async def test_fresh_separate_exit_confirmation_retains_unresolved_technical_content():
    from app.core.prompt.candidate_exit import EXIT_CONFIRMATION_SPEECH, EXIT_CONFIRMATION_SPEECH_VERSION
    from app.services.candidate_exit_confirmation import text_hash
    request = FRAGMENT + "我不想面了。"
    reply = "是的，确定结束。"
    text, _, data = unresolved_response(reply="我不想面了。" + reply)
    data["turn_intent"] = "stop_interview"
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    context = {"version": EXIT_CONFIRMATION_SPEECH_VERSION, "interview_id": interview["id"], "turn_id": turn["id"],
        "capture_id": "synthetic_capture", "question_act_id": "synthetic_exit_question", "question": EXIT_CONFIRMATION_SPEECH,
        "request_text": request, "reply_text": reply, "decision": "confirm", "transcript_sha256": text_hash(text)}
    interview.update(_semantic_first=True, _answer_completion_confirmed=True,
                     _interviewer_context={"exit_confirmation": context})
    service = _service(Gateway({"understanding": data, "followup": no_followup()}))
    understanding, followup = await service.prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.intent == "answer_unresolved"
    assert understanding.turn_intent == "stop_interview" and understanding.completion_basis.evidence_quotes == [reply]
    assert understanding.ambiguities and understanding.clarification_target.focus_quote == "蓝桥"
    assert not service.can_complete_without_confirmation(understanding, text) and not followup["selected"]


def test_unresolved_exit_prompt_keeps_exit_confirmation_separate_from_technical_clarification():
    from app.core.prompt.candidate_exit import EXIT_CONFIRMATION_INSTRUCTION
    from app.core.prompt.semantic_turn import SEMANTIC_TURN_INSTRUCTION
    assert "answer_unresolved+stop_interview" in SEMANTIC_TURN_INSTRUCTION
    assert "不要求先有content_clarification" in SEMANTIC_TURN_INSTRUCTION
    assert "answer_unresolved+stop_interview" in EXIT_CONFIRMATION_INSTRUCTION
    assert "不要求content_clarification" in EXIT_CONFIRMATION_INSTRUCTION


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [True, False])
@pytest.mark.parametrize("technical", [True, False])
async def test_waiting_keeps_prior_technical_ambiguity_and_claims_without_another_clarification(composite, technical):
    reply = "再等一下，我还没有说完。"
    text, before, data = unresolved_response(technical=technical, reply=reply)
    data.update(intent="answer" if technical else "not_finished", turn_intent="thinking", completion_basis=None,
                suggested_action="continue_listening",
                clarification_target={"resolution": "same_focus_unresolved", "source_ref": "C1"})
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    turn["allow_followup"] = composite
    interview.update(_semantic_first=True, _interviewer_context={
        "content_clarification": _clarification_context(before, reply),
        "input_context": {"version": "conversation_input_context.v1", "current_speech_start": len(before),
            "current_speech": reply, "responded_exchanges": [{"request_text": before,
                "reply_text": "请说明蓝桥指什么。", "kind": "question_clarification"}]}})
    gateway = Gateway({"understanding": data, "followup": no_followup()} if composite else data)
    service = _service(gateway)
    understanding, followup = await service.prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.turn_intent == "thinking"
    assert understanding.suggested_action == "continue_listening" and understanding.ambiguities
    assert understanding.clarification_target.focus_quote == "蓝桥" and bool(understanding.claims) is technical
    assert understanding.completion_basis is None and not followup["selected"]
    assert len(gateway.requests) == 1 and not service.can_complete_without_confirmation(understanding, text)


@pytest.mark.anyio
async def test_uncertain_acoustic_confidence_on_waiting_does_not_force_a_new_content_question():
    text, before, data = unresolved_response(reply="我还没说完。")
    data.update(intent="not_finished", turn_intent="thinking", completion_basis=None,
                suggested_action="continue_listening")
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text, "stt_confidence": .5})
    turn["allow_followup"] = False
    interview["_semantic_first"] = True
    service = _service(Gateway(data))
    result, followup = await service.prepare_decision(utterance, turn, interview)
    assert result.suggested_action == "continue_listening" and result.confidence == .5
    assert result.problem is None and result.completion_basis is None and not followup["selected"]
    assert not service.can_complete_without_confirmation(result, text)


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [True, False])
@pytest.mark.parametrize("technical", [True, False])
async def test_claim_mismatch_retry_explains_both_safe_repairs_without_inventing_a_claim(composite, technical):
    text, _, corrected = unresolved_response(technical=technical, reply="我不想面了。")
    corrected["turn_intent"] = "stop_interview"
    invalid = {**deepcopy(corrected), "answer_content": "partial", "claims": []}
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    turn["allow_followup"] = composite
    interview["_semantic_first"] = True
    responses = [{"understanding": value, "followup": no_followup()} if composite else value
                 for value in (invalid, corrected)]
    gateway = Gateway(*responses)
    service = _service(gateway)
    result, followup = await service.prepare_decision(utterance, turn, interview)
    assert result.problem is None and result.intent == "answer_unresolved"
    assert result.answer_content == ("partial" if technical else "none")
    assert bool(result.claims) is technical and result.ambiguities
    assert not service.can_complete_without_confirmation(result, text) and not followup["selected"]
    assert len(gateway.requests) == 2
    corrections = [message.content for message in gateway.requests[1].messages
                   if message.content.startswith("上一轮结果未通过合同校验")]
    assert len(corrections) == 1
    correction = corrections[0]
    assert "technical_content_claims_missing" in correction and "$.claims" in correction
    assert "answer_content=none、claims=[]" in correction and "保留partial/technical" in correction
    assert "不得编造claim" in correction and "不能删除原文中已有的可靠主张" in correction
    assert text not in correction and "蓝桥" not in correction


def test_unknown_content_correction_does_not_echo_untrusted_details():
    from app.core.prompt.semantic_turn import semantic_content_correction_instruction
    assert semantic_content_correction_instruction("untrusted candidate material") == ""


def test_complete_unresolved_none_prompt_example_is_a_valid_source_bound_contract():
    import json
    from app.core.prompt.semantic_turn import SEMANTIC_TURN_INSTRUCTION
    text = "先转到卡拉布，卡拉布那个状态。我不想继续面试了。"
    start = SEMANTIC_TURN_INSTRUCTION.index('{"answer_content":"none"')
    example, _ = json.JSONDecoder().raw_decode(SEMANTIC_TURN_INSTRUCTION[start:])
    contract = prompt_contract("interview_turn_understanding", {
        "transcript": text, "capability_points": ["状态迁移"], "semantic_first": True,
    })
    validate_structured_response(example, contract.response_schema)
    data = resolve_understanding_references(example, understanding_references(text, ["状态迁移"]))
    validate_semantic_turn(data, text)
    validate_unresolved_completion(data, text)
    assert data["answer_content"] == "none" and data["claims"] == [] and data["ambiguities"]
    for field in ("answer_content", "claims"):
        description = contract.response_schema["properties"][field]["description"]
        assert "无法解释" in description and "none" in description and "partial" in description


@pytest.mark.parametrize("delivered", [True, False])
@pytest.mark.parametrize("composite", [True, False])
def test_prompt_states_current_delivered_clarification_phase_after_general_rules(delivered, composite):
    from app.core.prompt.contracts import _interview_turn_decision, _interview_turn_understanding

    text, before, _ = unresolved_response()
    context = {"transcript": text, "capability_points": ["幂等键", "确认序号"], "semantic_first": True,
               "interviewer_context": {"content_clarification": _clarification_context(before)} if delivered else {}}
    contract = prompt_contract("interview_turn_decision" if composite else "interview_turn_understanding", context)
    original = (_interview_turn_decision if composite else _interview_turn_understanding)(context)
    assert contract.messages[0].role == "system"
    assert [message for message in contract.messages if message.role == "system"] == [contract.messages[0]]
    assert contract.messages[1:] == [message for message in original.messages if message.role != "system"]
    policy = contract.messages[0].content
    marker = "本轮内容澄清阶段："
    assert sum(message.content.count(marker) for message in contract.messages) == 1
    phase_index = policy.index(marker)
    assert policy.index("本轮采用语义优先接话合同") < phase_index
    phase = policy[phase_index:]
    if delivered:
        assert "已提供已送达的content_clarification" in phase and "播完后的新回复" in phase
        assert "thinking/continue_listening" in phase and "仍需独立退出确认" in phase
    else:
        assert "本轮禁止answer_unresolved+finish_answer" in phase and "首次技术残片加完成语应具体clarify" in phase
        assert "answer_unresolved+stop_interview提议独立退出确认" in phase


@pytest.mark.parametrize("context", [None, {}, {"delivered": True}, _clarification_context() | {"delivered": False}])
def test_missing_or_undelivered_clarification_never_prompts_as_delivered(context):
    from app.core.prompt.semantic_turn import content_clarification_instruction
    assert "本轮禁止answer_unresolved+finish_answer" in content_clarification_instruction(context)


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [True, False])
@pytest.mark.parametrize("technical", [True, False])
async def test_candidate_finish_intent_can_request_content_clarification_without_completion_authority(composite, technical):
    text, _, data = unresolved_response(technical=technical)
    data.update(intent="clarification_request", suggested_action="clarify", confidence=.5, completion_basis=None)
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    turn["allow_followup"] = composite
    interview["_semantic_first"] = True
    gateway = Gateway({"understanding": data, "followup": no_followup()} if composite else data)
    service = _service(gateway)
    result, followup = await service.prepare_decision(utterance, turn, interview)
    assert result.problem is None and result.intent == "clarification_request"
    assert result.turn_intent == "finish_answer" and result.suggested_action == "clarify"
    assert result.confidence == .5 and result.completion_basis is None and result.followup_allowed is False
    assert result.ambiguities and result.clarification_target.focus_quote == "蓝桥"
    assert bool(result.claims) is technical and not followup["selected"]
    assert not service.can_complete_without_confirmation(result, text) and len(gateway.requests) == 1
    # Both wire and canonical schemas accept the proposal. They do not grant a
    # finish: the centralized cross-field check and runtime retain the above
    # clarification action and absent completion basis.
    wire_schema = gateway.requests[0].json_schema
    validate_structured_response({"understanding": data, "followup": no_followup()} if composite else data, wire_schema)
    canonical = resolve_understanding_references(data, understanding_references(text, ["幂等键", "确认序号"]))
    validate_structured_response(canonical, service._canonical_schema(True))
    validate_semantic_turn(canonical, text)


@pytest.mark.parametrize("mutation", [
    {"suggested_action": "next"}, {"suggested_action": "accept"},
    {"confidence": .65}, {"confidence": .95}, {"followup_allowed": True},
    {"ambiguities": []}, {"clarification_target": None}, {"turn_intent": "stop_interview"},
    {"intent": "answer"}, {"intent": "answer_declined"},
    {"completion_basis": {"source": "explicit_server_intent", "evidence_quotes": [REPLY]}},
    {"clarification_target": {"evidence_quote": "未说出的原文。", "focus_quote": "原文"}},
])
def test_noncommitting_finish_clarification_requires_the_entire_strict_combination(mutation):
    text, _, data = unresolved_response()
    canonical = resolve_understanding_references(data, understanding_references(text, ["幂等键", "确认序号"]))
    canonical.update(intent="clarification_request", suggested_action="clarify", confidence=.5, completion_basis=None)
    canonical.update(mutation)
    with pytest.raises(SemanticContractError):
        validate_semantic_turn(canonical, text)


@pytest.mark.anyio
async def test_noncommitting_clarification_cannot_keep_undeclared_claim_evidence():
    text, _, data = unresolved_response(technical=True)
    data.update(intent="clarification_request", suggested_action="clarify", confidence=.5, completion_basis=None)
    data["evidence_ids"].remove("E1")
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    turn["allow_followup"] = False
    interview["_semantic_first"] = True
    service = _service(Gateway(data, data))
    result, followup = await service.prepare_decision(utterance, turn, interview)
    assert result.problem.reason_code == "claim_evidence_undeclared"
    assert result.problem.attempts == 2 and not followup["selected"]
    assert not service.can_complete_without_confirmation(result, text)
