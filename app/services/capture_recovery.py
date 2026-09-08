"""Safe classification of recognition failures, separate from answer submission."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

from app.core.errors import ApiError
from app.model_gateway.errors import ProviderError


_TRANSIENT = frozenset({
    "PROVIDER_TIMEOUT", "PROVIDER_NETWORK_ERROR", "PROVIDER_CONNECTION_FAILED",
    "PROVIDER_CONNECTION_CLOSED", "PROVIDER_STREAM_CLOSED", "PROVIDER_STREAM_FAILED",
    "PROVIDER_BACKPRESSURE_EXCEEDED", "PROVIDER_AUDIO_BACKPRESSURE",
    "PROVIDER_FINAL_TRANSCRIPT_MISSING", "PROVIDER_SCHEMA_INVALID",
    "PROVIDER_UNAVAILABLE", "PROVIDER_SERVICE_UNAVAILABLE", "PROVIDER_RATE_LIMITED",
    "PROVIDER_RECOVERY_IN_PROGRESS",
    "PROVIDER_STREAM_INTERRUPTED",
    "PROVIDER_STREAM_OPEN_FAILED", "PROVIDER_SERVER_ERROR",
    "PROVIDER_TRANSPORT_UNAVAILABLE", "PROVIDER_CIRCUIT_OPEN",
})
_KNOWN = _TRANSIENT | frozenset({
    "PROVIDER_AUTHENTICATION_FAILED", "PROVIDER_AUTH_FAILED", "PROVIDER_FORBIDDEN",
    "PROVIDER_CONFIGURATION_INVALID", "PROVIDER_AUDIO_STREAM_TOO_LARGE",
    "PROVIDER_AUDIO_CHUNK_TOO_LARGE", "PROVIDER_RECOVERY_BUFFER_EXHAUSTED",
    "PROVIDER_AUDIO_RECOVERY_BUFFER_EXCEEDED", "PROVIDER_STREAM_CLOSE_FAILED",
    "PROVIDER_BAD_REQUEST", "PROVIDER_AUDIO_FORMAT_UNSUPPORTED", "PROVIDER_AUDIO_UNAVAILABLE",
    "PROVIDER_CAPABILITY_MISSING", "PROVIDER_CONNECTION_DISABLED", "PROVIDER_COST_LIMIT_EXCEEDED",
    "PROVIDER_REAL_STT_REQUIRED", "PROVIDER_ROUTE_INVALID", "PROVIDER_ROUTE_MISSING",
    "PROVIDER_STREAMING_NOT_SUPPORTED",
    "PROVIDER_NOT_INSTALLED", "PROVIDER_MODEL_UNAVAILABLE", "PROVIDER_MODEL_TYPE_MISSING",
    "PROVIDER_ENTRYPOINT_INVALID", "PROVIDER_NOT_IMPLEMENTED",
    "EVIDENCE_OWNER_FENCED", "EVIDENCE_OWNERSHIP_LOST", "EVIDENCE_MEDIA_CHECKPOINT_CHANGED",
    "EVIDENCE_MEDIA_CHECKSUM_MISMATCH", "EVIDENCE_MEDIA_SEGMENT_GAP",
    "EVIDENCE_MEDIA_ALREADY_COMPLETE", "EVIDENCE_MEDIA_INCOMPLETE",
    "STT_STREAM_NOT_OPEN", "AGENT_EVIDENCE_NOT_OPEN", "INTERVIEW_NOT_IN_PROGRESS",
    "INTERVIEW_TURN_NOT_ACTIVE",
    "TURN_DECISION_STALE", "PERSISTENCE_CONFLICT", "EVIDENCE_COMMAND_RESULT_TIMEOUT",
})


@dataclass(frozen=True)
class CaptureFailure:
    stage: str
    cause_code: str
    cause_type: str
    retryable: bool

    @property
    def requires_new_capture(self) -> bool:
        return self.stage in {"send", "snapshot", "resume", "retry"} and self.cause_code.startswith("PROVIDER_")

    @property
    def safety_violation(self) -> bool:
        return self.cause_code.startswith("EVIDENCE_") or self.cause_code in {"INTERVIEW_NOT_IN_PROGRESS", "INTERVIEW_TURN_NOT_ACTIVE"}


def classify_capture_failure(exc: BaseException, stage: str) -> CaptureFailure:
    """Do not log exception messages, details, unknown vendor codes or responses."""
    stage = stage if stage in {"send", "snapshot", "resume", "commit", "retry", "understanding"} else "unknown"
    raw = str(getattr(exc, "code", "")).upper()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        raw = "PROVIDER_TIMEOUT"
    code = raw if raw in _KNOWN else "CAPTURE_INTERNAL_ERROR"
    kind = ("ProviderError" if isinstance(exc, ProviderError) else
            "ApiError" if isinstance(exc, ApiError) else
            "TimeoutError" if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) else "InternalError")
    # The provider may mark a dead socket non-retryable *on that socket*;
    # rebuilding it is allowed, but unknown errors never gain this authority.
    retryable = code in _TRANSIENT and stage in {"send", "snapshot", "resume", "retry"}
    return CaptureFailure(stage, code, kind, retryable)


def candidate_capture_recovery(session: dict[str, Any]) -> Optional[dict[str, Any]]:
    value = (session.get("agent_runtime") or {}).get("capture_recovery")
    if (not isinstance(value, dict) or session.get("status") != "in_progress"
            or value.get("turn_id") != session.get("current_turn_id")
            or value.get("status") not in {"recovering", "retry_required"}
            or not isinstance(value.get("turn_id"), str) or not value["turn_id"]
            or any(a.get("turn_id") == value["turn_id"] for a in session.get("answers", []))
            or (session.get("agent_runtime", {}).get("takeover") or {}).get("status") == "active"
            or not isinstance(value.get("capture_id"), str) or not 1 <= len(value["capture_id"]) <= 128
            or type(value.get("attempt")) is not int or not 0 <= value["attempt"] <= 3
            or type(value.get("max_attempts")) is not int or value.get("max_attempts") != 3):
        return None
    return {key: value[key] for key in
            ("status", "turn_id", "capture_id", "attempt", "max_attempts")}
