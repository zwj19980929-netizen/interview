"""Ephemeral, unscored microphone calibration for the interview agent.

The warm-up stream intentionally never opens a media recording.  Audio is
forwarded to the configured server-side STT provider, bounded in memory/time by
the transport, and discarded when the provider stream is closed.  Its final is
therefore useful only for the candidate's explicit "you heard me" check and can
never become CandidateAnswer evidence.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import StreamingAudioConfig, StreamingSTTRequest
from app.model_gateway.streaming import ValidatedSTTStream
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for


class WarmupCalibrationStream:
    """One non-persistent STT stream that cannot advance interview state."""

    def __init__(
        self,
        store: Any,
        interview_id: str,
        *,
        organization_id: str = "org_default",
        persistence: Optional[Persistence] = None,
    ) -> None:
        self.interview_id = interview_id
        self.organization_id = organization_id
        self.persistence = persistence or persistence_for(store)
        self.gateway = ModelGateway(store, persistence=self.persistence)
        self.stream: Optional[ValidatedSTTStream] = None
        self.byte_count = 0
        self.max_bytes = int(os.getenv("INTERVIEWER_WARMUP_MAX_AUDIO_BYTES", "8388608"))

    async def open(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        if self.stream is not None:
            raise ApiError(
                "WARMUP_STREAM_ALREADY_OPEN",
                "The warm-up calibration stream is already open.",
                status_code=409,
            )
        development_transcript = payload.get("development_transcript")
        if (
            development_transcript
            and os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower()
            == "production"
        ):
            raise ApiError(
                "DEVELOPMENT_TRANSCRIPT_NOT_ALLOWED",
                "Development transcript injection is disabled in production.",
                status_code=409,
            )
        audio = StreamingAudioConfig(
            content_type=str(payload.get("content_type") or "audio/pcm"),
            sample_rate_hz=int(payload.get("sample_rate_hz", 16000)),
            channels=int(payload.get("channels", 1)),
        )
        request = StreamingSTTRequest(
            organization_id=self.organization_id,
            interview_id=self.interview_id,
            turn_id="warmup_calibration",
            audio=audio,
            language=str(payload.get("language") or "zh-CN"),
            enable_partial=True,
            purpose="warmup_calibration",
            metadata={
                "development_transcript": development_transcript,
                "confidence": float(payload.get("development_confidence", 0.9)),
                "ephemeral": True,
                "unscored": True,
            },
        )
        try:
            self.stream = await self.gateway.open_stream(request)
        except ProviderError as exc:
            raise ApiError(
                exc.code.upper(), exc.message, status_code=502, details=exc.details
            ) from exc
        return [item.model_dump() for item in self.stream.ready_events]

    async def send_audio(self, chunk: bytes) -> List[Dict[str, Any]]:
        if self.stream is None:
            raise ApiError(
                "WARMUP_STREAM_NOT_OPEN",
                "Open the warm-up stream before sending audio.",
                status_code=409,
            )
        self.byte_count += len(chunk)
        if self.byte_count > self.max_bytes:
            await self.abort()
            raise ApiError(
                "WARMUP_AUDIO_TOO_LARGE",
                "Warm-up audio exceeded the ephemeral calibration limit.",
                status_code=413,
            )
        try:
            return [item.model_dump() for item in await self.stream.send_audio(chunk)]
        except ProviderError as exc:
            raise ApiError(
                exc.code.upper(), exc.message, status_code=502, details=exc.details
            ) from exc

    async def finish(self) -> List[Dict[str, Any]]:
        if self.stream is None:
            raise ApiError(
                "WARMUP_STREAM_NOT_OPEN",
                "No warm-up calibration stream is open.",
                status_code=409,
            )
        stream = self.stream
        self.stream = None
        try:
            events = await stream.finish()
        except ProviderError as exc:
            raise ApiError(
                exc.code.upper(), exc.message, status_code=502, details=exc.details
            ) from exc
        finals = [item for item in events if item.type == "transcript.final"]
        if len(finals) != 1 or not finals[0].text.strip():
            raise ApiError(
                "WARMUP_FINAL_INVALID",
                "Warm-up calibration requires exactly one non-empty server final.",
                status_code=502,
            )
        # No browser or provider transcript from this method is written to an
        # InterviewTurn. The caller may show it transiently for confirmation.
        return [item.model_dump() for item in events]

    async def abort(self) -> None:
        stream = self.stream
        self.stream = None
        self.byte_count = 0
        if stream is not None:
            await stream.abort()
