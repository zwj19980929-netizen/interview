"""Classify fixed adapter/gateway codes without replaying a committed answer.

The matrix is drawn from DashScope/Volcengine stream open, send and finalization,
HTTP transport adapters, ValidatedSTTStream and gateway/registry route checks.
No provider, gateway, network or business mutation is invoked by these tests.
"""
import asyncio
from dataclasses import asdict

import pytest

from app.core.errors import ApiError
from app.model_gateway.errors import ProviderError
from app.services.capture_recovery import classify_capture_failure


_CAPTURE_STAGES = ("send", "snapshot", "resume", "retry")
_TRANSIENT_ADAPTER_CODES = (
    "provider_stream_open_failed",  # DashScope, Volcengine, realtime_speech
    "provider_stream_failed",       # DashScope sender/reader
    "provider_stream_closed",       # Adapter and ValidatedSTTStream
    "provider_timeout",
    "provider_backpressure_exceeded",
    "provider_schema_invalid",
    "provider_final_transcript_missing",
    "provider_server_error",
    "provider_transport_unavailable",
    "provider_network_error",
    "provider_rate_limited",
    "provider_stream_interrupted",
    "provider_circuit_open",        # Gateway target selection
)
_NONRETRYABLE_ADAPTER_CODES = (
    "provider_auth_failed",
    "provider_route_invalid",
    "provider_route_missing",
    "provider_connection_disabled",
    "provider_capability_missing",
    "provider_real_stt_required",
    "provider_cost_limit_exceeded",
    "provider_bad_request",
    "provider_audio_format_unsupported",
    "provider_audio_chunk_too_large",
    "provider_audio_stream_too_large",
    "provider_streaming_not_supported",
    "provider_stream_close_failed",
    "provider_not_installed",
    "provider_model_unavailable",
    "provider_model_type_missing",
    "provider_entrypoint_invalid",
    "provider_not_implemented",
)
_SAFETY_CODES = (
    "EVIDENCE_OWNER_FENCED",
    "EVIDENCE_OWNERSHIP_LOST",
    "EVIDENCE_MEDIA_CHECKPOINT_CHANGED",
    "EVIDENCE_MEDIA_CHECKSUM_MISMATCH",
    "EVIDENCE_MEDIA_SEGMENT_GAP",
    "EVIDENCE_MEDIA_ALREADY_COMPLETE",
    "EVIDENCE_MEDIA_INCOMPLETE",
    "INTERVIEW_NOT_IN_PROGRESS",
    "INTERVIEW_TURN_NOT_ACTIVE",
)


def _errors(code, *, retryable):
    provider = ProviderError(code, "synthetic private upstream response",
                             retryable=retryable, details={"raw": "synthetic-private-token"})
    wrapped = ApiError(code.upper(), provider.message, status_code=502, details=provider.details)
    wrapped.__cause__ = provider
    return provider, wrapped


@pytest.mark.parametrize("code", _TRANSIENT_ADAPTER_CODES)
def test_actual_transient_adapter_codes_allow_bounded_rebuild_in_capture_stages(code):
    # A closed socket can be non-retryable *on that socket*. The recovery
    # classifier intentionally permits rebuilding it, not resending on it.
    for error in _errors(code, retryable=False):
        for stage in _CAPTURE_STAGES:
            failure = classify_capture_failure(error, stage)
            assert failure.cause_code == code.upper(), (code, stage)
            assert failure.stage == stage
            assert failure.retryable
            assert failure.requires_new_capture, "Exhausted retry must permit an explicit new capture"
            assert not failure.safety_violation
            assert failure.cause_type == ("ProviderError" if isinstance(error, ProviderError) else "ApiError")


@pytest.mark.parametrize("code", _NONRETRYABLE_ADAPTER_CODES)
def test_auth_route_cost_and_configuration_failures_require_explicit_recovery_not_auto_rebuild(code):
    # Upstream retryability is not authority to loop on a bad route, credential,
    # unsupported audio format or cost limit.
    for error in _errors(code, retryable=True):
        for stage in _CAPTURE_STAGES:
            failure = classify_capture_failure(error, stage)
            assert failure.cause_code == code.upper(), (code, stage)
            assert not failure.retryable
            assert failure.requires_new_capture
            assert not failure.safety_violation


@pytest.mark.parametrize("code", _TRANSIENT_ADAPTER_CODES + _NONRETRYABLE_ADAPTER_CODES)
def test_commit_provider_failures_never_replay_audio_or_create_a_replacement_capture(code):
    for error in _errors(code, retryable=True):
        failure = classify_capture_failure(error, "commit")
        assert failure.cause_code == code.upper()
        assert not failure.retryable
        assert not failure.requires_new_capture
        assert failure.stage == "commit"


@pytest.mark.parametrize("code", _SAFETY_CODES)
def test_ownership_and_evidence_integrity_failures_never_get_recovery_authority(code):
    for stage in _CAPTURE_STAGES + ("commit", "understanding"):
        failure = classify_capture_failure(ApiError(code, "Synthetic safety fence.", status_code=409), stage)
        assert failure.cause_code == code
        assert failure.safety_violation
        assert not failure.retryable and not failure.requires_new_capture


@pytest.mark.parametrize("stage", _CAPTURE_STAGES + ("commit", "unknown-private-stage"))
def test_unknown_vendor_codes_and_private_details_are_not_retained_in_failure_projection(stage):
    unknown = "provider_vendor_private_transcript_and_token"
    for error in (*_errors(unknown, retryable=True), RuntimeError("synthetic private transcript")):
        failure = classify_capture_failure(error, stage)
        projection = asdict(failure)
        assert failure.cause_code == "CAPTURE_INTERNAL_ERROR"
        assert failure.stage == (stage if stage in _CAPTURE_STAGES + ("commit",) else "unknown")
        assert not failure.retryable and not failure.requires_new_capture and not failure.safety_violation
        assert set(projection) == {"stage", "cause_code", "cause_type", "retryable"}
        assert all("private" not in str(value).lower() for value in projection.values())
        assert error not in projection.values()


def test_timeout_and_invalid_stage_are_normalized_without_expanding_replay_authority():
    for stage in _CAPTURE_STAGES:
        failure = classify_capture_failure(asyncio.TimeoutError("synthetic private timeout body"), stage)
        assert failure.cause_code == "PROVIDER_TIMEOUT" and failure.cause_type == "TimeoutError"
        assert failure.retryable and failure.requires_new_capture
    for stage in ("commit", "understanding", "unexpected-private-stage"):
        failure = classify_capture_failure(asyncio.TimeoutError(), stage)
        assert not failure.retryable and not failure.requires_new_capture
        assert failure.stage == (stage if stage in {"commit", "understanding"} else "unknown")
