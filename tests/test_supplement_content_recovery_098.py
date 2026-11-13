"""Expression recovery cannot repair semantic/source failures or buy more calls."""
import json
import logging

import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt import supplement_reply as supplement
from app.core.prompt.validation import StructuredResponseValidationError
from app.model_gateway.errors import ProviderError
from test_supplement_format_correction_096 import service_with_wire_responses, assert_single_wire


REPLY = "你还在听吗？"
REASONS = ("invalid_json_object", "reception_cannot_ask_question", "reception_current_control_conflict")


def response(**overrides):
    return {"intent": "presence", "answer_effect": "unchanged", "confidence": .95,
            "evidence_id": "E1", "reply_text": "我在，你还有什么要补充的吗？", **overrides}


def contract(reply=REPLY, **context):
    return prompt_contract("supplement_reply", {"reply": reply, **context})


@pytest.mark.parametrize("reason", REASONS)
def test_fixed_correction_reason_changes_instruction_but_not_schema_or_evidence(reason):
    plain, corrected = contract(), contract(correction_reason=reason)
    assert plain.version == "supplement_reply.v11"
    assert corrected.version == "supplement_reply.v12"
    assert corrected.response_schema == plain.response_schema
    assert corrected.messages[-1] == plain.messages[-1]
    assert reason in corrected.messages[-2].content
    assert "PRIVATE_REJECTED_BODY" not in str(corrected.messages)
    assert "不能" in corrected.messages[-2].content


@pytest.mark.parametrize("reason", ["PRIVATE_REJECTED_BODY", "provider_timeout", None, [], {}])
def test_unknown_or_non_string_correction_never_enters_the_prompt(reason):
    assert contract(correction_reason=reason) == contract()


@pytest.mark.parametrize("reason,path", [
    ("reception_cannot_ask_question", "$.reply_text"),
    ("reception_current_control_conflict", "$.kind"),
])
def test_content_correction_requires_exact_typed_reason_and_path(reason, path):
    exc = StructuredResponseValidationError("PRIVATE_REJECTED_BODY", reason_code=reason, schema_path=path)
    assert supplement.supplement_correction_reason(exc) == reason
    exc.schema_path = "$.wrong"
    assert supplement.supplement_correction_reason(exc) is None


@pytest.mark.parametrize("change", [{}, {"retryable": False}, {"code": "provider_timeout"},
    {"details": {"json_failure_kind": "invalid_json_object", "json_decode_reason": "syntax"}},
    {"details": {"json_failure_kind": "empty_content", "json_decode_reason": "non_object"}},
    {"details": []}])
def test_only_recoverable_non_object_provider_error_selects_format_correction(change):
    args = {"code": "provider_schema_invalid", "retryable": True,
            "details": {"json_failure_kind": "invalid_json_object", "json_decode_reason": "non_object"}, **change}
    exc = ProviderError(message="PRIVATE_REJECTED_BODY", **args)
    assert supplement.supplement_correction_reason(exc) == (None if change else "invalid_json_object")


@pytest.mark.parametrize("exc", [ValueError("reception_cannot_ask_question"), RuntimeError("invalid_json_object"),
    StructuredResponseValidationError("private", reason_code="reception_reply_not_plain_speech", schema_path="$.reply_text")])
def test_unrelated_errors_are_never_content_correction_eligible(exc):
    assert supplement.supplement_correction_reason(exc) is None


def test_presence_expression_resolves_once_after_strict_semantics_and_revalidates():
    raw = response()
    with pytest.raises(StructuredResponseValidationError, match="Reception"):
        supplement.validate_supplement_reply(raw, contract().response_schema, reply=REPLY)
    resolved = supplement.resolve_supplement_reply(raw, contract().response_schema, reply=REPLY)
    assert resolved.repair_reason == "reception_cannot_ask_question"
    assert resolved.decision == {"intent": "presence", "answer_effect": "unchanged", "confidence": .95,
        "evidence_quote": REPLY, "reply_text": supplement.SUPPLEMENT_PRESENCE_REPLY, "repeat_target": None}
    assert raw == response(), "the rejected provider output remains unmodified"
    checked = supplement.validate_supplement_reply({**raw, "reply_text": resolved.decision["reply_text"]},
        contract().response_schema, reply=REPLY)
    assert checked == resolved.decision


def test_presence_repair_threshold_is_inclusive_and_bad_replacement_is_rejected(monkeypatch):
    resolved = supplement.resolve_supplement_reply(response(confidence=.75), contract().response_schema, reply=REPLY)
    assert resolved.repair_reason == "reception_cannot_ask_question"
    monkeypatch.setattr(supplement, "SUPPLEMENT_PRESENCE_REPLY", "这仍然是反问吗？")
    with pytest.raises(StructuredResponseValidationError) as caught:
        supplement.resolve_supplement_reply(response(), contract().response_schema, reply=REPLY)
    assert caught.value.reason_code == "reception_cannot_ask_question"


@pytest.mark.parametrize("change", [
    {"confidence": .749}, {"confidence": True}, {"confidence": float("nan")},
    {"answer_effect": "updated"}, {"answer_effect": "uncertain"},
    {"intent": "wait"}, {"intent": "interview_dialogue"}, {"intent": "unclear", "reply_text": ""},
    {"evidence_id": "E999"}, {"extra": "PRIVATE_REJECTED_BODY"},
    {"repeat_target": "last_clarification"}, {"reply_text": "http://example.invalid?"},
    {"reply_text": "你\n还在吗？"}, {"reply_text": ""}, {"reply_text": "字" * 181},
])
def test_presence_repair_cannot_bypass_any_other_contract(change):
    with pytest.raises(StructuredResponseValidationError):
        supplement.resolve_supplement_reply(response(**change), contract().response_schema, reply=REPLY)


@pytest.mark.parametrize("reply", ["我已经说完了。", "这题我不会。", "结束面试吧。"])
def test_current_control_conflict_cannot_be_repaired_as_presence(reply):
    with pytest.raises(StructuredResponseValidationError) as caught:
        supplement.resolve_supplement_reply(response(), contract(reply).response_schema, reply=reply)
    assert caught.value.reason_code == "reception_current_control_conflict"
    assert caught.value.schema_path == "$.kind"


def test_valid_presence_and_other_intents_are_not_rewritten():
    raw = response(reply_text="我在听。")
    resolved = supplement.resolve_supplement_reply(raw, contract().response_schema, reply=REPLY)
    assert resolved.repair_reason is None and resolved.decision["reply_text"] == raw["reply_text"]
    raw = response(intent="wait", reply_text="好的，你慢慢想。")
    resolved = supplement.resolve_supplement_reply(raw, contract().response_schema, reply=REPLY)
    assert resolved.repair_reason is None and resolved.decision["intent"] == "wait"


@pytest.mark.anyio
async def test_service_repairs_presence_without_another_http_call_and_logs_no_body(caplog, monkeypatch):
    raw = response(reply_text="PRIVATE_REJECTED_BODY？")
    service, calls, store = service_with_wire_responses([json.dumps(raw)])
    logger = logging.getLogger("interviewer.speech_evidence")
    # Production logging may disable propagation earlier in the full suite.
    # Capture this logger directly and restore both settings after the call.
    with monkeypatch.context() as log_state:
        log_state.setattr(logger, "propagate", False)
        log_state.setattr(logger, "handlers", logger.handlers + (
            [] if caplog.handler in logger.handlers else [caplog.handler]))
        with caplog.at_level(logging.INFO, logger=logger.name):
            result = await service.classify_supplement_reply(REPLY, "org_default")
    assert result["reply_text"] == supplement.SUPPLEMENT_PRESENCE_REPLY
    assert result["intent"] == "presence" and result["answer_effect"] == "unchanged"
    assert len(calls) == len(store.model_invocations) == 1
    assert_single_wire(calls[0])
    assert store.model_invocations[0]["prompt_version"] == "supplement_reply.v11"
    assert "PRIVATE_REJECTED_BODY" not in caplog.text and REPLY not in caplog.text
    events = [json.loads(row.message) for row in caplog.records if '"event": "model.correction_applied"' in row.message]
    assert len(events) == 1
    assert events[0]["validation_stage"] == "supplement_content"
    assert events[0]["reason_code"] == "reception_cannot_ask_question"
    assert events[0]["outcome"] == "fallback_success"


@pytest.mark.anyio
async def test_failed_optional_diagnostic_does_not_break_valid_fixed_expression(monkeypatch):
    from app.core import model_diagnostics
    service, calls, _ = service_with_wire_responses([json.dumps(response())])
    def unavailable_logger(*args, **kwargs):
        raise RuntimeError("PRIVATE_LOG_ERROR")
    monkeypatch.setattr(model_diagnostics._LOG, "info", unavailable_logger)
    result = await service.classify_supplement_reply(REPLY, "org_default")
    assert result["reply_text"] == supplement.SUPPLEMENT_PRESENCE_REPLY and len(calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("reason,reply,first,second", [
    ("reception_cannot_ask_question", "我想稍微思考一下。", response(intent="wait"),
     response(intent="wait", reply_text="好的，你慢慢想。")),
    ("reception_current_control_conflict", "我已经说完了。", response(),
     response(intent="finish", reply_text="")),
])
async def test_content_failure_correction_stays_same_wire_budget_and_preserves_control(reason, reply, first, second):
    service, calls, store = service_with_wire_responses([json.dumps(first), json.dumps(second)])
    with pytest.raises(StructuredResponseValidationError) as caught:
        await service.classify_supplement_reply(reply, "org_default")
    assert supplement.supplement_correction_reason(caught.value) == reason
    result = await service.classify_supplement_reply(reply, "org_default", correction_reason=reason)
    assert result["intent"] == second["intent"] and result["answer_effect"] == second["answer_effect"]
    assert len(calls) == 2
    for call in calls:
        assert_single_wire(call)
    assert calls[0]["payload"]["response_format"] == calls[1]["payload"]["response_format"]
    assert calls[0]["payload"]["messages"][-1] == calls[1]["payload"]["messages"][-1]
    assert reason in calls[1]["payload"]["messages"][-2]["content"]
    assert [row["prompt_version"] for row in store.model_invocations] == ["supplement_reply.v11", "supplement_reply.v12"]
