"""One capture's spoken completion handshake; silence only asks a question."""

from __future__ import annotations

from app.domain.speech_quality import minimum_reported_confidence
from typing import Any
import logging

_LOG = logging.getLogger(__name__)

class SpokenSupplementConfirmation:
    """Keep the answer and its control replies on the same private recording.

    A server final fixes the reply boundary before asking. Only new server
    speech after that boundary may confirm completion. Neither playback,
    elapsed time, nor a preview grants permission to submit an answer.
    """

    def __init__(self, *, speak: Any, silence_seconds: float = 5.0) -> None:
        self.speak = speak
        self.silence_seconds = silence_seconds
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

    @property
    def speaking(self) -> bool:
        return self.phase == "speaking"

    def voice(self) -> None:
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

    async def _say(self, endpoint: Any, kind: str, *, afterwards: str) -> None:
        endpoint.assert_current()
        self.phase = "speaking"
        self._after_speech = afterwards
        self._playback_started_at = endpoint.clock()
        try:
            spoken = await self.speak(kind, endpoint.assert_current)
            if spoken and self.phase == "speaking":
                self._playback_started_at = endpoint.clock()
            if not spoken and self.phase == "speaking":
                self.phase = afterwards
                self.reply_seen = endpoint.revision != endpoint._proposal_revision
                endpoint._last_voice = endpoint.clock()
        except BaseException:
            if self.phase == "speaking":
                self.phase = afterwards
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
        if self.confirmed or endpoint._explicit_revision == endpoint.revision:
            self.confirmed = True
            endpoint._proposal_revision = endpoint.revision
            await endpoint._propose()
            return
        if self.phase == "listening":
            if self._clarification_revision == endpoint.revision:
                return
            if quiet < self.silence_seconds:
                return
            endpoint._proposal_revision = endpoint.revision
            final = await endpoint.capture.transcript_snapshot()
            endpoint.assert_current()
            endpoint.cover_transcript(final)
            if final is None:
                if self.boundary:
                    # After a previous supplement exchange, a segment with no
                    # ASR text must not trap the conversation in endless empty
                    # stream rotations. Ask again using the existing boundary
                    # only as conversation context. The unrecognized audio is
                    # retained/replayed; a new complete final is still required
                    # before classification and commit.
                    self._reminded = False
                    await self._say(endpoint, "check", afterwards="awaiting_reply")
                    return
                endpoint._retry_at = endpoint.clock() + self.silence_seconds
                await endpoint._notify("transcript_unavailable")
                return
            self.boundary = final.text.strip()
            self._clarification_revision = None
            self._boundary_segments = len(final.segments)
            self._reminded = False
            await self._say(endpoint, "check", afterwards="awaiting_reply")
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
            await self._reply(endpoint, final)
        finally:
            if not endpoint._closed and endpoint.capture.is_open:
                await endpoint.capture.resume_capture()

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
        if action == "finish" and decision["confidence"] >= 0.75:
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
