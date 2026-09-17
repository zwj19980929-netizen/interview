"""Semantic-first turn taking with a bounded spoken fallback handshake."""

from __future__ import annotations

from app.domain.speech_quality import minimum_reported_confidence
from app.services.conversation_understanding import ConversationUnderstandingService
from app.services.capture_recovery import classify_capture_failure
from typing import Any
import logging

_LOG = logging.getLogger(__name__)

class SpokenSupplementConfirmation:
    """Keep the answer and its control replies on the same private recording.

    A server final fixes the reply boundary before asking. Only new server
    speech after that boundary may confirm completion. Neither playback,
    elapsed time, nor a preview grants permission to submit an answer.
    """

    def __init__(self, *, speak: Any, silence_seconds: float = 5.0, semantic_first: bool = True,
                 respond_company: Any = None) -> None:
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
        self._boundary_segments = 0
        self._playback_started_at = 0.0
        self._missing_reply_since = None
        self._confirmed_final = None
        self._clarification_revision = None
        self._semantic_wait_identity = None
        self._reception_cache = None
        self._reception_seen_text = ""
        self._failure_spoken_identity = None

    @property
    def speaking(self) -> bool:
        return self.phase == "speaking"

    def voice(self) -> None:
        if self.phase == "pause_requested":
            self.phase = "listening"
        if self.confirmed:
            self.confirmed = False
            # Revoke the prepared transaction, not the question we already
            # asked. "No, next please" is another reply in this same exchange.
            self.phase = "awaiting_reply"
        if self.phase in {"awaiting_reply", "classifying"}:
            self.reply_seen = True

    def floor_returned(self, endpoint: Any) -> None:
        if self.phase != "speaking":
            return
        self.phase = self._after_speech
        self.reply_seen = False
        self._missing_reply_since = None
        endpoint._last_voice = endpoint.clock()
        endpoint._blocked_revision = None

    async def _say(self, endpoint: Any, kind: str, *, afterwards: str) -> bool:
        endpoint.assert_current()
        self.phase = "speaking"
        self._after_speech = afterwards
        self._playback_started_at = endpoint.clock()
        try:
            spoken = await self.speak(kind, endpoint.assert_current)
            if spoken and self.phase == "speaking":
                self._playback_started_at = endpoint.clock()
            if not spoken and self.phase == "speaking":
                self.phase = "listening" if afterwards == "pause_requested" else afterwards
                self.reply_seen = endpoint.revision != endpoint._proposal_revision
                endpoint._last_voice = endpoint.clock()
            return bool(spoken)
        except BaseException:
            if self.phase == "speaking":
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
                if (self._semantic_wait_identity is not None and preview is not None
                        and not getattr(preview, "has_unstable_tail", True)
                        and preview.text.strip() == self.boundary):
                    # Waiting on the same words must leave the next healthy
                    # recognition stream open, including long quiet pauses.
                    return
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
        # Keep this final's recognition cut until intent and answer preparation
        # finish. PCM intake/recording continues; new speech still revokes the
        # input revision and the capture's pending-audio fence.
        final = await endpoint.capture.transcript_snapshot(resume=False)
        try:
            endpoint.assert_current()
            if final is not None and await self._receive_request(endpoint, final):
                return
            await self._reply(endpoint, final)
        finally:
            if not endpoint._closed and endpoint.capture.is_open:
                await endpoint.capture.resume_capture()

    async def _listen(self, endpoint: Any, *, allow_answer_review: bool = True) -> None:
        final = await endpoint.capture.transcript_snapshot(resume=not self.semantic_first)
        endpoint.assert_current()
        endpoint.cover_transcript(final)
        if final is None:
            if self.boundary:
                # Existing boundary is conversational context only. Missing
                # audio/text never grants completion of an earlier answer.
                self._reminded = False
                await self._say(endpoint, "check", afterwards="awaiting_reply")
                return
            endpoint._retry_at = endpoint.clock() + self.silence_seconds
            await endpoint._notify("transcript_unavailable")
            return
        if self.semantic_first and await self._receive_request(endpoint, final):
            return
        if not allow_answer_review:
            endpoint._retry_at = endpoint._last_voice + self.silence_seconds
            return
        if self.semantic_first and await self._semantic_turn(endpoint, final):
            return
        self.boundary = final.text.strip()
        self._clarification_revision = None
        self._boundary_segments = len(final.segments)
        self._reminded = False
        await self._say(endpoint, "check", afterwards="awaiting_reply")

    async def _receive_request(self, endpoint: Any, final: Any, *, prepare_only: bool = False) -> bool:
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
        cache_key = (text, phase)
        if self._reception_cache and self._reception_cache[0] == cache_key:
            decision = self._reception_cache[1]
        else:
            previous = self._reception_seen_text
            new_text = text[len(previous):].strip() if previous and text.startswith(previous) else text
            if not new_text:
                return False
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
            self._reception_seen_text = text
        confidence = minimum_reported_confidence(final.confidence, decision["confidence"])
        if decision["kind"] == "other" or confidence is None or confidence < .75:
            return False
        if prepare_only:
            return True
        kind = decision["kind"]
        endpoint.trace("reception_selected", kind=kind, revision=endpoint.revision)
        self.confirmed = False
        self._confirmed_final = None
        endpoint._explicit_revision = None
        endpoint._discard_confirmed_preparation()
        await endpoint._notify("answer_listening")
        endpoint.assert_current()
        spoken = await self._say(endpoint, "reception_" + kind,
                                 afterwards="pause_requested" if kind == "pause" else "listening")
        if spoken:
            self._semantic_wait_identity = identity
            self.boundary, self._boundary_segments = text, len(final.segments)
        else:
            endpoint._retry_at = endpoint.clock() + 2
        return True

    async def _recover_interaction(self, endpoint: Any, final: Any) -> None:
        identity = endpoint._identity(final)
        if self._failure_spoken_identity == identity:
            return
        endpoint.assert_current()
        if await self._say(endpoint, "reception_unavailable", afterwards="listening"):
            self._failure_spoken_identity = identity

    async def _semantic_turn(self, endpoint: Any, final: Any) -> bool:
        """Understand before asking; this never fabricates a spoken reply."""
        if getattr(final, "type", None) != "transcript.final" or not getattr(final, "is_final", False):
            raise ValueError("Semantic completion requires a complete server final")
        identity = endpoint._identity(final)
        if self._semantic_wait_identity == identity:
            endpoint._blocked_revision = endpoint.revision
            return True
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
            await self._recover_interaction(endpoint, final)
            return True
        endpoint.assert_current()
        understanding = prepared.understanding
        if understanding.problem is not None:
            await endpoint._understanding_failed(record_failure=False)
            await self._recover_interaction(endpoint, final)
            return True
        if understanding.suggested_action == "respond_company":
            await self.company_answer(endpoint, prepared, final)
            return True
        if ConversationUnderstandingService.can_complete_without_confirmation(understanding, final.text):
            endpoint.trace("semantic_completion_selected", intent=understanding.turn_intent,
                           revision=endpoint.revision, text=final.text)
            # Reuse the prepared result against this exact paused final. The
            # evidence layer rechecks owner/capture/audio/context at commit.
            await endpoint._propose(final_snapshot=final, prepared_decision=prepared)
            return True
        if understanding.suggested_action == "continue_listening":
            self._semantic_wait_identity = identity
            self.boundary, self._boundary_segments = final.text.strip(), len(final.segments)
            await endpoint._notify("answer_listening")
            if not await self._say(endpoint, "reception_wait" if understanding.turn_intent == "thinking" else "reception_continue",
                                   afterwards="listening"):
                self._semantic_wait_identity = None
                endpoint._retry_at = endpoint.clock() + 2
            return True
        if understanding.suggested_action in {"clarify", "repeat", "pause"}:
            await endpoint._propose(final_snapshot=final, prepared_decision=prepared)
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
        self.boundary, self._boundary_segments = final.text.strip(), len(final.segments)
        self.phase, self._after_speech = "preparing_company", "listening"
        self._playback_started_at = endpoint.clock()
        try:
            # Extraction has its own 8s budget; retain the existing expression
            # service's synthesis budget instead of cutting both off at 15s.
            spoken = await endpoint._infer(self.respond_company(prepared, final, endpoint.assert_current), timeout=45)
            endpoint.assert_current()
            self._semantic_wait_identity = identity if spoken else None
            self._clarification_revision = endpoint.revision
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
        self.boundary = final.text.strip()
        self._boundary_segments = len(final.segments)
        self._clarification_revision = endpoint.revision
        endpoint._explicit_revision = None
        endpoint._discard_confirmed_preparation()
        self.phase, self._after_speech = "speaking", "listening"
        self._playback_started_at = endpoint.clock()
        try:
            spoken = await self.speak("answer_clarify", endpoint.assert_current,
                                      focus_quote=target.focus_quote if target else "")
            if not spoken and self.phase == "speaking":
                self.phase = "listening"
        except BaseException:
            self.phase = "listening"
            raise

    async def _reply(self, endpoint: Any, final: Any) -> None:
        endpoint.assert_current()
        endpoint.cover_transcript(final)
        if final is None:
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
        text = final.text.strip()
        if not text.startswith(self.boundary):
            # A revised old prefix is not a fresh answer to our question.
            self.boundary = text
            self._boundary_segments = len(final.segments)
            await self._say(endpoint, "clarify", afterwards="awaiting_reply")
            return
        reply = text[len(self.boundary):].strip()
        if not reply:
            self.reply_seen = False
            if (self._confirmed_final is not None
                    and endpoint._identity(final) == endpoint._identity(self._confirmed_final)):
                # Only a fresh complete server snapshot can certify that the
                # acoustic interruption added no words. Never reuse on None.
                await self._accept_finish(endpoint, final)
            return
        segments = final.segments[self._boundary_segments:]
        confidence = minimum_reported_confidence(final.confidence, *(s.confidence for s in segments))
        if confidence is not None and confidence < 0.65:
            self.boundary = text
            self._boundary_segments = len(final.segments)
            await self._say(endpoint, "clarify", afterwards="awaiting_reply")
            return
        self.phase = "classifying"
        # Bind classification failures to the same server words as answer
        # preparation. Acoustic revisions alone cannot buy new model calls.
        endpoint._use_preparation_budget(text)
        if endpoint._prepare_failures >= 3:
            self.phase = "awaiting_reply"
            endpoint._blocked_revision = endpoint.revision
            await endpoint._understanding_failed(record_failure=False)
            return
        budget_key = endpoint._preparation_budget_key
        budget_epoch = endpoint._preparation_budget_epoch
        try:
            decision = await endpoint._infer(
                endpoint.capture.classify_supplement_reply(reply), timeout=10,
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
            self.boundary = text
            self._boundary_segments = len(final.segments)
            await self._say(endpoint, "continue", afterwards="listening")
        elif action == "pause" and decision["confidence"] >= 0.75:
            await self.speak("pause", endpoint.assert_current)
        else:
            self._confirmed_final = None
            self.boundary = text
            self._boundary_segments = len(final.segments)
            await self._say(endpoint, "clarify", afterwards="awaiting_reply")

    async def _accept_finish(self, endpoint: Any, final: Any) -> None:
        self.confirmed = True
        self.phase = "confirmed"
        self._confirmed_final = final.model_copy(deep=True)
        self.boundary = final.text.strip()
        self._boundary_segments = len(final.segments)
        await endpoint._propose(final_snapshot=final)
