import asyncio
import base64
import binascii
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.adapters.private_media import media_recording_storage
from app.core.errors import ApiError
from app.domain.evidence_coordination import EvidenceCommitFence
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
    StableTranscriptPreview,
)
from app.model_gateway.streaming import ValidatedSTTStream
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.interviews import InterviewService
from app.services.agent_expression_audio import AgentExpressionAudioService
from app.services.evidence_media import (
    DurableEvidenceMedia,
    DurableEvidenceMediaWriter,
)
from app.services.continuous_stt import ContinuousSTT
from app.services.recognition_vocabulary import recognition_terms
from app.adapters.speech_activity import ServerSpeechActivity
from app.core.interview_agent_metrics import measure_interview_agent_stage


class StreamingInterviewSTT:
    """One turn's server streaming STT, recording, finalization and scoring interface."""

    def __init__(
        self,
        store: InMemoryStore,
        interview_id: str,
        *,
        organization_id: str = "org_default",
        persistence: Optional[Persistence] = None,
        commit_fence: Optional[EvidenceCommitFence] = None,
        commit_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        self.interview_id = interview_id
        self.organization_id = organization_id
        self.persistence = persistence or persistence_for(store)
        self.interviews = InterviewService(store, persistence=self.persistence)
        self.gateway = ModelGateway(store, persistence=self.persistence)
        self.media = media_recording_storage(self.persistence, organization_id)
        self.expression_audio = AgentExpressionAudioService(self.persistence)
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
        self.commit_fence = commit_fence
        self.commit_guard = commit_guard
        self.evidence_media = (
            DurableEvidenceMedia(
                store,
                organization_id=organization_id,
                persistence=self.persistence,
            )
            if commit_fence is not None
            else None
        )
        self.evidence_media_writer: Optional[DurableEvidenceMediaWriter] = None
        self.last_media_checkpoint: Optional[Dict[str, Any]] = None
        self.continuous = False
        self._capture_generation = 0

    def validate_candidate_token(self, token: Optional[str]) -> None:
        self.interviews.validate_candidate_token(
            self.interview_id, token, self.organization_id
        )

    async def open(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        if self.stream or self.recording:
            raise ApiError("STT_STREAM_ALREADY_OPEN", "An STT stream is already open.", status_code=409)
        self._capture_generation += 1
        generation = self._capture_generation
        self.continuous = bool(payload.get("_continuous_capture"))
        try:
            return await self._open_capture(payload, generation)
        except BaseException as exc:
            # Cancellation during the first provider handshake has allocated
            # recorders too. Never leave an apparently open capture behind.
            await self._discard_capture(generation)
            if isinstance(exc, ProviderError):
                raise ApiError(exc.code.upper(), exc.message, status_code=502, details=exc.details) from exc
            raise

    async def _open_capture(self, payload: Dict[str, Any], generation: int) -> List[Dict[str, Any]]:
        continuous = bool(payload.get("_continuous_capture"))
        turn = self.interviews.require_active_turn(
            self.interview_id,
            payload.get("turn_id"),
            organization_id=self.organization_id,
        )
        interview = self.interviews.get_interview(
            self.interview_id, self.organization_id
        )
        self.turn_id = turn["id"]
        if continuous:
            self._assert_capture_active(generation)
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
        self.last_media_checkpoint = None
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
        if self.commit_fence is not None:
            assert self.evidence_media is not None
            try:
                self.evidence_media_writer = self.evidence_media.open_writer(
                    interview_id=self.interview_id,
                    turn_id=self.turn_id,
                    content_type=self.content_type,
                    sample_rate_hz=sample_rate_hz,
                    channels=channels,
                    fence=self.commit_fence,
                )
            except BaseException:
                self.recording.abort()
                self.recording = None
                raise
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
            recognition_terms=recognition_terms(turn),
            metadata={
                "development_transcript": self.development_transcript,
                "confidence": self.development_confidence,
                "duration_ms": int(payload.get("duration_seconds", 0)) * 1000,
            },
        )
        self.stream = (await self._open_recognition_stream(request, generation)
                       if continuous else await self.gateway.open_stream(request))
        ready = [item.model_dump() for item in self.stream.ready_events]
        if continuous:
            self.stream = ContinuousSTT(
                self.stream, reopen=lambda: self._open_recognition_stream(request, generation),
                record=self._record_frame,
                bytes_per_second=sample_rate_hz * channels * 2,
                speech_activity=ServerSpeechActivity(sample_rate_hz=sample_rate_hz, channels=channels),
                max_bytes=int(os.getenv("INTERVIEWER_STT_STREAM_MAX_BYTES", "52428800")),
            )
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
                dialogue = await self.gateway.open_speech_dialogue(dialogue_request)
                if continuous:
                    try:
                        self._assert_capture_active(generation)
                    except BaseException:
                        try:
                            await asyncio.wait_for(dialogue.abort(), timeout=2.0)
                        except Exception:
                            pass
                        raise
                self.dialogue = dialogue
                ready.extend(item.model_dump() for item in self.dialogue.ready_events)
            except ProviderError as exc:
                # S2S is the low-latency expression track, never the evidence
                # track. Route/configuration failure therefore falls back to
                # the retained STT -> policy -> avatar/TTS cascade.
                self.dialogue = None
                ready.append(self._dialogue_error(exc.code, "S2S route unavailable; cascade remains active."))
        if continuous:
            self._assert_capture_active(generation)
        return ready

    async def _open_recognition_stream(self, request: StreamingSTTRequest,
                                       generation: int) -> ValidatedSTTStream:
        # This is a segment/recovery seam, not the high-frequency PCM path.
        # A current owner may still belong to a paused or superseded question.
        await self._ensure_capture_active(generation)
        stream = await self.gateway.open_stream(request)
        try:
            self._assert_capture_active(generation)
        except BaseException:
            try:
                await asyncio.wait_for(stream.abort(), timeout=2.0)
            except Exception:
                # Cleanup must not replace the authoritative ownership/session
                # failure or expose a provider response to the caller.
                pass
            finally:
                await self._discard_capture(generation)
            raise
        return stream

    async def _discard_capture(self, generation: int) -> None:
        # A late open may return after close + a new open on the same object.
        # Its cleanup must never detach the new generation's recorder/stream.
        if self._capture_generation == generation:
            try:
                await self.close(repair_disconnect=False)
            except Exception:
                pass

    async def _ensure_capture_active(self, generation: int) -> None:
        try:
            self._assert_capture_active(generation)
        except BaseException:
            await self._discard_capture(generation)
            raise

    async def send_audio(self, chunk: bytes) -> List[Dict[str, Any]]:
        if not self.stream or not self.recording:
            raise ApiError("STT_STREAM_NOT_OPEN", "Open the STT stream before sending audio.", status_code=409)
        if not self.continuous:
            self._record_frame(chunk)
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

    def _record_frame(self, chunk: bytes) -> None:
        if self.evidence_media_writer is not None:
            self.evidence_media_writer.append(chunk)
        if self.recording is None:
            raise ApiError("STT_STREAM_NOT_OPEN", "Recording is closed.", status_code=409)
        self.recording.append(chunk)

    @property
    def supports_stable_preview(self) -> bool:
        return isinstance(self.stream, ContinuousSTT) and self.stream.supports_stable_preview

    async def transcript_preview(self) -> Optional[StableTranscriptPreview]:
        if not isinstance(self.stream, ContinuousSTT):
            raise ApiError("CONTINUOUS_CAPTURE_REQUIRED", "Continuous capture required.", status_code=409)
        stream, generation = self.stream, self._capture_generation
        self._assert_commit_allowed()
        preview = await stream.preview()
        self._assert_commit_allowed()
        if stream is not self.stream or generation != self._capture_generation:
            raise ApiError("TURN_DECISION_STALE", "Capture changed during preview.", status_code=409)
        return preview

    def checkpoint_for_preparation(self) -> Optional[Dict[str, Any]]:
        # Full session/turn reads belong at actual preparation, not every
        # local preview poll. Ownership is checked on both paths.
        self._assert_capture_active(self._capture_generation)
        return self.checkpoint_incomplete()

    async def transcript_snapshot(self, *, resume: bool = True) -> Optional[StreamingSTTEvent]:
        if not isinstance(self.stream, ContinuousSTT):
            raise ApiError("CONTINUOUS_CAPTURE_REQUIRED", "Continuous capture required.", status_code=409)
        self._assert_commit_allowed()
        with measure_interview_agent_stage("stt_snapshot_ms" if resume else "stt_final_ms"):
            final = await self.stream.snapshot(resume=False)
        self._assert_commit_allowed()
        if self.evidence_media_writer is not None:
            # A durable prefix, NOT a complete answer or a complete capture.
            checkpoint = self.evidence_media_writer.seal(complete=False)
            self.last_media_checkpoint = checkpoint.model_dump(mode="json")
        if resume:
            await self.stream.resume()
        return final

    def assert_snapshot_current(self, prepared_decision: Any = None) -> None:
        if isinstance(self.stream, ContinuousSTT):
            try:
                self.stream.assert_can_commit()
            except ProviderError as exc:
                if exc.code == "provider_snapshot_stale":
                    raise ApiError("TURN_DECISION_STALE", "Audio arrived after snapshot.", status_code=409) from exc
                raise
            if prepared_decision is not None:
                self.interviews.assert_prepared_streaming_decision(
                    prepared_decision, self.stream.confirmed_final(), self.organization_id,
                )

    async def resume_capture(self) -> None:
        if isinstance(self.stream, ContinuousSTT):
            generation = self._capture_generation
            await self._ensure_capture_active(generation)
            await self.stream.resume()
            await self._ensure_capture_active(generation)

    @property
    def recovery_required(self) -> bool:
        return isinstance(self.stream, ContinuousSTT) and self.stream.recovery_required

    @property
    def recovery_error(self) -> Any:
        return self.stream.recovery_error if isinstance(self.stream, ContinuousSTT) else None

    async def recover_capture(self) -> None:
        if not isinstance(self.stream, ContinuousSTT):
            raise ApiError("CONTINUOUS_CAPTURE_REQUIRED", "Continuous capture required.", status_code=409)
        generation = self._capture_generation
        await self._ensure_capture_active(generation)
        await self.stream.recover()
        await self._ensure_capture_active(generation)

    def checkpoint_incomplete(self) -> Optional[Dict[str, Any]]:
        self._assert_commit_allowed()
        if self.evidence_media_writer is not None:
            self.last_media_checkpoint = self.evidence_media_writer.seal(complete=False).model_dump(mode="json")
        return self.last_media_checkpoint

    async def finish(
        self,
        payload: Dict[str, Any],
        *,
        on_dialogue_event: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        prepared_decision: Any = None,
    ) -> List[Dict[str, Any]]:
        if not self.stream or not self.recording or not self.turn_id:
            raise ApiError("STT_STREAM_NOT_OPEN", "No STT stream is open.", status_code=409)
        try:
            self._assert_commit_allowed()
        except BaseException:
            await self.close(repair_disconnect=False)
            raise
        committed_final = None
        if prepared_decision is not None and isinstance(self.stream, ContinuousSTT):
            self.assert_snapshot_current(prepared_decision)
            committed_final = self.stream.commit_final()
        if self.evidence_media_writer is not None:
            checkpoint = self.evidence_media_writer.seal(complete=True)
            self.last_media_checkpoint = checkpoint.model_dump(mode="json")
        recording = self.recording.finish()
        self.recording = None
        try:
            events = [committed_final] if committed_final is not None else await self.stream.finish()
            if any(item.type == "transcript.empty" for item in events):
                # A complete recognition with no words is a non-answer; do
                # not reinterpret it via batch repair or development text.
                raise ProviderError("provider_final_transcript_missing", "Recognition completed without words.",
                                    retryable=False)
            final = next(item for item in events if item.type == "transcript.final")
            self._assert_commit_allowed()
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
                    "media_evidence": self._media_evidence_payload(complete=True),
                },
                self.organization_id,
                evidence_fence=self.commit_fence,
                prepared_decision=prepared_decision,
            )
        except (ProviderError, StopIteration) as exc:
            if getattr(exc, "code", None) == "provider_final_transcript_missing":
                # No valid transcript is not an answer. Do not manufacture
                # semantics through a development/batch fallback on silence.
                result = self.interviews.record_untranscribed_capture(
                    self.interview_id, {
                        "turn_id": self.turn_id, "audio_uri": recording.audio_uri,
                        "content_type": recording.mime_type,
                        "media_evidence": self._media_evidence_payload(complete=True),
                    }, self.organization_id, evidence_fence=self.commit_fence,
                )
                await self.stream.abort()
                self.stream = None
                self.evidence_media_writer = None
                self.last_result = result
                await self._abort_dialogue()
                return [self._non_answer_event(result)]
            try:
                self._assert_commit_allowed()
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
                        "media_evidence": self._media_evidence_payload(complete=True),
                    },
                    self.organization_id,
                    evidence_fence=self.commit_fence,
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
        self.evidence_media_writer = None
        self.last_result = result
        response = [item.model_dump() for item in events]
        if not result.get("accepted", True):
            response.append(self._non_answer_event(result))
            await self._abort_dialogue()
            return response
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
            followup_event: Dict[str, Any] = {
                "type": "followup.selected",
                "payload": self._candidate_followup(result["followup"]),
                "delivery": "cascade",
            }
            if self.dialogue and prepared_decision is None:
                followup_text = str(result["followup"].get("question_text") or "").strip()
                try:
                    dialogue_events = await self.dialogue.commit(
                        RealtimeSpeechResponseCommand(
                            spoken_text=followup_text,
                            response_instructions=approved_spoken_response_instruction(followup_text),
                        ),
                    )
                    followup_event["delivery"] = "s2s"
                    followup_event["expression"] = self._materialize_dialogue_expression(
                        dialogue_events
                    )
                except (ApiError, ProviderError, ValueError) as exc:
                    response.append(
                        self._dialogue_error(
                            getattr(exc, "code", "dialogue_response_failed"),
                            "S2S follow-up failed; the approved cascade TTS path will be used.",
                        )
                    )
                finally:
                    await self._abort_dialogue()
            elif self.dialogue:
                await self._abort_dialogue()
            if on_dialogue_event:
                await on_dialogue_event(followup_event)
            else:
                response.append(followup_event)
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
        try:
            self._assert_commit_allowed()
        except BaseException:
            await self.close(repair_disconnect=False)
            raise
        if self.stream:
            await self.stream.abort()
            self.stream = None
        await self._abort_dialogue()
        if int(getattr(self.recording, "byte_count", 0)) <= 0:
            self.recording.abort()
            self.recording = None
            if self.evidence_media_writer is not None:
                self.evidence_media_writer.abort()
                self.evidence_media_writer = None
            return []
        if self.evidence_media_writer is not None and self.commit_fence is not None:
            checkpoint = self.evidence_media_writer.seal(complete=True)
            self.last_media_checkpoint = checkpoint.model_dump(mode="json")
            self.evidence_media_writer = None
            self.recording.abort()
            self.recording = None
            assert self.evidence_media is not None
            recovered = self.evidence_media.recover(
                interview_id=self.interview_id,
                turn_id=self.turn_id,
                fence=self.commit_fence,
            )
            audio_uri = recovered.audio_uri
            recording_mime_type = recovered.content_type
            source_byte_count = recovered.source_pcm_byte_count
            media_evidence = {
                "mode": "sealed_segments",
                "complete": recovered.complete,
                "stream_id": recovered.stream_id,
                "capture_revision": recovered.capture_revision,
                "sealed_segment_count": recovered.sealed_segment_count,
                "last_sealed_frame_sequence": recovered.last_sealed_frame_sequence,
                "ownership_epoch": recovered.ownership_epoch,
            }
        else:
            recording = self.recording.finish()
            self.recording = None
            audio_uri = recording.audio_uri
            recording_mime_type = recording.mime_type
            source_byte_count = int(getattr(recording, "byte_count", 0))
            media_evidence = None
        bytes_per_second = self.sample_rate_hz * self.channels * 2
        duration_seconds = max(1, int(source_byte_count / max(1, bytes_per_second)))
        try:
            self._assert_commit_allowed()
            result = await self.interviews.submit_audio_answer(
                self.interview_id,
                {
                    "turn_id": self.turn_id,
                    "audio_uri": audio_uri,
                    "content_type": recording_mime_type,
                    "language": self.language,
                    "duration_seconds": duration_seconds,
                    "development_transcript": self.development_transcript,
                    "development_confidence": self.development_confidence,
                    "media_evidence": media_evidence,
                },
                self.organization_id,
                evidence_fence=self.commit_fence,
            )
        except Exception as exc:
            return [
                {
                    "type": "stream.error",
                    "error_code": "stream_disconnected_batch_repair_failed",
                    "message": "Streaming disconnected; persisted audio could not be batch-transcribed.",
                    "audio_uri": audio_uri,
                    "details": {"error_type": type(exc).__name__},
                }
            ]
        self.last_result = result
        if not result.get("accepted", True):
            return [
                {
                    "type": "stream.error",
                    "error_code": "stream_disconnected_batch_repaired",
                    "message": "Streaming disconnected; persisted audio was repaired by batch STT.",
                },
                self._non_answer_event(result),
            ]
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

    async def recover_persisted(
        self,
        *,
        turn_id: str,
        language: str = "zh-CN",
        development_transcript: Optional[str] = None,
        development_confidence: float = 0.9,
    ) -> List[Dict[str, Any]]:
        """Repair a previous owner's complete sealed checkpoint with batch STT.

        This entry point deliberately does not open a streaming Provider or a
        process-local recording. It is safe for a successor ownership epoch.
        """

        if self.stream or self.recording or self.evidence_media_writer:
            raise ApiError(
                "STT_STREAM_ALREADY_OPEN",
                "Cannot recover persisted Evidence while a stream is open.",
                status_code=409,
            )
        if self.commit_fence is None:
            raise ApiError(
                "EVIDENCE_OWNER_FENCE_REQUIRED",
                "Persisted Evidence repair requires a current ownership fence.",
                status_code=409,
            )
        self._assert_commit_allowed()
        self.turn_id = turn_id
        self.language = language
        self.development_transcript = development_transcript
        self.development_confidence = development_confidence
        assert self.evidence_media is not None
        recovered = self.evidence_media.recover(
            interview_id=self.interview_id,
            turn_id=turn_id,
            fence=self.commit_fence,
        )
        if not recovered.complete:
            raise ApiError(
                "EVIDENCE_MEDIA_INCOMPLETE",
                "The sealed Evidence prefix is incomplete; the candidate must repeat the answer.",
                status_code=409,
            )
        self.content_type = recovered.content_type
        self.sample_rate_hz = recovered.sample_rate_hz
        self.channels = recovered.channels
        self.last_media_checkpoint = {
            "stream_id": recovered.stream_id,
            "capture_revision": recovered.capture_revision,
            "interview_id": recovered.interview_id,
            "turn_id": recovered.turn_id,
            "sealed_segment_count": recovered.sealed_segment_count,
            "last_sealed_frame_sequence": recovered.last_sealed_frame_sequence,
            "sealed_byte_count": recovered.source_pcm_byte_count,
            "ownership_epoch": recovered.ownership_epoch,
            "recoverability": "complete" if recovered.complete else "sealed_prefix",
        }
        bytes_per_second = self.sample_rate_hz * self.channels * 2
        duration_seconds = max(
            1,
            int(recovered.source_pcm_byte_count / max(1, bytes_per_second)),
        )
        result = await self.interviews.submit_audio_answer(
            self.interview_id,
            {
                "turn_id": turn_id,
                "audio_uri": recovered.audio_uri,
                "content_type": recovered.content_type,
                "language": language,
                "duration_seconds": duration_seconds,
                "development_transcript": development_transcript,
                "development_confidence": development_confidence,
                "media_evidence": {
                    "mode": "sealed_segments",
                    "complete": recovered.complete,
                    "stream_id": recovered.stream_id,
                    "capture_revision": recovered.capture_revision,
                    "sealed_segment_count": recovered.sealed_segment_count,
                    "last_sealed_frame_sequence": recovered.last_sealed_frame_sequence,
                    "ownership_epoch": recovered.ownership_epoch,
                },
            },
            self.organization_id,
            evidence_fence=self.commit_fence,
        )
        self.last_result = result
        return self._batch_repair_events(result)

    async def close(self, *, repair_disconnect: bool = False) -> List[Dict[str, Any]]:
        if repair_disconnect:
            return await self.recover_disconnect()
        self._capture_generation += 1
        stream, self.stream = self.stream, None
        dialogue, self.dialogue = self.dialogue, None
        recording, self.recording = self.recording, None
        writer, self.evidence_media_writer = self.evidence_media_writer, None
        try:
            if stream is not None:
                await stream.abort()
        finally:
            try:
                if dialogue is not None:
                    try:
                        await dialogue.abort()
                    except Exception:
                        pass
            finally:
                try:
                    if recording is not None:
                        recording.abort()
                finally:
                    if writer is not None:
                        writer.abort()
        return []

    @staticmethod
    def _non_answer_event(result: Dict[str, Any]) -> Dict[str, Any]:
        understanding = result.get("understanding") or {}
        return {
            "type": "utterance.not_accepted",
            "intent": understanding.get("intent"),
            "suggested_action": result.get("conversation_action"),
            "confidence": understanding.get("confidence"),
            "next_turn_id": result.get("next_turn_id"),
            "problem": result.get("understanding_problem")
            or understanding.get("problem"),
        }

    async def _abort_dialogue(self) -> None:
        dialogue = self.dialogue
        self.dialogue = None
        if dialogue:
            try:
                await dialogue.abort()
            except Exception:
                # Cleanup must not replace the authoritative STT outcome.
                return

    def _assert_commit_allowed(self) -> None:
        if self.commit_guard is not None:
            self.commit_guard()

    def _assert_capture_active(self, generation: int) -> None:
        self._assert_commit_allowed()
        if generation != self._capture_generation:
            raise ApiError("INTERVIEW_TURN_NOT_ACTIVE", "Capture was superseded.", status_code=409)
        session = self.interviews.get_interview(self.interview_id, self.organization_id)
        if session.get("status") != "in_progress":
            raise ApiError("INTERVIEW_NOT_IN_PROGRESS", "Interview is not in progress.", status_code=409)
        state = session.get("agent_runtime") or {}
        turn = next((item for item in session.get("turns", []) if item.get("id") == self.turn_id), None)
        if (not self.turn_id or session.get("current_turn_id") != self.turn_id
                or turn is None or turn.get("status") in {"completed", "skipped", "evaluating"}
                or any(item.get("turn_id") == self.turn_id for item in session.get("answers", []))
                or state.get("floor") == "human" or (state.get("takeover") or {}).get("status") == "active"):
            raise ApiError("INTERVIEW_TURN_NOT_ACTIVE", "Capture no longer belongs to an active candidate turn.",
                           status_code=409)

    def _media_evidence_payload(self, *, complete: bool) -> Optional[Dict[str, Any]]:
        checkpoint = self.last_media_checkpoint
        if checkpoint is None:
            return None
        return {
            "mode": "sealed_segments",
            "complete": complete,
            "stream_id": checkpoint["stream_id"],
            "capture_revision": checkpoint["capture_revision"],
            "sealed_segment_count": checkpoint["sealed_segment_count"],
            "last_sealed_frame_sequence": checkpoint[
                "last_sealed_frame_sequence"
            ],
            "ownership_epoch": checkpoint["ownership_epoch"],
        }

    def _batch_repair_events(self, result: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not result.get("accepted", True):
            return [
                {
                    "type": "stream.error",
                    "error_code": "stream_disconnected_batch_repaired",
                    "message": "Streaming disconnected; sealed audio was repaired by batch STT.",
                },
                self._non_answer_event(result),
            ]
        response = [
            {
                "type": "stream.error",
                "error_code": "stream_disconnected_batch_repaired",
                "message": "Streaming disconnected; sealed audio was repaired by batch STT.",
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
                {
                    "type": "followup.selected",
                    "payload": self._candidate_followup(result["followup"]),
                }
            )
        return response

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

    def _materialize_dialogue_expression(
        self, events: List[Any]
    ) -> Dict[str, Any]:
        audio_events = [item for item in events if item.type == "output.audio.delta"]
        if not audio_events or not any(item.type == "output.audio.done" for item in events):
            raise ApiError(
                "S2S_OUTPUT_AUDIO_INCOMPLETE",
                "Realtime speech did not produce a complete approved audio response.",
                status_code=502,
            )
        content_types = {str(item.audio_content_type).split(";", 1)[0].lower() for item in audio_events}
        sample_rates = {int(item.sample_rate_hz) for item in audio_events}
        if content_types != {"audio/pcm"} or len(sample_rates) != 1:
            raise ApiError(
                "S2S_OUTPUT_AUDIO_FORMAT_INVALID",
                "Realtime speech output must be one PCM16 stream.",
                status_code=502,
            )
        chunks: List[bytes] = []
        try:
            for item in audio_events:
                chunks.append(base64.b64decode(item.audio_base64, validate=True))
        except (ValueError, binascii.Error) as exc:
            raise ApiError(
                "S2S_OUTPUT_AUDIO_INVALID",
                "Realtime speech output audio is invalid.",
                status_code=502,
            ) from exc
        stored = self.expression_audio.store_pcm(
            organization_id=self.organization_id,
            interview_id=self.interview_id,
            turn_id=self.turn_id,
            pcm_s16le=b"".join(chunks),
            sample_rate_hz=next(iter(sample_rates)),
            channels=1,
        )
        return {
            "audio_uri": stored["audio_uri"],
            "duration_ms": stored["duration_ms"],
            "visemes": [],
            "delivery": "s2s",
        }
