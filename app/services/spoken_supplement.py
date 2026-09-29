"""Semantic-first turn taking with a bounded spoken fallback handshake."""

from __future__ import annotations

from app.domain.speech_quality import minimum_reported_confidence
from app.services.conversation_understanding import ConversationUnderstandingService
from app.services.capture_recovery import classify_capture_failure
from typing import Any
import hashlib
import logging

_LOG = logging.getLogger(__name__)


def _mapped_segments(transcript: Any) -> Any:
    """Map every segment's exact words into the complete server text."""
    segments = getattr(transcript, "segments", ())
    if not segments:
        return None
    text, cursor, mapped = transcript.text.strip(), 0, []
    for segment in segments:
        if not segment.text.strip():
            return None
        # Some adapters separate sentences with whitespace while others
        # concatenate them. Map exact segment words into the final; only
        # whitespace may occur between them, never missing recognized words.
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        words = segment.text.strip()
        if not text.startswith(words, cursor):
            return None
        start = cursor
        cursor += len(words)
        mapped.append((segment, start, cursor))
    if text[cursor:].strip():
        return None
    return mapped


def _timed_segments(transcript: Any) -> Any:
    """Return a complete, ordered capture timeline, or no timing authority."""
    mapped = _mapped_segments(transcript)
    if not mapped:
        return None
    previous_end = 0
    for segment, _, _ in mapped:
        if (type(segment.start_ms) is not int or type(segment.end_ms) is not int
                or segment.start_ms < previous_end or segment.end_ms <= segment.start_ms):
            return None
        previous_end = segment.end_ms
    return [segment for segment, _, _ in mapped], [start for _, start, _ in mapped]


def _speech_since(transcript: Any, previous: Any, previous_text: str) -> Any:
    """Separate fresh server speech from earlier words, without guessing edits.

    Exact text prefixes retain support for final providers without useful
    timestamps. If old words changed, only a complete capture-relative timeline
    with an intact old time boundary can identify new speech. A segment spanning
    that boundary, missing history or stream-relative/reset times is ambiguous.
    Current speech is never truncated: an early qualification, quoted command or
    unresolved transcription dispute must remain available to the classifier.
    """
    text = transcript.text.strip()
    if text.startswith(previous_text):
        return text[len(previous_text):].strip()
    prior_timing = _timed_segments(previous) if previous is not None else None
    timing = _timed_segments(transcript)
    if not prior_timing or not timing:
        return None
    prior_segments, _ = prior_timing
    segments, starts = timing
    boundary_ms = prior_segments[-1].end_ms
    past = [segment for segment in segments if segment.end_ms <= boundary_ms]
    fresh = [segment for segment in segments if segment.start_ms >= boundary_ms]
    if (len(past) + len(fresh) != len(segments) or not past
            or past[0].start_ms != prior_segments[0].start_ms
            or past[-1].end_ms != boundary_ms):
        return None
    return text[starts[len(past)]:].strip() if fresh else ""


class SpokenSupplementConfirmation:
    """Keep the answer and its control replies on the same private recording.

    A server final fixes the reply boundary before asking. Only new server
    speech after that boundary may confirm completion. Neither playback,
    elapsed time, nor a preview grants permission to submit an answer.
    """

    def __init__(self, *, speak: Any, silence_seconds: float = 5.0, semantic_first: bool = True,
                 respond_company: Any = None, interview_id: str = "", turn_id: str = "",
                 capture_id: str = "") -> None:
        self.speak = speak
        self.respond_company = respond_company
        self.silence_seconds = silence_seconds
        self.semantic_first = semantic_first
        self.phase = "listening"
        self.boundary = ""
        self.confirmed = False
        self.reply_seen = False
        self._after_speech = "awaiting_reply"
        self._reminded = False
        self._boundary_final = None
        self._playback_started_at = 0.0
        self._missing_reply_since = None
        self._confirmed_final = None
        self._confirmation_origin = None
        self._reply_question = ""
        self._clarification_revision = None
        self._semantic_wait_identity = None
        self._reception_cache = None
        self._reception_seen_text = ""
        self._reception_seen_final = None
        self._reception_review = None
        # Input invalidation revokes a decision, not the fact that these exact
        # server words have already received a response. Acoustic noise and
        # barge-in must not repeatedly reinterpret an old cumulative final.
        self._handled_final = None
        self._handled_trace_revision = None
        self._failure_spoken_identity = None
        self._idle_check_allowed = True
        self._idle_check_sent = False
        self._idle_no_input_revision = None
        # Ending this answer and leaving the entire interview are distinct
        # permissions. An answer-finish button or supplement reply never grants
        # the latter. All exit replies stay in the same authoritative capture.
        self._exit_identity = {"interview_id": interview_id, "turn_id": turn_id, "capture_id": capture_id}
        self._exit_waiting = False
        self._exit_request_final = None
        self._exit_question_act_id = None
        self._exit_question_delivered = False
        self._exit_reminded = False
        self._exit_context = None
        self._exit_context_final = None
        self._exit_evidence_identity = None
        self._exit_delivery_revision = None
        self._content_question_act_id = None
        self._content_question_focus = None
        self._content_request_final = None
        self._content_question_delivered = False
        self._content_delivery_pending = False

    def content_clarification_context(self, final: Any) -> Any:
        if not self._content_question_delivered or self._content_request_final is None:
            return None
        request = self._content_request_final.text.strip()
        text = final.text.strip()
        reply = text[len(request):].strip() if text.startswith(request) else ""
        if not reply or self._content_question_focus not in request:
            return None
        return {"version": "content_clarification.v1", "request_text": request,
                "focus_quote": self._content_question_focus, "reply_text": reply,
                "question_act_id": self._content_question_act_id, "delivered": True}

    def exit_context(self, final: Any) -> Any:
        """Project only a server-classified reply bound to this live exchange."""
        context = self._exit_context
        if not context or self._exit_request_final is None:
            return None
        if context["decision"] == "confirm":
            if (not self.confirmed or not self._exit_question_delivered
                    or self._exit_context_final is None
                    or self._exit_evidence_identity is None
                    or self._exit_evidence_identity(final) != self._exit_evidence_identity(self._exit_context_final)):
                return None
        if context["decision"] == "continue":
            # Retain the exact withdrawal reply and its original hash. Later
            # candidate words remain new input, including a new request to exit.
            if not final.text.strip().startswith(self._exit_context_final.text.strip()):
                return None
        return dict(context)

    async def gate_exit(self, endpoint: Any, prepared: Any, final: Any) -> bool:
        """Return True when the final commit must wait for a separate question."""
        intent = getattr(prepared.understanding, "turn_intent", None)
        context = self.exit_context(final)
        if (intent == "stop_interview" and context and context["decision"] == "confirm"
                and getattr(prepared, "exit_confirmation", None) == context):
            return False
        if context and context["decision"] == "confirm":
            await self._ask_exit(endpoint, final, clarify=True)
            return True
        if self._exit_waiting:
            # A final-only revision or an explicit finish button cannot bypass
            # the pending question. Re-read its new reply before any commit.
            await self._exit_reply(endpoint, final)
            return True
        if intent != "stop_interview":
            return False
        self._exit_reminded = False
        await self._ask_exit(endpoint, final)
        return True

    async def _ask_exit(self, endpoint: Any, final: Any, *, clarify: bool = False) -> None:
        from app.core.prompt.candidate_exit import EXIT_CONFIRMATION_SPEECH
        endpoint.assert_current()
        self._exit_waiting = True
        self._exit_evidence_identity = endpoint._identity
        self._exit_request_final = final.model_copy(deep=True)
        self._exit_question_act_id = None
        self._exit_question_delivered = False
        self._exit_delivery_revision = None
        self._exit_context = self._exit_context_final = None
        self.confirmed = False
        self._confirmed_final = None
        self._confirmation_origin = None
        self.reply_seen = False
        self._reply_question = EXIT_CONFIRMATION_SPEECH
        self._remember_boundary(final)
        endpoint._explicit_revision = None
        endpoint._discard_confirmed_preparation()
        endpoint.preparation.cancel()
        self.phase, self._after_speech = "preparing_speech", "awaiting_exit_reply"
        self._playback_started_at = endpoint.clock()
        try:
            selected = await endpoint._infer(self.speak(
                "exit_clarify" if clarify else "exit_check", endpoint.assert_current), timeout=45)
            endpoint.assert_current()
            act_id = selected.get("act_id") if isinstance(selected, dict) else None
            if isinstance(act_id, str) and act_id:
                self._exit_question_act_id = act_id
                if self.phase == "preparing_speech":
                    self.playback_selected(endpoint)
            elif self.phase in {"preparing_speech", "speaking"}:
                # Failed or merely planned speech is never a delivered question.
                self.phase = "awaiting_exit_reply"
                self._exit_question_delivered = False
        except BaseException:
            self._exit_question_delivered = False
            if self.phase in {"preparing_speech", "speaking"}:
                self.phase = "awaiting_exit_reply"
            raise
        finally:
            endpoint._last_voice = endpoint.clock()

    async def _exit_step(self, endpoint: Any, quiet: float) -> None:
        if self.confirmed:
            endpoint._proposal_revision = endpoint.revision
            await endpoint._propose()
            return
        if not self.reply_seen:
            if quiet >= 15 and not self._exit_reminded:
                self._exit_reminded = True
                endpoint._proposal_revision = endpoint.revision
                final = await endpoint.listening_snapshot()
                endpoint.assert_current()
                if final is not None:
                    await self._ask_exit(endpoint, final, clarify=True)
            return
        if quiet < endpoint.min_silence_seconds:
            return
        endpoint._proposal_revision = endpoint.revision
        final = await endpoint.listening_snapshot()
        endpoint.assert_current()
        await self._exit_reply(endpoint, final)

    async def _exit_reply(self, endpoint: Any, final: Any) -> None:
        endpoint.assert_current()
        endpoint.cover_transcript(final)
        if final is None:
            await endpoint._notify("transcript_unavailable")
            endpoint._retry_at = endpoint.clock() + 2
            return
        if getattr(final, "type", None) != "transcript.final" or not getattr(final, "is_final", False):
            raise ValueError("Exit confirmation requires a complete server final")
        previous = self._exit_request_final
        reply = _speech_since(final, previous, previous.text.strip()) if previous else None
        if reply is None or not final.text.strip().startswith(previous.text.strip()):
            # Interrupted/failed playback and ambiguous transcript edits need
            # a new delivered question; an earlier yes cannot be recycled.
            await self._ask_exit(endpoint, final, clarify=True)
            return
        if reply == "":
            self.reply_seen = False
            return
        if (not self._exit_question_delivered or not self._exit_question_act_id
                or self._exit_delivery_revision is None or endpoint.revision <= self._exit_delivery_revision):
            await self._ask_exit(endpoint, final, clarify=True)
            return
        mapped = _mapped_segments(final)
        reply_start = len(final.text.strip()) - len(reply)
        segments = ([segment for segment, _, end in mapped if end > reply_start]
                    if mapped else final.segments)
        confidence = minimum_reported_confidence(final.confidence, *(segment.confidence for segment in segments))
        if confidence is not None and confidence < .75:
            await self._ask_exit(endpoint, final, clarify=True)
            return
        self.phase = "exit_classifying"
        endpoint._use_preparation_budget(final.text)
        if endpoint.preparation_blocked:
            self.phase = "awaiting_exit_reply"
            endpoint._blocked_revision = endpoint.revision
            await endpoint._understanding_failed(record_failure=False)
            return
        try:
            decision = await endpoint._infer(endpoint.capture.classify_exit_reply(reply), timeout=10)
            endpoint.assert_current()
        except BaseException:
            self.phase = "awaiting_exit_reply"
            raise
        action = decision["intent"]
        if action not in {"confirm", "continue"} or decision["confidence"] < .75:
            await self._ask_exit(endpoint, final, clarify=True)
            return
        from app.core.prompt.candidate_exit import EXIT_CONFIRMATION_SPEECH
        self._exit_context = {
            "version": "candidate_exit_confirmation.v1", **self._exit_identity,
            "question_act_id": self._exit_question_act_id, "question": EXIT_CONFIRMATION_SPEECH,
            "request_text": previous.text.strip(), "reply_text": reply, "decision": action,
            "transcript_sha256": hashlib.sha256(final.text.strip().encode("utf-8")).hexdigest(),
        }
        self._exit_context_final = final.model_copy(deep=True)
        self._remember_boundary(final)
        if action == "continue":
            self._exit_waiting = False
            self.confirmed = False
            self._confirmed_final = None
            self.phase = "listening"
            self._semantic_wait_identity = None
            self._reception_seen_text = final.text.strip()
            self._reception_seen_final = final.model_copy(deep=True)
            self.reply_seen = False
            # A withdrawal can include a substantive completed answer. Review
            # that same final now; do not consume it merely as a control reply.
            if await self._semantic_turn(endpoint, final):
                return
            self._semantic_wait_identity = endpoint._identity(final)
            self._remember_handled(final)
            await self._say(endpoint, "continue", afterwards="listening")
            return
        self.confirmed = True
        self.phase = "exit_confirmed"
        self._confirmed_final = final.model_copy(deep=True)
        await endpoint._propose(final_snapshot=final, recognition_resumed=True)

    def transcript_changed(self) -> None:
        """Only new server words renew an idle check; microphone noise does not."""
        self._idle_check_allowed = True
        self._idle_check_sent = False
        self._idle_no_input_revision = None

    def idle_check_due(self, quiet: float) -> bool:
        return bool(self.phase == "listening" and self._idle_check_allowed
                    and not self._idle_check_sent and quiet >= 15)

    async def _idle_check(self, endpoint: Any, quiet: float) -> bool:
        if self._idle_no_input_revision == endpoint.revision:
            return True
        if not self._idle_check_allowed or self._idle_check_sent or quiet < 15:
            return False
        # Only an untouched initial turn or an already handled server phrase
        # can use this cheap presence check. Ordinary answers still go through
        # semantic review and the existing spoken completion handshake.
        initial = endpoint._last_voice is None
        if not initial and self._semantic_wait_identity is None and self._clarification_revision is None:
            return False
        endpoint._proposal_revision = endpoint.revision
        if initial:
            # No answer evidence is needed to gently check presence. A new
            # voice/ASR revision cancels the approved expression normally.
            self._idle_no_input_revision = endpoint.revision
        else:
            if getattr(endpoint.capture, "supports_stable_preview", False):
                # Presence is not a submission decision. A current stable
                # preview is enough to avoid interrupting recognized new words
                # without needlessly cutting/reopening a healthy recognizer.
                final = await endpoint.capture.transcript_preview()
                if final is not None and getattr(final, "has_unstable_tail", True):
                    return False
            else:
                final = await endpoint.listening_snapshot()
            endpoint.assert_current()
            if final is None or final.text.strip() != self.boundary:
                # Do not consume or classify a late/new final as an idle pause.
                return False
        self._idle_check_sent = True
        if await self._say(endpoint, "reception_idle_check", afterwards="listening"):
            endpoint.trace("idle_check", phase=self.phase, reason="initial" if initial else "handled_speech")
        else:
            # The durable floor may still belong to question playback. Retry
            # only after another quiet interval; never replace that output.
            self._idle_check_sent = False
            self._idle_no_input_revision = None
            if initial:
                endpoint._last_voice = None
            endpoint._retry_at = endpoint.clock() + 15
        return True

    def _remember_boundary(self, final: Any) -> None:
        self.boundary = final.text.strip()
        self._boundary_final = final.model_copy(deep=True)

    def _remember_handled(self, final: Any) -> None:
        self._handled_final = final.model_copy(deep=True)

    def _already_handled(self, final: Any) -> bool:
        previous = self._handled_final
        return bool(previous is not None and final is not None
                    and not getattr(final, "has_unstable_tail", False)
                    and _speech_since(final, previous, previous.text.strip()) == "")

    def _trace_handled(self, endpoint: Any, *, source: str) -> None:
        if self._handled_trace_revision != endpoint.revision:
            self._handled_trace_revision = endpoint.revision
            endpoint.trace("handled_final_suppressed", revision=endpoint.revision, source=source, phase=self.phase)

    @property
    def speaking(self) -> bool:
        return self.phase == "speaking"

    def voice(self) -> None:
        self._reception_review = None
        if self._exit_waiting:
            if self.confirmed:
                self.confirmed = False
                self._exit_context = self._exit_context_final = None
                self._confirmed_final = None
                self.phase = "awaiting_exit_reply"
            if self.phase in {"awaiting_exit_reply", "exit_classifying", "exit_confirmed"}:
                self.reply_seen = True
            return
        if self.phase == "pause_requested":
            self.phase = "listening"
        if self.confirmed:
            self.confirmed = False
            # Revoke the prepared transaction, not the question we already
            # asked. "No, next please" is another reply in this same exchange.
            self.phase = "awaiting_reply"
        if self.phase in {"awaiting_reply", "classifying"}:
            self.reply_seen = True

    async def complete_playback(self, endpoint: Any, *, reason: str) -> None:
        """Fence the reply boundary at delivered audio, not before synthesis.

        Keep the microphone echo gate closed while taking this final cut.
        Speech arriving before/during the question belongs to its prefix and
        cannot later become a new confirmation through a button or VAD noise.
        """
        verified = False
        try:
            if self.speaking and self._content_delivery_pending:
                self._content_question_delivered = False
                if reason == "agent_finished":
                    endpoint._proposal_revision = endpoint.revision
                    final = await endpoint.listening_snapshot()
                    endpoint.assert_current()
                    if (getattr(final, "type", None) == "transcript.final" and getattr(final, "is_final", False)
                            and self._content_question_focus in final.text):
                        self._content_request_final = final.model_copy(deep=True)
                        self._content_question_delivered = True
            if (self.speaking and self._exit_waiting and self._after_speech == "awaiting_exit_reply"
                    and reason == "agent_finished"):
                endpoint._proposal_revision = endpoint.revision
                final = await endpoint.listening_snapshot()
                endpoint.assert_current()
                if final is not None:
                    self._exit_request_final = final.model_copy(deep=True)
                    self._remember_boundary(final)
                    verified = True
        except Exception as exc:
            endpoint.trace("exit_delivery_boundary_unavailable", error_type=type(exc).__name__)
        finally:
            self._content_delivery_pending = False
            self.floor_returned(endpoint, reason=reason, exit_boundary_verified=verified)

    def floor_returned(self, endpoint: Any, *, reason: str = "agent_finished",
                       exit_boundary_verified: bool = False) -> None:
        if self.phase != "speaking":
            return
        if self._exit_waiting and self._after_speech == "awaiting_exit_reply":
            self._exit_question_delivered = reason == "agent_finished" and exit_boundary_verified
            self._exit_delivery_revision = endpoint.revision if self._exit_question_delivered else None
        self.phase = self._after_speech
        self.reply_seen = False
        self._missing_reply_since = None
        endpoint._last_voice = endpoint.clock()
        endpoint._blocked_revision = None

    def playback_selected(self, endpoint: Any) -> None:
        """Gate microphone echo only once playable output is selected."""
        if self.phase in {"preparing_speech", "preparing_company"}:
            self.phase = "speaking"
            self._playback_started_at = endpoint.clock()

    async def _say(self, endpoint: Any, kind: str, *, afterwards: str, reply_text: str = "",
                   repeat_target: Any = None) -> bool:
        endpoint.assert_current()
        self.phase = "preparing_speech"
        self._after_speech = afterwards
        self._playback_started_at = endpoint.clock()
        try:
            spoken = await endpoint._infer(self.speak(kind, endpoint.assert_current,
                                      **({"reply_text": reply_text} if reply_text else {}),
                                      **({"repeat_target": repeat_target} if repeat_target else {})), timeout=45)
            if spoken:
                self.playback_selected(endpoint)
                if kind in {"check", "clarify"}:
                    from app.core.prompt.contracts import SUPPLEMENT_SPEECH
                    self._reply_question = SUPPLEMENT_SPEECH[kind]
                    # A newly delivered question replaces the old exchange.
                    # Its answer cannot borrow an earlier finish decision.
                    self._confirmed_final = None
                    self._confirmation_origin = None
            if not spoken and self.phase in {"preparing_speech", "speaking"}:
                self.phase = "listening" if afterwards == "pause_requested" else afterwards
                self.reply_seen = endpoint.revision != endpoint._proposal_revision
                endpoint._last_voice = endpoint.clock()
            return bool(spoken)
        except BaseException:
            if self.phase in {"preparing_speech", "speaking"}:
                self.phase = "listening" if afterwards == "pause_requested" else afterwards
                self.reply_seen = endpoint.revision != endpoint._proposal_revision
            raise

    async def step(self, endpoint: Any, quiet: float) -> None:
        if self.speaking:
            if endpoint.clock() - self._playback_started_at >= 30:
                # Missing playback acknowledgement is not a candidate reply.
                # Cancel this output generation before reopening listening.
                await self.speak("playback_timeout", lambda: None)
                self._playback_started_at = endpoint.clock()
            return
        if self._exit_waiting:
            await self._exit_step(endpoint, quiet)
            return
        if self.phase == "pause_requested":
            endpoint._proposal_revision = endpoint.revision
            await self.speak("pause", endpoint.assert_current)
            return
        if self.confirmed or endpoint._explicit_revision == endpoint.revision:
            self.confirmed = True
            endpoint._proposal_revision = endpoint.revision
            await endpoint._propose()
            return
        if self.phase == "listening":
            if self._reception_review is not None:
                if quiet < endpoint.min_silence_seconds:
                    return
                final, revision, capture, kind = self._reception_review
                # This is a single reconciliation of the acknowledged words,
                # not a new polling inference. New input cancels both this
                # invitation and its speculative preparation.
                self._reception_review = None
                if revision == endpoint.revision and capture is endpoint.capture:
                    endpoint._proposal_revision = revision
                    if not await self._semantic_turn(endpoint, final, reception_kind=kind):
                        # A fast wait is only an acknowledgement. If complete
                        # understanding cannot settle the intent, restore the
                        # normal bounded spoken handshake instead of keeping
                        # the acknowledged transcript permanently locked.
                        self._semantic_wait_identity = None
                        self._remember_boundary(final)
                        self._clarification_revision = None
                        self._reminded = False
                        await self._say(endpoint, "check", afterwards="awaiting_reply")
                    return
            if await self._idle_check(endpoint, quiet):
                return
            if endpoint._last_voice is None:
                return
            if self._clarification_revision == endpoint.revision:
                return
            has_reception = (self.semantic_first and callable(getattr(endpoint.capture, "classify_reception", None))
                             and getattr(endpoint.capture, "supports_stable_preview", False))
            if quiet < (max(1.0, endpoint.min_silence_seconds) if has_reception else self.silence_seconds):
                return
            endpoint._proposal_revision = endpoint.revision
            early = quiet < self.silence_seconds
            preview = None
            if has_reception:
                preview = await endpoint.capture.transcript_preview()
                endpoint.assert_current()
                if self._already_handled(preview):
                    self._trace_handled(endpoint, source="stable_preview")
                    return
                if (self._semantic_wait_identity is not None and preview is not None
                        and not getattr(preview, "has_unstable_tail", True)
                        and preview.text.strip() == self.boundary):
                    # Waiting on the same words must leave the next healthy
                    # recognition stream open, including long quiet pauses.
                    return
                # The single speculative analysis runs alongside the small
                # reception call while the recognizer keeps accepting speech.
                # It cannot speak or submit; a matching final is still needed.
                if not endpoint._understanding_recovery_active and self._exit_context is None:
                    endpoint.preparation.start(preview)
            if early:
                # Do not cut ordinary answers into new STT streams just to
                # inspect a social request. A preview can only precompute.
                if (preview is None or getattr(preview, "has_unstable_tail", True)
                        or not await self._receive_request(endpoint, preview, prepare_only=True)):
                    return
            try:
                await self._listen(endpoint, allow_answer_review=not early)
            finally:
                if self.semantic_first and not endpoint._closed and endpoint.capture.is_open:
                    await endpoint.capture.resume_capture()
            return
        if not self.reply_seen:
            # One gentle reminder, then continue listening. No timeout means yes/no.
            if quiet >= 15 and not self._reminded:
                self._reminded = True
                endpoint._proposal_revision = endpoint.revision
                await self._say(endpoint, "clarify", afterwards="awaiting_reply")
            return
        if quiet < endpoint.min_silence_seconds:
            return
        endpoint._proposal_revision = endpoint.revision
        final = (await endpoint.listening_snapshot() if self.semantic_first
                 else await endpoint.capture.transcript_snapshot(resume=False))
        try:
            endpoint.assert_current()
            if not self.semantic_first:
                endpoint._observe_final_snapshot(final)
            if final is not None and await self._receive_request(endpoint, final, reconcile_complete=True):
                return
            await self._reply(endpoint, final)
        finally:
            if not endpoint._closed and endpoint.capture.is_open:
                await endpoint.capture.resume_capture()

    async def _listen(self, endpoint: Any, *, allow_answer_review: bool = True) -> None:
        final = (await endpoint.listening_snapshot() if self.semantic_first
                 else await endpoint.capture.transcript_snapshot(resume=True))
        endpoint.assert_current()
        if not self.semantic_first:
            endpoint._observe_final_snapshot(final)
        endpoint.cover_transcript(final)
        if final is None:
            if endpoint._awaiting_fresh_audio:
                return
            if self.boundary:
                # Existing boundary is conversational context only. Missing
                # audio/text never grants completion of an earlier answer.
                self._reminded = False
                await self._say(endpoint, "check", afterwards="awaiting_reply")
                return
            endpoint._retry_at = endpoint.clock() + self.silence_seconds
            await endpoint._notify("transcript_unavailable")
            return
        if self._already_handled(final):
            self._trace_handled(endpoint, source="authoritative_final")
            endpoint._blocked_revision = endpoint.revision
            return
        if self.semantic_first and await self._receive_request(endpoint, final, reconcile_complete=True):
            return
        if not allow_answer_review:
            endpoint._retry_at = endpoint._last_voice + self.silence_seconds
            return
        if self.semantic_first and await self._semantic_turn(endpoint, final):
            return
        self._remember_boundary(final)
        self._clarification_revision = None
        self._reminded = False
        await self._say(endpoint, "check", afterwards="awaiting_reply")

    async def _receive_request(self, endpoint: Any, final: Any, *, prepare_only: bool = False,
                               reconcile_complete: bool = False) -> bool:
        """A short social request may receive speech, never commit an answer."""
        classify = getattr(endpoint.capture, "classify_reception", None)
        if not self.semantic_first or not callable(classify):
            return False
        if not prepare_only and (getattr(final, "type", None) != "transcript.final" or not getattr(final, "is_final", False)):
            raise ValueError("Reception requires a complete server final")
        identity = None if prepare_only else endpoint._identity(final)
        if not prepare_only and self._semantic_wait_identity == identity:
            endpoint._blocked_revision = endpoint.revision
            return True
        text = final.text.strip()
        if not text:
            return False
        phase = "awaiting_reply" if self.phase in {"awaiting_reply", "classifying"} else "listening"
        previous = self._reception_seen_text
        new_text = _speech_since(final, self._reception_seen_final, previous)
        if not new_text:
            return False
        cache_key = (text, phase, previous)
        if self._reception_cache and self._reception_cache[0] == cache_key:
            decision = self._reception_cache[1]
        else:
            try:
                decision = await endpoint._infer(classify(new_text, phase=phase, preceding_text=previous), timeout=5.5)
                endpoint.assert_current()
            except Exception as exc:
                if getattr(exc, "code", None) == "TURN_DECISION_STALE":
                    raise
                # Failure does not fabricate a request or finish permission.
                # Full understanding still has its own bounded recovery path.
                endpoint.trace("reception_unavailable", error_type=type(exc).__name__)
                decision = {"kind": "other", "confidence": 0.0}
            self._reception_cache = (cache_key, decision)
        confidence = minimum_reported_confidence(final.confidence, decision["confidence"])
        if decision["kind"] == "other" or confidence is None or confidence < .75:
            if not prepare_only:
                self._reception_seen_text = text
                self._reception_seen_final = final.model_copy(deep=True)
            return False
        if prepare_only:
            return True
        kind = decision["kind"]
        endpoint.trace("reception_selected", kind=kind, revision=endpoint.revision)
        self.confirmed = False
        self._confirmed_final = None
        endpoint._explicit_revision = None
        endpoint._discard_confirmed_preparation()
        review_complete = reconcile_complete and kind in {"wait", "continue"}
        if not review_complete:
            endpoint.preparation.cancel()
        await endpoint._notify("answer_listening")
        endpoint.assert_current()
        stage = getattr(endpoint.capture, "stage_reception_exchange", None)
        token = stage(final, new_text, kind) if callable(stage) else None
        try:
            spoken = await self._say(endpoint, "reception_" + kind,
                                     afterwards="pause_requested" if kind == "pause" else "listening",
                                     reply_text=decision.get("reply_text", ""),
                                     repeat_target=decision.get("repeat_target"))
        finally:
            discard = getattr(endpoint.capture, "discard_reception_exchange", None)
            if callable(discard):
                discard(token)
        if spoken:
            # A preview or an interrupted synthesis has not answered this
            # request. Advance only after successful delivery, so a retry or
            # appended short phrase retains the still-pending request.
            self._reception_seen_text = text
            self._reception_seen_final = final.model_copy(deep=True)
            self._remember_handled(final)
            self._semantic_wait_identity = identity
            self._remember_boundary(final)
            self._idle_check_allowed = kind not in {"wait", "pause"}
            self._idle_check_sent = False
            if review_complete:
                self._reception_review = (final.model_copy(deep=True), endpoint.revision, endpoint.capture, kind)
        else:
            endpoint._retry_at = endpoint.clock() + 2
        return True

    async def _recover_interaction(self, endpoint: Any, final: Any) -> None:
        if self._failure_spoken_identity is not None:
            return
        endpoint.assert_current()
        if await self._say(endpoint, "reception_unavailable", afterwards="listening"):
            self._failure_spoken_identity = True

    async def _semantic_turn(self, endpoint: Any, final: Any, *, reception_kind: Any = None) -> bool:
        """Understand before asking; this never fabricates a spoken reply."""
        if getattr(final, "type", None) != "transcript.final" or not getattr(final, "is_final", False):
            raise ValueError("Semantic completion requires a complete server final")
        identity = endpoint._identity(final)
        if reception_kind is None and self._semantic_wait_identity == identity:
            endpoint._blocked_revision = endpoint.revision
            return True
        if reception_kind is None:
            await endpoint._notify("answer_preparing")
        endpoint.assert_current()
        try:
            prepared = await endpoint._prepare_transcript(final, semantic_first=True)
        except Exception as exc:
            if (getattr(exc, "code", None) == "TURN_DECISION_STALE"
                    or classify_capture_failure(exc, "understanding").safety_violation):
                raise
            # _prepare_transcript records each attempt once, including an
            # abandoned waiter. Publishing its failure must not count twice.
            await endpoint._understanding_failed(record_failure=False)
            if reception_kind is None:
                await self._recover_interaction(endpoint, final)
            return True
        endpoint.assert_current()
        understanding = prepared.understanding
        if understanding.problem is not None:
            await endpoint._understanding_failed(record_failure=False)
            if reception_kind is None:
                await self._recover_interaction(endpoint, final)
            return True
        await endpoint._understanding_succeeded()
        if understanding.suggested_action == "respond_company":
            if reception_kind is not None:
                self._semantic_wait_identity = None
            await self.company_answer(endpoint, prepared, final)
            return True
        if (understanding.turn_intent == "stop_interview"
                or ConversationUnderstandingService.can_complete_without_confirmation(understanding, final.text)):
            if reception_kind is not None:
                self._semantic_wait_identity = None
            endpoint.trace("semantic_completion_selected", intent=understanding.turn_intent,
                           revision=endpoint.revision, text=final.text)
            # Recognition has continued while reasoning. A fresh final cut
            # must match before the evidence transaction can accept anything.
            await endpoint._propose(final_snapshot=final, prepared_decision=prepared,
                                    recognition_resumed=True)
            return True
        if understanding.suggested_action == "continue_listening":
            self._semantic_wait_identity = identity
            self._remember_boundary(final)
            self._idle_check_allowed = understanding.turn_intent != "thinking"
            self._idle_check_sent = False
            await endpoint._notify("answer_listening")
            if reception_kind is not None:
                # The candidate has already heard a response. Full thinking
                # changes the wait policy without asking or saying it again.
                return True
            if await self._say(endpoint, "reception_wait" if understanding.turn_intent == "thinking" else "reception_continue",
                               afterwards="listening"):
                self._remember_handled(final)
            else:
                self._semantic_wait_identity = None
                endpoint._retry_at = endpoint.clock() + 2
            return True
        if understanding.suggested_action in {"clarify", "repeat", "pause"}:
            await endpoint._propose(final_snapshot=final, prepared_decision=prepared,
                                    recognition_resumed=True)
            return True
        # Ordinary complete-looking content does not grant consent to close
        # the topic. An uncertain turn retains the existing spoken handshake.
        await endpoint._notify("answer_listening")
        return False

    async def company_answer(self, endpoint: Any, prepared: Any, final: Any) -> None:
        """Insert a reply without sealing this answer or replacing its audio."""
        endpoint.assert_current()
        identity = endpoint._identity(final)
        if self._semantic_wait_identity == identity:
            return
        if self.respond_company is None:
            raise ValueError("Company question responder is unavailable")
        self.confirmed = False
        self._confirmed_final = None
        endpoint._explicit_revision = None
        endpoint._discard_confirmed_preparation()
        self._remember_boundary(final)
        self.phase, self._after_speech = "preparing_company", "listening"
        self._playback_started_at = endpoint.clock()
        try:
            # Extraction has its own 8s budget; retain the existing expression
            # service's synthesis budget instead of cutting both off at 15s.
            spoken = await endpoint._infer(self.respond_company(prepared, final, endpoint.assert_current), timeout=45)
            endpoint.assert_current()
            self._semantic_wait_identity = identity if spoken else None
            self._clarification_revision = endpoint.revision
            if spoken:
                self._remember_handled(final)
                self._idle_check_allowed = True
                self._idle_check_sent = False
            if self.phase == "preparing_company" or (not spoken and self.phase == "speaking"):
                self.phase = "listening"
        except BaseException:
            self.phase = "listening"
            raise

    async def clarify_answer(self, endpoint: Any, understanding: Any, final: Any) -> None:
        """Ask within this recording; a correction must retain the earlier answer."""
        endpoint.assert_current()
        target = understanding.clarification_target
        if target and (target.evidence_quote not in final.text or target.focus_quote not in target.evidence_quote):
            raise ValueError("Clarification target is not grounded in the current final")
        self.confirmed = False
        self._confirmed_final = None
        self._remember_boundary(final)
        self._clarification_revision = endpoint.revision
        endpoint._explicit_revision = None
        endpoint._discard_confirmed_preparation()
        self.phase, self._after_speech = "preparing_speech", "listening"
        self._content_question_act_id = None
        self._content_question_focus = target.focus_quote if target else None
        self._content_request_final = None
        self._content_question_delivered = False
        self._content_delivery_pending = False
        self._playback_started_at = endpoint.clock()
        try:
            spoken = await endpoint._infer(self.speak("answer_clarify", endpoint.assert_current,
                                      focus_quote=target.focus_quote if target else ""), timeout=45)
            if spoken:
                if isinstance(spoken, dict) and spoken.get("act_id") and target:
                    self._content_question_act_id = spoken["act_id"]
                    self._content_delivery_pending = True
                self.playback_selected(endpoint)
                self._remember_handled(final)
                self._idle_check_allowed = True
                self._idle_check_sent = False
            if not spoken and self.phase in {"preparing_speech", "speaking"}:
                self.phase = "listening"
        except BaseException:
            self.phase = "listening"
            raise

    async def _reply(self, endpoint: Any, final: Any) -> None:
        endpoint.assert_current()
        endpoint.cover_transcript(final)
        if final is None:
            if endpoint._awaiting_fresh_audio:
                return
            if self._missing_reply_since is None:
                self._missing_reply_since = endpoint.clock()
            if (not self._reminded
                    and endpoint.clock() - self._missing_reply_since >= self.silence_seconds):
                # Acoustic activity without recognized words is not a yes/no
                # reply. Keep its audio for recovery, but do not loop empty
                # snapshots forever while the interviewer stays silent.
                self._reminded = True
                await self._say(endpoint, "clarify", afterwards="awaiting_reply")
                return
            endpoint._retry_at = endpoint.clock() + 2
            await endpoint._notify("transcript_unavailable")
            return
        self._missing_reply_since = None
        if getattr(final, "type", None) != "transcript.final" or not getattr(final, "is_final", False):
            raise ValueError("Supplement completion requires a complete server final")
        text = final.text.strip()
        reply = _speech_since(final, self._boundary_final, self.boundary)
        if reply is None:
            # An ambiguous edit still needs another reply; a positively timed
            # new segment does not lose consent merely because old ASR changed.
            self._remember_boundary(final)
            await self._say(endpoint, "clarify", afterwards="awaiting_reply")
            return
        if not reply:
            self.reply_seen = False
            if (self._confirmed_final is not None
                    and endpoint._identity(final) == endpoint._identity(self._confirmed_final)):
                # Only a fresh complete server snapshot can certify that the
                # acoustic interruption added no words. Never reuse on None.
                await self._accept_finish(endpoint, final)
            return
        # Map the reply's exact suffix rather than trusting an old segment
        # count after repair. A segment spanning the boundary still contributes
        # its confidence; absent mapping falls back conservatively. Clear new
        # words must not inherit individual old-segment quality forever, while
        # the provider's aggregate final confidence remains a separate gate.
        mapped = _mapped_segments(final)
        reply_start = len(text) - len(reply)
        segments = ([segment for segment, _, end in mapped if end > reply_start]
                    if mapped else final.segments)
        confidence = minimum_reported_confidence(final.confidence, *(s.confidence for s in segments))
        if confidence is not None and confidence < 0.65:
            self._remember_boundary(final)
            await self._say(endpoint, "clarify", afterwards="awaiting_reply")
            return
        self.phase = "classifying"
        # Bind classification failures to the same server words as answer
        # preparation. Acoustic revisions alone cannot buy new model calls.
        endpoint._use_preparation_budget(text)
        if endpoint.preparation_blocked:
            self.phase = "awaiting_reply"
            endpoint._blocked_revision = endpoint.revision
            await endpoint._understanding_failed(record_failure=False)
            return
        budget_key = endpoint._preparation_budget_key
        budget_epoch = endpoint._preparation_budget_epoch
        reply_context = {}
        if (self._confirmed_final is not None and self._confirmation_origin is not None
                and self._boundary_final is not None
                and endpoint._identity(self._boundary_final) == endpoint._identity(self._confirmed_final)):
            origin_text, origin_final = self._confirmation_origin
            preceding = _speech_since(self._confirmed_final, origin_final, origin_text)
            if preceding:
                # This is evidence about the previous exchange, not current
                # consent. All new words still require a new classification,
                # and the full final/revision must pass the commit fence.
                reply_context = {"prior_confirmation": True, "preceding_text": preceding,
                                 "question": self._reply_question}
        try:
            decision = await endpoint._infer(
                endpoint.capture.classify_supplement_reply(reply, **reply_context), timeout=10,
            )
            endpoint.assert_current()
        except Exception as exc:
            self.phase = "awaiting_reply"
            if getattr(exc, "code", None) == "TURN_DECISION_STALE":
                raise
            if (endpoint._preparation_budget_key != budget_key
                    or endpoint._preparation_budget_epoch != budget_epoch):
                return
            endpoint.trace("supplement_classification_failed", error_type=type(exc).__name__,
                           cause_code=getattr(exc, "code", "supplement_classification_invalid"))
            await endpoint._understanding_failed()
            return
        except BaseException:
            self.phase = "awaiting_reply"
            raise
        if endpoint._prepare_failures:
            await endpoint._notify("supplement_awaiting_reply")
        action = decision["intent"]
        endpoint.trace("supplement_decided", intent=action, confidence=decision["confidence"],
                       revision=endpoint.revision, text=reply)
        _LOG.info("supplement_reply_decided intent=%s confident=%s", action, decision["confidence"] >= 0.75)
        if action == "company_question" and decision["confidence"] >= 0.75:
            self.phase = "listening"
            if not await self._semantic_turn(endpoint, final):
                await self._say(endpoint, "clarify", afterwards="awaiting_reply")
        elif action == "finish" and decision["confidence"] >= 0.75:
            await self._accept_finish(endpoint, final)
        elif action in {"continue", "supplement"} and decision["confidence"] >= 0.75:
            self._confirmed_final = None
            self._remember_boundary(final)
            await self._say(endpoint, "continue", afterwards="listening")
        elif action == "pause" and decision["confidence"] >= 0.75:
            await self.speak("pause", endpoint.assert_current)
        else:
            self._confirmed_final = None
            self._remember_boundary(final)
            await self._say(endpoint, "clarify", afterwards="awaiting_reply")

    async def _accept_finish(self, endpoint: Any, final: Any) -> None:
        if self._confirmed_final is None:
            self._confirmation_origin = (self.boundary, self._boundary_final)
        self.confirmed = True
        self.phase = "confirmed"
        self._confirmed_final = final.model_copy(deep=True)
        self._remember_boundary(final)
        await endpoint._propose(final_snapshot=final, recognition_resumed=self.semantic_first)
