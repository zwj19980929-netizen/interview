"""Post-gateway supplement failures retain safe facts and original control flow."""
import json
from types import SimpleNamespace

import pytest

from app.core import model_diagnostics, speech_diagnostics
from app.core.errors import ApiError
from app.core.prompt import supplement_reply
from app.core.prompt.validation import StructuredResponseValidationError
from app.model_gateway.errors import ProviderError
from test_supplement_format_correction_096 import (
    CONTEXT, REPLY, assert_single_wire, service_with_wire_responses, valid,
)


@pytest.fixture
def events(monkeypatch):
    rows = []
    logger = SimpleNamespace(info=lambda message: rows.append(json.loads(message)))
    monkeypatch.setattr(model_diagnostics, "_LOG", logger)
    monkeypatch.setattr(speech_diagnostics, "_LOG", logger)
    return rows


def post_gateway_events(events):
    return [row for row in events if row["event"] in {
        "model.understanding_rejected", "model.understanding_failed"}]


@pytest.mark.anyio
@pytest.mark.parametrize("correction,version", [(None, "supplement_reply.v11"),
    ("invalid_json_object", "supplement_reply.v12")])
@pytest.mark.parametrize("change,reason,path", [
    ({"reply_text": "PRIVATE_MODEL_REPLY"}, "supplement_silent_intent_has_reply", "$.reply_text"),
    ({"intent": "supplement"}, "supplement_answer_effect_conflict", "$.answer_effect"),
    ({"intent": "confirmation_unchanged"}, "supplement_confirmation_proof_required", "$.intent"),
    ({"intent": "transcript_correction", "reply_text": "PRIVATE_MODEL_REPLY"},
     "supplement_answer_effect_conflict", "$.answer_effect"),
    ({"repeat_target": "last_clarification"}, "supplement_repeat_target_without_repeat", "$.repeat_target"),
    ({"intent": "unclear"}, "reception_reply_missing", "$.reply_text"),
])
async def test_real_wire_success_then_contract_failure_is_correlated_without_body(
        events, correction, version, change, reason, path):
    service, calls, store = service_with_wire_responses([json.dumps(valid(**change))])
    with model_diagnostics.model_call_scope(interview_id="iv_synthetic", turn_id="turn_synthetic",
            capture_id="capture_synthetic", stage="supplement_reply") as scope:
        with pytest.raises(StructuredResponseValidationError) as caught:
            await service.classify_supplement_reply(REPLY, "org_default", conversation_context=CONTEXT,
                                                   correction_reason=correction)
    assert caught.value.reason_code == reason
    assert len(calls) == len(store.model_invocations) == 1
    assert_single_wire(calls[0])
    assert store.model_invocations[0]["status"] == "success"  # The wire really succeeded.
    row, = post_gateway_events(events)
    assert row["event"] == "model.understanding_rejected"
    assert row["validation_stage"] == "supplement_content"
    assert row["failure_source"] == "schema_or_content"
    assert row["failure_reason"] == row["schema_reason"] == reason
    assert row["schema_path"] == path and row["prompt_version"] == version
    assert row["error_type"] == "StructuredResponseValidationError"
    assert row["diagnostic_scope_id"] == scope.scope_id
    assert row["interview_id"] == "iv_synthetic" and row["turn_id"] == "turn_synthetic"
    serialized = json.dumps(events, ensure_ascii=False)
    for private in (REPLY, "PRIVATE_MODEL_REPLY", "synthetic-private-key", CONTEXT["current_question"]):
        assert private not in serialized


@pytest.mark.anyio
@pytest.mark.parametrize("error_type", [ValueError, TypeError, KeyError])
async def test_internal_post_gateway_error_is_not_mislabeled_schema_or_swallowed(events, monkeypatch, error_type):
    service, calls, store = service_with_wire_responses([json.dumps(valid())])
    failure = error_type("PRIVATE_INTERNAL_DETAILS")

    def reject(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(supplement_reply, "resolve_supplement_reply", reject)
    with pytest.raises(error_type) as caught:
        await service.classify_supplement_reply(REPLY, "org_default", conversation_context=CONTEXT)
    assert caught.value is failure and len(calls) == 1
    assert store.model_invocations[0]["status"] == "success"
    row, = post_gateway_events(events)
    assert row["event"] == "model.understanding_failed"
    assert row["failure_source"] == "internal" and row["error_type"] == error_type.__name__
    assert row["failure_reason"] == "unknown" and row["validation_stage"] == "supplement_content"
    assert "schema_reason" not in row and "schema_path" not in row
    assert "PRIVATE_INTERNAL_DETAILS" not in json.dumps(events)


@pytest.mark.anyio
async def test_wire_failure_is_not_reclassified_as_post_gateway_contract_failure(events):
    service, calls, store = service_with_wire_responses(['["PRIVATE_WIRE_BODY"]'])
    with pytest.raises(ProviderError) as caught:
        await service.classify_supplement_reply(REPLY, "org_default", conversation_context=CONTEXT)
    assert caught.value.code == "provider_schema_invalid"
    assert len(calls) == 1 and store.model_invocations[0]["status"] == "failed"
    assert not post_gateway_events(events)
    assert "PRIVATE_WIRE_BODY" not in json.dumps(events)


@pytest.mark.anyio
async def test_valid_result_is_unchanged_and_has_no_rejection_event(events):
    service, calls, _ = service_with_wire_responses([json.dumps(valid())])
    result = await service.classify_supplement_reply(REPLY, "org_default", conversation_context=CONTEXT)
    assert result["intent"] == "finish" and result["evidence_quote"] == REPLY
    assert len(calls) == 1 and not post_gateway_events(events)


@pytest.mark.anyio
async def test_failed_diagnostic_sink_cannot_replace_original_contract_error(monkeypatch):
    service, calls, _ = service_with_wire_responses([json.dumps(valid(reply_text="PRIVATE_MODEL_REPLY"))])

    def broken(_message):
        raise OSError("PRIVATE_SINK_ERROR")

    monkeypatch.setattr(model_diagnostics, "_LOG", SimpleNamespace(info=broken))
    with pytest.raises(StructuredResponseValidationError) as caught:
        await service.classify_supplement_reply(REPLY, "org_default", conversation_context=CONTEXT)
    assert caught.value.reason_code == "supplement_silent_intent_has_reply" and len(calls) == 1


@pytest.mark.parametrize("error,source,reason,error_type", [
    (StructuredResponseValidationError("PRIVATE_EXCEPTION", reason_code="supplement_answer_effect_conflict",
        schema_path="$.answer_effect"), "schema_or_content", "supplement_answer_effect_conflict", "StructuredResponseValidationError"),
    (ProviderError("provider_rate_limited", "PRIVATE_EXCEPTION", retryable=True),
        "provider", "provider_rate_limited", "ProviderError"),
    (ApiError("TURN_DECISION_STALE", "PRIVATE_EXCEPTION"), "control", "TURN_DECISION_STALE", "ApiError"),
    (ValueError("PRIVATE_EXCEPTION"), "internal", "unknown", "ValueError"),
    (TypeError("PRIVATE_EXCEPTION"), "internal", "unknown", "TypeError"),
])
def test_common_failure_projection_is_typed_and_metadata_only(error, source, reason, error_type):
    fields = model_diagnostics.safe_model_failure_fields(error,
        validation_stage="supplement_proof", prompt_version="supplement_reply.v10")
    assert fields["failure_source"] == source and fields["failure_reason"] == reason
    assert fields["error_type"] == error_type and fields["validation_stage"] == "supplement_proof"
    assert fields["prompt_version"] == "supplement_reply.v10"
    assert "PRIVATE_EXCEPTION" not in json.dumps(fields)
    if source == "schema_or_content":
        assert fields["schema_reason"] == reason and fields["schema_path"] == "$.answer_effect"
    else:
        assert "schema_reason" not in fields and "schema_path" not in fields


def test_untrusted_diagnostic_values_and_unknown_schema_keys_do_not_escape(events):
    error = StructuredResponseValidationError("PRIVATE_EXCEPTION", reason_code="PRIVATE_REASON",
        schema_path="$.PRIVATE_EXTRA_KEY")
    error.prompt = "PRIVATE_PROMPT"
    fields = model_diagnostics.safe_model_failure_fields(error,
        validation_stage="PRIVATE_STAGE", prompt_version="PRIVATE_VERSION")
    assert fields["failure_reason"] == fields["schema_reason"] == "unknown"
    assert "schema_path" not in fields and "prompt_version" not in fields
    speech_diagnostics.record_turn_control("supplement_classification_failed", interview_id="iv_synthetic",
        turn_id="turn_synthetic", capture_id="capture_synthetic", **fields,
        cause_code="PRIVATE_CAUSE", text="PRIVATE_TRANSCRIPT", response="PRIVATE_RESPONSE")
    row, = events
    assert row["event"] == "turn_control.supplement_classification_failed"
    assert row["failure_reason"] == "unknown" and row["error_type"] == "StructuredResponseValidationError"
    assert "PRIVATE_" not in json.dumps(row)
    assert "text_sha256" not in row and "text_chars" not in row


def test_failure_trace_does_not_trust_direct_reason_path_or_exception_type(events):
    speech_diagnostics.record_turn_control("supplement_classification_failed", interview_id="iv_synthetic",
        turn_id="turn_synthetic", schema_reason="PRIVATE_REASON", schema_path="$.PRIVATE_EXTRA_KEY",
        error_type="PRIVATE_EXCEPTION_TYPE", failure_source="PRIVATE_SOURCE", cause_code="PRIVATE_CAUSE",
        prompt_version="PRIVATE_VERSION", text="PRIVATE_TRANSCRIPT", exception="PRIVATE_EXCEPTION",
        failure_reason="provider_private_payload")
    row, = events
    assert "schema_path" not in row and "error_type" not in row
    assert row["failure_reason"] == "unknown" and "provider_private_payload" not in json.dumps(row)
    assert "PRIVATE_" not in json.dumps(row)


def test_unknown_exception_cannot_supply_arbitrary_provider_code_or_schema_metadata():
    error = ValueError("PRIVATE_EXCEPTION")
    error.code = "provider_PRIVATE_SECRET"
    error.reason_code = "supplement_answer_effect_conflict"
    error.schema_path = "$.answer_effect"
    fields = model_diagnostics.safe_model_failure_fields(error)
    assert fields["failure_source"] == "internal" and fields["failure_reason"] == "unknown"
    assert "schema_reason" not in fields and "schema_path" not in fields
    assert "PRIVATE_" not in json.dumps(fields)


def test_source_and_proof_events_keep_only_known_paths_and_fixed_stages(events):
    for stage in ("supplement_source", "supplement_proof"):
        failure = ValueError("PRIVATE_CAPTURE_OR_PROOF")
        model_diagnostics.record_model_diagnostic("understanding_failed",
            **model_diagnostics.safe_model_failure_fields(failure, validation_stage=stage),
            interview_id="iv_synthetic", turn_id="turn_synthetic")
    assert [row["validation_stage"] for row in events] == ["supplement_source", "supplement_proof"]
    assert all(row["failure_source"] == "internal" for row in events)
    assert "PRIVATE_CAPTURE_OR_PROOF" not in json.dumps(events)


@pytest.mark.parametrize("code", ["provider_network_error", "provider_output_truncated",
    "provider_stream_cleanup_failed", "provider_audio_recovery_buffer_exceeded", "model_configuration_not_ready"])
def test_common_failure_projection_keeps_existing_formal_provider_codes(events, code):
    failure = ProviderError(code, "PRIVATE_PROVIDER_BODY")
    fields = model_diagnostics.safe_model_failure_fields(failure)
    assert fields["failure_reason"] == code and fields["failure_source"] == "provider"
    speech_diagnostics.record_turn_control("supplement_classification_failed", interview_id="iv_synthetic",
        turn_id="turn_synthetic", **fields)
    row, = events
    assert row["failure_reason"] == code and "PRIVATE_PROVIDER_BODY" not in json.dumps(row)


def test_arbitrary_provider_prefix_is_not_a_formal_failure_code():
    failure = ProviderError("provider_private_payload", "PRIVATE_BODY")
    fields = model_diagnostics.safe_model_failure_fields(failure)
    assert fields["failure_source"] == "provider" and fields["failure_reason"] == "unknown"
    assert "provider_private_payload" not in json.dumps(fields)


@pytest.mark.anyio
@pytest.mark.parametrize("code", ["provider_network_error", "provider_output_truncated"])
async def test_generic_observed_wait_preserves_existing_provider_diagnostics(events, code):
    failure = ProviderError(code, "PRIVATE_RESPONSE", retryable=True)

    async def work():
        raise failure

    with pytest.raises(ProviderError) as caught:
        await model_diagnostics.diagnosed_wait_for(work(), timeout=1, stage="turn_decision",
                                                  timeout_reason="decision_timeout")
    assert caught.value is failure
    row = events[-1]
    assert row["event"] == "model.operation_finished" and row["outcome"] == "failed"
    assert row["failure_reason"] == code and row["error_type"] == "ProviderError"
    assert "schema_reason" not in row and "schema_path" not in row
    assert "PRIVATE_RESPONSE" not in json.dumps(events)
