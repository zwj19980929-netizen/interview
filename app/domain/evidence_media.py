"""Durable media contracts for authoritative candidate Evidence."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class EvidenceMediaCheckpoint(BaseModel):
    """Highest contiguous audio position durably sealed for one turn."""

    model_config = ConfigDict(extra="forbid")

    stream_id: str = Field(min_length=1, max_length=128)
    interview_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    capture_revision: int = Field(ge=1)
    sealed_segment_count: int = Field(ge=0)
    last_sealed_frame_sequence: int = Field(ge=0)
    sealed_byte_count: int = Field(ge=0)
    ownership_epoch: int = Field(ge=1)
    recoverability: Literal["none", "sealed_prefix", "complete"]


class RecoveredEvidenceRecording(BaseModel):
    """Private recording materialized from a verified complete checkpoint."""

    model_config = ConfigDict(extra="forbid")

    stream_id: str = Field(min_length=1, max_length=128)
    interview_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    capture_revision: int = Field(ge=1)
    audio_uri: str = Field(min_length=1, max_length=512)
    content_type: str = Field(min_length=1, max_length=128)
    byte_count: int = Field(gt=0)
    source_pcm_byte_count: int = Field(gt=0)
    sample_rate_hz: int = Field(ge=8000, le=192000)
    channels: int = Field(ge=1, le=2)
    last_sealed_frame_sequence: int = Field(gt=0)
    sealed_segment_count: int = Field(gt=0)
    ownership_epoch: int = Field(ge=1)
    complete: bool
    discarded_unsealed_bytes: Optional[int] = Field(default=None, ge=0)
