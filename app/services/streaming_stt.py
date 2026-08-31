import asyncio
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.adapters.private_media import media_recording_storage
from app.core.errors import ApiError
from app.core.prompt.realtime_dialogue import (
    approved_spoken_response_instruction,
    session_instruction,
)
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.dialogue import ValidatedSpeechDialogueStream
from app.model_gateway.schemas import (
    RealtimeSpeechDialogueRequest,
    RealtimeSpeechResponseCommand,
    StreamingAudioConfig,
    StreamingSTTEvent,
    StreamingSTTRequest,
)
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
        self.persistence = persistence_for(store)
        self.media = media_recording_storage(self.persistence)
        self.recording: Optional[Any] = None
        self.stream: Optional[ValidatedSTTStream] = None
        self.dialogue: Optional[ValidatedSpeechDialogueStream] = None
        self.turn_id: Optional[str] = None
        self.content_type = "audio/webm;codecs=opus"
        self.language = "zh-CN"
        self.development_transcript: Optional[str] = None
        self.development_confidence = 0.9
        self.sample_rate_hz = 48000
        self.channels = 1
        self.last_result: Optional[Dict[str, Any]] = None
        self.dialogue_mode = "cascade"

    def validate_candidate_token(self, token: Optional[str]) -> None:
        self.interviews.validate_candidate_token(self.interview_id, token)

    async def open(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        if self.stream or self.recording:
            raise ApiError("STT_STREAM_ALREADY_OPEN", "An STT stream is already open.", status_code=409)
        turn = self.interviews.require_active_turn(self.interview_id, payload.get("turn_id"))
        interview = self.interviews.get_interview(self.interview_id)
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
        sample_rate_hz = int(payload.get("sample_rate_hz", 48000))
        channels = int(payload.get("channels", 1))
        self.sample_rate_hz = sample_rate_hz
        self.channels = channels
        self.last_result = None
        self.dialogue_mode = str(
            interview.get("settings", {}).get("speech_dialogue_mode") or "cascade"
        )
        self.recording = self.media.start_recording(
            self.interview_id,
            self.turn_id,
            self.content_type,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
        )
        request = StreamingSTTRequest(
            interview_id=self.interview_id,
            turn_id=self.turn_id,
            audio=StreamingAudioConfig(
                content_type=self.content_type,
                sample_rate_hz=sample_rate_hz,
                channels=channels,
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
            ready = [item.model_dump() for item in self.stream.ready_events]
        except ProviderError as exc:
            self.recording.abort()
            self.recording = None
            raise ApiError(exc.code.upper(), exc.message, status_code=502, details=exc.details) from exc
        if self.dialogue_mode == "s2s":
            dialogue_request = RealtimeSpeechDialogueRequest(
                organization_id=str(interview.get("organization_id") or "org_default"),
                interview_id=self.interview_id,
                turn_id=self.turn_id,
                input_audio=StreamingAudioConfig(
                    content_type=self.content_type,
                    sample_rate_hz=sample_rate_hz,
                    channels=channels,
                ),
                output_audio=StreamingAudioConfig(
                    content_type="audio/pcm",
                    sample_rate_hz=24000,
                    channels=1,
                ),
                language=self.language,
                voice=str(interview.get("settings", {}).get("voice_profile_id") or "default"),
                turn_detection="manual",
                session_instructions=session_instruction(self.language),
                metadata={"development_transcript": self.development_transcript or ""},
            )
            try:
                self.dialogue = await self.gateway.open_speech_dialogue(dialogue_request)
                ready.extend(item.model_dump() for item in self.dialogue.ready_events)
            except ProviderError as exc:
                # S2S is the low-latency expression track, never the evidence
                # track. Route/configuration failure therefore falls back to
                # the retained STT -> policy -> avatar/TTS cascade.
                self.dialogue = None
                ready.append(self._dialogue_error(exc.code, "S2S route unavailable; cascade remains active."))
        return ready

    async def send_audio(self, chunk: bytes) -> List[Dict[str, Any]]:
        if not self.stream or not self.recording:
            raise ApiError("STT_STREAM_NOT_OPEN", "Open the STT stream before sending audio.", status_code=409)
        self.recording.append(chunk)
        try:
            stt_task = self.stream.send_audio(chunk)
            dialogue_task = self.dialogue.send_audio(chunk) if self.dialogue else None
            if dialogue_task is None:
                events = await stt_task
                dialogue_events: List[Any] = []
            else:
                stt_result, dialogue_result = await asyncio.gather(
                    stt_task, dialogue_task, return_exceptions=True
                )
                if isinstance(stt_result, Exception):
                    raise stt_result
                events = stt_result
                if isinstance(dialogue_result, Exception):
                    dialogue_events = [
                        self._dialogue_error(
                            getattr(dialogue_result, "code", "provider_stream_failed"),
                            "S2S stream failed; authoritative STT continues.",
                        )
                    ]
                    await self._abort_dialogue()
                else:
                    dialogue_events = [item.model_dump() for item in dialogue_result]
        except ProviderError as exc:
            raise ApiError(exc.code.upper(), exc.message, status_code=502, details=exc.details) from exc
        return [item.model_dump() for item in events] + dialogue_events

    async def finish(
        self,
        payload: Dict[str, Any],
        *,
        on_dialogue_event: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ) -> List[Dict[str, Any]]:
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
        self.last_result = result
        response = [item.model_dump() for item in events]
        response.append(
            {
                "type": "answer.accepted",
                "answer_id": result["answer"]["id"],
                "next_turn_id": result.get("next_turn_id"),
            }
        )
        response.append(
            {
                "type": "evaluation.queued",
                "answer_id": result["answer"]["id"],
                "work_item_id": result.get("evaluation_work_id"),
            }
        )
        if result.get("followup"):
            followup_event = {
                "type": "followup.selected",
                "payload": self._candidate_followup(result["followup"]),
                "delivery": "s2s" if self.dialogue else "cascade",
            }
            if on_dialogue_event and self.dialogue:
                await on_dialogue_event(followup_event)
            else:
                response.append(followup_event)
            if self.dialogue:
                followup_text = str(result["followup"].get("question_text") or "").strip()
                try:
                    async def emit_dialogue(item: Any) -> None:
                        if on_dialogue_event:
                            await on_dialogue_event(item.model_dump())

                    dialogue_events = await self.dialogue.commit(
                        RealtimeSpeechResponseCommand(
                            spoken_text=followup_text,
                            response_instructions=approved_spoken_response_instruction(followup_text),
                        ),
                        on_event=emit_dialogue if on_dialogue_event else None,
                    )
                    if not on_dialogue_event:
                        response.extend(item.model_dump() for item in dialogue_events)
                except (ProviderError, ValueError) as exc:
                    response.append(
                        self._dialogue_error(
                            getattr(exc, "code", "dialogue_response_failed"),
                            "S2S follow-up failed; client will use the normal avatar/TTS path.",
                        )
                    )
                    await self._abort_dialogue()
        else:
            await self._abort_dialogue()
        return response

    async def recover_disconnect(self) -> List[Dict[str, Any]]:
        """Finish persisted audio and repair it with batch STT after a socket loss."""
        if not self.recording or not self.turn_id:
            if self.stream:
                await self.stream.abort()
                self.stream = None
            await self._abort_dialogue()
            return []
        if self.stream:
            await self.stream.abort()
            self.stream = None
        await self._abort_dialogue()
        if int(getattr(self.recording, "byte_count", 0)) <= 0:
            self.recording.abort()
            self.recording = None
            return []
        recording = self.recording.finish()
        self.recording = None
        bytes_per_second = self.sample_rate_hz * self.channels * 2
        duration_seconds = max(1, int(getattr(recording, "byte_count", 0) / max(1, bytes_per_second)))
        try:
            result = await self.interviews.submit_audio_answer(
                self.interview_id,
                {
                    "turn_id": self.turn_id,
                    "audio_uri": recording.audio_uri,
                    "content_type": recording.mime_type,
                    "language": self.language,
                    "duration_seconds": duration_seconds,
                    "development_transcript": self.development_transcript,
                    "development_confidence": self.development_confidence,
                },
            )
        except Exception as exc:
            return [
                {
                    "type": "stream.error",
                    "error_code": "stream_disconnected_batch_repair_failed",
                    "message": "Streaming disconnected; persisted audio could not be batch-transcribed.",
                    "audio_uri": recording.audio_uri,
                    "details": {"error_type": type(exc).__name__},
                }
            ]
        self.last_result = result
        response = [
            {
                "type": "stream.error",
                "error_code": "stream_disconnected_batch_repaired",
                "message": "Streaming disconnected; persisted audio was repaired by batch STT.",
            },
            {
                "type": "answer.accepted",
                "answer_id": result["answer"]["id"],
                "next_turn_id": result.get("next_turn_id"),
            },
            {
                "type": "evaluation.queued",
                "answer_id": result["answer"]["id"],
                "work_item_id": result.get("evaluation_work_id"),
            },
        ]
        if result.get("followup"):
            response.append(
                {"type": "followup.selected", "payload": self._candidate_followup(result["followup"])}
            )
        return response

    async def close(self, *, repair_disconnect: bool = False) -> List[Dict[str, Any]]:
        if repair_disconnect:
            return await self.recover_disconnect()
        if self.stream:
            await self.stream.abort()
            self.stream = None
        await self._abort_dialogue()
        if self.recording:
            self.recording.abort()
            self.recording = None
        return []

    async def _abort_dialogue(self) -> None:
        dialogue = self.dialogue
        self.dialogue = None
        if dialogue:
            try:
                await dialogue.abort()
            except Exception:
                # Cleanup must not replace the authoritative STT outcome.
                return

    @staticmethod
    def _candidate_followup(value: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "turn_id": value.get("turn_id"),
            "parent_turn_id": value.get("parent_turn_id"),
            "root_turn_id": value.get("root_turn_id"),
            "followup_depth": value.get("followup_depth", 1),
            "question_text": value.get("question_text"),
        }

    def _dialogue_error(self, code: str, message: str) -> Dict[str, Any]:
        return {
            "stream_id": self.dialogue.stream_id if self.dialogue else "dialogue_unavailable",
            "sequence": 1,
            "type": "dialogue.error",
            "error_code": str(code),
            "message": message,
        }
