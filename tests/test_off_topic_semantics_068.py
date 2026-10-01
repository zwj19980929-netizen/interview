"""Clear irrelevant speech is answer evidence, never a recognition failure.

All transcripts and provider outputs are synthetic; no external model calls.
"""
from copy import deepcopy

import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.understanding_references import understanding_references
from app.core.prompt.validation import StructuredResponseValidationError
from test_prepared_turn_decision import Gateway, _input, _service
from test_semantic_evidence_quality_067 import _speech
from test_semantic_recovery_066 import _clarification_context


BODY = "周末我和朋友玩了一场桌游，最后我们一起赢了。"
FINISH = "我的话说完了。"
TEXT = BODY + FINISH


def off_topic_response(text=TEXT, points=("幂等键", "确认序号"), *, intent="finish_answer",
                       technical=False, relevant=False):
    refs = understanding_references(text, list(points))
    evidence_ids = list(refs["evidence"])
    ending = intent in {"finish_answer", "finish_topic", "decline_topic", "stop_interview"}
    data = {
        "intent": "answer" if relevant else "off_topic",
        "answer_summary": "描述了持久检查点。" if relevant else "谈论了与本题无关的个人经历。",
        "claims": ([{"claim": list(refs["evidence"].values())[0], "evidence_id": evidence_ids[0]}]
                   if technical else []),
        "evidence_ids": evidence_ids,
        "covered_point_ids": list(refs["capabilities"])[:1] if relevant else [],
        "missing_point_ids": list(refs["capabilities"])[1:] if relevant else list(refs["capabilities"]),
        "ambiguities": [], "contradictions": [], "clarification_target": None,
        "confidence": .96, "answer_content": "partial" if technical else "none",
        "turn_intent": intent, "suggested_action": "next" if ending else "accept",
        "followup_allowed": relevant and intent in {"answering", "finish_answer"},
        "completion_basis": {"source": "explicit_server_intent", "evidence_ids": evidence_ids[-1:]} if ending else None,
    }
    return {"understanding": data, "followup": {
        "selected": False, "question_text": "", "evidence_id": "", "target_point_ids": [],
        "rationale": "", "difficulty": "mid", "sensitive_attribute_inference": False, "leaks_answer": False,
    }}


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [False, True])
@pytest.mark.parametrize("turn_intent,tail", [("finish_answer", FINISH),
    ("finish_topic", "下一题吧。"), ("decline_topic", "这题我不会了。"),
    ("stop_interview", "我不想继续面试了。")])
async def test_clear_off_topic_can_finish_once_without_fabricating_claims(composite, turn_intent, tail):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": BODY + tail})
    interview["_semantic_first"] = True
    turn["allow_followup"] = composite
    wire = off_topic_response(utterance.text, intent=turn_intent)
    service = _service(Gateway(wire if composite else wire["understanding"]))

    result, followup = await service.prepare_decision(utterance, turn, interview)

    assert result.problem is None and result.intent == "off_topic"
    assert result.answer_content == "none" and result.claims == []
    assert result.covered_capability_points == [] and result.missing_capability_points == ["幂等键", "确认序号"]
    assert result.ambiguities == [] and result.clarification_target is None
    assert result.evidence_quotes == [BODY, tail]
    assert result.completion_basis.evidence_quotes == [tail]
    assert result.followup_allowed is False and not followup["selected"]
    assert service.can_complete_without_confirmation(result, utterance.text) is (turn_intent != "stop_interview")
    assert service.accepts_as_answer(result, utterance.text)
    assert len(service.gateway.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("intent", ["answering", "thinking"])
async def test_unfinished_clear_irrelevance_is_understood_without_completion_or_clarification(intent):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": BODY})
    interview["_semantic_first"] = True
    service = _service(Gateway(off_topic_response(BODY, intent=intent)))
    result, followup = await service.prepare_decision(utterance, turn, interview)
    assert result.problem is None and result.intent == "off_topic"
    assert result.suggested_action == ("continue_listening" if intent == "thinking" else "accept")
    assert result.completion_basis is None and not result.followup_allowed and not followup["selected"]
    assert not service.can_complete_without_confirmation(result, BODY)
    assert not service.accepts_as_answer(result, BODY)


@pytest.mark.anyio
@pytest.mark.parametrize("relevant", [False, True])
async def test_real_technical_claim_survives_irrelevant_tail_without_invented_coverage(relevant):
    body = "我使用幂等键记录已完成请求。" if relevant else "我曾使用图形引擎绘制三维场景。"
    text = body + BODY + FINISH
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    interview["_semantic_first"] = True
    service = _service(Gateway(off_topic_response(text, technical=True, relevant=relevant)))
    result, followup = await service.prepare_decision(utterance, turn, interview)
    assert result.problem is None and result.intent == ("answer" if relevant else "off_topic")
    assert result.answer_content == "partial" and result.claims[0].evidence_quote == body
    assert result.covered_capability_points == (["幂等键"] if relevant else [])
    assert result.followup_allowed is relevant and not followup["selected"]
    assert service.can_complete_without_confirmation(result, text)


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [False, True])
async def test_covered_point_and_bound_technical_claim_resolve_contradictory_off_topic_label_once(composite):
    text = "多表写操作放进同一事务。" + BODY + FINISH
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    turn["question_snapshot"]["key_points"] = ["原子性", "隔离性"]
    turn["allow_followup"] = composite
    interview["_semantic_first"] = True
    wire = off_topic_response(text, ["原子性", "隔离性"], technical=True, relevant=True)
    wire["understanding"].update(intent="off_topic", followup_allowed=False)
    original = deepcopy(wire)
    service = _service(Gateway(wire if composite else wire["understanding"]))

    result, _ = await service.prepare_decision(utterance, turn, interview)

    assert result.problem is None and result.intent == "answer" and result.followup_allowed
    assert result.answer_content == "partial" and result.claims[0].evidence_quote == "多表写操作放进同一事务。"
    assert result.covered_capability_points == ["原子性"] and result.missing_capability_points == ["隔离性"]
    assert result.completion_basis.evidence_quotes == [FINISH]
    assert len(service.gateway.requests) == 1 and wire == original


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", ["no_claim", "undeclared_claim", "foreign_point", "point_partition", "ambiguity", "basis_missing"])
async def test_relevance_normalization_cannot_repair_missing_evidence_or_completion(invalid):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": "我使用幂等键。" + BODY + FINISH})
    interview["_semantic_first"] = True
    wire = off_topic_response(utterance.text, technical=True, relevant=True)
    data = wire["understanding"]
    data.update(intent="off_topic", followup_allowed=False)
    if invalid == "no_claim": data["claims"] = []
    elif invalid == "undeclared_claim": data["evidence_ids"] = ["E2", "E3"]
    elif invalid == "foreign_point": data["covered_point_ids"] = ["P99"]
    elif invalid == "point_partition": data["missing_point_ids"] = ["P1", "P2"]
    elif invalid == "ambiguity": data["ambiguities"] = ["技术含义待核实。"]
    elif invalid == "basis_missing": data["completion_basis"] = None
    service = _service(Gateway(deepcopy(wire), deepcopy(wire)))
    result, followup = await service.prepare_decision(utterance, turn, interview)
    assert result.problem is not None and result.completion_basis is None and not followup["selected"]


@pytest.mark.anyio
async def test_old_erroneous_clarification_does_not_make_clear_irrelevance_unresolved():
    utterance, turn, interview = _speech([BODY, FINISH], [.96, .99])
    interview["_interviewer_context"]["content_clarification"] = _clarification_context(BODY)
    service = _service(Gateway(off_topic_response()))
    result, followup = await service.prepare_decision(utterance, turn, interview)
    assert result.problem is None and result.intent == "off_topic" and not result.ambiguities
    assert service.can_complete_without_confirmation(result, TEXT) and not followup["selected"]
    assert len(service.gateway.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["claims_on_none", "coverage", "ambiguity", "missing_basis",
    "foreign_evidence", "low_semantic", "quoted_finish", "retracted_finish", "old_finish"])
async def test_irrelevance_never_relaxes_source_quality_or_completion_protection(case):
    text = TEXT
    if case == "quoted_finish": text = BODY + "朋友说：‘我说完了。’"
    if case == "retracted_finish": text = BODY + "我说完了。不对，我还没说完。"
    if case == "old_finish": text = TEXT + "再说一点。"
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    interview["_semantic_first"] = True
    wire = off_topic_response(text)
    interview["_interviewer_context"] = {"input_context": {
        "version": "conversation_input_context.v1", "current_speech_start": 0,
        "current_speech": text, "responded_exchanges": []}}
    data = wire["understanding"]
    if case in {"quoted_finish", "retracted_finish", "old_finish"}:
        data["completion_basis"]["evidence_ids"] = ["E2"]
    if case == "claims_on_none": data["claims"] = [{"claim": "虚构技术内容", "evidence_id": "E1"}]
    elif case == "coverage": data.update(covered_point_ids=["P1"], missing_point_ids=["P2"])
    elif case == "ambiguity": data.update(ambiguities=["术语无法核实"], clarification_target={"evidence_id": "E1", "focus_quote": "桌游"})
    elif case == "missing_basis": data["completion_basis"] = None
    elif case == "foreign_evidence": data["evidence_ids"] = ["E99"]
    elif case == "low_semantic": data["confidence"] = .74
    elif case == "old_finish":
        interview["_interviewer_context"] = {"input_context": {
            "version": "conversation_input_context.v1", "current_speech_start": len(TEXT),
            "current_speech": "再说一点。", "responded_exchanges": []}}
    service = _service(Gateway(deepcopy(wire), deepcopy(wire)))
    result, followup = await service.prepare_decision(utterance, turn, interview)
    assert result.problem is not None and result.completion_basis is None
    assert not service.can_complete_without_confirmation(result, text) and not followup["selected"]
    assert not service.accepts_as_answer(result, text)


@pytest.mark.anyio
async def test_low_acoustic_quality_of_current_finish_never_authorizes_off_topic_commit():
    utterance, turn, interview = _speech([BODY, FINISH], [.96, .3])
    turn["allow_followup"] = False
    service = _service(Gateway(off_topic_response()["understanding"]))
    result, followup = await service.prepare_decision(utterance, turn, interview)
    assert result.problem is None and result.suggested_action == "clarify"
    assert result.completion_basis is None and not service.accepts_as_answer(result, TEXT)
    assert not service.can_complete_without_confirmation(result, TEXT) and not followup["selected"]


@pytest.mark.anyio
async def test_legacy_off_topic_label_does_not_acquire_new_submission_permission():
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": BODY})
    data = off_topic_response(BODY, intent="answering")["understanding"]
    for name in ("turn_intent", "answer_content", "followup_allowed", "completion_basis"):
        data.pop(name)
    turn["allow_followup"] = False
    service = _service(Gateway(data))
    result = await service.understand(utterance, turn, interview)
    assert result.problem is None and result.intent == "off_topic"
    assert not service.accepts_as_answer(result, BODY)


@pytest.mark.parametrize("purpose", ["interview_turn_understanding", "interview_turn_decision"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_versioned_contract_distinguishes_clear_irrelevance_from_recognition_and_refusal(purpose, confirmed):
    contract = prompt_contract(purpose, {"transcript": TEXT, "capability_points": ["幂等键"],
        "semantic_first": True, "completion_confirmed": confirmed})
    text = "\n".join(message.content for message in contract.messages)
    assert "跑题是相关性判断，不是识别失败" in text
    assert "只有控制请求或寒暄不能虚构一份off_topic回答" in text
    assert "不能因旧澄清记录继续制造歧义" in text
    assert "semantic_controls.v2" in text


@pytest.mark.anyio
async def test_reception_business_rejection_logs_only_fixed_reason_and_source_identifiers(caplog):
    reply = "你愿意先说一下私人爱好吗？"
    gateway = Gateway({"kind": "interview_dialogue", "confidence": .95,
                       "evidence_id": "E1", "reply_text": reply})
    with pytest.raises(StructuredResponseValidationError) as error:
        await _service(gateway).classify_reception(BODY, "org_default", current_turn_id="turn_synthetic")
    assert error.value.reason_code == "reception_cannot_ask_question"
    assert "conversation_reception_rejected" in caplog.text
    assert "validation_stage=reception_content" in caplog.text
    assert "reason=reception_cannot_ask_question path=$.reply_text" in caplog.text
    assert "turn_id=turn_synthetic provider_request_id=synthetic_request" in caplog.text
    assert BODY not in caplog.text and reply not in caplog.text
