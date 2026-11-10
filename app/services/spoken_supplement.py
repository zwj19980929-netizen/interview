"""Semantic-first turn taking with a bounded spoken fallback handshake."""

from __future__ import annotations

from app.domain.speech_quality import minimum_reported_confidence
from app.core.prompt.validation import StructuredResponseValidationError
from app.model_gateway.errors import ProviderError
from app.services.conversation_understanding import ConversationUnderstandingService
from app.services.capture_recovery import classify_capture_failure
from typing import Any
import hashlib
import logging
import asyncio
import time

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
        # Failed classification is unknown, not a successful `other` receipt.
        # Text/context budgets survive audio revisions and provider metadata.
        self._reception_attempts = {}
        self._reception_failure = None
        self._reception_seen_text = ""
        self._reception_seen_final = None
        self._reception_review = None
        # Input invalidation revokes a decision, not the fact that these exact
        # server words have already received a response. Acoustic noise and
        # barge-in must not repeatedly reinterpret an old cumulative final.
        self._handled_final = None
        self._handled_trace_revision = None
        self._failure_spoken_identity = None
        self._recovery_review = None
        self._idle_check_allowed = True
        self._idle_check_sent = False
        self._idle_no_input_revision = None
        self._off_topic_redirected = False
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
        self._supplement_question_delivered = False
        self._supplement_delivery_pending = False
        self._unplayed_speech_checkpoint = None
        self._pending_prompt = None
        self._pending_prompt_checkpoint = None
        self._selecting_supplement_prompt = False
        self._prompt_attempts = 0
        self._prompt_budgets = {}
        self._prompt_stage_deadline = 0.0
        self._prepared_answer = None
        self._prepared_answer_final = None
        self._confirmed_reused_prepared = None

    def begin_unplayed_speech(self) -> Any:
        """Remember delivered input cursors before one exact output is selected."""
        checkpoint = {name: getattr(self, name) for name in (
            "_handled_final", "_handled_trace_revision", "_reception_seen_text", "_reception_seen_final",
            "_failure_spoken_identity", "_idle_check_allowed", "_idle_check_sent", "_off_topic_redirected")}
        checkpoint["failed"] = False
        self._unplayed_speech_checkpoint = checkpoint
        if self._selecting_supplement_prompt:
            self._pending_prompt_checkpoint = checkpoint
        return checkpoint

    def restore_unplayed_speech(self, checkpoint: Any) -> bool:
        """Undo only the unheard attempt; never treat its question as delivered."""
        if checkpoint is None or checkpoint is not self._unplayed_speech_checkpoint or checkpoint["failed"]:
            return False
        checkpoint["failed"] = True
        for name, value in checkpoint.items():
            if name != "failed":
                setattr(self, name, value)
        self.phase = "listening"
        self._after_speech = "listening"
        self.confirmed = self.reply_seen = False
        self._confirmed_final = self._confirmation_origin = None
        self.boundary = ""
        self._boundary_final = self._missing_reply_since = None
        self._semantic_wait_identity = self._reception_review = self._clarification_revision = None
        self._reply_question = ""
        self._exit_waiting = self._exit_question_delivered = False
        self._exit_request_final = self._exit_question_act_id = self._exit_delivery_revision = None
        self._exit_context = self._exit_context_final = self._exit_evidence_identity = None
        self._content_question_act_id = self._content_question_focus = self._content_request_final = None
        self._content_question_delivered = self._content_delivery_pending = False
        self._supplement_question_delivered = self._supplement_delivery_pending = False
        return True

    def _speech_result_current(self) -> bool:
        # A zero-audio task can fail between selection and its caller resuming.
        # That late successful selection result must not re-advance the cursors.
        return not (self._unplayed_speech_checkpoint or {}).get("failed", False)

    @property
    def content_clarification_active(self) -> bool:
        """A targeted exchange needs a fresh final and its delivery context.

        This starts before speech selection and remains true for the current
        capture, including interrupted playback and replies. It only disables
        context-free speculation; delivery and completion still require their
        separate evidence checks.
        """
        return self._content_question_focus is not None

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

    def should_classify_supplement_reply(self, transcript: Any) -> bool:
        """Route new replies before analysis; a preview grants no authority.

        Exit and targeted content clarification keep their own full-evidence
        paths. The existing reply classifier still checks quality, fresh final
        evidence and current ownership before it can confirm anything.
        """
        if (not self.semantic_first or not self._supplement_question_delivered
                or self.confirmed or self._exit_waiting or self.content_clarification_active
                or self._boundary_final is None or transcript is None
                or not getattr(transcript, "text", "").strip()
                or getattr(transcript, "has_unstable_tail", False)):
            return False
        final = (getattr(transcript, "type", None) == "transcript.final"
                 and getattr(transcript, "is_final", False))
        preview = getattr(transcript, "has_unstable_tail", None) is False
        return bool((final or preview) and _speech_since(transcript, self._boundary_final, self.boundary))

    @staticmethod
    def needs_supplement_confirmation(understanding: Any) -> bool:
        """Ordinary answers close after a separate opportunity to supplement."""
        intent = getattr(understanding, "intent", None)
        turn_intent = getattr(understanding, "turn_intent", None)
        action = getattr(understanding, "suggested_action", None)
        if action in {"clarify", "repeat", "pause", "respond_company"}:
            return False
        return ((intent in {"answer", "off_topic"} and turn_intent == "finish_answer")
                or (intent == "answer" and turn_intent == "answering"))

    async def gate_answer_completion(self, endpoint: Any, prepared: Any, final: Any) -> bool:
        """Keep ordinary answers in this capture until a fresh supplement reply.

        The endpoint calls this after its final-cut fence, including revisions
        discovered only at that cut. Explicit finish and other semantic intents
        retain their own authority; this gate never grants interview exit.
        """
        if (not self.needs_supplement_confirmation(prepared.understanding)
                or self.confirmed or endpoint._explicit_revision == endpoint.revision):
            return False
        endpoint.assert_current()
        if self._supplement_question_delivered:
            # A candidate may have chosen to add detail and returned to normal
            # listening. Classify only the new reply; do not ask the same check
            # again or infer consent from the earlier answer's finish phrase.
            await self._reply(endpoint, final)
        else:
            self._prepared_answer = prepared
            self._prepared_answer_final = final.model_copy(deep=True)
            self._remember_boundary(final)
            self._clarification_revision = None
            self._semantic_wait_identity = None
            self._reminded = False
            endpoint._discard_confirmed_preparation()
            endpoint.preparation.cancel()
            await self._say(endpoint, "check", afterwards="awaiting_reply")
        return True

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
        self._exit_reply_blocked_revision = None
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
                "exit_clarify" if clarify else "exit_check", endpoint.assert_current), timeout=45,
                use_interaction_budget=False)
            endpoint.assert_current()
            if not self._speech_result_current():
                selected = None
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
        if getattr(self, "_exit_reply_blocked_revision", None) == endpoint.revision:
            return
        if self.confirmed:
            endpoint._proposal_revision = endpoint.revision
            if callable(getattr(endpoint.capture, "prepare_exit_decision", None)):
                await endpoint._propose_exit(self._confirmed_final)
            else:
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
        # A control reply has an independent, small allowance. Failed technical
        # reasoning cannot prevent a candidate from confirming or withdrawing.
        # Provider metadata/timing revisions cannot buy another model attempt.
        # Full provenance is still checked separately before any exit effect.
        reply_key = (self._exit_question_act_id, hashlib.sha256(final.text.strip().encode("utf-8")).hexdigest())
        if getattr(self, "_exit_reply_attempt_key", None) != reply_key:
            self._exit_reply_attempt_key, self._exit_reply_attempts = reply_key, 0
        if self._exit_reply_attempts >= 2:
            self.phase = "awaiting_exit_reply"
            self._exit_reply_blocked_revision = endpoint.revision
            return
        self._exit_reply_attempts += 1
        try:
            decision = await endpoint._infer(endpoint.capture.classify_exit_reply(reply), timeout=10,
                                              use_interaction_budget=False, operation="exit_reply")
            endpoint.assert_current()
        except BaseException:
            self.phase = "awaiting_exit_reply"
            if self._exit_reply_attempts >= 2:
                self._exit_reply_blocked_revision = endpoint.revision
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
        if callable(getattr(endpoint.capture, "prepare_exit_decision", None)):
            await endpoint._propose_exit(final)
        else:
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
        try:
            # This is a later optional presence reminder, not more reasoning
            # about the already answered request. Its own bounded speech must
            # neither spend nor renew that earlier preparation deadline.
            spoken = await self._say(endpoint, "reception_idle_check", afterwards="listening", recovery=True)
        except Exception as exc:
            if (getattr(exc, "code", None) == "TURN_DECISION_STALE"
                    or classify_capture_failure(exc, "speech").safety_violation):
                raise
            # Consume this one attempt before I/O. Optional reminder failure
            # cannot turn a successfully handled request into retry_required.
            endpoint.trace("idle_check_unavailable", error_type=type(exc).__name__)
            return True
        if spoken:
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
        self._recovery_review = None
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

    async def complete_playback(self, endpoint: Any, *, reason: str,
                                delivery_guard: Any = None) -> None:
        """Fence the reply boundary at delivered audio, not before synthesis.

        Keep the echo gate closed while taking this final cut. The ingress
        separately buffers PCM received after the verified playback drain;
        it enters the resumed stream only after this old-prefix boundary is
        frozen. Earlier playback audio cannot become a new confirmation.
        """
        verified = False
        supplement_verified = False
        try:
            if delivery_guard is not None:
                delivery_guard()
            if self.speaking and self._supplement_delivery_pending:
                self._supplement_question_delivered = False
                if reason == "agent_finished":
                    endpoint._proposal_revision = endpoint.revision
                    final = await endpoint.listening_snapshot()
                    if delivery_guard is not None:
                        delivery_guard()
                    endpoint.assert_current()
                    if (getattr(final, "type", None) == "transcript.final"
                            and getattr(final, "is_final", False)):
                        self._remember_boundary(final)
                        supplement_verified = True
            if self.speaking and self._content_delivery_pending:
                self._content_question_delivered = False
                if reason == "agent_finished":
                    endpoint._proposal_revision = endpoint.revision
                    final = await endpoint.listening_snapshot()
                    if delivery_guard is not None:
                        delivery_guard()
                    endpoint.assert_current()
                    if (getattr(final, "type", None) == "transcript.final" and getattr(final, "is_final", False)
                            and self._content_question_focus in final.text):
                        self._content_request_final = final.model_copy(deep=True)
                        self._content_question_delivered = True
            if (self.speaking and self._exit_waiting and self._after_speech == "awaiting_exit_reply"
                    and reason == "agent_finished"):
                endpoint._proposal_revision = endpoint.revision
                final = await endpoint.listening_snapshot()
                if delivery_guard is not None:
                    delivery_guard()
                endpoint.assert_current()
                if final is not None:
                    self._exit_request_final = final.model_copy(deep=True)
                    self._remember_boundary(final)
                    verified = True
        except Exception as exc:
            endpoint.trace("exit_delivery_boundary_unavailable", error_type=type(exc).__name__)
        finally:
            self._content_delivery_pending = False
            self._supplement_delivery_pending = False
            self.floor_returned(endpoint, reason=reason, exit_boundary_verified=verified,
                                supplement_boundary_verified=supplement_verified)
            if supplement_verified and endpoint._recovery_stage == "supplement_prompt":
                await endpoint._understanding_succeeded()

    def abandon_delivery(self, endpoint: Any) -> None:
        """A cancelled handoff may retain raw PCM, never its confirmation grant."""
        self._supplement_question_delivered = False
        self._exit_question_delivered = False
        self._exit_delivery_revision = None
        self._content_question_delivered = False
        self._supplement_delivery_pending = False
        self._content_delivery_pending = False
        self.confirmed = False
        self._recovery_review = None
        self.floor_returned(endpoint, reason="handoff_interrupted")

    def floor_returned(self, endpoint: Any, *, reason: str = "agent_finished",
                       exit_boundary_verified: bool = False,
                       supplement_boundary_verified: bool = False) -> None:
        if self.phase != "speaking":
            return
        if reason != "agent_finished":
            self._recovery_review = None
        if self._exit_waiting and self._after_speech == "awaiting_exit_reply":
            self._exit_question_delivered = reason == "agent_finished" and exit_boundary_verified
            self._exit_delivery_revision = endpoint.revision if self._exit_question_delivered else None
        if self._after_speech == "awaiting_reply":
            self._supplement_question_delivered = reason == "agent_finished" and supplement_boundary_verified
            if self._supplement_question_delivered:
                self._pending_prompt = None
                self._pending_prompt_checkpoint = None
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
                   repeat_target: Any = None, reply_prompt_version: str = "", recovery: bool = False) -> bool:
        endpoint.assert_current()
        prompt = kind in {"check", "clarify"}
        if prompt:
            identity = (kind, self.boundary, endpoint.revision)
            if self._pending_prompt is None or self._pending_prompt[0] != identity:
                self._pending_prompt = (identity, kind, afterwards, self._boundary_final)
                self._prompt_attempts, self._prompt_stage_deadline = self._prompt_budgets.get(
                    identity[:2], (0, time.monotonic() + 25.0))
            if self._prompt_attempts >= 2 or time.monotonic() >= self._prompt_stage_deadline:
                await self._prompt_failed(endpoint, safe_to_retry=self._prompt_cleanup_confirmed())
                return False
            self._prompt_attempts += 1
            self._prompt_budgets[identity[:2]] = (self._prompt_attempts, self._prompt_stage_deadline)
        self._selecting_supplement_prompt = prompt
        if prompt:
            self._pending_prompt_checkpoint = None
        self.phase = "preparing_speech"
        self._after_speech = afterwards
        self._playback_started_at = endpoint.clock()
        if prompt:
            from app.core.prompt.contracts import SUPPLEMENT_SPEECH
            self._reply_question = SUPPLEMENT_SPEECH[kind]
            self._supplement_question_delivered = False
            self._supplement_delivery_pending = True
            self._confirmed_final = self._confirmation_origin = None
        try:
            spoken = await endpoint._infer(self.speak(kind, endpoint.assert_current,
                                      **({"reply_text": reply_text} if reply_text else {}),
                                      **({"reply_prompt_version": reply_prompt_version} if reply_prompt_version and reply_text else {}),
                                      **({"repeat_target": repeat_target} if repeat_target else {})),
                                      timeout=(max(.001, min(12, self._prompt_stage_deadline - time.monotonic())) if prompt
                                               else 12 if recovery else 45),
                                      use_interaction_budget=not (recovery or prompt))
            if not self._speech_result_current():
                spoken = False
            if spoken:
                self.playback_selected(endpoint)
            if not spoken and prompt:
                # Only the output owner's exact zero-PCM cleanup checkpoint
                # may authorize reselecting a failed question.
                await self._prompt_failed(endpoint, safe_to_retry=self._prompt_cleanup_confirmed())
            elif not spoken and self.phase in {"preparing_speech", "speaking"}:
                self._supplement_delivery_pending = False
                self.phase = "listening" if afterwards == "pause_requested" else afterwards
                self.reply_seen = endpoint.revision != endpoint._proposal_revision
                endpoint._last_voice = endpoint.clock()
            return bool(spoken)
        except Exception as exc:
            self._supplement_delivery_pending = False
            if (getattr(exc, "code", None) == "TURN_DECISION_STALE"
                    or classify_capture_failure(exc, "understanding").safety_violation):
                self._pending_prompt = None
                self.phase = "listening"
                raise
            if prompt:
                await self._prompt_failed(endpoint, safe_to_retry=self._prompt_cleanup_confirmed(),
                    retryable=getattr(exc, "retryable", True))
                return False
            if self.phase in {"preparing_speech", "speaking"}:
                self.phase = "listening" if afterwards == "pause_requested" else afterwards
                self.reply_seen = endpoint.revision != endpoint._proposal_revision
            raise
        except BaseException:
            self._pending_prompt = None
            raise
        finally:
            self._selecting_supplement_prompt = False

    def _prompt_cleanup_confirmed(self) -> bool:
        return bool(self._pending_prompt_checkpoint is not None
                    and self._pending_prompt_checkpoint is self._unplayed_speech_checkpoint
                    and self._pending_prompt_checkpoint.get("failed"))

    async def _prompt_failed(self, endpoint: Any, *, safe_to_retry: bool, retryable: bool = True) -> None:
        pending = self._pending_prompt
        if pending is None:
            return
        identity, _, _, boundary_final = pending
        self._supplement_question_delivered = self._supplement_delivery_pending = False
        if boundary_final is not None:
            self._remember_boundary(boundary_final)
        # Unknown/partly played output keeps its playback watchdog and cannot
        # be replayed until the output owner proves exact safe cleanup.
        if safe_to_retry or self.phase != "speaking":
            self.phase = "retrying_speech"
        retryable = retryable and safe_to_retry
        endpoint._understanding_recovery_active = True
        endpoint._recovery_stage = "supplement_prompt"
        endpoint._recovery_reason = "unavailable" if retryable else "non_retryable"
        endpoint._recovery_retryable = retryable
        current = identity[2] == endpoint.revision
        if (safe_to_retry and retryable and current and self._prompt_attempts < 2
                and time.monotonic() < self._prompt_stage_deadline):
            endpoint._retry_at = endpoint.clock() + 1
            endpoint._blocked_revision = None
            await endpoint._notify("understanding_retrying")
        else:
            endpoint._blocked_revision = endpoint.revision
            await endpoint._notify("understanding_retry_exhausted" if retryable else "understanding_result_rejected")

    async def retry_unplayed_expression(self, endpoint: Any) -> bool:
        if self._pending_prompt is None or not self._prompt_cleanup_confirmed():
            return False
        await self._prompt_failed(endpoint, safe_to_retry=True)
        return True

    def retry_pending_prompt(self, endpoint: Any) -> None:
        failed = self._reception_failure
        if failed is not None:
            key, capture = failed
            attempt = self._reception_attempts.get(key) or {}
            phase = "awaiting_reply" if self.phase in {"awaiting_reply", "classifying"} else "listening"
            current_text = endpoint._covered_server_transcript or endpoint._last_server_transcript
            if (capture is endpoint.capture and attempt.get("retryable")
                    and key == (current_text.strip(), phase, self._reception_seen_text)):
                # Only this unresolved source is renewed. Keep successful
                # decisions and delivered-history cursors intact.
                self._reception_attempts.pop(key, None)
                self._reception_failure = None
                self._recovery_review = None
        if self._exit_waiting:
            self._exit_reply_attempt_key = None
            self._exit_reply_blocked_revision = None
        if self._pending_prompt is not None:
            # Renew the stage only after exact failed-output cleanup proof.
            if not self._prompt_cleanup_confirmed():
                return
            self._prompt_attempts = 0
            self._prompt_stage_deadline = time.monotonic() + 25
            self._prompt_budgets[self._pending_prompt[0][:2]] = (0, self._prompt_stage_deadline)
            self.phase = "retrying_speech"

    async def control_step(self, endpoint: Any, quiet: float) -> None:
        """Inspect each new input once while technical preparation is blocked."""
        if self._exit_waiting:
            await self._exit_step(endpoint, quiet)
            return
        has_reception = self.semantic_first and callable(getattr(endpoint.capture, "classify_reception", None))
        if quiet < (max(1.0, endpoint.min_silence_seconds) if has_reception else self.silence_seconds):
            return
        if not has_reception and (
                not endpoint._last_server_transcript
                or endpoint._last_server_transcript == endpoint._covered_server_transcript):
            # Without a reception seam, only a new text hint warrants one
            # authoritative inspection. Audio noise cannot repeatedly cut
            # STT. The hint itself never renews the model budget; only the
            # final observed by listening_snapshot may select new evidence.
            return
        if getattr(self, "_blocked_control_revision", None) == endpoint.revision:
            return
        endpoint._proposal_revision = endpoint.revision
        if getattr(endpoint.capture, "supports_stable_preview", False):
            preview = await endpoint.capture.transcript_preview()
            endpoint.assert_current()
            if preview is None or getattr(preview, "has_unstable_tail", True):
                # Same words can become stable without a new audio revision.
                # Only a real inspection spends this revision's allowance.
                return
            if endpoint._identity(preview) == getattr(self, "_blocked_control_identity", None):
                self._blocked_control_revision = endpoint.revision
                return
        self._blocked_control_revision = endpoint.revision
        final = await endpoint.listening_snapshot()
        endpoint.assert_current()
        if final is not None:
            self._blocked_control_identity = endpoint._identity(final)
            if has_reception:
                await self._receive_request(endpoint, final)

    async def step(self, endpoint: Any, quiet: float) -> None:
        if self.phase == "retrying_speech":
            if (endpoint.preparation_blocked or endpoint._blocked_revision == endpoint.revision
                    or endpoint.clock() < endpoint._retry_at):
                return
            pending = self._pending_prompt
            if pending is None:
                self.phase = "listening"
                return
            identity, kind, afterwards, _ = pending
            if identity[2] != endpoint.revision:
                self._pending_prompt = None
                self.phase = "listening"
                return
            endpoint._proposal_revision = endpoint.revision
            await self._say(endpoint, kind, afterwards=afterwards)
            return
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
            if self._confirmed_final is not None and self._confirmed_reused_prepared is not None:
                await endpoint._propose(final_snapshot=self._confirmed_final,
                    prepared_decision=self._confirmed_reused_prepared, recognition_resumed=True)
            else:
                await endpoint._propose()
            return
        if self.phase == "listening":
            if self._recovery_review is not None:
                revision, capture, identity = self._recovery_review
                self._recovery_review = None
                if (revision == endpoint.revision and capture is endpoint.capture
                        and not endpoint._closed and capture.is_open and not endpoint.preparation_blocked):
                    # This notice has already returned the floor. Reconcile
                    # pure background work against a new authoritative cut;
                    # its completion never grants a reply or submit permission.
                    endpoint._proposal_revision = revision
                    try:
                        await self._listen(endpoint, recovery_identity=identity)
                    finally:
                        if not endpoint._closed and endpoint.capture.is_open:
                            await endpoint.capture.resume_capture()
                    return
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
                if (not endpoint._understanding_recovery_active and self._exit_context is None
                        and not self.should_classify_supplement_reply(preview)):
                    endpoint.prepare_stable_preview(preview)
            if early:
                if self.should_classify_supplement_reply(preview):
                    # Only the new authoritative final may route this exchange.
                    # Do not spend a second reception call on its preview.
                    return
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
                try:
                    await self._say(endpoint, "clarify", afterwards="awaiting_reply", recovery=True)
                except Exception as exc:
                    if (getattr(exc, "code", None) == "TURN_DECISION_STALE"
                            or classify_capture_failure(exc, "speech").safety_violation):
                        raise
                    endpoint.trace("supplement_reminder_unavailable", error_type=type(exc).__name__)
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
            if (final is not None and not self.should_classify_supplement_reply(final)
                    and await self._receive_request(endpoint, final, reconcile_complete=True)):
                return
            await self._reply(endpoint, final)
        finally:
            if not endpoint._closed and endpoint.capture.is_open:
                await endpoint.capture.resume_capture()

    async def _listen(self, endpoint: Any, *, allow_answer_review: bool = True,
                      recovery_identity: Any = None) -> None:
        final = (await endpoint.listening_snapshot() if self.semantic_first
                 else await endpoint.capture.transcript_snapshot(resume=True))
        endpoint.assert_current()
        if not self.semantic_first:
            endpoint._observe_final_snapshot(final)
        endpoint.cover_transcript(final)
        if final is None:
            if endpoint._awaiting_fresh_audio:
                return
            if self.boundary and not self._supplement_question_delivered:
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
        if recovery_identity is not None and endpoint._identity(final) == recovery_identity:
            # The notice did not add a candidate reply. Join the existing pure
            # preparation; it still validates current context and permissions.
            handled = await self._semantic_turn(endpoint, final,
                                                supplement_reply=self._supplement_question_delivered)
            if not handled:
                self._remember_boundary(final)
                await self._say(endpoint, "clarify", afterwards="awaiting_reply")
            return
        if self.should_classify_supplement_reply(final):
            if not allow_answer_review:
                endpoint._retry_at = endpoint._last_voice + self.silence_seconds
                return
            # One small decision routes this new utterance. Ordinary dialogue
            # and uncertainty must never fall through to cumulative analysis.
            endpoint._begin_interaction(final.text)
            await self._reply(endpoint, final)
            return
        if (self.semantic_first
                and await self._receive_request(endpoint, final, reconcile_complete=True)):
            return
        if not allow_answer_review:
            endpoint._retry_at = endpoint._last_voice + self.silence_seconds
            return
        if self.semantic_first and await self._semantic_turn(endpoint, final):
            return
        if self._supplement_question_delivered:
            await self._reply(endpoint, final)
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
        if prepare_only and getattr(final, "has_unstable_tail", True):
            return False
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
        if not prepare_only:
            endpoint._begin_interaction(text)
        cache_key = (text, phase, previous)
        if self._reception_cache and self._reception_cache[0] == cache_key:
            decision = self._reception_cache[1]
        else:
            attempt = self._reception_attempts.setdefault(cache_key, {"count": 0, "retryable": True})
            decision = None
            while attempt["count"] < 2 and attempt["retryable"]:
                endpoint.assert_current()
                attempt["count"] += 1
                # An input-driven cancellation still spent this attempt. Keep
                # its unresolved source so an explicit retry can renew it if
                # the same speech survives the next authoritative snapshot.
                self._reception_failure = (cache_key, endpoint.capture)
                try:
                    decision = await endpoint._infer(classify(new_text, phase=phase, preceding_text=previous),
                        timeout=5.5, use_interaction_budget=not prepare_only and not endpoint.preparation_blocked)
                    endpoint.assert_current()
                    break
                except Exception as exc:
                    if classify_capture_failure(exc, "understanding").safety_violation:
                        attempt["retryable"] = False
                        raise
                    if getattr(exc, "code", None) == "TURN_DECISION_STALE":
                        raise
                    attempt["retryable"] = (isinstance(exc, (TimeoutError, asyncio.TimeoutError,
                                                            StructuredResponseValidationError))
                                            or isinstance(exc, ProviderError) and exc.retryable)
                    endpoint.trace("reception_unavailable", error_type=type(exc).__name__)
                    # A preview cannot speak and spends at most one call per
                    # inspection. The authoritative cut may use the remaining
                    # attempt before falling back to complete answer analysis.
                    if prepare_only:
                        break
            if decision is None:
                # Exhaustion never consumes the speech or fabricates `other`.
                # Normal answer analysis remains available on the same input.
                return False
            self._reception_cache = (cache_key, decision)
            if self._reception_failure and self._reception_failure[0] == cache_key:
                self._reception_failure = None
        confidence = minimum_reported_confidence(final.confidence, decision["confidence"])
        if decision["kind"] == "other" or confidence is None or confidence < .75:
            if not prepare_only:
                self._reception_seen_text = text
                self._reception_seen_final = final.model_copy(deep=True)
            return False
        if prepare_only:
            return True
        if decision["kind"] == "stop_interview":
            endpoint.assert_current()
            # Cached preview semantics may only ask a question after the
            # authoritative final and capture checks have succeeded.
            if self._exit_waiting:
                return True
            endpoint.trace("reception_selected", kind="stop_interview", revision=endpoint.revision)
            self._exit_reminded = False
            await self._ask_exit(endpoint, final)
            return True
        return await self._respond_reception(endpoint, final, new_text, decision,
                                             reconcile_complete=reconcile_complete)

    async def _respond_reception(self, endpoint: Any, final: Any, new_text: str,
                                 decision: Any, *, reconcile_complete: bool = False,
                                 reply_prompt_version: str = "") -> bool:
        """Speak a validated dialogue decision without interpreting the answer."""
        endpoint.assert_current()
        text, identity = final.text.strip(), endpoint._identity(final)
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
                                     reply_prompt_version=reply_prompt_version,
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
        endpoint.assert_current()
        start = getattr(endpoint, "start_recovery_preparation", None)
        started = callable(start) and start(final)
        if self._failure_spoken_identity is not None:
            return
        if started:
            # The endpoint owns only bounded, side-effect-free preparation.
            # Selection/playback of this optional notice remains independent.
            self._recovery_review = (endpoint.revision, endpoint.capture, endpoint._identity(final))
        # Spend this one optional notice before I/O. Its failure must neither
        # retry the same notice nor be counted as another understanding call.
        self._failure_spoken_identity = True
        try:
            if not await self._say(endpoint, "reception_unavailable", afterwards="listening", recovery=True):
                self._recovery_review = None
        except Exception as exc:
            # Failed selection or partial delivery is not an ACK. Retain pure
            # preparation, but let normal fresh-input/silence recovery own it.
            self._recovery_review = None
            if getattr(exc, "code", None) == "TURN_DECISION_STALE":
                raise
            endpoint.trace("recovery_speech_unavailable", error_type=type(exc).__name__)

    async def _semantic_turn(self, endpoint: Any, final: Any, *, reception_kind: Any = None,
                             supplement_reply: bool = False) -> bool:
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
        if self.needs_supplement_confirmation(understanding):
            if supplement_reply:
                # This final already came from an uncertain supplement reply.
                # Sending an ordinary answer back through its gate would call
                # _reply recursively and buy another classification attempt.
                return False
            # Obtain the current final again before asking or accepting a reply;
            # the endpoint's gate owns the one supplement exchange per capture.
            await endpoint._propose(final_snapshot=final, prepared_decision=prepared,
                                    recognition_resumed=True)
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
        if getattr(understanding, "intent", None) == "off_topic":
            # Coherent unrelated speech is not a transcription defect or
            # permission to finish. Redirect once, then keep this recording.
            self._semantic_wait_identity = identity
            self._remember_boundary(final)
            self._idle_check_allowed = False
            await endpoint._notify("answer_listening")
            if self._off_topic_redirected:
                self._remember_handled(final)
            elif await self._say(endpoint, "reception_off_topic", afterwards="listening"):
                self._off_topic_redirected = True
                self._remember_handled(final)
            else:
                self._semantic_wait_identity = None
                endpoint._retry_at = endpoint.clock() + 2
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
            if not self._speech_result_current():
                spoken = False
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
        # A preparation started before this question has neither its delivery
        # boundary nor its fresh reply. Do not leave that context-free request
        # running alongside the authoritative clarification exchange.
        if self.content_clarification_active:
            endpoint.preparation.cancel()
        self._playback_started_at = endpoint.clock()
        try:
            spoken = await endpoint._infer(self.speak("answer_clarify", endpoint.assert_current,
                                      focus_quote=target.focus_quote if target else ""), timeout=45)
            if not self._speech_result_current():
                spoken = False
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
        if not self._supplement_question_delivered:
            # A selection, early reply or interrupted question cannot authorize
            # completion. Retain these words and offer the check audibly again.
            self._remember_boundary(final)
            await self._say(endpoint, "check", afterwards="awaiting_reply")
            return
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
        endpoint._begin_interaction(text)
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
                endpoint.capture.classify_supplement_reply(reply, final=final,
                    boundary_final=self._boundary_final, **reply_context), timeout=10,
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
            if not await self._semantic_turn(endpoint, final, supplement_reply=True):
                await self._say(endpoint, "clarify", afterwards="awaiting_reply")
        elif action == "finish" and decision["confidence"] >= 0.75:
            await self._accept_finish(endpoint, final, reply_text=reply, decision=decision)
        elif action in {"continue", "supplement"} and decision["confidence"] >= 0.75:
            self.confirmed = False
            self._confirmed_final = None
            self._remember_boundary(final)
            self._remember_handled(final)
            self._semantic_wait_identity = endpoint._identity(final)
            await self._say(endpoint, "continue", afterwards="listening")
        elif action == "pause" and decision["confidence"] >= 0.75:
            await self.speak("pause", endpoint.assert_current)
        elif action == "stop_interview" and decision["confidence"] >= 0.75:
            # This only asks the separate exit question. Its new, delivered
            # reply and full evidence still authorize any eventual departure.
            await self._ask_exit(endpoint, final)
        elif action in {"finish_topic", "decline_topic"} and decision["confidence"] >= 0.75:
            # These controls need the full domain decision. A small routing
            # result cannot manufacture a completion basis or reuse an answer.
            self.phase = "listening"
            if not await self._semantic_turn(endpoint, final, supplement_reply=True):
                self._remember_boundary(final)
                await self._say(endpoint, "clarify", afterwards="awaiting_reply")
        elif action in {"question_clarification", "repeat", "interview_dialogue", "presence",
                        "audio_problem", "transcript_correction", "wait", "off_topic"} and decision["confidence"] >= 0.75:
            from app.core.prompt.supplement_reply import SUPPLEMENT_REPLY_VERSION
            await self._respond_reception(endpoint, final, reply,
                {**decision, "kind": action}, reply_prompt_version=SUPPLEMENT_REPLY_VERSION)
        else:
            self.confirmed = False
            self._confirmed_final = None
            endpoint._explicit_revision = None
            endpoint._discard_confirmed_preparation()
            endpoint.preparation.cancel()
            # Uncertainty is a normal conversation outcome, not an analysis
            # error. Ask what the candidate wants without spending a full call
            # or interpreting earlier technical words as a new permission.
            self._remember_boundary(final)
            if await self._say(endpoint, "clarify", afterwards="awaiting_reply"):
                self._remember_handled(final)

    async def _accept_finish(self, endpoint: Any, final: Any, *, reply_text: str = "", decision: Any = None) -> None:
        prepared = (self._confirmed_reused_prepared if self._confirmed_final is not None
                    and endpoint._identity(self._confirmed_final) == endpoint._identity(final) else None)
        confirm_prepared = getattr(endpoint.capture, "confirm_prepared_decision", None)
        if (callable(confirm_prepared) and decision is not None and self._prepared_answer is not None
                and self._prepared_answer_final is not None):
            prepared = confirm_prepared(self._prepared_answer, self._prepared_answer_final,
                self._boundary_final, final, reply_text=reply_text, decision=decision,
                question_delivered=self._supplement_question_delivered)
        if self._confirmed_final is None:
            self._confirmation_origin = (self.boundary, self._boundary_final)
        self.confirmed = True
        self.phase = "confirmed"
        self._confirmed_reused_prepared = prepared
        self._confirmed_final = final.model_copy(deep=True)
        self._remember_boundary(final)
        await endpoint._propose(final_snapshot=final, prepared_decision=prepared,
                                recognition_resumed=self.semantic_first)
