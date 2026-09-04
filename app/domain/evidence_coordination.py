"""Provider-neutral ownership contracts for authoritative candidate Evidence.

These values cross the service/persistence seam.  LiveKit room, Redis and STT
objects deliberately do not: an ownership epoch answers only which process may
produce authoritative effects for one interview.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class EvidenceCommitFence(BaseModel):
    """Immutable proof presented again in the CandidateAnswer transaction."""

    model_config = ConfigDict(extra="forbid")

    ownership_id: str = Field(min_length=1, max_length=256)
    owner_instance_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=128)
    ownership_epoch: int = Field(ge=1)


class EvidenceOwnershipGrant(BaseModel):
    """Result of attaching one control connection to the Evidence Module."""

    model_config = ConfigDict(extra="forbid")

    ownership_id: str = Field(min_length=1, max_length=256)
    interview_id: str = Field(min_length=1, max_length=128)
    organization_id: str = Field(min_length=1, max_length=128)
    local_instance_id: str = Field(min_length=1, max_length=128)
    owner_instance_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=128)
    ownership_epoch: int = Field(ge=1)
    lease_expires_at: str = Field(min_length=1, max_length=64)
    control_connection_id: str = Field(min_length=1, max_length=128)
    control_generation: int = Field(ge=1)
    disposition: Literal["local_owner", "remote_owner"]

    @property
    def is_local_owner(self) -> bool:
        return self.disposition == "local_owner"

    def commit_fence(self) -> EvidenceCommitFence:
        return EvidenceCommitFence(
            ownership_id=self.ownership_id,
            owner_instance_id=self.owner_instance_id,
            lease_id=self.lease_id,
            ownership_epoch=self.ownership_epoch,
        )


EvidenceCommandType = Literal[
    "evidence.open",
    "speech.started",
    "speech.stopped",
    "evidence.continue",
    "evidence.seal",
    "evidence.reset",
    "evidence.backfill",
]


class EvidenceCommandSubmission(BaseModel):
    """Minimal, provider-free command accepted by the durable journal."""

    model_config = ConfigDict(extra="forbid")

    command_type: EvidenceCommandType
    idempotency_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    turn_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    causation_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    payload: Dict[str, Any] = Field(default_factory=dict)
    deadline_seconds: float = Field(default=15.0, ge=0.1, le=120.0)


class EvidenceCommandOutcome(BaseModel):
    """Safe terminal projection cached for idempotent controller retries."""

    model_config = ConfigDict(extra="forbid")

    disposition: Literal["applied", "rejected"]
    effective_turn_id: Optional[str] = Field(
        default=None, min_length=1, max_length=128
    )
    event_cursor: Optional[int] = Field(default=None, ge=0)
    error_code: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Z0-9_]+$",
    )
    retryable: bool = False


class EvidenceCommandReceipt(BaseModel):
    """Submission/result receipt; it never contains audio or transcript text."""

    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1, max_length=128)
    disposition: Literal["accepted", "duplicate"]
    status: Literal["pending", "running", "completed", "rejected", "expired"]
    outcome: Optional[EvidenceCommandOutcome] = None


class ClaimedEvidenceCommand(BaseModel):
    """One at-least-once owner execution claim under a current fence."""

    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1, max_length=128)
    claim_id: str = Field(min_length=1, max_length=128)
    command_type: EvidenceCommandType
    turn_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    causation_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    payload: Dict[str, Any] = Field(default_factory=dict)
    control_generation: int = Field(ge=1)
    ownership_epoch: int = Field(ge=1)
    attempt_count: int = Field(ge=1)
    deadline_at: str = Field(min_length=1, max_length=64)
