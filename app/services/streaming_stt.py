import os
from typing import Any, Dict, List, Optional

from app.adapters.local_media import LocalMediaRecording, LocalMediaStorage
from app.core.errors import ApiError
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import StreamingAudioConfig, StreamingSTTEvent, StreamingSTTRequest
from app.model_gateway.streaming import ValidatedSTTStream
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.interviews import InterviewService


class StreamingInterviewSTT:
    """One turn's server streaming STT, recording, finalization and scoring interface."""

    def __init__(self, store: InMemoryStore, interview_id: str) -> None:
        self.interview_id = interview_id
        self.interviews = InterviewService(store)
        self.gateway = ModelGateway(store, persistence=persistence_for(store))
        self.media = LocalMediaStorage()
        self.recording: Optional[LocalMediaRecording] = None
        self.stream: Optional[ValidatedSTTStream] = None
        self.turn_id: Optional[str] = None
        self.content_type = "audio/webm;codecs=opus"
        self.language = "zh-CN"
        self.development_transcript: Optional[str] = None
        self.development_confidence = 0.9

    def validate_candidate_token(self, token: Optional[str]) -> None:
        self.interviews.validate_candidate_token(self.interview_id, token)

    async def open(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        if self.stream or self.recording:
            raise ApiError("STT_STREAM_ALREADY_OPEN", "An STT stream is already open.", status_code=409)
        turn = self.interviews.require_active_turn(self.interview_id, payload.get("turn_id"))
        self.turn_id = turn["id"]
        self.content_type = str(payload.get("content_type") or "audio/webm;codecs=opus")
        self.language = str(payload.get("language") or "zh-CN")
        self.development_transcript = payload.get("development_transcript")
        self.development_confidence = float(payload.get("development_confidence", 0.9))
        if (
            self.development_transcript
            and os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production"
        ):
            raise ApiError(
                "DEVELOPMENT_TRANSCRIPT_NOT_ALLOWED",
                "Development transcript injection is disabled in production.",
                status_code=409,
            )
        self.recording = self.media.start_recording(self.interview_id, self.turn_id, self.content_type)
        request = StreamingSTTRequest(
            interview_id=self.interview_id,
            turn_id=self.turn_id,
            audio=StreamingAudioConfig(
                content_type=self.content_type,
                sample_rate_hz=int(payload.get("sample_rate_hz", 48000)),
                channels=int(payload.get("channels", 1)),
            ),
            language=self.language,
            enable_partial=bool(payload.get("enable_partial", True)),
            metadata={
                "development_transcript": self.development_transcript,
                "confidence": self.development_confidence,
                "duration_ms": int(payload.get("duration_seconds", 0)) * 1000,
            },
        )
        try:
            self.stream = await self.gateway.open_stream(request)
            return [item.model_dump() for item in self.stream.ready_events]
        except ProviderError as exc:
            self.recording.abort()
            self.recording = None
            raise ApiError(exc.code.upper(), exc.message, status_code=502, details=exc.details) from exc

    async def send_audio(self, chunk: bytes) -> List[Dict[str, Any]]:
        if not self.stream or not self.recording:
            raise ApiError("STT_STREAM_NOT_OPEN", "Open the STT stream before sending audio.", status_code=409)
        self.recording.append(chunk)
        try:
            events = await self.stream.send_audio(chunk)
        except ProviderError as exc:
            raise ApiError(exc.code.upper(), exc.message, status_code=502, details=exc.details) from exc
        return [item.model_dump() for item in events]

    async def finish(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self.stream or not self.recording or not self.turn_id:
            raise ApiError("STT_STREAM_NOT_OPEN", "No STT stream is open.", status_code=409)
        recording = self.recording.finish()
        self.recording = None
        try:
            events = await self.stream.finish()
            final = next(item for item in events if item.type == "transcript.final")
            result = await self.interviews.submit_streaming_answer(
                self.interview_id,
                {
                    "turn_id": self.turn_id,
                    "audio_uri": recording.audio_uri,
                    "content_type": recording.mime_type,
                    "final_transcript": final.text,
                    "confidence": final.confidence,
                    "language": final.language,
                    "duration_seconds": int(payload.get("duration_seconds", 0)),
                    "provider": final.provider.model_dump() if final.provider else {},
                    "segments": [item.model_dump() for item in final.segments],
                },
            )
        except (ProviderError, StopIteration) as exc:
            try:
                result = await self.interviews.submit_audio_answer(
                    self.interview_id,
                    {
                        "turn_id": self.turn_id,
                        "audio_uri": recording.audio_uri,
                        "content_type": recording.mime_type,
                        "language": self.language,
                        "duration_seconds": int(payload.get("duration_seconds", 0)),
                        "development_transcript": self.development_transcript,
                        "development_confidence": self.development_confidence,
                    },
                )
                events = [
                    StreamingSTTEvent(
                        stream_id=self.stream.stream_id,
                        sequence=999999,
                        type="stream.error",
                        error_code="stream_failed_batch_repaired",
                    )
                ]
            except Exception as repair_exc:
                raise ApiError(
                    "STT_STREAM_AND_REPAIR_FAILED",
                    "Streaming transcription and batch repair both failed.",
                    status_code=502,
                    details={"stream_error": str(exc), "repair_error": str(repair_exc)},
                ) from repair_exc
        self.stream = None
        response = [item.model_dump() for item in events]
        response.append(
            {
                "type": "evaluation.completed",
                "answer_id": result["answer"]["id"],
                "score": result["evaluation"]["score"],
                "confidence": result["evaluation"]["confidence"],
                "next_turn_id": result.get("next_turn_id"),
            }
        )
        if result.get("status") == "report_ready":
            response.append({"type": "interview.completed", "status": "report_ready"})
        return response

    async def close(self) -> None:
        if self.stream:
            await self.stream.abort()
            self.stream = None
        if self.recording:
            self.recording.abort()
            self.recording = None
