"""Metadata-only model call diagnostics; observation never changes call policy."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import logging
import math
import re
from time import perf_counter
from typing import Any, Optional

from app.core.ids import new_id
from app.core.interaction_budget import remaining_interaction_seconds
from app.core.time import utc_now


_LOG = logging.getLogger("interviewer.speech_evidence")
_STAGES = frozenset({"answer_understanding", "answer_preparation", "turn_decision", "followup",
                     "supplement_reply", "reception", "provider"})
_CANCEL_REASONS = frozenset({
    "caller_cancelled", "interaction_timeout", "preparation_timeout", "owner_changed", "input_changed",
    "capture_closed", "session_closed", "task_shutdown", "superseded", "deadline", "local_timeout",
    "waiter_cancelled", "explicit_retry", "new_audio", "new_client_input", "new_transcript",
    "endpoint_closed", "interaction_deadline", "inference_timeout", "external_cancel",
    "preparation_replaced", "speculative_timeout", "confirmed_timeout", "understanding_timeout",
    "decision_timeout", "followup_timeout", "reception_timeout", "supplement_timeout", "child_timeout",
})
_EVENTS = frozenset({"attempt_started", "attempt_finished", "operation_started", "operation_finished",
                     "scope_started", "scope_finished", "scope_cancel_requested", "audit_failed",
                     "understanding_rejected", "understanding_failed", "correction_applied"})
_SUPPLEMENT_CONTRACT_REASONS = frozenset({
    "supplement_confirmation_proof_required", "supplement_answer_effect_conflict",
    "supplement_silent_intent_has_reply", "supplement_repeat_target_without_repeat",
    "reception_confidence_invalid", "reception_current_control_conflict",
    "reception_repeat_target_without_repeat", "reception_silent_kind_has_reply",
    "reception_reply_missing", "reception_reply_not_plain_speech", "reception_cannot_ask_question",
})
_CONTROL_FAILURE_REASONS = frozenset({
    "TURN_DECISION_STALE", "AGENT_EVIDENCE_NOT_OPEN", "AGENT_EVIDENCE_FINISHING",
    "AGENT_EVIDENCE_ALREADY_OPEN", "EVIDENCE_MEDIA_INCOMPLETE",
})
_PROVIDER_FAILURE_REASONS = frozenset({
    "provider_timeout", "provider_cancelled", "provider_auth_failed", "provider_authentication_failed",
    "provider_billing_required", "provider_rate_limited", "provider_circuit_open", "provider_unavailable",
    "provider_bad_request", "provider_forbidden", "provider_configuration_invalid", "provider_capability_missing",
    "provider_connection_disabled", "provider_cost_limit_exceeded", "provider_real_stt_required",
    "provider_route_invalid", "provider_route_missing", "provider_schema_invalid", "provider_server_error",
    "provider_streaming_not_supported", "provider_unexpected_error",
    "provider_audio_chunk_too_large", "provider_audio_final_missing", "provider_audio_format_unsupported",
    "provider_audio_recovery_buffer_exceeded", "provider_audio_stream_too_large", "provider_audio_unavailable",
    "provider_backpressure_exceeded", "provider_dialogue_event_overflow", "provider_dialogue_input_failed",
    "provider_dialogue_input_overflow", "provider_dialogue_input_timeout", "provider_dialogue_response_mismatch",
    "provider_entrypoint_invalid", "provider_final_transcript_missing", "provider_health_failed",
    "provider_media_url_invalid", "provider_model_type_missing", "provider_model_unavailable",
    "provider_network_error", "provider_not_implemented", "provider_not_installed", "provider_output_audio_too_large",
    "provider_output_truncated", "provider_probe_cancelled", "provider_probe_cleanup_failed", "provider_probe_failed",
    "provider_route_changed", "provider_snapshot_stale", "provider_stream_cleanup_failed", "provider_stream_close_failed",
    "provider_stream_closed", "provider_stream_failed", "provider_stream_interrupted", "provider_stream_open_failed",
    "provider_transport_unavailable", "model_configuration_missing", "model_configuration_not_ready",
    "evaluation_evidence_invalid", "evaluation_timeout", "resume_evidence_budget_exceeded",
    "resume_evidence_item_too_large", "resume_question_evidence_missing", "resume_question_generation_invalid",
    "resume_question_not_grounded", "resume_text_empty",
})
# Only declared supplement/reception fields, never a rejected extra key or an
# arbitrary exception attribute. Other contracts retain their own diagnostics.
SUPPLEMENT_DIAGNOSTIC_SCHEMA_PATHS = frozenset({
    "$", "$.intent", "$.kind", "$.answer_effect", "$.confidence", "$.evidence_id",
    "$.evidence_quote", "$.reply_text", "$.repeat_target",
})
_FAILURE_REASONS = frozenset({
    "unknown", "timeout", "unavailable", "invalid_result", "retry_exhausted", "non_retryable",
    "call_budget_exhausted", "wire_schema_invalid", "reference_resolution_invalid",
    "enum", "type", "required", "min_properties", "additional_properties", "min_items", "max_items", "unique_items",
    "minimum", "maximum", "min_length", "max_length", "semantic_control_contract_invalid", "semantic_current_speech_conflict",
    "answer_declined_contract_invalid", "answer_evidence_empty", "answer_leakage_heuristic", "capability_binding_invalid",
    "claim_evidence_undeclared", "clarification_target_contract_invalid", "clarification_targets_conversation_control",
    "company_question_contract_invalid", "company_question_covers_technical_evidence", "completion_action_invalid",
    "completion_basis_required", "completion_basis_unexpected", "completion_confidence_insufficient",
    "company_question_span_invalid", "clarification_focus_not_verbatim",
    "completion_followup_permission_invalid", "completion_has_unresolved_ambiguity", "completion_intent_content_mismatch",
    "content_none_has_technical_evidence", "declined_answer_completion_scope_invalid", "declined_answer_has_technical_content",
    "detail_claim_binding_invalid", "detail_rationale_missing", "difficulty_escalation", "evidence_binding_invalid",
    "evidence_not_verbatim", "finish_answer_without_content", "off_topic_contract_invalid", "point_duplicates",
    "point_not_frozen", "point_partition_invalid", "question_shape_invalid", "safety_gate_rejected",
    "semantic_completion_evidence_invalid", "technical_content_claims_missing", "unresolved_answer_contract_invalid",
    "unresolved_clarification_required", "unresolved_clarification_source_invalid",
}) | _SUPPLEMENT_CONTRACT_REASONS | _CONTROL_FAILURE_REASONS | _PROVIDER_FAILURE_REASONS
_COMPANY_CONTRACT_DETAILS = frozenset({
    "missing_question", "completion_basis_present", "followup_allowed", "action_mismatch",
    "low_confidence", "unresolved_ambiguity", "intent_content_mismatch", "quote_mismatch",
    "invalid_span", "unexpected_question", "unexpected_action", "unexpected_intent",
})
_OUTCOMES = frozenset({"success", "fallback_success", "failed", "timeout", "cancelled", "started"})
_IDENTIFIERS = frozenset({"interview_id", "turn_id", "capture_id", "diagnostic_scope_id", "parent_scope_id",
                         "invocation_id", "organization_id", "route_id", "provider_connection_id",
                         "model_configuration_id", "provider_id", "model", "provider_request_id", "capability", "purpose"})
_NUMBERS = frozenset({"revision", "attempt", "fallback_index", "requested_timeout_ms", "effective_timeout_ms",
                     "interaction_remaining_ms", "elapsed_ms", "latency_ms"})
_current: ContextVar[Optional["ModelCallScope"]] = ContextVar("model_call_diagnostic_scope", default=None)


def _identifier(value: Any) -> Optional[str]:
    # Identifiers are trusted internal metadata, never arbitrary text/exception messages.
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}", value) else None


def safe_model_diagnostic_fields(fields: dict, *, strict_failure_reasons: bool = False) -> dict:
    safe = {}
    for key, value in fields.items():
        if key in _IDENTIFIERS:
            accepted = _identifier(value)
            if accepted is not None:
                safe[key] = accepted
        elif key in _NUMBERS and type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 86_400_000:
            safe[key] = int(value)
        elif key == "stage" and isinstance(value, str) and value in _STAGES:
            safe[key] = value
        elif key == "cancel_reason" and isinstance(value, str) and value in _CANCEL_REASONS:
            safe[key] = value
        elif key in {"outcome", "status"} and isinstance(value, str) and value in _OUTCOMES:
            safe[key] = value
        elif key in {"error_code", "failure_reason", "reason_code"} and isinstance(value, str):
            safe[key] = value if (value in _CANCEL_REASONS or value in _FAILURE_REASONS
                                  or not strict_failure_reasons and re.fullmatch(r"provider_[a-z_]{1,64}", value)) else "unknown"
        elif key == "operation" and isinstance(value, str) and value in {"infer", "prepare_decision", "confirmed_prepare", "speculative_prepare", "speculative_wait", "confirmed_wait"}:
            safe[key] = value
        elif key == "source" and isinstance(value, str) and value in {"audio", "client", "transcript", "stable_evidence"}:
            safe[key] = value
        elif key == "failure_source" and isinstance(value, str) and value in {"provider", "schema_or_content", "semantic", "control", "internal"}:
            safe[key] = value
        elif key == "error_type" and isinstance(value, str) and value in {"TimeoutError", "CancelledError", "ProviderError", "ApiError", "ValueError",
                                                "KeyError", "TypeError", "RuntimeError", "StructuredResponseValidationError", "OtherError"}:
            safe[key] = value
        elif key == "validation_stage" and isinstance(value, str):
            safe[key] = value if value in {"provider", "wire_schema", "understanding_content", "understanding_semantic",
                                          "understanding_materialize", "understanding_canonical", "understanding_references",
                                          "understanding_domain", "followup", "followup_references", "followup_gate", "followup_act",
                                          "followup_repair", "reception_content", "supplement_content",
                                          "supplement_source", "supplement_proof"} else "unknown"
        elif key == "contract_detail" and isinstance(value, str) and value in _COMPANY_CONTRACT_DETAILS:
            safe[key] = value
        elif key == "schema_path" and isinstance(value, str) and re.fullmatch(r"\$[A-Za-z0-9_.\[\]]{0,160}", value):
            safe[key] = re.sub(r"\[\d+\]", "[]", value)
        elif key == "schema_reason" and isinstance(value, str):
            safe[key] = value if value in _FAILURE_REASONS else "unknown"
        elif key == "prompt_version" and isinstance(value, str) and re.fullmatch(r"[a-z_]{1,64}\.v[0-9]{1,6}", value):
            safe[key] = value
        elif key in {"retryable", "provider_invoked"} and type(value) is bool:
            safe[key] = value
    return safe


def safe_model_failure_fields(exc: BaseException, *, validation_stage=None, prompt_version=None) -> dict:
    """Project typed failure metadata without reading messages or granting retry.

    This observes the original exception; it does not wrap it, modify it or
    infer a provider failure from an internal ValueError/TypeError. Unknown
    schema paths are omitted rather than exposing a rejected field name.
    """
    try:
        from app.core.errors import ApiError
        from app.core.prompt.validation import StructuredResponseValidationError
        from app.model_gateway.errors import ProviderError

        fields = {"failure_reason": "unknown", "failure_source": "internal",
                  "error_type": type(exc).__name__, "validation_stage": validation_stage,
                  "prompt_version": prompt_version}
        if isinstance(exc, StructuredResponseValidationError):
            reason = exc.reason_code if isinstance(exc.reason_code, str) and exc.reason_code in _FAILURE_REASONS else "unknown"
            fields.update(failure_source="schema_or_content", failure_reason=reason, schema_reason=reason)
            if isinstance(exc.schema_path, str) and exc.schema_path in SUPPLEMENT_DIAGNOSTIC_SCHEMA_PATHS:
                fields["schema_path"] = exc.schema_path
        elif isinstance(exc, ProviderError):
            fields.update(failure_source="provider", failure_reason=(
                exc.code if isinstance(exc.code, str) and exc.code in _PROVIDER_FAILURE_REASONS else "unknown"))
        elif isinstance(exc, ApiError):
            fields.update(failure_source="control", failure_reason=(
                exc.code if isinstance(exc.code, str) and exc.code in _CONTROL_FAILURE_REASONS else "unknown"))
        safe = safe_model_diagnostic_fields(fields, strict_failure_reasons=True)
        safe.setdefault("error_type", "OtherError")
        return safe
    except Exception:
        # Even a malformed optional diagnostic attribute cannot mask the
        # original failure or alter its cancellation/retry policy.
        return {"failure_reason": "unknown", "failure_source": "internal", "error_type": "OtherError"}


@dataclass
class ModelCallScope:
    scope_id: str
    fields: dict
    parent: Optional["ModelCallScope"] = field(default=None, repr=False)
    _cancel_reason: Optional[str] = field(default=None, repr=False)

    @property
    def diagnostic_scope_id(self) -> str:
        return self.scope_id

    @property
    def parent_scope_id(self) -> Optional[str]:
        return self.parent.scope_id if self.parent else None

    @property
    def cancel_reason(self) -> str:
        if self._cancel_reason is not None:
            return self._cancel_reason
        return self.parent.cancel_reason if self.parent else "caller_cancelled"

    def cancel(self, reason: str) -> None:
        # The cancelling owner supplies a category, never an exception message.
        if self._cancel_reason is None and isinstance(reason, str) and reason in _CANCEL_REASONS:
            self._cancel_reason = reason


def current_model_call_scope() -> Optional[ModelCallScope]:
    return _current.get()


@contextmanager
def model_call_scope(*, interview_id=None, turn_id=None, capture_id=None, stage=None,
                     operation=None, revision=None, inherit=True):
    parent = _current.get() if inherit else None
    fields = dict(parent.fields) if parent else {}
    fields.update(safe_model_diagnostic_fields({key: value for key, value in {
        "interview_id": interview_id, "turn_id": turn_id, "capture_id": capture_id,
        "stage": stage or operation, "revision": revision,
    }.items() if value is not None}))
    scope = ModelCallScope(new_id("model_scope"), fields, parent)
    token = _current.set(scope)
    try:
        yield scope
    finally:
        _current.reset(token)


def safe_model_call_context(metadata=None, scope=None) -> dict:
    source = metadata if isinstance(metadata, dict) else {}
    safe = safe_model_diagnostic_fields({key: source.get(key) for key in (
        "interview_id", "turn_id", "capture_id", "prompt_version")})
    current = scope if scope is not None else _current.get()
    if current is not None:
        safe.update(current.fields)
        safe["diagnostic_scope_id"] = current.scope_id
        if current.parent_scope_id:
            safe["parent_scope_id"] = current.parent_scope_id
    return safe


def record_model_diagnostic(event: str, **fields) -> None:
    """Optional stdout observation cannot mask cancellation or domain failures."""
    try:
        if event not in _EVENTS:
            return
        _LOG.info(json.dumps({"event": "model." + event, "at": utc_now(),
                              **safe_model_diagnostic_fields(fields)}, ensure_ascii=True))
    except Exception:
        pass


def model_timing_fields(timeout) -> dict:
    remaining = remaining_interaction_seconds()
    fields = {}
    if type(timeout) in {int, float} and math.isfinite(timeout) and timeout >= 0:
        fields.update(requested_timeout_ms=round(timeout * 1000), effective_timeout_ms=round(timeout * 1000))
    if remaining is not None:
        fields["interaction_remaining_ms"] = round(remaining * 1000)
    return safe_model_diagnostic_fields(fields)


async def diagnosed_wait_for(awaitable, *, timeout, stage, timeout_reason,
                             interview_id=None, turn_id=None):
    """Observe exactly the caller's wait_for, without clipping or extending it."""
    with model_call_scope(interview_id=interview_id, turn_id=turn_id, stage=stage) as scope:
        fields = {**safe_model_call_context(scope=scope), **model_timing_fields(timeout)}
        started = perf_counter()
        record_model_diagnostic("operation_started", **fields, outcome="started")
        outcome, reason, error_type = "success", None, None
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except asyncio.TimeoutError as exc:
            outcome = "timeout"
            owns_timeout = (type(timeout) in {int, float} and perf_counter() - started >= timeout
                            and isinstance(exc.__cause__, asyncio.CancelledError))
            scope.cancel(timeout_reason if owns_timeout else "child_timeout")
            reason = scope.cancel_reason
            raise
        except asyncio.CancelledError:
            outcome, reason = "cancelled", scope.cancel_reason
            raise
        except Exception as exc:
            outcome = "failed"
            reason = getattr(exc, "code", None)
            error_type = type(exc).__name__
            raise
        finally:
            record_model_diagnostic("operation_finished", **fields, outcome=outcome,
                                    elapsed_ms=int((perf_counter() - started) * 1000),
                                    cancel_reason=reason if outcome in {"timeout", "cancelled"} else None,
                                    failure_reason=reason if outcome == "failed" else None, error_type=error_type)
