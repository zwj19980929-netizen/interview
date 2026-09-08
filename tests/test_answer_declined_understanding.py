"""A clearly declined question is distinct from unreliable speech evidence."""

from copy import deepcopy

import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.understanding_references import understanding_references
from app.core.prompt.validation import StructuredResponseValidationError, validate_structured_response
from app.domain.interview_agent import TurnUnderstanding
from app.services.conversation_understanding import ConversationUnderstandingService, UnderstandingContentError
from test_prepared_turn_decision import Gateway, _input, _service


def declined_data(transcript):
    references = understanding_references(transcript, ["幂等键", "确认序号"])
    return {
        "intent": "answer_declined", "answer_summary": "候选人表示本题不再作答。",
        "claims": [], "evidence_ids": list(references["evidence"]),
        "covered_point_ids": [], "missing_point_ids": ["P1", "P2"],
        "ambiguities": [], "contradictions": [], "confidence": .95,
        "suggested_action": "next",
    }


def no_followup():
    return {
        "selected": False, "question_text": "", "evidence_id": "",
        "target_point_ids": [], "rationale": "", "difficulty": "mid",
        "sensitive_attribute_inference": False, "leaks_answer": False,
    }


@pytest.mark.parametrize("confirmed,expected_understanding,expected_decision", [
    (False, "interview_turn_understanding.v6", "interview_turn_decision.v5"),
    (True, "interview_turn_understanding.v7", "interview_turn_decision.v6"),
])
def test_declined_contract_versions_and_reference_schema(confirmed, expected_understanding, expected_decision):
    text = "这部分没有做过，咱们聊下一道吧。"
    context = {"transcript": text, "capability_points": ["幂等键", "确认序号"],
               "completion_confirmed": confirmed}
    contract = prompt_contract("interview_turn_understanding", context)
    decision = prompt_contract("interview_turn_decision", context)
    assert contract.version == expected_understanding and decision.version == expected_decision
    validate_structured_response(declined_data(text), contract.response_schema)
    validate_structured_response({"understanding": declined_data(text), "followup": no_followup()},
                                 decision.response_schema)
    for invalid in ({"extra": True}, {"intent": "skip_by_keyword"}, {"evidence_ids": ["E999"]}):
        with pytest.raises(StructuredResponseValidationError):
            validate_structured_response({**declined_data(text), **invalid}, contract.response_schema)


@pytest.mark.parametrize("text,confirmed", [
    ("我也不知道，这题答不上来，进入下一题吧。", False),
    ("这块没有接触过，咱们接着聊下一道吧。", False),
    ("我得想一下。还是没想起来，就答到这里吧。", True),
    ("我现在没有补充了。没有补充了。", True),
    ("没有别的要说了，接着往下吧。", True),
])
@pytest.mark.anyio
async def test_semantic_decline_preserves_evidence_without_technical_claims_or_followup(text, confirmed):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    interview["_answer_completion_confirmed"] = confirmed
    gateway = Gateway({"understanding": declined_data(text), "followup": no_followup()})
    understanding, followup = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.intent == "answer_declined" and understanding.suggested_action == "next"
    assert understanding.confidence >= .75 and understanding.problem is None
    assert understanding.claims == [] and understanding.covered_capability_points == []
    assert understanding.missing_capability_points == ["幂等键", "确认序号"]
    assert understanding.evidence_quotes and all(quote in text for quote in understanding.evidence_quotes)
    assert not understanding.ambiguities and not followup["selected"]
    assert len(gateway.requests) == 1


@pytest.mark.parametrize("change", [
    {"suggested_action": "accept"}, {"confidence": .74}, {"answer_summary": ""},
    {"evidence_ids": []}, {"claims": [{"claim": "虚构的技术做法", "evidence_id": "E1"}]},
    {"covered_point_ids": ["P1"], "missing_point_ids": ["P2"]},
    {"ambiguities": ["尚未确定发言含义"]}, {"contradictions": ["答复仍矛盾"]},
])
@pytest.mark.anyio
async def test_malformed_decline_cannot_become_an_authorized_next_action(change):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": "这题我不会，进入下一题吧。"})
    interview["_answer_completion_confirmed"] = True
    value = {"understanding": {**declined_data(utterance.text), **change}, "followup": no_followup()}
    gateway = Gateway(value, value)
    result, followup = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert result.intent == "clarification_request" and result.suggested_action == "pause"
    assert result.problem.reason_code == "answer_declined_contract_invalid"
    assert result.problem.attempts == 2 and not followup["selected"]


def test_declined_evidence_must_be_nonblank_even_when_called_after_generic_validation():
    value = {
        "intent": "answer_declined", "answer_summary": "未作答", "claims": [],
        "evidence_quotes": [" "], "covered_capability_points": [],
        "missing_capability_points": ["幂等键"], "ambiguities": [], "contradictions": [],
        "confidence": .95, "suggested_action": "next",
    }
    with pytest.raises(UnderstandingContentError, match="answer_declined_contract_invalid"):
        ConversationUnderstandingService._validate_understanding_content(value, "这题不会。", ["幂等键"])


@pytest.mark.parametrize("acoustic_confidence", [.5, .7])
@pytest.mark.anyio
async def test_low_speech_confidence_cannot_authorize_decline(acoustic_confidence):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": "这题不会，进入下一题吧。", "stt_confidence": acoustic_confidence})
    turn["allow_followup"] = False
    interview["_answer_completion_confirmed"] = True
    gateway = Gateway(declined_data(utterance.text))
    result, followup = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert result.intent == "clarification_request" and result.suggested_action == "clarify"
    assert result.confidence < .75 and not followup["selected"]


@pytest.mark.parametrize("text,intent,action,confidence,ambiguities", [
    ("现在还没想到，让我再想想。", "not_finished", "continue_listening", .95, []),
    ("我没有补充，但刚才那段根本不是我说的。", "clarification_request", "clarify", .6, ["候选人否认转写内容"]),
])
@pytest.mark.anyio
async def test_existing_wait_and_transcription_dispute_are_not_coerced_to_decline(text, intent, action, confidence, ambiguities):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    turn["allow_followup"] = False
    interview["_answer_completion_confirmed"] = intent != "not_finished"
    value = {**declined_data(text), "intent": intent, "suggested_action": action,
             "confidence": confidence, "ambiguities": ambiguities}
    result, followup = await _service(Gateway(value)).prepare_decision(utterance, turn, interview)
    assert result.intent == intent and result.suggested_action == action
    assert result.problem is None and not followup["selected"]


@pytest.mark.anyio
async def test_technical_answer_with_term_misspelling_and_ending_stays_an_answer():
    utterance, turn, interview = _input()
    text = "我用redios缓存确认序号，恢复时从最后一个序号继续。底层细节不会了，下一题吧。"
    utterance = utterance.model_copy(update={"text": text})
    turn["allow_followup"] = False
    interview["_answer_completion_confirmed"] = True
    value = {**declined_data(text), "intent": "answer", "answer_summary": "记录确认序号用于恢复。",
             "claims": [{"claim": "缓存确认序号", "evidence_id": "E1"}],
             "evidence_ids": ["E1"], "covered_point_ids": ["P2"], "missing_point_ids": ["P1"],
             "suggested_action": "accept"}
    result, _ = await _service(Gateway(value)).prepare_decision(utterance, turn, interview)
    assert result.intent == "answer" and result.suggested_action == "accept" and result.claims
    assert result.evidence_quotes == [text.split("。")[0] + "。"]
    assert result.problem is None and not result.ambiguities


@pytest.mark.anyio
async def test_historical_versions_remain_readable_without_migrating_old_facts():
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": "这题不会，进入下一题吧。"})
    turn["allow_followup"] = False
    result = await _service(Gateway(declined_data(utterance.text))).understand(utterance, turn, interview)
    payload = result.model_dump(mode="json")
    for version in ("interview_turn_understanding.v1", "interview_turn_understanding.v5", "interview_turn_decision.v4"):
        historical = deepcopy(payload)
        historical.update(prompt_version=version, intent="clarification_request", suggested_action="clarify")
        assert TurnUnderstanding.model_validate(historical).prompt_version == version
