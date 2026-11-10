"""Reversible answer endpoint proposals, independent of media/vendor/control I/O."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import inspect
import math
import logging
import time
from weakref import WeakKeyDictionary, WeakSet
from array import array
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from types import SimpleNamespace

from app.core.errors import ApiError
from app.core.interaction_budget import interaction_deadline, remaining_interaction_seconds
from app.core.model_diagnostics import (
    current_model_call_scope, diagnosed_wait_for, model_call_scope, record_model_diagnostic, safe_model_call_context,
)
from app.core.answer_processing_budget import (
    AnswerProcessingBudget, AnswerCorrectionContinuation, answer_processing_budget, require_answer_correction,
)
from app.core.interview_agent_metrics import measure_interview_agent_stage, observe_interview_agent_metric
from app.services.capture_recovery import CaptureFailure, classify_capture_failure
from app.model_gateway.errors import ProviderError
from app.services.speculative_turn_preparation import SpeculativeTurnPreparation

_LOG = logging.getLogger(__name__)
_CONFIRMED_PREPARATION_TIMEOUT = 45.0
_INTERACTION_PREPARATION_TIMEOUT = 20.0
_INTERACTION_RECOVERY_TIMEOUT = 40.0
_MAX_PREPARATION_FAILURES = 2
_SPECULATIVE_PREPARATION_TIMEOUT = 20.0
_SPECULATIVE_SILENCE_SECONDS = 3.0


class InteractionBudgetExpired(asyncio.TimeoutError):
    """The bounded authoritative attempt cannot make another automatic call."""


@dataclass
class _PreparationAttempt:
    budget_epoch: int
    evidence_key: str
    failure_recorded: bool = False
    diagnostic_scope: Any = field(default_factory=current_model_call_scope, repr=False)
    discarded: bool = False


class _SpeculativeModelAllowance(AnswerProcessingBudget):
    """One speculative provider call also debits its exact evidence allowance."""
    def __init__(self, shared: AnswerProcessingBudget, on_consume: Callable[[], None]) -> None:
        super().__init__(limit=1)
        self.shared, self.on_consume = shared, on_consume

    @property
    def correction_budget(self) -> AnswerProcessingBudget:
        return self.shared.correction_budget

    def consume(self) -> bool:
        if self.used >= self.limit or not self.shared.consume():
            return False
        self.used += 1
        self.on_consume()
        return True


class AnswerEndpoint:
    """Audio activity invalidates proposals; only a current final may commit.

    Silence never commits by itself. Model unavailability conservatively keeps
    listening. All callbacks use server evidence, never browser transcripts.
    """
    def __init__(self, *, detector: Any, capture: Any,
                 commit: Callable[[Any, Callable[[], None]], Awaitable[None]],
                 notify: Callable[[str], Awaitable[None]],
                 on_failure: Optional[Callable[[BaseException], Awaitable[None]]] = None,
                 on_snapshot: Optional[Callable[[Any], Awaitable[None]]] = None,
                 confirmation: Any = None,
                 speech_activity: Any = None,
                 trace: Any = None,
                 diagnostic_context: Optional[dict] = None,
                 threshold: float = 0.6, rms_threshold: float = 0.006,
                 min_silence_seconds: float = 0.7,
                 recovery_timeout: float = 15.0, recovery_backoff: float = 0.25,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not 0 < threshold < 1 or not 0 < rms_threshold < 0.2:
            raise ValueError("Invalid endpoint calibration")
        self.detector, self.capture = detector, capture
        self.commit, self.notify = commit, notify
        self.on_failure = on_failure
        self.on_snapshot = on_snapshot
        self.confirmation = confirmation
        self.speech_activity = speech_activity
        self.trace = trace or (lambda *_args, **_kwargs: None)
        self._diagnostic_context = {
            key: (diagnostic_context or {}).get(key) for key in ("interview_id", "turn_id", "capture_id")
        }
        self.threshold, self.rms_threshold = threshold, rms_threshold
        self.min_silence_seconds = max(0.3, min_silence_seconds)
        self.clock = clock
        self.revision = 0
        self._last_voice: Optional[float] = None
        self._tail = bytearray()
        self._frozen_tail: Optional[bytes] = None
        self._probability: Optional[float] = None
        self._prediction_retry_at = 0.0
        self._explicit_revision: Optional[int] = None
        self._blocked_revision: Optional[int] = None
        self._blocked_preview: Any = None
        self._closed = False
        self._started_at = self.clock()
        self._task: Optional[asyncio.Task] = None
        self._proposal_revision: Optional[int] = None
        self._inference_task: Optional[asyncio.Task] = None
        self._inference_scope = None
        self._diagnostic_cancelled_tasks = WeakSet()
        self._detector_warning_sent = False
        self._uncertain_revision: Optional[int] = None
        self._retry_at = 0.0
        self._prepare_failures = 0
        self._preparation_terminal = False
        self._terminal_provider_failure = False
        self._speculative_model_used = False
        self._speculative_preparing = False
        self._speculative_attempt: Optional[_PreparationAttempt] = None
        self._speculative_attempts = WeakKeyDictionary()
        self._understanding_recovery_active = False
        self._manual_retry_notice_pending = False
        self._preparation_budget_text = ""
        self._preparation_budget_epoch = 0
        self._preparation_budget_key = ""
        self._preparation_failures_by_evidence: dict[str, int] = {}
        self._supplement_calls: dict[tuple, int] = {}
        self._preparation_failure_notice_pending = False
        self._last_server_transcript = ""
        self._interaction_text = ""
        self._interaction_deadline = None
        self._interaction_started_at = None
        self._model_budgets: dict[str, AnswerProcessingBudget] = {}
        self._recovery_stage = "answer_understanding"
        self._recovery_reason = "unavailable"
        self._recovery_retryable = True
        self._interaction_budget_exhausted = False
        self._covered_server_transcript = ""
        self._missing_final_attempts = 0
        self._awaiting_fresh_audio = False
        self._missing_final_notice_sent = False
        self._confirmed_preparation: Optional[asyncio.Task] = None
        self._confirmed_preparation_scope = None
        self._confirmed_preparation_identity: Any = None
        self._confirmed_preparation_capture: Any = None
        self._confirmed_preparation_kwargs: Optional[dict] = None
        self._confirmed_preparation_deadline = 0.0
        self._confirmed_preparation_attempt: Optional[_PreparationAttempt] = None
        self._preparation_tasks: set[asyncio.Task] = set()
        self.failure: Optional[CaptureFailure] = None
        self.recovery_attempt = 0
        self.max_recovery_attempts = 3
        self.recovery_timeout = max(0.01, min(15.0, recovery_timeout))
        self.recovery_backoff = max(0.0, min(0.5, recovery_backoff))
        self.preparation = SpeculativeTurnPreparation(self._call_speculative_decision,
                                                      lambda value: (self.capture, self._identity(value)),
                                                      timeout=_SPECULATIVE_PREPARATION_TIMEOUT,
                                                      diagnostic_context=self._diagnostic_context,
                                                      on_done=self._speculative_finished)

    def start(self) -> None:
        self._started_at = self.clock()
        self._task = asyncio.create_task(self._run())

    def candidate_floor_returned(self) -> None:
        """Question playback time is not candidate silence."""
        if self._last_voice is None:
            self._started_at = self.clock()
        else:
            self._last_voice = self.clock()

    def observe_audio(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        self._tail.extend(pcm)
        del self._tail[:-38_400]
        samples = array("h", pcm)
        rms = math.sqrt(sum(x * x for x in samples) / max(1, len(samples))) / 32768
        voiced = self.speech_activity.observe(pcm) if self.speech_activity is not None else rms >= self.rms_threshold
        if voiced:
            self.speech_started(source="audio")
        elif (self._last_voice is not None and self._frozen_tail is None
              and self.clock() - self._last_voice >= 0.25):
            self._frozen_tail = bytes(self._tail)

    def speech_started(self, *, source: str = "client") -> None:
        if self._closed:
            return
        if source in {"audio", "client"}:
            self._refresh_missing_final_origin()
        if source == "transcript":
            # Subtitle hypotheses revoke authority immediately, but are not
            # evidence that the stable input of an owned analysis changed.
            # Keep that pure work within its original bound until a complete
            # snapshot reconciles its identity; it cannot advance this turn.
            self._invalidate_answer_corrections()
        self.revision += 1
        if (self.confirmation and self.confirmation.confirmed) or (
                self._inference_task is not None and not self._inference_task.cancelled()):
            self.trace("input_invalidated", source=source, revision=self.revision,
                       phase=self.confirmation.phase if self.confirmation else "preparing")
        if self.confirmation is not None:
            self.confirmation.voice()
        self._last_voice = self.clock()
        self._frozen_tail = None
        self._probability = None
        self._explicit_revision = None
        self._prediction_retry_at = 0
        if not self._prepare_failures:
            self._retry_at = 0
        if self._inference_task is not None:
            self._mark_task_cancellation(self._inference_task, self._inference_scope,
                                         "input_changed", source=source)
            self._inference_task.cancel()

    def observe_transcript(self, text: str) -> None:
        """Server ASR can revoke a proposal even when acoustic VAD missed speech.

        A hypothesis revokes the current waiter and completion authority, not
        owned pure analysis. Only stable/full evidence can supersede that
        work or select its budget. Repeated subtitles do not restart silence.
        """
        text = text.strip()
        if text and self._covered_server_transcript.startswith(text):
            return
        if text and text != self._last_server_transcript:
            self._last_server_transcript = text
            if self.confirmation is not None:
                self.confirmation.transcript_changed()
            self.speech_started(source="transcript")

    def cover_transcript(self, final: Any) -> None:
        """A current authoritative final covers its preceding partials.

        Advancing this watermark is not new input. A late delivery of that
        same prefix must not cancel the confirmation that used its final.
        """
        if final is not None:
            self._begin_interaction(final.text)
            self._use_preparation_budget(final.text)
            self._reconcile_preparation_evidence(final)
            self._covered_server_transcript = final.text.strip()
            self._last_server_transcript = self._covered_server_transcript

    def _reconcile_preparation_evidence(self, snapshot: Any) -> None:
        """Supersede pure work only with full evidence, never subtitle text."""
        changed = (self.preparation.has_preparation
                   and not self.preparation.matching(snapshot, include_failed=True))
        confirmed_changed = (self._confirmed_preparation is not None
                             and self._identity(snapshot) != self._confirmed_preparation_identity)
        if changed or confirmed_changed:
            self._invalidate_answer_corrections()
        if changed:
            self.preparation.cancel(reason="input_changed", source="stable_evidence")
        if confirmed_changed:
            self._discard_confirmed_preparation(reason="input_changed", source="stable_evidence")

    def request_finish(self) -> None:
        self._interaction_deadline = None
        self._manual_retry_notice_pending = self._understanding_recovery_active
        self._reset_missing_final_budget()
        self._explicit_revision = self.revision
        self._blocked_revision = None
        self._blocked_preview = None
        self._reset_preparation_budget()
        self._invalidate_answer_corrections()
        self._model_budgets.clear()
        if self._last_voice is None:
            self._last_voice = self.clock() - self.min_silence_seconds

    def continue_speaking(self) -> None:
        """An explicit user action renews retries; microphone noise does not."""
        retrying = self._prepare_failures > 0
        self._interaction_deadline = None
        self._manual_retry_notice_pending = self._understanding_recovery_active
        self._reset_missing_final_budget()
        self._reset_preparation_budget()
        self._invalidate_answer_corrections()
        self._model_budgets.clear()
        self.speech_started()
        if self.confirmation is not None:
            # Continuing withdraws the old completion fact, unlike processing
            # retry. An unchanged final must not restore that old permission.
            self.confirmation._confirmation_proof = None
            self.confirmation._confirmed_final = None
            self.confirmation._confirmation_origin = None
            self.confirmation._confirmed_reused_prepared = None
        if self.confirmation is not None and not self.confirmation.speaking:
            if (not getattr(self.confirmation, "_exit_waiting", False)
                    and not (retrying and self.confirmation.phase == "awaiting_reply")):
                self.confirmation.phase = "listening"

    def retry_answer_processing(self) -> bool:
        """Renew failed processing without turning confirmed speech into new input."""
        if self._closed or not self.capture.is_open or not self._understanding_recovery_active:
            return False
        if ((self._inference_task is not None and not self._inference_task.done())
                or (self._confirmed_preparation is not None and not self._confirmed_preparation.done())
                or self._manual_retry_notice_pending):
            return True  # Duplicate clicks never purchase another concurrent request.
        if not self._recovery_retryable:
            return False
        self._interaction_deadline = None
        self._invalidate_answer_corrections()
        self._model_budgets.clear()
        self._reset_preparation_budget()
        self._manual_retry_notice_pending = True
        retry_prompt = getattr(self.confirmation, "retry_pending_prompt", None)
        if callable(retry_prompt):
            retry_prompt(self)
        self._begin_interaction(self._covered_server_transcript or self._preparation_budget_text)
        self._last_voice = self.clock() - self.min_silence_seconds
        return True

    @property
    def preparation_recovery_details(self) -> dict:
        if not self._understanding_recovery_active:
            return {}
        attempts = (getattr(self.confirmation, "_prompt_attempts", 0) if self._recovery_stage == "supplement_prompt"
                    else self._prepare_failures)
        return {"stage": self._recovery_stage, "reason_code": self._recovery_reason,
                "attempt": min(_MAX_PREPARATION_FAILURES, attempts),
                "max_attempts": _MAX_PREPARATION_FAILURES, "retryable": self._recovery_retryable}

    def assert_current(self) -> None:
        if (self._closed or not self.capture.is_open
                or self._proposal_revision != self.revision):
            raise ApiError("TURN_DECISION_STALE", "Candidate input changed; keep listening.", status_code=409)

    async def listening_snapshot(self) -> Any:
        """Publish a corrected prefix, then resume ASR before model work.

        Cover the final before reopening so its own corrected subtitle is not
        mistaken for a new candidate utterance. Later speech still revokes it.
        """
        try:
            final = await self.capture.transcript_snapshot(resume=False)
            self._observe_final_snapshot(final)
            self.assert_current()
            self.cover_transcript(final)
            if final is not None and self.on_snapshot is not None:
                await self.on_snapshot(final)
            return final
        finally:
            if not self._closed and self.capture.is_open:
                await self.capture.resume_capture()

    def _reset_missing_final_budget(self) -> None:
        self._missing_final_attempts = 0
        self._awaiting_fresh_audio = False
        self._missing_final_notice_sent = False

    def _refresh_missing_final_origin(self) -> None:
        origin = getattr(self.capture, "recovery_origin", None)
        if origin is not None and origin != getattr(self, "_missing_final_origin", None):
            self._missing_final_origin = origin
            self._reset_missing_final_budget()

    @property
    def awaiting_transcript(self) -> bool:
        return bool(not self._closed and self.capture.is_open and self._awaiting_fresh_audio)

    async def replay_waiting_notice(self) -> None:
        """Restore a lost control notice without reopening or finalizing audio."""
        if self.awaiting_transcript:
            self._missing_final_notice_sent = True
            await self._notify("transcript_retry_required")

    def _observe_final_snapshot(self, final: Any) -> None:
        """Bound unresolved cuts independently of changing ASR hypotheses.

        Keep the existing recognizer and recorder open. A partial cannot buy
        another replay of the same unresolved audio; actual acoustic speech or
        an explicit retry may renew attempts, but never completion permission.
        """
        if final is not None:
            self._reset_missing_final_budget()
            return
        self._refresh_missing_final_origin()
        self._missing_final_attempts += 1
        if self._missing_final_attempts >= 3:
            self._awaiting_fresh_audio = True

    async def _run(self) -> None:
        try:
            while not self._closed and self.capture.is_open:
                # Stable-preview polling is local, but its ownership guard
                # still reads a lease. Bound that work and long-text copying.
                await asyncio.sleep(0.1 if getattr(self.capture, "supports_stable_preview", False) else 0.05)
                if getattr(self.capture, "recovery_required", False):
                    await self._recover_capture(self.capture.recovery_error, "send")
                    continue
                await self._publish_preparation_failure()
                if self._awaiting_fresh_audio:
                    if not self._missing_final_notice_sent:
                        self._missing_final_notice_sent = True
                        await self._notify("transcript_retry_required")
                    continue
                if self._last_voice is None and self.confirmation is None:
                    continue
                quiet = self.clock() - (self._last_voice if self._last_voice is not None else self._started_at)
                if self.confirmation is not None:
                    if ((self._confirmed_preparation_kwargs or {}).get("completion_confirmed") and (
                            self.confirmation.phase == "listening" or (
                            self.confirmation.speaking
                            and self.confirmation._after_speech == "listening"))):
                        self._discard_confirmed_preparation()
                    # A blocked understanding attempt still owns any selected
                    # recovery playback. Maintain its ACK watchdog so missing
                    # playback completion cannot leave microphone echo gated.
                    if (not self.confirmation.speaking and (
                            self._blocked_revision == self.revision and not self.preparation_blocked
                            and not self.confirmation.idle_check_due(quiet)
                            or self.clock() < self._retry_at)):
                        continue
                    try:
                        if self.preparation_blocked and not self.confirmation.speaking:
                            await self.confirmation.control_step(self, quiet)
                        else:
                            await self.confirmation.step(self, quiet)
                    except ApiError as exc:
                        if exc.code == "TURN_DECISION_STALE":
                            if not self._closed and not self.confirmation.speaking:
                                await self._notify(self._listening_notice())
                        else:
                            await self._recover_capture(exc, "snapshot")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # Intent/service errors leave this capture open and
                        # retry the same reply with a bounded budget.
                        if getattr(self.capture, "recovery_required", False):
                            await self._recover_capture(exc, "snapshot")
                        else:
                            await self._understanding_failed(interaction_expired=isinstance(exc, InteractionBudgetExpired))
                    continue
                if quiet >= 12 and self._uncertain_revision != self.revision:
                    self._uncertain_revision = self.revision
                    await self._notify("endpoint_uncertain")
                if self._blocked_revision == self.revision or self.clock() < self._retry_at:
                    continue
                if quiet < 0.25:
                    continue
                revision = self.revision
                explicit = self._explicit_revision == revision
                if not explicit and self._probability is None:
                    if self.clock() < self._prediction_retry_at:
                        continue
                    tail = self._frozen_tail or bytes(self._tail)
                    probability = None
                    try:
                        with measure_interview_agent_stage("turn_detector_ms"):
                            prediction = await self.detector.predict(tail, sample_rate_hz=16000, language="zh-CN")
                        candidate = prediction.probability
                        if type(candidate) in (int, float) and math.isfinite(candidate) and 0 <= candidate <= 1:
                            probability = float(candidate)
                    except Exception:
                        # Detector failure is uncertainty, never an interview
                        # failure or permission to end an answer by timeout.
                        pass
                    if revision != self.revision:
                        continue
                    self._probability = probability
                    self._prediction_retry_at = self.clock() + 2
                    if self._probability is None:
                        if not self._detector_warning_sent:
                            self._detector_warning_sent = True
                            await self._notify("detector_unavailable")
                        continue
                    if self._detector_warning_sent:
                        self._detector_warning_sent = False
                        await self._notify("answer_detector_ready")
                if not explicit and (self._probability or 0) < self.threshold:
                    continue
                if quiet < self.min_silence_seconds:
                    continue
                self._proposal_revision = revision
                await self._propose()
        except asyncio.CancelledError:
            raise
        finally:
            await self._close_preparations()

    async def _propose_exit(self, final: Any) -> None:
        """Commit a delivered exit handshake without inventing answer semantics."""
        if getattr(self, "_exit_committing", False):
            return
        self._exit_committing = True
        stage = "snapshot"
        recovery_handled = False
        try:
            self.assert_current()
            if final is None or self.confirmation is None:
                return
            context = self.confirmation.exit_context(final)
            if not context or context.get("decision") != "confirm":
                return
            current = await self.capture.transcript_snapshot(resume=False)
            self.assert_current()
            self._observe_final_snapshot(current)
            if current is None or self._identity(current) != self._identity(final):
                self.confirmation.confirmed = False
                self.confirmation._confirmed_final = None
                self.confirmation._exit_context = self.confirmation._exit_context_final = None
                if current is not None:
                    await self.capture.resume_capture()
                    await self.confirmation._ask_exit(self, current, clarify=True)
                return
            prepared = self.capture.prepare_exit_decision(current, exit_confirmation=context)
            self.assert_current()
            self.preparation.cancel()
            self._discard_confirmed_preparation()
            stage = "commit"
            # This local transaction has its own bounded window, independent
            # of the technical answer's expired model budget.
            with measure_interview_agent_stage("decision_commit_ms"):
                await asyncio.wait_for(self.commit(prepared, self.assert_current), timeout=self.recovery_timeout)
        except ApiError as exc:
            self.trace("exit_proposal_rejected", stage=stage, cause_code=exc.code, revision=self.revision)
            if exc.code != "TURN_DECISION_STALE":
                recovery_handled = True
                await self._recover_capture(exc, stage)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.warning("Exit proposal failed: stage=%s error_type=%s", stage, type(exc).__name__)
            recovery_handled = True
            await self._recover_capture(exc, stage)
        finally:
            self._exit_committing = False
            if not recovery_handled and not self._closed and self.capture.is_open:
                await self.capture.resume_capture()

    async def _propose(self, *, final_snapshot: Any = None, prepared_decision: Any = None,
                       recognition_resumed: bool = False) -> None:
        stage = "snapshot"
        recovery_handled = False
        nonclosing = bool(getattr(self.capture, "supports_stable_preview", False))
        needs_resume = final_snapshot is not None or not nonclosing
        preparing = False
        already_finalized = final_snapshot is not None
        try:
            self.assert_current()
            if already_finalized:
                # Spoken completion already obtained this exact server final
                # while recognition was paused. Do not open/finish an empty
                # replacement stream just to read the same confirmed answer.
                final = final_snapshot
            elif nonclosing:
                final = await self.capture.transcript_preview()
                if ((final is None or final.has_unstable_tail)
                        and self._explicit_revision == self._proposal_revision):
                    # An explicit early-finish request is a fallback, not the
                    # normal endpoint policy. It must still work if this
                    # provider has not emitted a usable stable sentence.
                    needs_resume = True
                    final = await self.listening_snapshot()
                    recognition_resumed = True
                    already_finalized = True
            else:
                # Optional provider capability: adapters without stable
                # sentences retain the existing authoritative-final path.
                final = await self.capture.transcript_snapshot()
            self.assert_current()
            if not nonclosing and not already_finalized:
                self._observe_final_snapshot(final)
            if already_finalized:
                self.cover_transcript(final)
            if final is None or (nonclosing and not already_finalized and final.has_unstable_tail):
                if not nonclosing or already_finalized:
                    self._blocked_revision = self.revision
                    await self._notify("transcript_unavailable")
                # Stable sentences can arrive after VAD went quiet. Poll
                # without cutting the stream or blocking this audio revision.
                return
            preview_key = (self.revision, self._identity(final))
            if nonclosing and not already_finalized and preview_key == self._blocked_preview:
                return
            self._use_preparation_budget(final.text)
            if self.preparation_blocked:
                self._blocked_revision = self.revision
                await self._understanding_failed(record_failure=False)
                return
            preparing = True
            if prepared_decision is None and self._completed_confirmed_preparation(final) is None:
                await self._notify("answer_preparing")
                self.assert_current()
            stage = "understanding"
            if nonclosing and not already_finalized:
                observe_interview_agent_metric("stt_preview_prepared", 1)
            prepared = prepared_decision if prepared_decision is not None else await self._prepare_transcript(final)
            self.assert_current()
            if prepared.understanding.problem is not None:
                await self._understanding_failed(record_failure=False)
                return
            await self._understanding_succeeded()
            if (prepared.understanding.suggested_action == "continue_listening"
                    and not (self.confirmation and not self.confirmation.confirmed
                        and self._explicit_revision != self.revision
                        and self.confirmation.needs_supplement_confirmation(
                        prepared.understanding))):
                if nonclosing:
                    self._blocked_preview = preview_key
                else:
                    self._blocked_revision = self.revision
                return
            stage = "snapshot"
            if nonclosing and not already_finalized:
                # Late provider evidence (including quiet speech missed by
                # local VAD) can invalidate preparation without any cut.
                latest = await self.capture.transcript_preview()
                self.assert_current()
                if (latest is None or latest.has_unstable_tail
                        or self._identity(latest) != self._identity(final)):
                    return
            # Only the final commit candidate cuts recognition. Its complete
            # result remains mandatory: sentence timing is not an input ACK.
            needs_resume = True
            confirmed = (final if already_finalized and not recognition_resumed
                         else await self.capture.transcript_snapshot(resume=False))
            self.assert_current()
            if not (already_finalized and not recognition_resumed):
                self._observe_final_snapshot(confirmed)
            if confirmed is None:
                self._blocked_revision = None
                return
            if self._identity(confirmed) != self._identity(final):
                self._blocked_revision = None
                if not nonclosing and not recognition_resumed:
                    return
                observe_interview_agent_metric("stt_preview_final_revised", 1)
                # A corrected final can need new reasoning. Resume recognition
                # during that work, then revalidate a fresh cut before commit.
                stage = "understanding"
                self.cover_transcript(confirmed)
                if self.on_snapshot is not None:
                    await self.on_snapshot(confirmed)
                await self.capture.resume_capture()
                self.assert_current()
                if self.confirmation and confirmed.text != final.text:
                    # A final-only addition can escape VAD and partials. The
                    # old spoken confirmation cannot authorize new words.
                    self.confirmation.confirmed = False
                    self.confirmation._confirmed_final = None
                    self.confirmation.phase = "listening"
                    self.confirmation._semantic_wait_identity = None
                    self._explicit_revision = None
                semantic_first = bool(self.confirmation and self.confirmation.semantic_first
                                      and not self.confirmation.confirmed)
                prepared = await self._prepare_transcript(confirmed, semantic_first=semantic_first)
                self.assert_current()
                if prepared.understanding.problem is not None:
                    await self._understanding_failed(record_failure=False)
                    return
                await self._understanding_succeeded()
                if (prepared.understanding.suggested_action == "continue_listening"
                        and not (self.confirmation and not self.confirmation.confirmed
                            and self._explicit_revision != self.revision
                            and self.confirmation.needs_supplement_confirmation(
                            prepared.understanding))):
                    return
                if (semantic_first and self._explicit_revision != self.revision
                        and prepared.understanding.suggested_action not in {"clarify", "repeat", "pause", "respond_company"}):
                    from app.services.conversation_understanding import ConversationUnderstandingService
                    if (getattr(prepared.understanding, "turn_intent", None) != "stop_interview"
                            and not self.confirmation.needs_supplement_confirmation(prepared.understanding)
                            and not ConversationUnderstandingService.can_complete_without_confirmation(
                                prepared.understanding, confirmed.text)):
                        return
                latest = await self.capture.transcript_snapshot(resume=False)
                self.assert_current()
                self._observe_final_snapshot(latest)
                if latest is None or self._identity(latest) != self._identity(confirmed):
                    return
                confirmed = latest
            if self.confirmation is not None:
                if await self.confirmation.gate_exit(self, prepared, confirmed):
                    return
            elif getattr(prepared.understanding, "turn_intent", None) == "stop_interview":
                # A stream without a spoken handshake cannot grant permission
                # to leave the whole interview, even from a clear first request.
                self._blocked_revision = self.revision
                await self._notify("endpoint_uncertain")
                return
            if self.confirmation is not None and prepared.understanding.suggested_action == "respond_company":
                await self.confirmation.company_answer(self, prepared, confirmed)
                return
            if self.confirmation is not None and prepared.understanding.suggested_action == "clarify":
                await self.confirmation.clarify_answer(self, prepared.understanding, confirmed)
                return
            if self.confirmation is not None:
                if await self.confirmation.gate_answer_completion(self, prepared, confirmed):
                    return
            stage = "commit"
            with measure_interview_agent_stage("decision_commit_ms"):
                with interaction_deadline(self._interaction_deadline):
                    await self.commit(prepared, self.assert_current)
            self._discard_confirmed_preparation()
        except ApiError as exc:
            # An unchanged transcript is insufficient when the frozen turn,
            # ownership or question budget failed the transaction's fence.
            if stage == "commit":
                self._discard_confirmed_preparation()
                if self.confirmation is not None:
                    # Policy, act or turn changes can invalidate composed
                    # reuse even when candidate words did not change.
                    self.confirmation._confirmed_reused_prepared = None
            failure = classify_capture_failure(exc, stage)
            self.trace("proposal_rejected", stage=stage, cause_code=failure.cause_code, revision=self.revision)
            _LOG.info("Answer proposal rejected: stage=%s code=%s", stage, failure.cause_code)
            if exc.code == "TURN_DECISION_STALE":
                observe_interview_agent_metric("turn_decision_cancelled", 1)
            elif stage == "understanding" and not failure.safety_violation:
                await self._understanding_failed(record_failure=False)
            else:
                recovery_handled = True
                await self._recover_capture(exc, stage)
        except asyncio.TimeoutError as exc:
            if stage == "understanding" and not classify_capture_failure(exc, stage).safety_violation:
                await self._understanding_failed(record_failure=False)
            else:
                recovery_handled = True
                await self._recover_capture(exc, stage)
        except asyncio.CancelledError:
            self._closed = True
            raise
        except Exception as exc:
            # No transcript, provider response or exception body is logged.
            _LOG.warning("Answer proposal failed: stage=%s error_type=%s", stage, type(exc).__name__)
            if stage == "understanding" and not classify_capture_failure(exc, stage).safety_violation:
                await self._understanding_failed(record_failure=False)
            else:
                recovery_handled = True
                await self._recover_capture(exc, stage)
        finally:
            if not recovery_handled and not self._closed and self.capture.is_open:
                if needs_resume:
                    try:
                        await asyncio.wait_for(self.capture.resume_capture(), timeout=self.recovery_timeout)
                    except Exception as exc:
                        await self._recover_capture(exc, "resume")
                    else:
                        if not (self.confirmation and self.confirmation.speaking):
                            await self._notify(self._listening_notice())
                elif preparing and not (self.confirmation and self.confirmation.speaking):
                    await self._notify(self._listening_notice())

    def _listening_notice(self) -> str:
        if (self.confirmation and self.confirmation.phase in
                {"awaiting_exit_reply", "exit_classifying", "exit_confirmed"}):
            return "exit_awaiting_reply"
        return ("supplement_awaiting_reply" if self.confirmation and
                self.confirmation.phase in {"awaiting_reply", "classifying"}
                else "answer_listening")

    async def _call_speculative_decision(self, transcript: Any, **kwargs: Any) -> Any:
        if self._speculative_model_used or self._speculative_preparing:
            raise ApiError("TURN_DECISION_STALE", "This capture already used speculative preparation.", status_code=409)
        self._speculative_preparing = True
        key = hashlib.sha256(transcript.text.strip().encode("utf-8")).hexdigest()
        shared = self._model_budgets.setdefault(key, AnswerProcessingBudget(limit=2, defer_corrections=True))
        allowance = _SpeculativeModelAllowance(shared, lambda: setattr(self, "_speculative_model_used", True))
        try:
            with answer_processing_budget(allowance):
                return await self.capture.prepare_decision(transcript, **kwargs)
        finally:
            self._speculative_preparing = False

    def _record_speculative_failure(self, attempt: _PreparationAttempt, problem: Any,
                                    transcript: Any, capture: Any) -> None:
        if (self._closed or capture is not self.capture or not self.preparation.has_preparation
                or getattr(problem, "code", None) == "TURN_DECISION_STALE"):
            return
        terminal = (getattr(problem, "retryable", True) is False
                    and getattr(problem, "reason_code", None) != "call_budget_exhausted")
        if not terminal and (self._interaction_started_at is None
                             or transcript.text.strip() != self._interaction_text):
            # A preview can precede the first authoritative interaction. Its
            # failure is charged when that interaction adopts the same words.
            return
        if getattr(problem, "reason_code", None) == "call_budget_exhausted":
            shared = self._model_budgets.get(attempt.evidence_key)
            if shared is not None and shared.used < shared.limit:
                problem = SimpleNamespace(source="provider", retryable=True, reason_code="call_budget_exhausted")
        self._record_preparation_failure(attempt, problem=problem,
                                         interaction_expired=isinstance(problem, InteractionBudgetExpired))

    def _speculative_finished(self, task: asyncio.Task, transcript: Any) -> None:
        if (task.cancelled() or not self.preparation.owns(task)
                or not self.preparation.matching(transcript, include_failed=True)):
            return
        error = task.exception()
        problem = error if error is not None else task.result().understanding.problem
        if problem is None:
            return
        attempt = self._speculative_attempts.get(task)
        if attempt is None:
            return
        self._record_speculative_failure(attempt, problem, transcript, self.capture)

    async def _call_prepare_decision(self, transcript: Any, **kwargs: Any) -> Any:
        # Stable previews, authoritative calls and schema repair share one
        # mutable allowance. VAD revisions and provider reconnections do not
        # replenish it; exact evidence checks remain separate from this ledger.
        key = hashlib.sha256(transcript.text.strip().encode("utf-8")).hexdigest()
        budget = self._model_budgets.setdefault(key, AnswerProcessingBudget(limit=2, defer_corrections=True))
        with answer_processing_budget(budget):
            return await self.capture.prepare_decision(transcript, **kwargs)

    def _preparation_kwargs(self, transcript: Any, *, semantic_first: bool = False) -> dict:
        kwargs = ({"semantic_first": True} if semantic_first else
                  {"completion_confirmed": True} if self.confirmation and self.confirmation.confirmed else {})
        if kwargs.get("completion_confirmed") and getattr(self.confirmation, "semantic_first", False):
            kwargs["semantic_first"] = True
        for name in ("exit_confirmation", "content_clarification"):
            method = "exit_context" if name == "exit_confirmation" else "content_clarification_context"
            get_context = getattr(self.confirmation, method, None)
            context = get_context(transcript) if callable(get_context) else None
            if context is not None:
                kwargs[name] = context
                kwargs["semantic_first"] = True
        return deepcopy(kwargs)

    def _validate_preparation(self, prepared: Any, transcript: Any, kwargs: dict) -> None:
        validate = getattr(self.capture, "assert_preparation_current", None)
        if callable(validate):
            try:
                validate(prepared, transcript, **kwargs)
            except Exception:
                self.preparation.cancel()
                self._discard_confirmed_preparation()
                raise

    async def _prepare_transcript(self, transcript: Any, *, semantic_first: bool = False) -> Any:
        with measure_interview_agent_stage("understanding_prepare_ms"):
            self._begin_interaction(transcript.text)
            self._use_preparation_budget(transcript.text)
            if self.preparation_blocked:
                self._blocked_revision = self.revision
                raise ApiError("UNDERSTANDING_RETRY_EXHAUSTED", "New server words or an explicit retry are required.",
                               status_code=503)
            kwargs = self._preparation_kwargs(transcript, semantic_first=semantic_first)
            if (kwargs == {"semantic_first": True}
                    and self.preparation.matching(transcript, include_failed=True)):
                preview_failure = None
                preview_scope = self.preparation.diagnostic_scope
                preview_attempt = self._speculative_attempt
                try:
                    prepared = self.preparation.ready(transcript)
                    if prepared is None:
                        prepared = await self._infer(self.preparation.take(transcript, include_failed=True),
                                                     timeout=_SPECULATIVE_PREPARATION_TIMEOUT,
                                                     operation="speculative_wait")
                    self.assert_current()
                    if prepared is not None and prepared.understanding.problem is None:
                        self._validate_preparation(prepared, transcript, kwargs)
                        observe_interview_agent_metric("stt_preview_reused", 1)
                        return prepared
                    preview_failure = prepared.understanding.problem if prepared is not None else None
                except Exception as exc:
                    if (getattr(exc, "code", None) == "TURN_DECISION_STALE"
                            or classify_capture_failure(exc, "understanding").safety_violation):
                        raise
                    preview_failure = exc
                # Charge this exact speculative failure before starting the
                # second provider call. Waiting on it may have spent most of
                # the initial 20s; recovery uses the original 40s bound.
                self.preparation.cancel()
                if preview_failure is not None:
                    if getattr(preview_failure, "reason_code", None) == "call_budget_exhausted":
                        shared = self._model_budgets.get(self._preparation_budget_key)
                        if shared is not None and shared.used < shared.limit:
                            # Speculation intentionally permits only one call;
                            # its local repair limit does not spend the final's
                            # remaining shared allowance.
                            preview_failure = SimpleNamespace(source="provider", retryable=True,
                                                              reason_code="call_budget_exhausted")
                    attempt = preview_attempt or _PreparationAttempt(
                        self._preparation_budget_epoch, self._preparation_budget_key, diagnostic_scope=preview_scope)
                    if not attempt.failure_recorded:
                        self._record_preparation_failure(attempt, problem=preview_failure,
                            interaction_expired=isinstance(preview_failure, InteractionBudgetExpired))
                    await self._publish_preparation_failure()
                    if self.preparation_blocked:
                        raise ApiError("UNDERSTANDING_RETRY_EXHAUSTED", "Preparation recovery is unavailable.", status_code=503)
            task = self._successful_preparation(transcript, kwargs)
            if task is not None:
                return task.result()
            if (self._interaction_deadline <= time.monotonic()
                    and not self._preparation_matches(transcript, kwargs)):
                attempt = _PreparationAttempt(self._preparation_budget_epoch, self._preparation_budget_key)
                try:
                    # Preserve the zero-remaining diagnostic without starting
                    # a provider task or renewing the interaction window.
                    return await self._infer(self._call_prepare_decision(transcript, **kwargs), timeout=45,
                                             preparation_attempt=attempt, operation="prepare_decision")
                except asyncio.TimeoutError as exc:
                    self._record_preparation_failure(attempt, interaction_expired=True)
                    raise
            task = self._ensure_preparation(transcript, kwargs)
            attempt = self._confirmed_preparation_attempt
            remaining = self._confirmed_preparation_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self._record_preparation_failure(attempt, interaction_expired=True)
                self._discard_confirmed_preparation(reason="interaction_timeout", cancel=False)
                raise InteractionBudgetExpired()
            # A sound revokes this waiter and commit eligibility immediately.
            # Keep at most one bounded, side-effect-free model preparation; a
            # fresh complete snapshot must match every field before reuse.
            try:
                prepared = await self._infer(self._wait_confirmed_preparation(task), timeout=remaining,
                                             operation="confirmed_wait" if kwargs.get("completion_confirmed")
                                             else "prepare_decision")
                if prepared.understanding.problem is None:
                    self._validate_preparation(prepared, transcript, kwargs)
                return prepared
            except AnswerCorrectionContinuation as continuation:
                # The rejected provider call has finished. Charge that failure
                # once, then start the one correction with the original 40s
                # recovery deadline rather than the old task's inherited 20s.
                self._record_preparation_failure(attempt, problem=continuation)
                self.assert_current()
                continuation.assert_current()
                if self._confirmed_preparation is task:
                    self._discard_confirmed_preparation()
                with require_answer_correction(continuation):
                    return await self._prepare_transcript(transcript, semantic_first=semantic_first)
            except asyncio.TimeoutError as exc:
                self._record_preparation_failure(attempt, interaction_expired=isinstance(exc, InteractionBudgetExpired))
                if self._confirmed_preparation is task:
                    self._discard_confirmed_preparation(reason="interaction_timeout" if isinstance(exc, InteractionBudgetExpired)
                                                        else "preparation_timeout",
                                                        cancel=self._confirmed_preparation_deadline > time.monotonic())
                raise

    def _preparation_matches(self, transcript: Any, kwargs: dict) -> bool:
        return (self._confirmed_preparation is not None
                and self._confirmed_preparation_capture is self.capture
                and self._confirmed_preparation_identity == self._identity(transcript)
                and self._confirmed_preparation_kwargs == kwargs)

    def _successful_preparation(self, transcript: Any, kwargs: dict) -> Optional[asyncio.Task]:
        task = self._confirmed_preparation
        if (not self._preparation_matches(transcript, kwargs) or not task.done() or task.cancelled()
                or task.exception() is not None or task.result().understanding.problem is not None):
            return None
        self._validate_preparation(task.result(), transcript, kwargs)
        return task

    def _ensure_preparation(self, transcript: Any, kwargs: dict) -> asyncio.Task:
        if not self._preparation_matches(transcript, kwargs):
            self._discard_confirmed_preparation()
        task = self._confirmed_preparation
        if task is not None:
            return task
        # The same endpoint owns one bounded preparation. Context or evidence
        # changes cannot leave speculative and authoritative requests running.
        self.preparation.cancel()
        predecessors = tuple(self._preparation_tasks) + self.preparation.pending_tasks
        attempt = _PreparationAttempt(self._preparation_budget_epoch, self._preparation_budget_key)
        frozen_kwargs = deepcopy(kwargs)
        with interaction_deadline(self._interaction_deadline):
            with model_call_scope(**self._diagnostic_context, stage="answer_preparation",
                                  revision=self.revision, inherit=False) as scope:
                task = asyncio.create_task(self._bounded_confirmed_preparation(
                    transcript.model_copy(deep=True), frozen_kwargs, predecessors))
        self._confirmed_preparation_scope = scope
        attempt.diagnostic_scope = scope
        self._confirmed_preparation = task
        self._confirmed_preparation_identity = self._identity(transcript)
        self._confirmed_preparation_capture = self.capture
        self._confirmed_preparation_kwargs = frozen_kwargs
        self._confirmed_preparation_attempt = attempt
        self._confirmed_preparation_deadline = min(time.monotonic() + _CONFIRMED_PREPARATION_TIMEOUT,
                                                   self._interaction_deadline)
        self._preparation_tasks.add(task)
        task.add_done_callback(lambda done: self._preparation_finished(done, attempt))
        return task

    def start_recovery_preparation(self, final: Any) -> bool:
        """Prepare only; a fresh final and the normal caller still own progress."""
        if (self._closed or not self.capture.is_open or self.preparation_blocked
                or self._proposal_revision != self.revision
                or getattr(final, "type", None) != "transcript.final" or not getattr(final, "is_final", False)):
            return False
        self._begin_interaction(final.text)
        self._use_preparation_budget(final.text)
        kwargs = self._preparation_kwargs(final, semantic_first=True)
        if kwargs == {"semantic_first": True} and self.preparation.matching(final):
            ready = self.preparation.ready(final)
            if ready is not None:
                self._validate_preparation(ready, final, kwargs)
            return True
        if self._successful_preparation(final, kwargs) is not None:
            return True
        if (self._preparation_matches(final, kwargs)
                and self._confirmed_preparation_deadline <= time.monotonic()):
            self._record_preparation_failure(self._confirmed_preparation_attempt, problem=asyncio.TimeoutError(),
                                             interaction_expired=self._interaction_deadline <= time.monotonic())
            self._discard_confirmed_preparation(reason="preparation_timeout", cancel=False)
            if self.preparation_blocked:
                return False
        if self._interaction_deadline <= time.monotonic():
            return False
        budget = self._model_budgets.get(self._preparation_budget_key)
        if (not self._preparation_matches(final, kwargs) and budget is not None
                and budget.used >= budget.limit):
            return False
        self._ensure_preparation(final, kwargs)
        return True

    def _completed_confirmed_preparation(self, transcript: Any) -> Optional[asyncio.Task]:
        task = self._confirmed_preparation
        if (not self.confirmation or not self.confirmation.confirmed
                or getattr(transcript, "type", None) != "transcript.final"
                or not getattr(transcript, "is_final", False)
                or task is None):
            return None
        return self._successful_preparation(transcript, self._preparation_kwargs(transcript))

    async def _bounded_confirmed_preparation(self, transcript: Any, kwargs: dict,
                                             predecessors: tuple = ()) -> Any:
        async def prepare():
            # Cancelling a waiter never starts a second provider call. Even a
            # slow cancellation must release its owned work before recovery.
            if predecessors:
                await asyncio.gather(*(asyncio.shield(task) for task in predecessors), return_exceptions=True)
            return await self._call_prepare_decision(transcript, **kwargs)

        return await diagnosed_wait_for(
            prepare(),
            timeout=min(_CONFIRMED_PREPARATION_TIMEOUT, remaining_interaction_seconds()
                        if remaining_interaction_seconds() is not None else _CONFIRMED_PREPARATION_TIMEOUT),
            stage="answer_preparation", timeout_reason="confirmed_timeout" if kwargs.get("completion_confirmed")
            else "preparation_timeout",
        )

    @staticmethod
    async def _wait_confirmed_preparation(task: asyncio.Task) -> Any:
        return await asyncio.shield(task)

    def _preparation_finished(self, task: asyncio.Task, attempt: _PreparationAttempt) -> None:
        self._preparation_tasks.discard(task)
        # Retrieve failures even when acoustic input already cancelled the
        # waiter. Failed/uncertain results must never become cache entries.
        failed = self._observe_preparation_result(task, attempt)
        if failed and self._confirmed_preparation is task:
            self._discard_confirmed_preparation()

    def _observe_preparation_result(self, task: asyncio.Task, attempt: _PreparationAttempt) -> bool:
        if task.cancelled():
            return True
        error = task.exception()
        if attempt.discarded:
            return True
        failed = error is not None or task.result().understanding.problem is not None
        if failed and not (isinstance(error, ApiError) and error.code == "TURN_DECISION_STALE"):
            self._record_preparation_failure(attempt, problem=(task.result().understanding.problem if error is None else
                error),
                interaction_expired=isinstance(error, InteractionBudgetExpired))
        return failed

    def _discard_confirmed_preparation(self, *, reason: str = "superseded", source: Optional[str] = None,
                                        cancel: bool = True) -> None:
        task, self._confirmed_preparation = self._confirmed_preparation, None
        scope, self._confirmed_preparation_scope = self._confirmed_preparation_scope, None
        self._confirmed_preparation_identity = None
        self._confirmed_preparation_capture = None
        self._confirmed_preparation_kwargs = None
        self._confirmed_preparation_deadline = 0.0
        attempt, self._confirmed_preparation_attempt = self._confirmed_preparation_attempt, None
        if task is not None and not task.done():
            if attempt is not None:
                attempt.discarded = True
            if cancel:
                self._mark_task_cancellation(task, scope, reason, source=source)
                task.cancel()
            elif scope is not None:
                # Its own deadline already began cancellation. A second
                # cancel can detach slow provider cleanup from wait_for.
                scope.cancel(reason)

    async def _close_preparations(self) -> None:
        await self.preparation.close()
        self._discard_confirmed_preparation(reason="capture_closed")
        tasks = tuple(self._preparation_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _infer(self, work: Any, *, timeout: float,
                     preparation_attempt: Optional[_PreparationAttempt] = None,
                     use_interaction_budget: bool = True, operation: str = "infer") -> Any:
        with model_call_scope(**self._diagnostic_context, stage="answer_preparation", revision=self.revision) as scope:
            if preparation_attempt is not None:
                preparation_attempt.diagnostic_scope = scope
            return await self._infer_observed(work, timeout=timeout, preparation_attempt=preparation_attempt,
                                             use_interaction_budget=use_interaction_budget,
                                             operation=operation, scope=scope)

    def _mark_task_cancellation(self, task: Optional[asyncio.Task], scope: Any, reason: str,
                                *, source: Optional[str] = None) -> None:
        if task is None or task.done() or scope is None or task in self._diagnostic_cancelled_tasks:
            return
        self._diagnostic_cancelled_tasks.add(task)
        scope.cancel(reason)
        record_model_diagnostic("scope_cancel_requested", **safe_model_call_context(scope=scope),
                                cancel_reason=scope.cancel_reason, source=source)

    async def _infer_observed(self, work: Any, *, timeout: float,
                              preparation_attempt: Optional[_PreparationAttempt],
                              use_interaction_budget: bool, operation: str, scope: Any) -> Any:
        started = time.monotonic()
        requested_timeout = timeout
        deadline = self._interaction_deadline if use_interaction_budget else time.monotonic() + timeout
        with interaction_deadline(deadline):
            remaining = remaining_interaction_seconds()
            effective_deadline = time.monotonic() + remaining if remaining is not None else None
            if remaining is not None:
                timeout = min(timeout, remaining)
            fields = {**safe_model_call_context(scope=scope), "operation": operation,
                      "requested_timeout_ms": requested_timeout * 1000,
                      "effective_timeout_ms": max(0, timeout) * 1000,
                      "interaction_remaining_ms": remaining * 1000 if remaining is not None else None}
            record_model_diagnostic("scope_started", **fields, outcome="started")
            if timeout <= 0:
                if inspect.iscoroutine(work):
                    work.close()
                elif isinstance(work, asyncio.Future):
                    work.cancel()
                scope.cancel("interaction_timeout" if use_interaction_budget else "local_timeout")
                record_model_diagnostic("scope_finished", **fields, outcome="timeout",
                                        cancel_reason=scope.cancel_reason, elapsed_ms=(time.monotonic() - started) * 1000)
                raise InteractionBudgetExpired() if use_interaction_budget else asyncio.TimeoutError()
            self._inference_task = asyncio.create_task(work)
            self._inference_scope = scope
        if preparation_attempt is not None:
            self._inference_task.add_done_callback(
                lambda done: self._observe_preparation_result(done, preparation_attempt)
            )
        outcome, failure_reason = "success", None
        try:
            # Child revocation differs from whole-worker cancellation.
            done, _ = await asyncio.wait({self._inference_task}, timeout=timeout)
            if not done:
                if use_interaction_budget and effective_deadline is not None and time.monotonic() >= effective_deadline:
                    scope.cancel("interaction_timeout")
                    raise InteractionBudgetExpired()
                scope.cancel("local_timeout")
                raise asyncio.TimeoutError()
            if self._inference_task.cancelled():
                raise ApiError("TURN_DECISION_STALE", "New input cancelled preparation.", status_code=409)
            try:
                result = self._inference_task.result()
                problem = getattr(getattr(result, "understanding", None), "problem", None)
                if problem is not None:
                    outcome = "failed"
                    failure_reason = getattr(problem, "reason_code", None) or getattr(problem, "code", None)
                return result
            except asyncio.TimeoutError:
                if use_interaction_budget and effective_deadline is not None and time.monotonic() >= effective_deadline:
                    raise InteractionBudgetExpired() from None
                raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            if self._closed and scope.cancel_reason == "caller_cancelled":
                scope.cancel("capture_closed")
            raise
        except asyncio.TimeoutError as exc:
            outcome = "timeout"
            if isinstance(exc, InteractionBudgetExpired):
                scope.cancel("interaction_timeout")
            elif scope.cancel_reason == "caller_cancelled":
                # A child can report its own timeout before this waiter has
                # expired. Do not attribute that exception to our deadline.
                scope.cancel("child_timeout")
            raise
        except Exception as exc:
            outcome = "cancelled" if (getattr(exc, "code", None) == "TURN_DECISION_STALE"
                                       and self._inference_task is not None and self._inference_task.cancelled()) else "failed"
            failure_reason = getattr(exc, "code", None)
            raise
        finally:
            if self._inference_task is not None:
                if not self._inference_task.done():
                    self._mark_task_cancellation(self._inference_task, scope, scope.cancel_reason)
                    self._inference_task.cancel()
                    await asyncio.gather(self._inference_task, return_exceptions=True)
                # Cancellation can win just after the child has failed. The
                # finished child still needs its exception consumed by us.
                elif not self._inference_task.cancelled():
                    self._inference_task.exception()
            self._inference_task = None
            self._inference_scope = None
            record_model_diagnostic("scope_finished", **fields, outcome=outcome,
                                    elapsed_ms=(time.monotonic() - started) * 1000,
                                    cancel_reason=scope.cancel_reason if outcome in {"cancelled", "timeout"} else None,
                                    failure_reason=failure_reason)

    def _begin_interaction(self, text: str) -> None:
        text = text.strip()
        if text and (text != self._interaction_text or self._interaction_deadline is None):
            self._interaction_text = text
            self._interaction_started_at = time.monotonic()
            self._interaction_deadline = self._interaction_started_at + _INTERACTION_PREPARATION_TIMEOUT

    def prepare_stable_preview(self, preview: Any) -> None:
        if preview is None or getattr(preview, "has_unstable_tail", True) or not preview.text.strip():
            return
        # Reconcile before the one-speculation/owned-formal-work early exits.
        # New stable evidence must revoke stale work even when it cannot buy
        # another speculative call. A transient tail does not do so.
        self._reconcile_preparation_evidence(preview)
        quiet = self.clock() - (self._last_voice if self._last_voice is not None else self._started_at)
        if (self._confirmed_preparation is not None or self._speculative_model_used
                or self._speculative_preparing or self._terminal_provider_failure
                or quiet < _SPECULATIVE_SILENCE_SECONDS):
            return
        if getattr(self.confirmation, "content_clarification_active", False):
            # Preview preparation cannot carry the delivered-question/fresh-
            # reply proof. The formal final path supplies and validates it.
            self.preparation.cancel()
            return
        if (preview.text.strip() == self._interaction_text and self._interaction_deadline is not None
                and time.monotonic() >= self._interaction_deadline):
            return  # Speculation cannot bypass an expired attempt for these words.
        # Speculation may precede the authoritative final by speech, silence or
        # provider rotation. Its own bound must not spend the final's budget.
        with interaction_deadline(time.monotonic() + _SPECULATIVE_PREPARATION_TIMEOUT):
            task = self.preparation.start(preview)
        if task is not None and task not in self._speculative_attempts:
            attempt = _PreparationAttempt(self._preparation_budget_epoch,
                hashlib.sha256(preview.text.strip().encode("utf-8")).hexdigest(),
                diagnostic_scope=self.preparation.diagnostic_scope)
            self._speculative_attempts[task] = self._speculative_attempt = attempt

    async def _recover_capture(self, exc: BaseException, stage: str) -> None:
        self.failure = classify_capture_failure(exc, stage)
        self._proposal_revision = None
        if not self.failure.retryable:
            await self._notify("capture_retry_required" if self.failure.requires_new_capture else "capture_failed")
            self._closed = True
            return
        for attempt in range(1, self.max_recovery_attempts + 1):
            if self._closed or not self.capture.is_open:
                return
            self.recovery_attempt = attempt
            await self._notify("capture_recovering")
            if self._closed or not self.capture.is_open:
                return
            try:
                # Recovery reconstructs recognition only, never batch-submits
                # an answer and never closes the recorder on a transient fault.
                await asyncio.wait_for(self.capture.recover_capture(), self.recovery_timeout)
                if self._closed or not self.capture.is_open:
                    return
                self._blocked_revision = None
                self._probability = None
                self._prediction_retry_at = 0
                self._retry_at = self.clock() + 0.5
                await self._notify("capture_recovered")
                return
            except asyncio.CancelledError:
                raise
            except Exception as recovery_error:
                self.failure = classify_capture_failure(recovery_error, "resume")
                if not self.failure.retryable:
                    await self._notify("capture_retry_required" if self.failure.requires_new_capture else "capture_failed")
                    self._closed = True
                    return
                if attempt < self.max_recovery_attempts:
                    await asyncio.sleep(self.recovery_backoff * attempt)
        # This is not InterviewSession.paused. The owner seals the incomplete
        # evidence and offers a fenced new capture for the same unanswered turn.
        await self._notify("capture_retry_required")
        self._closed = True

    @staticmethod
    def _identity(final: Any) -> dict:
        # Transport/finality flags differ for a preview and a true final;
        # semantic evidence/provenance fields must match exactly in both.
        return {
            "text": final.text.strip(), "confidence": final.confidence,
            "language": final.language,
            "segments": [item.model_dump(mode="json") for item in final.segments],
            "provider": final.provider.model_dump(mode="json") if final.provider else None,
        }

    def _use_preparation_budget(self, text: str) -> None:
        # Reopening a provider or revising timestamps cannot buy more model
        # calls for the same server words. The exact provenance still fences
        # successful preparation reuse and commit separately.
        text = text.strip()
        if text and text != self._preparation_budget_text:
            self._preparation_budget_text = text
            self._preparation_budget_key = hashlib.sha256(text.encode("utf-8")).hexdigest()
            # Fresh speech may make one new request, but does not replenish
            # the continuous failure episode's automatic retry budget.
            self._preparation_failure_notice_pending = self._preparation_failure_notice_pending or self.preparation_blocked
            self._blocked_revision = None
            self._blocked_preview = None

    @property
    def preparation_blocked(self) -> bool:
        tried = self._preparation_failures_by_evidence.get(self._preparation_budget_key, 0)
        return bool(self._terminal_provider_failure
                    or tried and (self._preparation_terminal or self._prepare_failures >= _MAX_PREPARATION_FAILURES))

    def _reset_preparation_budget(self) -> None:
        self._preparation_budget_epoch += 1
        self._preparation_failures_by_evidence.clear()
        self._supplement_calls.clear()
        self._prepare_failures = 0
        self._preparation_terminal = self._terminal_provider_failure
        self._recovery_retryable = not self._terminal_provider_failure
        self._interaction_budget_exhausted = False
        self._preparation_failure_notice_pending = False
        self._retry_at = 0
        self._blocked_revision = None
        self._blocked_preview = None

    def _start_supplement_attempt(self) -> Optional[_PreparationAttempt]:
        """Debit a same-capture/words call before I/O; cancellation never refunds it."""
        key = (id(self.capture), self._preparation_budget_key)
        used = self._supplement_calls.get(key, 0)
        if used >= _MAX_PREPARATION_FAILURES:
            self._record_preparation_failure(problem=SimpleNamespace(
                source="schema_or_content", reason_code="call_budget_exhausted", retryable=False))
            return None
        self._supplement_calls[key] = used + 1
        return _PreparationAttempt(self._preparation_budget_epoch, self._preparation_budget_key)

    def _record_preparation_failure(self, attempt: Optional[_PreparationAttempt] = None, *, problem: Any = None,
                                    interaction_expired: bool = False) -> None:
        evidence_key = self._preparation_budget_key
        if attempt is not None:
            if attempt.discarded:
                return
            if attempt.failure_recorded:
                if (interaction_expired and attempt.budget_epoch == self._preparation_budget_epoch
                        and attempt.evidence_key == self._preparation_budget_key):
                    self._extend_recovery_or_exhaust()
                    self._preparation_failure_notice_pending = True
                return
            attempt.failure_recorded = True
            if attempt.budget_epoch != self._preparation_budget_epoch:
                return
            evidence_key = attempt.evidence_key
        failures = self._preparation_failures_by_evidence.get(evidence_key, 0) + 1
        self._preparation_failures_by_evidence[evidence_key] = failures
        self._prepare_failures += 1
        self._understanding_recovery_active = True
        self._recovery_stage = "answer_understanding"
        reason = str(getattr(problem, "reason_code", "") or getattr(problem, "code", "")).lower()
        allowed = {"call_budget_exhausted", "provider_timeout", "provider_auth_failed", "provider_billing_required",
                   "provider_rate_limited", "provider_circuit_open", "provider_unavailable"}
        self._recovery_reason = (
            "invalid_result" if (getattr(problem, "source", None) == "schema_or_content"
                                 or isinstance(problem, ProviderError) and problem.code == "provider_schema_invalid") else
            reason if reason in allowed else
            "timeout" if interaction_expired or isinstance(problem, asyncio.TimeoutError) else "unavailable"
        )
        if problem is not None and not getattr(problem, "retryable", reason not in {
                "provider_auth_failed", "provider_authentication_failed", "provider_billing_required",
                "provider_forbidden", "provider_configuration_invalid", "provider_route_missing"}):
            self._preparation_terminal = True
            if reason != "call_budget_exhausted" and (getattr(problem, "source", None) == "provider"
                    or isinstance(problem, ProviderError) or reason in {"provider_auth_failed", "provider_authentication_failed",
                        "provider_billing_required", "provider_forbidden", "provider_configuration_invalid", "provider_route_missing"}):
                self._terminal_provider_failure = True
            # Content rejection stops automatic calls for these words, while
            # an explicit processing retry may renew the same saved evidence.
            # Permanent provider failures retain their separate hard block.
            self._recovery_retryable = (reason == "call_budget_exhausted"
                                       or getattr(problem, "source", None) == "schema_or_content")
            if not self._recovery_retryable and reason not in allowed:
                self._recovery_reason = "non_retryable"
        if evidence_key != self._preparation_budget_key:
            return
        self._extend_recovery_or_exhaust()
        self._retry_at = self.clock() + min(4, self._prepare_failures)
        self._preparation_failure_notice_pending = True
        if self.preparation_blocked:
            self._blocked_revision = self.revision
        diagnostic = safe_model_call_context(scope=attempt.diagnostic_scope if attempt is not None else None)
        self.trace("understanding_retry_budget", failures=self._prepare_failures,
                   exhausted=self.preparation_blocked, revision=self.revision,
                   stage=self._recovery_stage, reason_code=self._recovery_reason,
                   retryable=self._recovery_retryable,
                   diagnostic_scope_id=diagnostic.get("diagnostic_scope_id"),
                   parent_scope_id=diagnostic.get("parent_scope_id"),
                   interaction_remaining_ms=(max(0, self._interaction_deadline - time.monotonic()) * 1000
                                             if self._interaction_deadline is not None else None))

    def _extend_recovery_or_exhaust(self) -> None:
        if self._preparation_terminal:
            return
        if (self._interaction_started_at is not None and self._prepare_failures < _MAX_PREPARATION_FAILURES
                and time.monotonic() < self._interaction_started_at + _INTERACTION_RECOVERY_TIMEOUT):
            self._interaction_deadline = self._interaction_started_at + _INTERACTION_RECOVERY_TIMEOUT
            return
        self._interaction_budget_exhausted = self._preparation_terminal = True
        self._blocked_revision = self.revision

    async def _understanding_succeeded(self) -> None:
        recovering = self._understanding_recovery_active
        self._understanding_recovery_active = False
        self._reset_preparation_budget()
        if self.confirmation is not None:
            self.confirmation._failure_spoken_identity = None
        if recovering:
            await self._notify("understanding_recovered")

    async def _publish_preparation_failure(self) -> None:
        if self._manual_retry_notice_pending and not (self.confirmation and self.confirmation.speaking):
            self._manual_retry_notice_pending = False
            await self._notify("understanding_retrying")
        if self._preparation_failure_notice_pending:
            self._preparation_failure_notice_pending = False
            await self._notify("understanding_retry_exhausted" if self._interaction_budget_exhausted else
                               "understanding_result_rejected" if self._preparation_terminal else
                               "understanding_retry_exhausted" if self._prepare_failures >= _MAX_PREPARATION_FAILURES
                               else "understanding_unavailable")

    async def _understanding_failed(self, *, record_failure: bool = True, interaction_expired: bool = False) -> None:
        if record_failure:
            self._record_preparation_failure(interaction_expired=interaction_expired)
        await self._publish_preparation_failure()

    async def expression_preparation_failed(self) -> None:
        """A selected reply could not become playable within its budget.

        Preserve this capture and stop automatic retries for these words. Only
        new authoritative speech or the existing explicit retry controls may
        start another preparation; failure never grants answer completion.
        """
        retry_prompt = getattr(self.confirmation, "retry_unplayed_expression", None)
        if callable(retry_prompt) and await retry_prompt(self):
            return
        if not self.preparation_blocked:
            self._record_preparation_failure()
        self._preparation_terminal = True
        self._blocked_revision = self.revision
        self._preparation_failure_notice_pending = False
        await self._notify("understanding_retry_exhausted")

    async def _notify(self, reason: str) -> None:
        critical = reason in {"capture_recovering", "capture_recovered", "capture_retry_required", "capture_failed"}
        try:
            await asyncio.wait_for(self.notify(reason), timeout=5 if critical else 2)
        except Exception as exc:
            # A transient UI/event failure must not silently kill the endpoint
            # worker while the microphone continues. Durable truth recovers
            # through the existing snapshot/owner paths.
            _LOG.warning("Answer notice failed: reason=%s error_type=%s", reason, type(exc).__name__)
            if critical:
                # A failed durable transition is not merely a missing UI
                # event. No endpoint may disappear while still accepting PCM.
                self._closed = True
                try:
                    await asyncio.wait_for(self.capture.abort(), timeout=3)
                except Exception as close_error:
                    _LOG.warning("Capture shutdown failed: error_type=%s", type(close_error).__name__)
                if self.on_failure is not None:
                    try:
                        await asyncio.wait_for(self.on_failure(exc), timeout=3)
                    except Exception as state_error:
                        _LOG.warning("Capture failure state unavailable: error_type=%s", type(state_error).__name__)

    def _invalidate_answer_corrections(self) -> None:
        for budget in self._model_budgets.values():
            budget.invalidate_corrections()

    async def close(self) -> None:
        self._closed = True
        self._invalidate_answer_corrections()
        self.revision += 1
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._close_preparations()
        self._tail.clear()
        self._frozen_tail = None
