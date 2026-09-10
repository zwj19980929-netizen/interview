import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.validation import StructuredResponseValidationError, validate_structured_response
from app.core.time import utc_now
from app.domain.interview_agent import ConversationUtterance
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.repositories.memory import InMemoryStore
from app.services.conversation_understanding import ConversationUnderstandingService


TRANSCRIPT = "我会记录音频序号，重连时从最后确认的位置继续。"


def _input():
    turn = {
        "id": "turn_prepared", "root_turn_id": "turn_prepared",
        "is_followup": False, "followup_depth": 0, "allow_followup": True,
        "question_spoken_text": "请说明如何实现断线恢复。",
        "question_snapshot": {
            "question_text": "请说明如何实现断线恢复。",
            "key_points": [{"text": "幂等键"}, {"text": "确认序号"}],
            "difficulty": "mid", "standard_answer": "recoverable_checkpoint fenced_epoch",
        },
    }
    interview = {
        "id": "iv_prepared", "organization_id": "org_default", "turns": [turn],
        "scheduled_end_at": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
    }
    utterance = ConversationUtterance(
        utterance_id="utterance_prepared", revision=1, speaker="candidate",
        text=TRANSCRIPT, is_final=True, authoritative=True,
        audio_uri="private-file://synthetic_audio", stt_confidence=0.95,
        source="server_streaming", created_at=utc_now(),
    )
    return utterance, turn, interview


def _data():
    return {
        "understanding": {"clarification_target": None,
            "intent": "answer", "answer_summary": "使用序号记录恢复位置。",
            "claims": [{"claim": "记录确认位置", "evidence_id": "E1"}],
            "evidence_ids": ["E1"], "covered_point_ids": ["P2"],
            "missing_point_ids": ["P1"], "ambiguities": [], "contradictions": [],
            "confidence": 0.9, "suggested_action": "followup",
        },
        "followup": {
            "selected": True, "question_text": "你如何避免恢复后重复提交？",
            "evidence_id": "E1", "target_point_ids": ["P1"],
            "rationale": "核验缺失能力点", "difficulty": "mid",
            "sensitive_attribute_inference": False, "leaks_answer": False,
        },
    }


class Gateway:
    def __init__(self, *responses, error=None):
        self.responses = list(responses)
        self.requests = []
        self.error = error

    async def invoke(self, capability, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        data = self.responses.pop(0)
        return ChatJSONResponse(
            data=deepcopy(data), usage=Usage(), provider=ProviderMeta(
                provider_id="synthetic_test", model="prepared_decision_test",
                request_id="synthetic_request", latency_ms=1,
            ),
        )


def _service(gateway):
    return ConversationUnderstandingService(InMemoryStore(), gateway=gateway)


@pytest.mark.anyio
async def test_one_inference_prepares_understanding_and_fenced_evidence_bound_followup():
    gateway = Gateway(_data())
    utterance, turn, interview = _input()
    before = deepcopy(interview)
    understanding, decision = await _service(gateway).prepare_decision(utterance, turn, interview)

    assert len(gateway.requests) == 1
    assert gateway.requests[0].purpose == "interview_turn_understanding"
    assert gateway.requests[0].metadata["prompt_version"] == "interview_turn_decision.v7"
    assert understanding.prompt_version == "interview_turn_decision.v7"
    assert understanding.evidence_quotes == [TRANSCRIPT]
    assert understanding.claims[0].evidence_quote == TRANSCRIPT
    assert decision["selected"] is True
    assert decision["target_key_points"] == ["幂等键"]
    assert decision["conversation_act"]["approved_by"] == "controlled_followup_gate"
    assert decision["conversation_act"]["prompt_version"] == "interview_turn_decision.v7"
    assert decision["conversation_act"]["evaluative"] is False
    assert decision["followup_depth"] == 1
    assert decision["evidence_quotes"] == [TRANSCRIPT]
    assert interview == before  # preparation has no domain/persistence effects
    assert "recoverable_checkpoint" not in str(gateway.requests[0].messages)


@pytest.mark.parametrize("text,intent", [
    ("请再说一遍。", "request_repeat"),
    ("我还没说完。", "not_finished"),
    ("请暂停一下。", "pause"),
])
@pytest.mark.anyio
async def test_deterministic_meta_uses_no_llm_and_never_prepares_probe(text, intent):
    gateway = Gateway()
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text, "stt_confidence": 0.1})
    understanding, decision = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.intent == intent
    assert decision["selected"] is False
    assert gateway.requests == []


@pytest.mark.anyio
async def test_unresolved_semantic_ambiguity_overrides_a_confident_advancement_proposal():
    data = _data()
    data["understanding"]["ambiguities"] = ["候选人否认了字幕中的技术主张，需先核实实际表达。"]
    gateway = Gateway(data)
    utterance, turn, interview = _input()
    understanding, decision = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.suggested_action == "clarify" and understanding.confidence < .65
    assert not decision["selected"]
    assert understanding.evidence_quotes == [TRANSCRIPT]


@pytest.mark.parametrize("case,reason", [
    ("disabled", "followup_disabled"),
    ("depth", "depth_budget_exhausted"),
    ("time", "time_budget_exhausted"),
    ("session", "session_budget_exhausted"),
    ("length", "answer_length_outside_budget"),
    ("confidence", "low_confidence_clarification_required"),
])
@pytest.mark.anyio
async def test_preflight_ineligible_cases_only_run_existing_understanding(case, reason):
    utterance, turn, interview = _input()
    if case == "disabled":
        turn["allow_followup"] = False
    elif case == "depth":
        turn["followup_depth"] = 2
    elif case == "time":
        interview["scheduled_end_at"] = datetime.now(timezone.utc).isoformat()
    elif case == "session":
        interview["turns"].append({"id": "already_probed", "is_followup": True})
    elif case == "length":
        interview["followup_policy"] = {"min_answer_chars": 1000}
    elif case == "confidence":
        utterance = utterance.model_copy(update={"stt_confidence": 0.2})
    gateway = Gateway(_data()["understanding"])
    understanding, decision = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert len(gateway.requests) == 1
    assert gateway.requests[0].metadata["prompt_version"] == "interview_turn_understanding.v8"
    assert decision["selected"] is False
    assert decision["reason"] == reason
    if case == "confidence":
        assert understanding.suggested_action == "clarify"


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(extra="not allowed"),
    lambda d: d["understanding"].update(covered_point_ids=[], missing_point_ids=[]),
    lambda d: d["understanding"].update(missing_point_ids=["P1", "P1"]),
    lambda d: d["understanding"]["claims"][0].update(evidence_id="E999"),
    lambda d: d["followup"].update(target_point_ids=["P1", "P1"]),
    lambda d: d["followup"].update(evidence_id="E999"),
    lambda d: d["followup"].update(difficulty="expert"),
])
@pytest.mark.anyio
async def test_entire_wire_and_understanding_evidence_must_pass_before_approval(mutate, caplog):
    data = _data()
    mutate(data)
    gateway = Gateway(data, data)
    understanding, decision = await _service(gateway).prepare_decision(*_input())
    assert len(gateway.requests) == 2
    assert understanding.problem.code == "UNDERSTANDING_RESULT_REJECTED"
    assert understanding.problem.attempts == 2
    assert understanding.claims == []
    assert understanding.evidence_quotes == []
    assert decision["selected"] is False
    assert "conversation_act" not in decision
    assert TRANSCRIPT not in caplog.text
    assert "婚姻情况" not in caplog.text
    assert "上一轮结果未通过合同校验" in str(gateway.requests[1].messages)


@pytest.mark.parametrize("mutate", [
    lambda d: d["followup"].update(target_point_ids=["P2"]),
    lambda d: d["followup"].update(question_text="你的婚姻情况是什么？"),
    lambda d: d["followup"].update(question_text="你如何实现？怎么验证？"),
    lambda d: d["followup"].update(question_text="recoverable_checkpoint fenced_epoch 如何使用？"),
    lambda d: d["followup"].update(sensitive_attribute_inference=True),
    lambda d: d["followup"].update(leaks_answer=True),
])
@pytest.mark.anyio
async def test_optional_probe_safety_rejection_preserves_validated_answer(mutate, caplog):
    data = _data()
    mutate(data)
    gateway = Gateway(data)
    understanding, decision = await _service(gateway).prepare_decision(*_input())
    assert len(gateway.requests) == 1
    assert understanding.problem is None
    assert understanding.claims and understanding.evidence_quotes == [TRANSCRIPT]
    assert decision["selected"] is False
    assert decision["reason"] == "followup_proposal_rejected"
    assert "conversation_act" not in decision
    assert TRANSCRIPT not in caplog.text and "婚姻情况" not in caplog.text


@pytest.mark.anyio
async def test_invalid_then_corrected_pair_uses_bounded_retry_without_second_followup_call():
    invalid = _data()
    invalid["understanding"]["missing_point_ids"] = []
    gateway = Gateway(invalid, _data())
    understanding, decision = await _service(gateway).prepare_decision(*_input())
    assert understanding.problem is None
    assert decision["selected"] is True
    assert len(gateway.requests) == 2
    assert {r.purpose for r in gateway.requests} == {"interview_turn_understanding"}


@pytest.mark.anyio
async def test_followup_cannot_quote_a_real_but_undeclared_evidence_span():
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": TRANSCRIPT + "第二段仅作为背景。"})
    data = _data()
    data["followup"]["evidence_id"] = "E2"
    gateway = Gateway(data)
    understanding, decision = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert len(gateway.requests) == 1 and understanding.problem is None
    assert understanding.evidence_quotes == [TRANSCRIPT]
    assert decision["selected"] is False
    assert decision["reason"] == "followup_proposal_rejected"


@pytest.mark.anyio
async def test_time_budget_is_checked_again_after_inference_returns():
    utterance, turn, interview = _input()

    class TimeExpiredGateway(Gateway):
        async def invoke(self, *args):
            result = await super().invoke(*args)
            interview["scheduled_end_at"] = datetime.now(timezone.utc).isoformat()
            return result

    gateway = TimeExpiredGateway(_data())
    understanding, decision = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None
    assert decision["selected"] is False
    assert decision["reason"] == "time_budget_exhausted"


@pytest.mark.anyio
async def test_already_probed_capability_cannot_be_selected_again():
    utterance, turn, interview = _input()
    interview["turns"].extend([
        {"id": "other_root", "is_followup": False},
        {
            "id": "already_probed", "is_followup": True,
            "root_turn_id": turn["id"], "target_key_points": ["幂等键"],
        },
    ])
    gateway = Gateway(_data())
    understanding, decision = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None
    assert decision["selected"] is False
    assert decision["reason"] == "capability_points_covered_or_already_probed"
    assert gateway.requests[0].metadata["previously_probed_ids"] == ["P1"]


@pytest.mark.anyio
async def test_root_budget_preflight_does_not_start_composite_inference():
    utterance, turn, interview = _input()
    interview["turns"].extend([
        {"id": "root_2", "is_followup": False},
        {"id": "root_3", "is_followup": False},
        {"id": "probe_1", "is_followup": True, "root_turn_id": turn["id"]},
        {"id": "probe_2", "is_followup": True, "root_turn_id": turn["id"]},
    ])
    gateway = Gateway(_data()["understanding"])
    _, decision = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert decision["reason"] == "root_budget_exhausted"
    assert gateway.requests[0].metadata["prompt_version"] == "interview_turn_understanding.v8"


@pytest.mark.parametrize("confidence,action", [(0.1, "followup"), (0.9, "continue_listening")])
@pytest.mark.anyio
async def test_nonaccepted_or_low_model_confidence_never_publishes_proposed_probe(confidence, action):
    data = _data()
    data["understanding"].update(confidence=confidence, suggested_action=action)
    understanding, decision = await _service(Gateway(data)).prepare_decision(*_input())
    assert decision["selected"] is False
    assert "conversation_act" not in decision


@pytest.mark.anyio
async def test_provider_failure_never_fabricates_followup_or_discards_cancellation():
    gateway = Gateway(error=ProviderError("provider_unavailable", "private upstream detail", retryable=True))
    understanding, decision = await _service(gateway).prepare_decision(*_input())
    assert understanding.problem.code == "UNDERSTANDING_PROVIDER_UNAVAILABLE"
    assert decision["selected"] is False
    assert len(gateway.requests) == 1
    assert "private upstream detail" not in understanding.model_dump_json()

    gateway = Gateway(error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _service(gateway).prepare_decision(*_input())


@pytest.mark.anyio
async def test_non_authoritative_transcript_rejected_before_model_invocation():
    utterance, turn, interview = _input()
    gateway = Gateway()
    with pytest.raises(ValueError, match="authoritative"):
        await _service(gateway).prepare_decision(
            utterance.model_copy(update={"authoritative": False}), turn, interview,
        )
    assert gateway.requests == []


def test_composite_prompt_enforces_reference_schema_without_loosening_existing_contract():
    context = {"transcript": TRANSCRIPT, "capability_points": ["幂等键", "确认序号"], "difficulty": "mid"}
    contract = prompt_contract("interview_turn_decision", context)
    assert contract.version == "interview_turn_decision.v7"
    validate_structured_response(_data(), contract.response_schema)
    assert contract.response_schema["properties"]["understanding"] == prompt_contract(
        "interview_turn_understanding", context,
    ).response_schema
    bad = _data()
    bad["followup"]["target_point_ids"] = ["P999"]
    with pytest.raises(StructuredResponseValidationError):
        validate_structured_response(bad, contract.response_schema)


@pytest.mark.anyio
async def test_mock_gateway_returns_composite_contract_through_same_service_interface():
    understanding, decision = await ConversationUnderstandingService(InMemoryStore()).prepare_decision(*_input())
    assert understanding.problem is None
    assert understanding.prompt_version == "interview_turn_decision.v7"
    assert decision["selected"] is True
