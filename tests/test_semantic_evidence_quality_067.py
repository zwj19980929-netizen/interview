"""Current control quality and server-derived actions preserve evidence gates."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.semantic_turn import derive_semantic_controls, validate_semantic_turn
from app.domain.interview_agent import ConversationUtterance
from app.domain.speech_quality import control_evidence_confidence, transcript_confidence_segments
from test_answer_declined_understanding import no_followup
from test_prepared_turn_decision import Gateway, _input, _service
from test_semantic_recovery_066 import unresolved_response, _clarification_context
from test_semantic_turn_policy import semantic_response


def _speech(parts, confidence, *, acoustic=None):
    utterance, turn, interview = _input()
    segments = [{"text": text, "start_ms": i * 1000, "end_ms": (i + 1) * 1000,
                 "confidence": measured} for i, (text, measured) in enumerate(zip(parts, confidence))]
    text = "".join(parts)
    known = [value for value in confidence if value is not None]
    utterance = ConversationUtterance.model_validate({**utterance.model_dump(), "text": text,
        "stt_confidence": acoustic if acoustic is not None else min(known) if known else None,
        "stt_segments": transcript_confidence_segments(text, segments)})
    interview["_semantic_first"] = True
    interview["_interviewer_context"] = {"input_context": {
        "version": "conversation_input_context.v1", "current_speech_start": 0,
        "current_speech": text, "responded_exchanges": []}}
    return utterance, turn, interview


def test_exact_source_mapping_keeps_original_spelling_and_occurrence_positions():
    text = "同句。同句。"
    spans = transcript_confidence_segments(text, [
        {"text": " 同句。", "confidence": .3, "start_ms": 0, "end_ms": 1000},
        {"text": "同句。\n", "confidence": .99, "start_ms": 1000, "end_ms": 2000},
    ])
    assert [row["text"] for row in spans] == ["同句。", "同句。"]
    assert control_evidence_confidence(text, spans, [(3, 6)]) == .99
    assert control_evidence_confidence(text, spans, [(0, 6)]) == .3


@pytest.mark.parametrize("change", ["rewrite", "overlap", "unknown", "gap", "mutated", "missing"])
def test_uncertain_or_mutated_control_source_cannot_invent_higher_quality(change):
    text = "旧句。新句。"
    raw = [{"text": "旧句。", "confidence": .3, "start_ms": 0, "end_ms": 1000},
           {"text": "新句。", "confidence": .99, "start_ms": 1000, "end_ms": 2000}]
    if change == "rewrite":
        raw[1]["text"] = "改句。"
    elif change == "overlap":
        raw[1]["start_ms"] = 500
    elif change == "unknown":
        raw[1]["confidence"] = None
    spans = transcript_confidence_segments(text, raw)
    if change == "gap":
        spans = spans[1:]
    elif change == "mutated":
        spans[1]["text"] = "改句。"
    elif change == "missing":
        spans = []
    assert control_evidence_confidence(text, spans, [(3, 6)]) is None


def test_utterance_rejects_incomplete_source_and_approved_speech_cannot_supply_acoustics():
    utterance, _, _ = _speech(["旧句。", "新句。"], [.3, .99])
    for update in ({"stt_segments": [utterance.stt_segments[1].model_dump()]},
                   {"source": "approved_act", "authoritative": False}):
        with pytest.raises(ValidationError):
            ConversationUtterance.model_validate({**utterance.model_dump(), **update})


@pytest.mark.anyio
@pytest.mark.parametrize("technical", [False, True])
async def test_clear_completion_after_delivered_clarification_does_not_upgrade_technical_quality(technical):
    text, before, data = unresolved_response(technical=technical)
    reply = text[len(before):]
    utterance, turn, interview = _speech([before, reply], [.3, .99])
    turn["allow_followup"] = False
    interview["_interviewer_context"] = {"content_clarification": _clarification_context(before)}
    gateway = Gateway(data)
    service = _service(gateway)
    result, followup = await service.prepare_decision(utterance, turn, interview)
    assert result.problem is None and result.intent == "answer_unresolved"
    assert result.confidence == .95 and service.can_complete_without_confirmation(result, text)
    assert result.ambiguities and bool(result.claims) is technical and not followup["selected"]
    assert utterance.stt_confidence == .3 and utterance.stt_segments[0].confidence == .3
    assert utterance.text == text and len(gateway.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("quality,segments", [(.3, True), (None, True), (.99, False)])
async def test_uncertain_current_completion_still_requires_clarification(quality, segments):
    text, before, data = unresolved_response()
    utterance, turn, interview = _speech([before, text[len(before):]], [.3, quality])
    if not segments:
        utterance = utterance.model_copy(update={"stt_segments": []})
    turn["allow_followup"] = False
    interview["_interviewer_context"] = {"content_clarification": _clarification_context(before)}
    service = _service(Gateway(data))
    result, followup = await service.prepare_decision(utterance, turn, interview)
    assert result.problem is None and result.confidence == .3
    assert result.completion_basis is None and result.suggested_action == "clarify"
    assert not service.can_complete_without_confirmation(result, text) and not followup["selected"]


@pytest.mark.anyio
@pytest.mark.parametrize("intent,reply", [("decline_topic", "这题我不会。"), ("stop_interview", "我不想面了。")])
async def test_clear_current_decline_or_exit_has_its_own_quality_but_exit_requires_separate_confirmation(intent, reply):
    before = "我先想一下。"
    utterance, turn, interview = _speech([before, reply], [.3, .99])
    turn["allow_followup"] = False
    data = semantic_response(content="none", intent=intent, technical=False)["understanding"]
    data["evidence_ids"] = ["E2"]
    interview["_interviewer_context"] = {"input_context": {
        "version": "conversation_input_context.v1", "current_speech_start": len(before),
        "current_speech": reply, "responded_exchanges": []}}
    service = _service(Gateway(data))
    result, _ = await service.prepare_decision(utterance, turn, interview)
    assert result.problem is None and result.turn_intent == intent and result.confidence == .9
    assert result.completion_basis.evidence_quotes == [reply]
    assert service.can_complete_without_confirmation(result, utterance.text) is (intent == "decline_topic")
    assert utterance.stt_confidence == .3


@pytest.mark.anyio
async def test_better_acoustics_cannot_bypass_missing_delivered_clarification():
    text, before, data = unresolved_response()
    utterance, turn, interview = _speech([before, text[len(before):]], [.3, .99])
    turn["allow_followup"] = False
    service = _service(Gateway(data, data))
    result, followup = await service.prepare_decision(utterance, turn, interview)
    assert result.problem.reason_code == "unresolved_clarification_required"
    assert not service.can_complete_without_confirmation(result, text) and not followup["selected"]


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [False, True])
async def test_both_understanding_paths_derive_controls_without_reclassifying_or_retrying(composite):
    utterance, turn, interview = _speech(["我用幂等键。", "下一题吧。"], [.8, .99])
    turn["allow_followup"] = composite
    response = semantic_response()
    response["understanding"].update(suggested_action="continue_listening", followup_allowed=True)
    gateway = Gateway(response if composite else response["understanding"])
    service = _service(gateway)
    result, followup = await service.prepare_decision(utterance, turn, interview)
    assert result.problem is None and result.turn_intent == "finish_topic"
    assert result.suggested_action == "next" and result.followup_allowed is False
    assert result.confidence == .9 and result.claims and result.completion_basis.evidence_quotes == ["下一题吧。"]
    assert not followup["selected"] and len(gateway.requests) == 1
    validate_semantic_turn(result.model_dump(), utterance.text)


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["basis_missing", "basis_forged", "retraction", "quotation", "claims_missing"])
async def test_derived_controls_never_repair_untrusted_meaning_or_completion_evidence(change):
    reply = "下一题吧。"
    if change == "retraction":
        reply = "下一题。不对我还没说完。"
    elif change == "quotation":
        reply = "客户说：‘下一题。’我会记录他的要求。"
    utterance, turn, interview = _speech(["我用幂等键。", reply], [.3, .99])
    turn["allow_followup"] = False
    data = semantic_response()["understanding"]
    data.update(suggested_action="continue_listening", followup_allowed=True)
    if change == "basis_missing":
        data["completion_basis"] = None
    elif change == "basis_forged":
        data["completion_basis"]["evidence_ids"] = ["E99"]
    elif change == "claims_missing":
        data["claims"] = []
    gateway = Gateway(deepcopy(data), deepcopy(data))
    service = _service(gateway)
    result, followup = await service.prepare_decision(utterance, turn, interview)
    assert result.problem is not None and result.completion_basis is None
    assert not service.can_complete_without_confirmation(result, utterance.text) and not followup["selected"]


def test_repeated_current_control_needs_an_exact_fresh_boundary_for_quality_upgrade():
    utterance, _, _ = _speech(["我说完了。", "我说完了。"], [.99, .3])
    assert _service(Gateway())._control_evidence_quality(utterance, [(0, 5)], {}) is None
    context = {"input_context": {"version": "conversation_input_context.v1", "current_speech_start": 5,
                               "current_speech": "我说完了。", "responded_exchanges": []}}
    assert _service(Gateway())._control_evidence_quality(utterance, [(0, 5)], context) is None
    assert _service(Gateway())._control_evidence_quality(utterance, [(5, 10)], context) == .3


def test_control_projection_is_pure_and_prompt_keeps_strict_provider_compatible_shape():
    raw = semantic_response()["understanding"]
    raw.update(suggested_action="pause", followup_allowed=True)
    before = deepcopy(raw)
    projected = derive_semantic_controls(raw)
    assert raw == before and projected["suggested_action"] == "next" and not projected["followup_allowed"]
    assert projected["completion_basis"] == raw["completion_basis"] and projected["claims"] == raw["claims"]
    contract = prompt_contract("interview_turn_understanding", {"semantic_first": True,
        "transcript": "我用幂等键。下一题吧。", "capability_points": ["幂等键"]})
    assert contract.version == "interview_turn_understanding.v62"
    assert "semantic_controls.v1" in "".join(message.content for message in contract.messages)
    assert {"suggested_action", "followup_allowed", "completion_basis"} <= set(contract.response_schema["required"])
