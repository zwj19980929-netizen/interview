"""A failed optional probe must not restart validated answer understanding."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.core.prompt.validation import StructuredResponseValidationError, validate_structured_response
from app.core.time import utc_now
from app.domain.interview_agent import ConversationUtterance
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.repositories.memory import InMemoryStore
from app.services import conversation_understanding as understanding_module
from app.services.conversation_understanding import ConversationUnderstandingService


TEXT = "我会保存处理序号，断线后从已确认位置重放。第二句只说明测试背景。"


def inputs():
    turn = {
        "id": "turn_optional", "root_turn_id": "turn_optional", "is_followup": False,
        "followup_depth": 0, "allow_followup": True, "question_spoken_text": "怎样恢复连接？",
        "question_snapshot": {
            "key_points": [{"text": "幂等键"}, {"text": "确认位置"}], "difficulty": "mid",
            "standard_answer": "recoverable_checkpoint fenced_epoch",
        },
    }
    interview = {
        "id": "iv_optional", "organization_id": "org_default", "turns": [turn],
        "_answer_completion_confirmed": True,
        "scheduled_end_at": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
    }
    utterance = ConversationUtterance(
        utterance_id="utterance_optional", revision=1, speaker="candidate", text=TEXT,
        is_final=True, authoritative=True, audio_uri="private-file://synthetic_optional",
        stt_confidence=.95, source="server_streaming", created_at=utc_now(),
    )
    return utterance, turn, interview


def response():
    return {
        "understanding": {"clarification_target": None,
            "intent": "answer", "answer_summary": "记录位置用于恢复。",
            "claims": [{"claim": "保留确认位置", "evidence_id": "E1"}],
            "evidence_ids": ["E1"], "covered_point_ids": ["P2"], "missing_point_ids": ["P1"],
            "ambiguities": [], "contradictions": [], "confidence": .9, "suggested_action": "accept",
        },
        "followup": {
            "selected": True, "question_text": "你怎样避免重复处理？", "evidence_id": "E1",
            "target_point_ids": ["P1"], "rationale": "核验缺失点", "difficulty": "mid",
            "sensitive_attribute_inference": False, "leaks_answer": False,
        },
    }


class ValidatingGateway:
    def __init__(self, data, *, request_id="synthetic_request"):
        self.data = data
        self.request_id = request_id
        self.requests = []

    async def invoke(self, capability, request):
        self.requests.append(request)
        data = deepcopy(self.data)
        # Mirror the real gateway: success requires the entire wire schema,
        # including the followup object, to pass before service-level gates.
        validate_structured_response(data, request.json_schema)
        return ChatJSONResponse(
            data=data, usage=Usage(), provider=ProviderMeta(
                provider_id="synthetic", model="optional_probe", request_id=self.request_id, latency_ms=1,
            ),
        )


@pytest.mark.parametrize("patch,reason,path", [
    ({"target_point_ids": ["P2"]}, "capability_binding_invalid", "$.followup.target_point_ids"),
    ({"evidence_id": "E2"}, "evidence_binding_invalid", "$.followup.evidence_id"),
    ({"question_text": "怎样执行？又怎样验证？"}, "question_shape_invalid", "$.followup.question_text"),
    ({"sensitive_attribute_inference": True}, "safety_gate_rejected", "$.followup"),
    ({"question_text": "recoverable_checkpoint 和 fenced_epoch 如何使用？"}, "answer_leakage_heuristic", "$.followup.question_text"),
])
@pytest.mark.anyio
async def test_wire_valid_optional_gate_rejection_preserves_answer_with_one_call(patch, reason, path, caplog):
    data = response()
    data["followup"].update(patch)
    gateway = ValidatingGateway(data)
    service = ConversationUnderstandingService(InMemoryStore(), gateway=gateway)
    args = inputs()
    before = deepcopy(args[2])

    understanding, decision = await service.prepare_decision(*args)

    assert len(gateway.requests) == 1
    assert understanding.problem is None and understanding.suggested_action == "accept"
    assert understanding.claims[0].evidence_quote == "我会保存处理序号，断线后从已确认位置重放。"
    assert understanding.missing_capability_points == ["幂等键"]
    assert decision["selected"] is False and decision["reason"] == "followup_proposal_rejected"
    assert "conversation_act" not in decision
    assert args[2] == before
    assert "validation_stage=followup_gate" in caplog.text
    assert f"reason={reason} path={path} attempt=1" in caplog.text
    assert "interview_id=iv_optional turn_id=turn_optional provider_request_id=synthetic_request" in caplog.text
    assert TEXT not in caplog.text and data["followup"]["question_text"] not in caplog.text


@pytest.mark.anyio
async def test_optional_act_domain_failure_keeps_verified_understanding(caplog):
    args = inputs()
    # A stale/malformed server turn context can still fail ApprovedAct after
    # model evidence has passed; no invalid act or fallback may be published.
    args[1]["id"] = args[1]["root_turn_id"] = "t" * 129
    gateway = ValidatingGateway(response())
    service = ConversationUnderstandingService(InMemoryStore(), gateway=gateway)
    understanding, decision = await service.prepare_decision(*args)
    assert len(gateway.requests) == 1 and understanding.problem is None
    assert decision["selected"] is False and "conversation_act" not in decision
    assert "validation_stage=followup_act reason=followup_act_invalid path=$.followup" in caplog.text
    assert args[1]["id"] not in caplog.text


@pytest.mark.anyio
async def test_optional_reference_resolution_failure_does_not_repeat_understanding(monkeypatch, caplog):
    original = understanding_module.understanding_references

    class UnavailableOptionalReference(dict):
        def __getitem__(self, key):
            if key == "E2":
                raise KeyError("private_response_marker")
            return super().__getitem__(key)

    def references(*args):
        result = original(*args)
        result["evidence"] = UnavailableOptionalReference(result["evidence"])
        return result

    monkeypatch.setattr(understanding_module, "understanding_references", references)
    data = response()
    data["followup"]["evidence_id"] = "E2"
    gateway = ValidatingGateway(data)
    service = ConversationUnderstandingService(InMemoryStore(), gateway=gateway)
    understanding, decision = await service.prepare_decision(*inputs())
    assert len(gateway.requests) == 1 and understanding.problem is None
    assert decision["selected"] is False and "conversation_act" not in decision
    assert "validation_stage=followup_references reason=reference_resolution_invalid path=$.followup" in caplog.text
    assert "private_response_marker" not in caplog.text


@pytest.mark.anyio
async def test_canonical_capability_limit_still_rejects_entire_understanding(caplog):
    args = inputs()
    args[1]["question_snapshot"]["key_points"][0]["text"] = "能" * 241
    gateway = ValidatingGateway(response())
    service = ConversationUnderstandingService(InMemoryStore(), gateway=gateway)
    understanding, decision = await service.prepare_decision(*args)
    assert len(gateway.requests) == 2
    assert understanding.problem.code == "UNDERSTANDING_RESULT_REJECTED"
    assert understanding.claims == [] and understanding.evidence_quotes == []
    assert decision["selected"] is False
    assert "validation_stage=understanding_canonical reason=max_length path=$.understanding.missing_capability_points[]" in caplog.text
    assert "interview_id=iv_optional turn_id=turn_optional provider_request_id=synthetic_request" in caplog.text
    assert "能" * 241 not in caplog.text


@pytest.mark.anyio
async def test_resolved_nonverbatim_evidence_is_still_rejected(monkeypatch, caplog):
    original = understanding_module.resolve_understanding_references

    def wrong_evidence(*args):
        data = original(*args)
        data["evidence_quotes"] = ["private_response_marker"]
        return data

    monkeypatch.setattr(understanding_module, "resolve_understanding_references", wrong_evidence)
    gateway = ValidatingGateway(response())
    service = ConversationUnderstandingService(InMemoryStore(), gateway=gateway)
    understanding, decision = await service.prepare_decision(*inputs())
    assert len(gateway.requests) == 2
    assert understanding.problem.reason_code == "evidence_not_verbatim"
    assert understanding.claims == [] and decision["selected"] is False
    assert "validation_stage=understanding_content reason=evidence_not_verbatim path=$.understanding" in caplog.text
    assert "private_response_marker" not in caplog.text


@pytest.mark.parametrize("patch,reason", [
    ({"missing_point_ids": []}, "point_partition_invalid"),
    ({"claims": [{"claim": "private_response_marker", "evidence_id": "E2"}]}, "claim_evidence_undeclared"),
    ({"claims": [], "evidence_ids": []}, "answer_evidence_empty"),
])
@pytest.mark.anyio
async def test_business_understanding_failure_cannot_escape_via_optional_probe(patch, reason, caplog):
    data = response()
    data["understanding"].update(patch)
    data["followup"]["target_point_ids"] = ["P2"]
    gateway = ValidatingGateway(data)
    service = ConversationUnderstandingService(InMemoryStore(), gateway=gateway)
    understanding, decision = await service.prepare_decision(*inputs())
    assert len(gateway.requests) == 2
    assert understanding.problem.reason_code == reason
    assert understanding.claims == [] and decision["selected"] is False
    assert f"validation_stage=understanding_content reason={reason} path=$.understanding" in caplog.text
    assert "turn_followup_proposal_rejected" not in caplog.text
    assert "private_response_marker" not in caplog.text and TEXT not in caplog.text


@pytest.mark.anyio
async def test_wire_invalid_optional_object_still_fails_before_answer_is_accepted(caplog):
    data = response()
    data["followup"]["private_response_marker"] = "private_secret"
    gateway = ValidatingGateway(data)
    service = ConversationUnderstandingService(InMemoryStore(), gateway=gateway)
    understanding, decision = await service.prepare_decision(*inputs())
    assert len(gateway.requests) == 2
    assert understanding.problem is not None and decision["selected"] is False
    assert "reason=additional_properties path=$.followup" in caplog.text
    assert "private_response_marker" not in caplog.text and "private_secret" not in caplog.text


def test_schema_error_keeps_legacy_message_and_exposes_static_safe_diagnostics():
    schema = {"type": "array", "items": {"type": "object", "properties": {
        "value": {"type": "string", "enum": ["allowed"]},
    }}}
    with pytest.raises(StructuredResponseValidationError) as error:
        validate_structured_response([{"value": "private_response_marker"}], schema)
    assert str(error.value) == "AI response failed enum validation at $[0].value."
    assert error.value.reason_code == "enum" and error.value.schema_path == "$[].value"
    assert "private_response_marker" not in str(error.value)


@pytest.mark.parametrize("composite", [True, False])
@pytest.mark.anyio
async def test_each_rejection_has_scope_without_reusing_a_previous_provider_request_id(composite, caplog):
    data = response()
    data["understanding"]["missing_point_ids"] = []

    class SecondRequestRejected(ValidatingGateway):
        async def invoke(self, capability, request):
            if self.requests:
                self.requests.append(request)
                raise ProviderError("provider_schema_invalid", "private_exception_body", retryable=True)
            return await super().invoke(capability, request)

    gateway = SecondRequestRejected(data if composite else data["understanding"])
    service = ConversationUnderstandingService(InMemoryStore(), gateway=gateway)
    if composite:
        await service.prepare_decision(*inputs())
    else:
        await service.understand(*inputs())
    records = [record.getMessage() for record in caplog.records if "contract_rejected" in record.getMessage()]
    assert len(records) == 2 and len(gateway.requests) == 2
    assert all("interview_id=iv_optional turn_id=turn_optional" in record for record in records)
    assert records[0].endswith("provider_request_id=synthetic_request")
    assert records[1].endswith("provider_request_id=-")
    assert "private_exception_body" not in caplog.text and TEXT not in caplog.text


@pytest.mark.anyio
async def test_unexpected_provider_identifier_is_omitted_from_diagnostics(caplog):
    data = response()
    data["followup"]["target_point_ids"] = ["P2"]
    gateway = ValidatingGateway(data, request_id="https://private.invalid/path?token=private_secret\n")
    service = ConversationUnderstandingService(InMemoryStore(), gateway=gateway)
    await service.prepare_decision(*inputs())
    assert "interview_id=iv_optional turn_id=turn_optional provider_request_id=-" in caplog.text
    assert "private.invalid" not in caplog.text and "private_secret" not in caplog.text
