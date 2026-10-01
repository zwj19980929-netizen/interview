"""Latest control requests remain separate from accumulated answer evidence."""
from copy import deepcopy

import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.semantic_turn import (
    CurrentSpeechContractError, validate_current_speech,
)
from app.core.prompt.understanding_references import understanding_references
from app.core.prompt.validation import StructuredResponseValidationError
from test_prepared_turn_decision import Gateway, _input, _service


def _context(text, *, prior="", received=None):
    return {"version": "conversation_input_context.v1", "current_speech": text[len(prior):],
            "current_speech_start": len(prior), "responded_exchanges": received or []}


def _exchange(text, start=0, reply="好的，你慢慢想。"):
    return {"request_text": text, "reply_text": reply, "request_start": start,
            "request_end": start + len(text)}


def _wire(text, intent, *, technical=False, evidence_index=None, focus=None):
    evidence = understanding_references(text, ["幂等键", "确认序号"])["evidence"]
    latest = evidence_index or list(evidence)[-1]
    ending = intent in {"finish_answer", "finish_topic", "decline_topic", "stop_interview"}
    clarification = intent == "clarify"
    data = {"intent": "answer" if technical else "answer_declined" if ending else
                      "clarification_request" if clarification else "not_finished",
        "answer_content": "partial" if technical else "none", "turn_intent": intent,
        "answer_summary": "保存确认位置。" if technical else "候选人不再继续。" if ending else "等待候选人。",
        "claims": [{"claim": "保存确认位置", "evidence_id": "E2"}] if technical else [],
        "evidence_ids": ["E2", latest] if technical and latest != "E2" else [latest],
        "covered_point_ids": ["P2"] if technical else [],
        "missing_point_ids": ["P1"] if technical else ["P1", "P2"],
        "ambiguities": ["当前疑点。"] if clarification else [], "contradictions": [],
        "confidence": .6 if clarification else .95,
        "suggested_action": "clarify" if clarification else "next" if ending else "continue_listening",
        "followup_allowed": intent == "finish_answer",
        "completion_basis": {"source": "explicit_server_intent", "evidence_ids": [latest]} if ending else None,
        "clarification_target": {"evidence_id": latest, "focus_quote": focus} if focus else None,
    }
    return {"understanding": data, "followup": {"selected": False, "question_text": "",
        "evidence_id": "", "target_point_ids": [], "rationale": "", "difficulty": "mid",
        "sensitive_attribute_inference": False, "leaks_answer": False}}


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [True, False])
@pytest.mark.parametrize("latest,expected,technical", [
    ("我不会。", "decline_topic", False),
    ("这题我不会。", "decline_topic", False),
    ("这一题我不会。", "decline_topic", False),
    ("这道题答不上来。", "decline_topic", False),
    ("我已经说完了呀。", "finish_answer", True),
    ("我不想继续面试了。", "stop_interview", False),
    ("我不想面了。", "stop_interview", False),
    ("我们结束面试吧。", "stop_interview", False),
    ("这题我不会，我们结束面试吧。", "stop_interview", False),
    ("我们结束面试吧。", "stop_interview", True),
])
async def test_old_thinking_cannot_override_new_request_and_one_correction_preserves_claims(
    composite, latest, expected, technical, caplog,
):
    prior = "我想一下。"
    text = prior + ("我会保存确认位置。" if technical else "") + latest
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    interview.update(_semantic_first=True, _interviewer_context={"input_context":
        _context(text, prior=prior, received=[_exchange(prior)])})
    wrong = _wire(text, "thinking", technical=technical)
    right = _wire(text, expected, technical=technical)
    if not composite:
        turn["allow_followup"] = False
        wrong, right = wrong["understanding"], right["understanding"]
    gateway = Gateway(wrong, right)
    understanding, followup = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.turn_intent == expected
    assert bool(understanding.claims) is technical
    if technical:
        assert understanding.claims[0].evidence_quote == "我会保存确认位置。"
    assert understanding.completion_basis.evidence_quotes == [latest]
    assert len(gateway.requests) == 2 and not followup["selected"]
    correction = "\n".join(message.content for message in gateway.requests[1].messages)
    assert "semantic_current_speech_conflict" in correction and "纠正当前话语" in correction
    assert text not in caplog.text


@pytest.mark.anyio
async def test_stop_scope_mismatch_is_rejected_even_when_the_quote_is_verbatim():
    text = "这题我不会。我们结束面试吧。"
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    interview.update(_semantic_first=True, _interviewer_context={"input_context": _context(text)})
    gateway = Gateway(_wire(text, "decline_topic"), _wire(text, "stop_interview"))
    understanding, _ = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.turn_intent == "stop_interview" and understanding.problem is None
    assert len(gateway.requests) == 2


@pytest.mark.anyio
async def test_repeated_conflict_never_turns_the_phrase_check_into_completion_authority():
    text = "我们结束面试吧。"
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    interview.update(_semantic_first=True, _interviewer_context={"input_context": _context(text)})
    wrong = _wire(text, "decline_topic")
    gateway = Gateway(wrong, wrong)
    understanding, followup = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem.reason_code == "semantic_current_speech_conflict"
    assert understanding.completion_basis is None and not followup["selected"]
    assert not _service(gateway).can_complete_without_confirmation(understanding, text)
    assert len(gateway.requests) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("text,wrong_intent,expected,technical", [
    ("我想一下。这题我不会。", "stop_interview", "decline_topic", False),
    ("我想一下。我会保存确认位置。我已经说完了。", "stop_interview", "finish_answer", True),
    ("我想一下。我会保存确认位置。我已经说完了。", "finish_topic", "finish_answer", True),
])
async def test_answer_or_topic_completion_cannot_be_upgraded_to_a_wider_stop(
    text, wrong_intent, expected, technical,
):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    interview.update(_semantic_first=True, _interviewer_context={"input_context": _context(text, prior="我想一下。")})
    gateway = Gateway(_wire(text, wrong_intent, technical=technical), _wire(text, expected, technical=technical))
    understanding, _ = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.turn_intent == expected
    assert len(gateway.requests) == 2


@pytest.mark.anyio
async def test_answered_concept_question_cannot_become_new_clarification_after_wait():
    question, wait = "缓存是什么意思？", "我来想一下。"
    text = question + wait
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    exchanges = [_exchange(question, reply="缓存是临时保存常用数据。"), _exchange(wait, len(question))]
    interview.update(_semantic_first=True, _interviewer_context={"input_context":
        _context(text, prior=text, received=exchanges)})
    wrong = _wire(text, "clarify", evidence_index="E1", focus=question)
    gateway = Gateway(wrong, _wire(text, "thinking"))
    understanding, _ = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.turn_intent == "thinking"
    assert understanding.clarification_target is None and len(gateway.requests) == 2


@pytest.mark.anyio
async def test_new_concept_request_is_not_erased_just_because_old_request_used_same_words():
    question = "缓存是什么意思？"
    text = question + "刚才没听清，缓存是什么意思？"
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    interview.update(_semantic_first=True, _interviewer_context={"input_context":
        _context(text, prior=question, received=[_exchange(question)])})
    gateway = Gateway(_wire(text, "clarify", evidence_index="E2", focus="缓存是什么意思？"))
    understanding, _ = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.turn_intent == "clarify"
    assert len(gateway.requests) == 1


def test_completion_cannot_only_cite_words_before_current_input_boundary():
    old, new = "我讲完了。", "我还想补充一点。"
    data = {"turn_intent": "finish_answer", "answer_content": "partial",
            "completion_basis": {"evidence_quotes": [old]}, "clarification_target": None}
    with pytest.raises(CurrentSpeechContractError):
        validate_current_speech(data, old + new, _context(old + new, prior=old))
    # Legacy callers with no proven scope keep their existing contract.
    validate_current_speech(data, old + new)


def test_sentence_reference_may_overlap_current_boundary_but_must_include_current_words():
    old, new = "我先想一下，", "这题我不会。"
    data = {"turn_intent": "decline_topic", "answer_content": "none",
            "completion_basis": {"evidence_quotes": [old + new]}, "clarification_target": None}
    validate_current_speech(data, old + new, _context(old + new, prior=old))


@pytest.mark.parametrize("text", [
    "同事说他不会。", "如果我说我们结束面试吧，应该怎么处理？", "我不想结束面试。",
    "日志里写着：我们结束面试吧。", "我引用‘我们结束面试吧’这句话。",
    "这题我不会，不过我用过相似方案，先讲一下。", "我已经说完了，不对，我还想继续讲。",
])
def test_quotes_negations_hypotheticals_and_elaboration_remain_semantic_decisions(text):
    data = {"turn_intent": "thinking", "answer_content": "none",
            "completion_basis": None, "clarification_target": None}
    before = deepcopy(data)
    validate_current_speech(data, text, _context(text))
    assert data == before


@pytest.mark.anyio
@pytest.mark.parametrize("text", ["这题我不会。", "我已经说完了。", "我们结束面试吧。"])
async def test_fast_wait_cannot_swallow_a_direct_control_request(text):
    gateway = Gateway({"kind": "wait", "confidence": .95, "evidence_id": "E1",
                       "reply_text": "好的，你慢慢想。"})
    with pytest.raises(StructuredResponseValidationError, match="conversation contract") as exc:
        await _service(gateway).classify_reception(text, "org_default", preceding_text="我想一下。")
    assert exc.value.reason_code == "reception_current_control_conflict"
    assert len(gateway.requests) == 1


@pytest.mark.anyio
async def test_fast_other_is_still_only_silent_routing_not_completion():
    gateway = Gateway({"kind": "other", "confidence": .95, "evidence_id": "E1", "reply_text": ""})
    result = await _service(gateway).classify_reception("我们结束面试吧。", "org_default")
    assert result["kind"] == "other" and result["reply_text"] == ""
    assert "completion_basis" not in result


def test_updated_contracts_separate_latest_scope_from_technical_evidence():
    contract = prompt_contract("interview_turn_decision", {"semantic_first": True,
        "transcript": "我想一下。这题我不会。", "capability_points": ["恢复"]})
    text = "\n".join(message.content for message in contract.messages)
    assert contract.version == "interview_turn_decision.v105"
    assert "semantic_turn.v19" in text and "current_speech" in text
    assert "不需要再要求候选人重复说" in text and "必须stop_interview" in text
    assert "概念提问、重读请求、思考、不会和结束控制本身都不构成技术claim" in text


@pytest.mark.anyio
async def test_quoted_customer_stop_and_actual_answer_completion_keep_technical_claims():
    text = '客户说“结束面试吧”时，我会记录请求并关闭采集通道。我的回答说完了。'
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    interview.update(_semantic_first=True, _interviewer_context={"input_context": _context(text)})
    wrong = _wire(text, "stop_interview", evidence_index="E1")
    correct = _wire(text, "finish_answer", technical=True)
    correct["understanding"].update(claims=[{"claim": "记录请求并关闭采集", "evidence_id": "E1"}],
                                    evidence_ids=["E1", "E2"])
    gateway = Gateway(wrong, correct)
    understanding, _ = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.turn_intent == "finish_answer"
    assert understanding.claims and "记录请求" in understanding.claims[0].evidence_quote
    assert understanding.completion_basis.evidence_quotes == ["我的回答说完了。"]
    assert len(gateway.requests) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("text", [
    '客户说“结束面试吧”时，我会记录请求并关闭采集通道。',
    '客户说“结束面试吧”时，我会记录请求并关闭采集通道。我的回答说完了。',
    '客户说"结束面试吧"时，我会记录请求。',
])
async def test_repeated_quote_misinterpretation_cannot_authorize_stop(text):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    interview.update(_semantic_first=True, _interviewer_context={"input_context": _context(text)})
    wrong = _wire(text, "stop_interview", evidence_index="E1")
    gateway = Gateway(wrong, wrong)
    understanding, followup = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem.reason_code == "semantic_current_speech_conflict"
    assert understanding.completion_basis is None and not followup["selected"]
    assert len(gateway.requests) == 2


@pytest.mark.parametrize("text,basis", [
    ('客户说“结束面试吧”。我不想继续面试了。', ["我不想继续面试了。"]),
    ('客户说“结束面试吧”，我现在也要结束整场面试。', ['客户说“结束面试吧”，我现在也要结束整场面试。']),
])
def test_own_stop_request_outside_quotation_is_not_blocked(text, basis):
    validate_current_speech({"turn_intent": "stop_interview", "answer_content": "none",
        "completion_basis": {"evidence_quotes": basis}, "clarification_target": None}, text, _context(text))


@pytest.mark.parametrize("text,basis", [
    ('客户说“这题我不会。我们结束面试吧。”我会记录请求。', ["我们结束面试吧。"]),
    ('客户说“结束面试吧”，我会记录请求。', ["结束面试吧"]),
    ('客户说"这题我不会。我们结束面试吧。"我会记录请求。', ["我们结束面试吧。"]),
    ('客户说“结束面试吧”。结束面试吧。', ["结束面试吧"]),
])
def test_completion_evidence_inside_cross_sentence_quote_or_ambiguous_duplicate_is_rejected(text, basis):
    with pytest.raises(CurrentSpeechContractError):
        validate_current_speech({"turn_intent": "stop_interview", "answer_content": "none",
            "completion_basis": {"evidence_quotes": basis}, "clarification_target": None}, text, _context(text))


@pytest.mark.parametrize("purpose,confirmed,expected", [
    ("interview_turn_understanding", False, "interview_turn_understanding.v84"),
    ("interview_turn_understanding", True, "interview_turn_understanding.v85"),
    ("interview_turn_decision", False, "interview_turn_decision.v115"),
    ("interview_turn_decision", True, "interview_turn_decision.v116"),
])
def test_legacy_resolved_context_changes_memory_but_never_the_output_authority(purpose, confirmed, expected):
    old, new = "缓存是什么意思？", "我会保存确认位置。"
    context = {"transcript": old + new, "capability_points": ["恢复"], "completion_confirmed": confirmed,
               "interviewer_context": {"input_context": _context(old + new, prior=old, received=[_exchange(old)])}}
    contract = prompt_contract(purpose, context)
    legacy = prompt_contract(purpose, {key: value for key, value in context.items() if key != "interviewer_context"})
    assert contract.version == expected and contract.response_schema == legacy.response_schema
    content = "\n".join(message.content for message in contract.messages)
    assert "current_speech" in content and "不能将该旧问题再作为clarification_target" in content
    assert "semantic_turn.v19" not in content and "semantic_turn.v20" not in content
    schema = contract.response_schema["properties"].get("understanding", contract.response_schema)
    assert "turn_intent" not in schema["properties"] and "completion_basis" not in schema["properties"]


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [True, False])
async def test_legacy_authoritative_understanding_rejects_old_answered_question_without_completion_permission(composite):
    question, wait = "缓存是什么意思？", "我来想一下。"
    text = question + wait
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    interview["_interviewer_context"] = {"input_context": _context(text, prior=question, received=[_exchange(question)])}
    wrong = _wire(text, "clarify", evidence_index="E1", focus=question)
    correct = _wire(text, "thinking")
    for result in (wrong, correct):
        for key in ("answer_content", "turn_intent", "followup_allowed", "completion_basis"):
            result["understanding"].pop(key)
    if not composite:
        wrong, correct = wrong["understanding"], correct["understanding"]
    gateway = Gateway(wrong, correct)
    service = _service(gateway)
    if composite:
        understanding, followup = await service.prepare_decision(utterance, turn, interview)
        assert not followup["selected"]
    else:
        understanding = await service.understand(utterance, turn, interview)
    assert understanding.problem is None and understanding.suggested_action == "continue_listening"
    assert understanding.turn_intent is None and understanding.completion_basis is None
    assert not service.can_complete_without_confirmation(understanding, text)
    assert len(gateway.requests) == 2
