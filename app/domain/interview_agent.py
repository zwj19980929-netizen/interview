"""Stable contracts owned by the real-time interview agent deep module.

The transport, provider and React layers exchange these contracts but cannot
advance the interview lifecycle themselves.  All server facts that are not
declared on :class:`OpenAgentSession` are deliberately loaded by the runtime.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FloorOwner(str, Enum):
    AGENT = "agent"
    CANDIDATE = "candidate"
    HUMAN = "human"
    NONE = "none"


class Replayability(str, Enum):
    REPLAYABLE = "replayable"
    TRANSIENT = "transient"


class ClientCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    webrtc: bool
    audio_worklet: bool
    webgl: bool
    camera: bool
    microphone: bool
    speaker: bool
    avatar_fps: float = Field(ge=0, le=240)
    media_recorder: bool = True
    browser: str = Field(default="unknown", max_length=160)


class OpenAgentSession(BaseModel):
    """Connection facts supplied by the authenticated transport only."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    interview_id: str = Field(min_length=1, max_length=128)
    principal: Any
    connection_id: str = Field(min_length=1, max_length=128)
    recovery_cursor: int = Field(default=0, ge=0)
    capabilities: ClientCapabilities


class AvatarPlaybackReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    performance_id: str = Field(min_length=1, max_length=128)
    status: Literal["playing", "blocked", "failed", "buffering"]


class ClientSignal(BaseModel):
    """One idempotent input to the runtime.

    Binary audio remains bytes until it reaches the Evidence chain. It is never
    serialized into an event payload or audit record.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    type: str = Field(min_length=1, max_length=96)
    idempotency_key: str = Field(min_length=1, max_length=160)
    turn_id: Optional[str] = Field(default=None, max_length=128)
    causation_id: Optional[str] = Field(default=None, max_length=128)
    payload: Dict[str, Any] = Field(default_factory=dict)
    audio: Optional[bytes] = None

    @model_validator(mode="after")
    def require_audio_only_for_audio_signal(self) -> "ClientSignal":
        if self.audio is not None and self.type != "evidence.audio.chunk":
            raise ValueError("binary audio is accepted only by evidence.audio.chunk")
        if self.type == "evidence.audio.chunk" and self.audio is None:
            raise ValueError("evidence.audio.chunk requires binary audio")
        if self.audio is not None and not (1 <= len(self.audio) <= 256 * 1024):
            raise ValueError("one evidence audio frame must contain 1 to 262144 bytes")
        if self.type == "avatar.performance.playback":
            AvatarPlaybackReport.model_validate(self.payload)
        return self


class AgentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)
    session_sequence: int = Field(ge=1)
    type: Literal[
        "session.snapshot",
        "floor.changed",
        "speech.started",
        "speech.stopped",
        "transcript.partial",
        "transcript.final",
        "conversation.act.selected",
        "avatar.performance.started",
        "avatar.performance.playback",
        "avatar.performance.producer_finished",
        "avatar.performance.cue",
        "avatar.performance.interrupted",
        "avatar.performance.stopped",
        "takeover.changed",
        "problem",
        "completed",
    ]
    turn_id: Optional[str] = Field(default=None, max_length=128)
    causation_id: Optional[str] = Field(default=None, max_length=128)
    occurred_at: str = Field(min_length=1, max_length=64)
    replayability: Replayability
    payload: Dict[str, Any] = Field(default_factory=dict)


class VisemeCue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at_ms: int = Field(ge=0)
    duration_ms: int = Field(gt=0, le=10_000)
    shape: Literal[
        "sil",
        "PP",
        "FF",
        "TH",
        "DD",
        "kk",
        "CH",
        "SS",
        "nn",
        "RR",
        "aa",
        "E",
        "ih",
        "oh",
        "ou",
    ]
    weight: float = Field(ge=0, le=1)


class GestureCue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at_ms: int = Field(ge=0)
    duration_ms: int = Field(gt=0, le=60_000)
    gesture: Literal[
        "idle",
        "blink",
        "look_at_candidate",
        "breathe",
        "nod",
        "listen",
        "think",
        "interrupt",
        "farewell",
    ]
    intensity: float = Field(default=1.0, ge=0, le=1)


class LiveSpeechBinding(BaseModel):
    """Exact approved room-track binding; no credentials or provider URLs."""

    model_config = ConfigDict(extra="forbid", strict=True)

    output_id: str = Field(min_length=1, max_length=128)
    publisher_identity: str = Field(min_length=1, max_length=160)
    track_sid: str = Field(min_length=1, max_length=128)
    track_name: str = Field(min_length=1, max_length=160)
    sample_rate_hz: Literal[24000]
    channels: Literal[1]


class AvatarPerformance(BaseModel):
    """One approved, interruptible local avatar performance."""

    model_config = ConfigDict(extra="forbid")

    performance_id: str = Field(min_length=1, max_length=128)
    turn_id: Optional[str] = Field(default=None, max_length=128)
    audio_uri: Optional[str] = Field(default=None, max_length=2048)
    audio_clock_origin_ms: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=1200)
    visemes: List[VisemeCue] = Field(min_length=1, max_length=10_000)
    gestures: List[GestureCue] = Field(default_factory=list, max_length=256)
    alignment_source: Literal["provider_timestamp", "g2p_estimate"]
    delivery: Literal["pre_generated", "cascade", "s2s", "streaming_tts"] = "cascade"
    live_audio: Optional[LiveSpeechBinding] = None
    interruptible: bool = True

    @model_validator(mode="after")
    def validate_live_audio(self) -> "AvatarPerformance":
        if self.delivery == "streaming_tts":
            if self.live_audio is None or self.audio_uri:
                raise ValueError("streaming TTS requires a track binding and no audio URL")
            if (self.live_audio.publisher_identity != "expression:%s" % self.performance_id
                    or self.live_audio.track_name != "approved-expression:%s" % self.performance_id):
                raise ValueError("live track must belong to this performance")
        elif self.live_audio is not None:
            raise ValueError("asset performances cannot include a live track")
        return self

    @field_validator("visemes")
    @classmethod
    def require_monotonic_visemes(cls, value: List[VisemeCue]) -> List[VisemeCue]:
        positions = [cue.at_ms for cue in value]
        if positions != sorted(positions):
            raise ValueError("viseme cues must be ordered by at_ms")
        return value


class TakeoverLease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)
    expires_at: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    media_participant_identity: Optional[str] = Field(
        default=None, min_length=1, max_length=160
    )
    media_permit_generation: int = Field(default=0, ge=0)
    media_permit_lease_version: Optional[int] = Field(default=None, ge=1)
    media_permit_issued_at: Optional[str] = Field(default=None, max_length=64)
    media_permit_expires_at: Optional[str] = Field(default=None, max_length=64)


class ConversationUtterance(BaseModel):
    """Versioned speech evidence; only server-final audio may be authoritative."""

    model_config = ConfigDict(extra="forbid")

    utterance_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    speaker: Literal["candidate", "agent", "human"]
    text: str = Field(min_length=1, max_length=12_000)
    is_final: bool
    authoritative: bool
    audio_uri: Optional[str] = Field(default=None, max_length=2048)
    stt_confidence: float = Field(ge=0, le=1)
    source: Literal["server_streaming", "server_batch", "approved_act", "human_intervention"]
    created_at: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def authoritative_requires_server_final_audio(self) -> "ConversationUtterance":
        if self.authoritative and (
            not self.is_final
            or self.source not in {"server_streaming", "server_batch"}
            or not self.audio_uri
        ):
            raise ValueError("authoritative utterance requires server final and persisted audio")
        return self


class ClaimEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1, max_length=600)
    evidence_quote: str = Field(min_length=1, max_length=300)


class UnderstandingProblem(BaseModel):
    """Sanitised failure projected when semantic understanding is unavailable."""

    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "UNDERSTANDING_PROVIDER_UNAVAILABLE",
        "UNDERSTANDING_RESULT_REJECTED",
    ]
    source: Literal["provider", "schema_or_content"]
    recoverable: bool
    action: Literal["clarify", "pause"]
    retryable: bool
    reason_code: Optional[Literal[
        "provider_unavailable", "wire_schema_invalid", "point_duplicates",
        "point_not_frozen", "point_partition_invalid", "evidence_not_verbatim",
        "answer_evidence_empty", "claim_evidence_undeclared", "answer_declined_contract_invalid",
    ]] = None
    attempts: int = Field(default=1, ge=1, le=2)


class TurnUnderstanding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    understanding_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    # v1 remains readable for immutable historical understandings only.
    prompt_version: Literal["interview_turn_understanding.v1", "interview_turn_understanding.v2", "interview_turn_understanding.v3", "interview_turn_understanding.v4", "interview_turn_understanding.v5", "interview_turn_understanding.v6", "interview_turn_understanding.v7", "interview_turn_decision.v1", "interview_turn_decision.v2", "interview_turn_decision.v3", "interview_turn_decision.v4", "interview_turn_decision.v5", "interview_turn_decision.v6"]
    utterance_id: str = Field(min_length=1, max_length=128)
    intent: Literal[
        "answer",
        "answer_declined",
        "request_repeat",
        "not_finished",
        "pause",
        "clarification_request",
        "off_topic",
    ]
    answer_summary: str = Field(max_length=800)
    claims: List[ClaimEvidence] = Field(default_factory=list, max_length=12)
    evidence_quotes: List[str] = Field(default_factory=list, max_length=12)
    covered_capability_points: List[str] = Field(default_factory=list, max_length=20)
    missing_capability_points: List[str] = Field(default_factory=list, max_length=20)
    ambiguities: List[str] = Field(default_factory=list, max_length=8)
    contradictions: List[str] = Field(default_factory=list, max_length=8)
    confidence: float = Field(ge=0, le=1)
    suggested_action: Literal[
        "accept", "clarify", "repeat", "continue_listening", "pause", "followup", "next"
    ]
    provider: Dict[str, Any]
    problem: Optional[UnderstandingProblem] = None
    created_at: str = Field(min_length=1, max_length=64)


class ApprovedConversationAct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    act_id: str = Field(min_length=1, max_length=128)
    act_type: Literal[
        "question",
        "repeat",
        "clarification",
        "followup",
        "neutral_bridge",
        "opening",
        "warmup_confirmation",
        "closing",
        "unscored_intervention",
        "supplement_check",
        "supplement_continue",
        "supplement_clarify",
    ]
    text: str = Field(min_length=1, max_length=1000)
    turn_id: Optional[str] = Field(default=None, max_length=128)
    root_turn_id: Optional[str] = Field(default=None, max_length=128)
    evidence_quotes: List[str] = Field(default_factory=list, max_length=4)
    target_capability_points: List[str] = Field(default_factory=list, max_length=2)
    followup_depth: int = Field(default=0, ge=0, le=2)
    evaluative: bool = False
    approved_by: str = Field(min_length=1, max_length=128)
    prompt_version: Optional[str] = Field(default=None, max_length=128)
    created_at: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def enforce_expression_safety(self) -> "ApprovedConversationAct":
        """Keep every AI expression bound to an auditable, neutral fact."""

        if self.evaluative:
            raise ValueError("conversation acts cannot evaluate the candidate")
        if self.act_type == "followup":
            if not self.root_turn_id:
                raise ValueError("followup acts require a frozen root turn")
            if self.followup_depth < 1:
                raise ValueError("followup acts require a positive followup depth")
            if not any(item.strip() for item in self.evidence_quotes):
                raise ValueError("followup acts require non-empty evidence")
            if not any(item.strip() for item in self.target_capability_points):
                raise ValueError("followup acts require a frozen capability target")
        if self.act_type != "unscored_intervention":
            normalized = " ".join(self.text.casefold().split())
            prohibited_feedback = (
                "回答得很好",
                "答得很好",
                "回答正确",
                "完全正确",
                "非常棒",
                "做得很好",
                "excellent answer",
                "great answer",
                "correct answer",
                "well done",
            )
            if any(fragment in normalized for fragment in prohibited_feedback):
                raise ValueError(
                    "AI conversation acts cannot contain evaluative feedback"
                )
        return self


STABLE_AGENT_EVENT_TYPES = frozenset(AgentEvent.model_fields["type"].annotation.__args__)
