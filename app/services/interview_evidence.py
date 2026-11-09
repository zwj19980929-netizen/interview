"""Connection-independent authoritative audio Evidence chain.

The chain owns the only formal streaming STT instance and its private audio
recording. It deliberately has no WebSocket, LiveKit or conversation policy
knowledge, which lets a control channel reconnect without finalizing or
duplicating the candidate's in-flight answer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional

from app.core.errors import ApiError
from app.core.answer_processing_budget import answer_correction_binding
from app.domain.evidence_coordination import EvidenceCommitFence
from app.domain.speech_quality import transcript_confidence_segments
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.services.streaming_stt import StreamingInterviewSTT
from app.services.warmup_calibration import WarmupCalibrationStream

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvidenceOpenResult:
    kind: str
    turn_id: Optional[str]
    events: List[Dict[str, Any]]


@dataclass(frozen=True)
class EvidenceAudioResult:
    kind: str
    turn_id: Optional[str]
    events: List[Dict[str, Any]]
    first_server_audio: bool


@dataclass(frozen=True)
class EvidenceFinishResult:
    kind: str
    turn_id: Optional[str]
    events: List[Dict[str, Any]]
    final: Optional[Dict[str, Any]]
    interview_result: Optional[Dict[str, Any]]


class InterviewEvidenceChain:
    """One interview's warm-up/formal evidence stream, independent of sockets."""

    def __init__(
        self,
        store: Any,
        interview_id: str,
        *,
        organization_id: str = "org_default",
        persistence: Optional[Persistence] = None,
        commit_fence: Optional[EvidenceCommitFence] = None,
        commit_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        self.store = store
        self.interview_id = interview_id
        self.organization_id = organization_id
        self.persistence = persistence or persistence_for(store)
        self.commit_fence = commit_fence
        self.commit_guard = commit_guard
        self._stt: Optional[StreamingInterviewSTT] = None
        self._warmup: Optional[WarmupCalibrationStream] = None
        self._server_audio_seen = False
        self._finishing = False
        self._input_revoked = False
        self._lock = asyncio.Lock()
        self._pending_reception_exchange = None

    @property
    def is_open(self) -> bool:
        return not self._input_revoked and (self._stt is not None or self._warmup is not None)

    def revoke_audio_input(self) -> None:
        """Close the local input gate before any asynchronous provider cleanup."""
        self._input_revoked = True

    @property
    def kind(self) -> Optional[str]:
        if self._warmup is not None:
            return "warmup"
        if self._stt is not None:
            return "formal"
        return None

    @property
    def turn_id(self) -> Optional[str]:
        return self._stt.turn_id if self._stt is not None else None

    async def open(
        self,
        payload: Dict[str, Any],
        *,
        turn_id: Optional[str],
        calibration_status: str,
    ) -> EvidenceOpenResult:
        async with self._lock:
            if self._finishing:
                raise ApiError(
                    "AGENT_EVIDENCE_FINISHING",
                    "The previous Evidence stream is still finalizing.",
                    status_code=409,
                )
            if self._stt is not None or self._warmup is not None:
                raise ApiError(
                    "AGENT_EVIDENCE_ALREADY_OPEN",
                    "Evidence stream is already open.",
                    status_code=409,
                )
            self._server_audio_seen = False
            self._input_revoked = False
            if calibration_status != "completed":
                if calibration_status not in {"listening", "retrying"}:
                    raise ApiError(
                        "WARMUP_NOT_LISTENING",
                        "Finish the opening before starting microphone calibration.",
                        status_code=409,
                    )
                stream = WarmupCalibrationStream(
                    self.store,
                    self.interview_id,
                    organization_id=self.organization_id,
                    persistence=self.persistence,
                )
                events = await stream.open(payload)
                self._warmup = stream
                return EvidenceOpenResult(
                    kind="warmup", turn_id=None, events=events
                )
            stream = StreamingInterviewSTT(
                self.store,
                self.interview_id,
                organization_id=self.organization_id,
                persistence=self.persistence,
                commit_fence=self.commit_fence,
                commit_guard=self.commit_guard,
            )
            events = await stream.open({**payload, "turn_id": turn_id,
                                        "_continuous_capture": self.commit_fence is not None})
            self._stt = stream
            return EvidenceOpenResult(
                kind="formal", turn_id=stream.turn_id, events=events
            )

    async def send_audio(self, pcm_s16le: bytes) -> Optional[EvidenceAudioResult]:
        # LiveKit 会在 ASR WebSocket 尚处于握手阶段时持续送入静音帧。
        # 此时证据门仍是关闭的，必须无锁快速丢弃；如果先等待 _lock，
        # 网络握手会占住锁并让两秒媒体缓冲被无意义的静音填满。
        if not self.is_open:
            return None
        async with self._lock:
            if not self.is_open:
                # LiveKit is continuously subscribed while the Evidence gate is
                # closed. Agent speech and room ambience never enter STT.
                return None
            if not pcm_s16le or len(pcm_s16le) > 64 * 1024:
                raise ApiError(
                    "AGENT_AUDIO_FRAME_INVALID",
                    "Authoritative audio frames must contain 1 to 65536 bytes.",
                    status_code=422,
                )
            first = not self._server_audio_seen
            self._server_audio_seen = True
            if self._warmup is not None:
                events = await self._warmup.send_audio(pcm_s16le)
                return EvidenceAudioResult(
                    kind="warmup",
                    turn_id=None,
                    events=events,
                    first_server_audio=first,
                )
            assert self._stt is not None
            events = await self._stt.send_audio(pcm_s16le)
            return EvidenceAudioResult(
                kind="formal",
                turn_id=self._stt.turn_id,
                events=events,
                first_server_audio=first,
            )

    @property
    def supports_stable_preview(self) -> bool:
        return self._stt is not None and self._stt.supports_stable_preview

    async def transcript_preview(self) -> Any:
        stream = self._stt
        if stream is None or self._input_revoked:
            raise ApiError("AGENT_EVIDENCE_NOT_OPEN", "Evidence is not open.", status_code=409)
        preview = await stream.transcript_preview()
        if stream is not self._stt or self._input_revoked:
            raise ApiError("TURN_DECISION_STALE", "Capture changed during preview.", status_code=409)
        return preview

    async def transcript_snapshot(self, *, resume: bool = True) -> Any:
        stream = self._stt
        if stream is None:
            raise ApiError("AGENT_EVIDENCE_NOT_OPEN", "Evidence is not open.", status_code=409)
        return await stream.transcript_snapshot(resume=resume)

    async def resume_capture(self) -> None:
        if self._stt is not None:
            await self._stt.resume_capture()

    def assert_snapshot_current(self, prepared_decision: Any = None) -> None:
        if self._stt is None or self._input_revoked:
            raise ApiError("TURN_DECISION_STALE", "Capture is no longer active.", status_code=409)
        self._stt._assert_commit_allowed()
        self._stt.assert_snapshot_current(prepared_decision)

    @property
    def recovery_required(self) -> bool:
        return self._stt is not None and self._stt.recovery_required

    @property
    def recovery_origin(self) -> Any:
        return self._stt.recovery_origin if self._stt is not None else None

    @property
    def recovery_error(self) -> Any:
        return self._stt.recovery_error if self._stt is not None else None

    async def recover_capture(self) -> None:
        stream = self._stt
        if stream is None:
            raise ApiError("AGENT_EVIDENCE_NOT_OPEN", "Evidence is not open.", status_code=409)
        await stream.recover_capture()
        if stream is not self._stt:
            raise ApiError("TURN_DECISION_STALE", "Capture changed during recovery.", status_code=409)

    def checkpoint_incomplete(self) -> Any:
        return self._stt.checkpoint_incomplete() if self._stt is not None else None

    def stage_reception_exchange(self, final: Any, request_text: str, kind: str) -> Any:
        """Stage authoritative words; a preview or an approval is not a dialogue receipt."""
        self._pending_reception_exchange = None
        stream = self._stt
        if stream is None or self._input_revoked:
            raise ApiError("TURN_DECISION_STALE", "Capture changed.", status_code=409)
        if (getattr(final, "type", None) != "transcript.final" or not getattr(final, "is_final", False)
                or not request_text.strip() or not final.text.strip().endswith(request_text.strip())):
            raise ValueError("Reception history requires fresh complete server speech")
        stream._assert_commit_allowed()
        try:
            with self.persistence.transaction(self.organization_id, read_only=True) as tx:
                session = tx.interview_sessions.get(self.interview_id)
        except Exception as exc:
            # Optional memory must not replace the expression's own durable
            # authorization checks, nor prevent an otherwise valid response.
            _LOG.warning("Reception memory unavailable: stage=read error_type=%s", type(exc).__name__)
            return None
        if not session or session.get("current_turn_id") != self.turn_id or session.get("status") != "in_progress":
            raise ApiError("TURN_DECISION_STALE", "Interview changed.", status_code=409)
        sequence = max((event.get("session_sequence", 0) for event in session.get("agent_events", [])
                        if type(event.get("session_sequence")) is int), default=0)
        token = {"stream": stream, "turn_id": self.turn_id, "after_sequence": sequence,
                 "request_text": request_text.strip(), "kind": kind,
                 "source_stream_id": final.stream_id}
        from app.services.conversation_input_context import reception_source_boundary
        token["source_boundary"] = reception_source_boundary(
            final.text.strip(), request_text.strip(),
            segments=transcript_confidence_segments(final.text.strip(), getattr(final, "segments", [])),
            source_capture=getattr(stream, "last_media_checkpoint", None),
        )
        self._pending_reception_exchange = token
        return token

    def discard_reception_exchange(self, token: Any) -> None:
        if token is self._pending_reception_exchange:
            self._pending_reception_exchange = None

    def record_reception_exchange(self, *, performance_id: str, guard: Callable[[], None]) -> bool:
        """Pair a live request with an exact selected response, never with a later output.

        This small memory is not an utterance, an answer, or proof of audible
        playback. Stale input can safely skip it; it cannot fail the interview.
        """
        staged = self._pending_reception_exchange
        if (staged is None or self._input_revoked or self._stt is not staged["stream"]
                or self.turn_id != staged["turn_id"]):
            self.discard_reception_exchange(staged)
            return False
        try:
            guard()
            self._stt._assert_commit_allowed()
            from app.services.conversation_input_context import capture_source_fingerprint
            proof = staged.get("source_boundary")
            if proof and proof["capture_fingerprint"] != capture_source_fingerprint(
                    getattr(self._stt, "last_media_checkpoint", None)):
                return False
            with self.persistence.transaction(self.organization_id) as tx:
                if self.commit_fence is not None:
                    from app.services.evidence_coordination import assert_current_evidence_fence
                    assert_current_evidence_fence(tx, self.commit_fence)
                session = tx.interview_sessions.get(self.interview_id)
                if not session:
                    return False
                state = session.get("agent_runtime") or {}
                if (session.get("status") != "in_progress" or session.get("current_turn_id") != self.turn_id
                        or state.get("floor") == "human" or (state.get("takeover") or {}).get("status") == "active"
                        or state.get("active_performance_id") != performance_id):
                    return False
                selected = next((event for event in reversed(session.get("agent_events", []))
                                 if event.get("type") == "conversation.act.selected"), None)
                if (selected is None or selected.get("turn_id") != self.turn_id
                        or type(selected.get("session_sequence")) is not int
                        or selected["session_sequence"] <= staged["after_sequence"]
                        or not selected.get("event_id")
                        or (state.get("active_expression_act_event_id") is not None
                            and state["active_expression_act_event_id"] != selected["event_id"])):
                    return False
                turn = next((item for item in session.get("turns", []) if item.get("id") == self.turn_id), None)
                payload = selected.get("payload") or {}
                act = next((item for item in (turn or {}).get("conversation_acts", [])
                            if item.get("act_id") == payload.get("act_id") and item.get("turn_id") == self.turn_id), None)
                if (not turn or not act or payload.get("approved") is not True
                        or not act.get("text") or payload.get("text") != act["text"]):
                    return False
                exchanges = turn.setdefault("reception_exchanges", [])
                if any(item.get("selected_event_id") == selected["event_id"] for item in exchanges):
                    return False
                request = staged["request_text"]
                digest = hashlib.sha256(request.encode("utf-8")).hexdigest()
                exchanges.append({"exchange_id": "reception_" + hashlib.sha256(
                    (selected["event_id"] + digest).encode()).hexdigest()[:24],
                    "request_text": request[-1600:], "request_sha256": digest,
                    "act_id": act["act_id"], "selected_event_id": selected["event_id"],
                    "session_sequence": selected["session_sequence"], "created_at": selected.get("occurred_at", ""),
                    "source_stream_id": staged["source_stream_id"], "kind": staged["kind"],
                    # Explicitly retain missing proof. Only historical receipts
                    # without this field may use the legacy unique-text cursor.
                    "source_boundary": staged.get("source_boundary")})
                del exchanges[:-6]
                tx.interview_sessions.update(session, expected_version=session["version"])
            return True
        except ApiError:
            return False
        except Exception as exc:
            # Selection/playback already happened. A failed optional receipt
            # cannot reopen echo intake or make the endpoint repeat its words.
            _LOG.warning("Reception memory unavailable: stage=write error_type=%s", type(exc).__name__)
            return False
        finally:
            self.discard_reception_exchange(staged)

    async def classify_reception(self, text: str, *, phase: str, preceding_text: str = "") -> Any:
        stream = self._stt
        if stream is None or self._input_revoked:
            raise ApiError("AGENT_EVIDENCE_NOT_OPEN", "Evidence is not open.", status_code=409)
        stream._assert_commit_allowed()
        session = stream.interviews.get_interview(self.interview_id, self.organization_id)
        turn = next(item for item in session["turns"] if item["id"] == self.turn_id)
        # Only candidate-visible conversation and actual scheduling facts enter
        # free speech. Never pass whole turns, rubrics or assessment answers.
        from app.services.dialogue_history import recent_turn_dialogue
        history = []
        turns = session.get("turns", [])
        current_index = next(index for index, item in enumerate(turns) if item["id"] == self.turn_id)
        for prior in turns[max(0, current_index - 1):current_index + 1]:
            history.extend({"role": item["speaker"], **{key: value for key, value in item.items() if key != "speaker"}}
                           for item in recent_turn_dialogue(session, prior, max_acts=6))
        plan = session.get("plan_snapshot") or {}
        facts = {"duration_minutes": plan.get("duration_minutes"),
                 "completed_topics": len({item.get("root_turn_id") or item.get("id") for item in session.get("turns", [])
                     if any(answer.get("turn_id") == item.get("id") for answer in session.get("answers", []))})}
        skill = stream.interviews.supervisor.load_skill(session)
        from app.services.dialogue_focus import question_for_repeat, clarification_for_repeat
        root = next((item for item in session.get("turns", []) if item["id"] == turn.get("root_turn_id")), turn)
        result = await stream.interviews.conversation.classify_reception(text, self.organization_id,
            phase=phase, preceding_text=preceding_text,
            history=history[-16:], process_facts=facts, skill_instructions=skill.get("instructions", ""),
            question=question_for_repeat(turn), current_question=question_for_repeat(turn),
            root_question=question_for_repeat(root), last_clarification=clarification_for_repeat(turn),
            current_turn_id=turn["id"], root_turn_id=root["id"], parent_turn_id=turn.get("parent_turn_id"))
        if stream is not self._stt or self._input_revoked:
            raise ApiError("TURN_DECISION_STALE", "Capture changed.", status_code=409)
        stream._assert_commit_allowed()
        return result

    def _supplement_dialogue_context(self, stream: Any) -> Any:
        """Freeze only public dialogue for the prompt, full context for fences."""
        from app.services.dialogue_history import recent_turn_dialogue
        from app.services.dialogue_focus import question_for_repeat, clarification_for_repeat
        from app.services.supplement_context import supplement_context

        session = stream.interviews.get_interview(self.interview_id, self.organization_id)
        turn = stream.interviews.lifecycle.require_active_turn(session, stream.turn_id, allowed_statuses=("asking",))
        supplement = supplement_context(session, turn)
        turns = session.get("turns", [])
        index = next(index for index, item in enumerate(turns) if item["id"] == turn["id"])
        history = []
        for previous in turns[max(0, index - 1):index + 1]:
            for item in recent_turn_dialogue(session, previous, max_acts=6):
                if item["speaker"] == "interviewer" and item.get("delivery_status") != "delivered":
                    continue
                history.append({"role": item["speaker"], **{key: value for key, value in item.items() if key != "speaker"}})
        delivered = [item for item in history if item["role"] == "interviewer" and item.get("turn_id") == turn["id"]]
        clarification = clarification_for_repeat(turn)
        if clarification not in {item["text"] for item in delivered}:
            clarification = ""
        root = next((item for item in turns if item["id"] == turn.get("root_turn_id")), turn)
        context = {"stage": supplement["stage"], "current_question": question_for_repeat(turn),
            "root_question": question_for_repeat(root), "last_clarification": clarification,
            "last_delivered_utterance": delivered[-1]["text"] if delivered else "", "history": history[-8:],
            "reply_target": ("supplement_question" if delivered[-1].get("act_id") == supplement.get("question_act_id")
                             else "other_delivered_speech") if delivered else "unknown",
            "process_facts": {"duration_minutes": (session.get("plan_snapshot") or {}).get("duration_minutes")}}
        return context, supplement.get("question", ""), stream.interviews._prepared_context_fingerprint(session, turn)

    async def classify_supplement_reply(self, reply: str, *, prior_confirmation: bool = False,
                                        preceding_text: str = "", question: str = "",
                                        final: Any = None, boundary_final: Any = None) -> Any:
        stream = self._stt
        if stream is None or self._input_revoked:
            raise ApiError("AGENT_EVIDENCE_NOT_OPEN", "Evidence is not open.", status_code=409)
        stream._assert_commit_allowed()
        turn_id, generation, recognizer = stream.turn_id, stream._capture_generation, stream.stream

        def evidence_identity() -> Any:
            # Observe existing snapshots only. Do not cut, resume, correct or
            # otherwise operate the recognizer to classify conversation.
            values = (final, boundary_final, getattr(recognizer, "_final", None))
            return tuple(hashlib.sha256(json.dumps(value.model_dump(mode="json"), sort_keys=True,
                ensure_ascii=False).encode("utf-8")).hexdigest() if value is not None else None for value in values)

        if final is not None or boundary_final is not None:
            if final is None or boundary_final is None:
                raise ApiError("TURN_DECISION_STALE", "A complete reply boundary is required.", status_code=409)
            stream.interviews._require_streaming_final(final)
            stream.interviews._require_streaming_final(boundary_final)
            from app.services.spoken_supplement import _speech_since
            if _speech_since(final, boundary_final, boundary_final.text.strip()) != reply:
                raise ApiError("TURN_DECISION_STALE", "Reply no longer matches its source.", status_code=409)
        frozen_evidence = evidence_identity()
        if final is not None and frozen_evidence[2] is not None and frozen_evidence[0] != frozen_evidence[2]:
            raise ApiError("TURN_DECISION_STALE", "Reply source changed before classification.", status_code=409)
        context, delivered_question, context_fingerprint = self._supplement_dialogue_context(stream)
        if final is not None and context["stage"] != "awaiting_reply":
            raise ApiError("TURN_DECISION_STALE", "Supplement question has not been delivered.", status_code=409)
        result = await stream.interviews.conversation.classify_supplement_reply(
            reply, self.organization_id, prior_confirmation=prior_confirmation,
            preceding_text=preceding_text, question=delivered_question or question,
            conversation_context=context,
        )
        if (stream is not self._stt or self._input_revoked or stream.turn_id != turn_id
                or stream._capture_generation != generation or stream.stream is not recognizer
                or evidence_identity() != frozen_evidence):
            raise ApiError("TURN_DECISION_STALE", "Capture changed.", status_code=409)
        stream._assert_commit_allowed()
        if self._supplement_dialogue_context(stream)[2] != context_fingerprint:
            raise ApiError("TURN_DECISION_STALE", "Conversation changed.", status_code=409)
        return result

    async def classify_exit_reply(self, reply: str) -> Any:
        stream = self._stt
        if stream is None or self._input_revoked:
            raise ApiError("AGENT_EVIDENCE_NOT_OPEN", "Evidence is not open.", status_code=409)
        stream._assert_commit_allowed()
        result = await stream.interviews.conversation.classify_exit_reply(reply, self.organization_id)
        if stream is not self._stt or self._input_revoked:
            raise ApiError("TURN_DECISION_STALE", "Capture changed.", status_code=409)
        stream._assert_commit_allowed()
        return result

    def prepare_exit_decision(self, final: Any, *, exit_confirmation: Any) -> Any:
        """Bind a delivered control confirmation to this live evidence owner."""
        stream = self._stt
        if stream is None or self._input_revoked:
            raise ApiError("TURN_DECISION_STALE", "Capture is no longer active.", status_code=409)
        stream._assert_commit_allowed()
        prepared = stream.interviews.prepare_streaming_exit(
            self.interview_id, stream.turn_id, final, self.organization_id,
            exit_confirmation=exit_confirmation,
        )
        stream._assert_commit_allowed()
        return replace(prepared, capture_binding=stream)

    async def prepare_decision(self, final: Any, *, completion_confirmed: bool = False,
                               semantic_first: bool = False, exit_confirmation: Any = None,
                               content_clarification: Any = None) -> Any:
        if self._stt is None:
            raise ApiError("AGENT_EVIDENCE_NOT_OPEN", "Evidence is not open.", status_code=409)
        # Preview itself is cheap and read-only; seal a durable, incomplete
        # prefix only when a new speculative decision is actually requested.
        stream = self._stt
        stream._assert_commit_allowed()
        checkpoint = stream.checkpoint_for_preparation() or {}
        if not checkpoint.get("last_sealed_frame_sequence"):
            raise ApiError("EVIDENCE_MEDIA_INCOMPLETE", "Durable prefix required.", status_code=409)
        snapshot_ref = "evidence-checkpoint://%s/%s/%s" % (
            checkpoint["stream_id"], checkpoint["capture_revision"],
            checkpoint["last_sealed_frame_sequence"],
        )
        fence = self.commit_fence.model_dump_json() if self.commit_fence is not None else None

        def correction_guard() -> None:
            if stream is not self._stt or self._input_revoked:
                raise ApiError("TURN_DECISION_STALE", "Capture changed.", status_code=409)
            if (self.commit_fence.model_dump_json() if self.commit_fence is not None else None) != fence:
                raise ApiError("TURN_DECISION_STALE", "Capture ownership changed.", status_code=409)
            stream._assert_commit_allowed()

        with answer_correction_binding((stream, fence), guard=correction_guard):
            prepared = await stream.interviews.prepare_streaming_decision(
                self.interview_id, stream.turn_id, final, self.organization_id,
                snapshot_ref=snapshot_ref,
                completion_confirmed=completion_confirmed,
                semantic_first=semantic_first,
                exit_confirmation=exit_confirmation,
                content_clarification=content_clarification,
            )
        if stream is not self._stt or self._input_revoked:
            raise ApiError("TURN_DECISION_STALE", "Capture changed.", status_code=409)
        stream._assert_commit_allowed()
        return replace(prepared, capture_binding=stream)

    def assert_preparation_current(self, prepared: Any, transcript: Any, **kwargs: Any) -> None:
        """Retained reasoning stays bound to the live authoritative capture.

        This does not seal audio, certify completeness or authorize a commit.
        Check the external owner guard before the service opens a read scope.
        """
        stream = self._stt
        if (stream is None or self._input_revoked
                or getattr(prepared, "capture_binding", None) is not stream
                or prepared.interview_id != self.interview_id
                or prepared.turn_id != stream.turn_id):
            raise ApiError("TURN_DECISION_STALE", "Capture changed since preparation.", status_code=409)
        stream._assert_commit_allowed()
        stream.interviews.assert_streaming_preparation_current(
            prepared, transcript, self.organization_id, **kwargs,
        )

    def confirm_prepared_decision(self, prepared: Any, base_final: Any,
                                  delivered_boundary_final: Any, final: Any, *,
                                  reply_text: str, decision: Dict[str, Any],
                                  question_delivered: bool) -> Any:
        """Reuse unchanged prepared evidence under the live capture's fence."""
        stream = self._stt
        if stream is None or self._input_revoked:
            raise ApiError("TURN_DECISION_STALE", "Capture is no longer active.", status_code=409)
        stream._assert_commit_allowed()
        if getattr(prepared, "capture_binding", None) is not stream:
            return None
        return stream.interviews.confirm_prepared_streaming_decision(
            prepared, base_final, delivered_boundary_final, final, self.organization_id,
            reply_text=reply_text, decision=decision, question_delivered=question_delivered,
        )

    async def finish(self, payload: Dict[str, Any], *, prepared_decision: Any = None,
                     commit_guard: Any = None) -> EvidenceFinishResult:
        async with self._lock:
            if commit_guard is not None:
                commit_guard()
            if self._warmup is not None:
                stream = self._warmup
                # 端点一旦确认就先关闭证据门；Provider final 等待期间到达的
                # LiveKit 静音不应继续排队争抢同一把锁。
                self._warmup = None
                self._finishing = True
                kind = "warmup"
                turn_id = None
            elif self._stt is not None:
                stream = self._stt
                if prepared_decision is not None:
                    binding = getattr(prepared_decision, "capture_binding", None)
                    if binding is not None and binding is not stream:
                        raise ApiError("TURN_DECISION_STALE", "Prepared capture changed.", status_code=409)
                    stream.assert_snapshot_current(prepared_decision)
                turn_id = stream.turn_id
                self._stt = None
                self._finishing = True
                kind = "formal"
            else:
                raise ApiError(
                    "AGENT_EVIDENCE_NOT_OPEN",
                    "Evidence stream is not open.",
                    status_code=409,
                )

        try:
            if kind == "warmup":
                events = await stream.finish()
                final = next(
                    (
                        item
                        for item in events
                        if item.get("type") == "transcript.final"
                    ),
                    None,
                )
                if final is None:
                    raise ApiError(
                        "WARMUP_FINAL_INVALID",
                        "Warm-up calibration did not produce one server final.",
                        status_code=502,
                    )
                return EvidenceFinishResult(
                    kind="warmup",
                    turn_id=None,
                    events=events,
                    final=final,
                    interview_result=None,
                )
            try:
                events = await stream.finish(payload, prepared_decision=prepared_decision)
            except BaseException:
                try:
                    await stream.close(repair_disconnect=True)
                except BaseException:
                    pass
                raise
            return EvidenceFinishResult(
                kind="formal",
                turn_id=turn_id,
                events=events,
                final=next(
                    (
                        item
                        for item in events
                        if item.get("type") == "transcript.final"
                    ),
                    None,
                ),
                interview_result=stream.last_result,
            )
        finally:
            async with self._lock:
                self._finishing = False

    async def abort_warmup(self) -> None:
        async with self._lock:
            stream = self._warmup
            self._warmup = None
            if stream is not None:
                await stream.abort()

    async def abort(self) -> None:
        """Drop process-local streams without repairing or committing an answer."""
        self.revoke_audio_input()
        async with self._lock:
            warmup = self._warmup
            formal = self._stt
            self._warmup = None
            self._stt = None
            if warmup is not None:
                await warmup.abort()
            if formal is not None:
                await formal.close(repair_disconnect=False)

    async def close_for_disconnect(self) -> Optional[EvidenceFinishResult]:
        """Repair persisted formal audio after the reconnect grace expires."""

        async with self._lock:
            if self._warmup is not None:
                stream = self._warmup
                self._warmup = None
                await stream.abort()
                return None
            if self._stt is None:
                return None
            stream = self._stt
            self._stt = None
            turn_id = stream.turn_id
            events = await stream.close(repair_disconnect=True)
            return EvidenceFinishResult(
                kind="formal",
                turn_id=turn_id,
                events=events,
                final=next(
                    (
                        item
                        for item in events
                        if item.get("type") == "transcript.final"
                    ),
                    None,
                ),
                interview_result=stream.last_result,
            )

    async def recover_persisted_turn(
        self,
        turn_id: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> EvidenceFinishResult:
        """Batch-repair a prior owner's durably complete media checkpoint."""

        async with self._lock:
            if self.is_open:
                raise ApiError(
                    "AGENT_EVIDENCE_ALREADY_OPEN",
                    "Cannot repair persisted Evidence while a stream is open.",
                    status_code=409,
                )
            stream = StreamingInterviewSTT(
                self.store,
                self.interview_id,
                organization_id=self.organization_id,
                persistence=self.persistence,
                commit_fence=self.commit_fence,
                commit_guard=self.commit_guard,
            )
            values = payload or {}
            events = await stream.recover_persisted(
                turn_id=turn_id,
                language=str(values.get("language") or "zh-CN"),
                development_transcript=values.get("development_transcript"),
                development_confidence=float(
                    values.get("development_confidence", 0.9)
                ),
            )
            return EvidenceFinishResult(
                kind="formal",
                turn_id=turn_id,
                events=events,
                final=None,
                interview_result=stream.last_result,
            )
