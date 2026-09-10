"""Reversible answer endpoint proposals, independent of media/vendor/control I/O."""

from __future__ import annotations

import asyncio
import hashlib
import math
import logging
import time
from array import array
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from app.core.errors import ApiError
from app.core.interview_agent_metrics import measure_interview_agent_stage, observe_interview_agent_metric
from app.services.capture_recovery import CaptureFailure, classify_capture_failure

_LOG = logging.getLogger(__name__)
_CONFIRMED_PREPARATION_TIMEOUT = 45.0


@dataclass
class _PreparationAttempt:
    budget_epoch: int
    evidence_key: str
    failure_recorded: bool = False


class AnswerEndpoint:
    """Audio activity invalidates proposals; only a current final may commit.

    Silence never commits by itself. Model unavailability conservatively keeps
    listening. All callbacks use server evidence, never browser transcripts.
    """
    def __init__(self, *, detector: Any, capture: Any,
                 commit: Callable[[Any, Callable[[], None]], Awaitable[None]],
                 notify: Callable[[str], Awaitable[None]],
                 on_failure: Optional[Callable[[BaseException], Awaitable[None]]] = None,
                 confirmation: Any = None,
                 speech_activity: Any = None,
                 trace: Any = None,
                 threshold: float = 0.6, rms_threshold: float = 0.006,
                 min_silence_seconds: float = 0.7,
                 recovery_timeout: float = 15.0, recovery_backoff: float = 0.25,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not 0 < threshold < 1 or not 0 < rms_threshold < 0.2:
            raise ValueError("Invalid endpoint calibration")
        self.detector, self.capture = detector, capture
        self.commit, self.notify = commit, notify
        self.on_failure = on_failure
        self.confirmation = confirmation
        self.speech_activity = speech_activity
        self.trace = trace or (lambda *_args, **_kwargs: None)
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
        self._task: Optional[asyncio.Task] = None
        self._proposal_revision: Optional[int] = None
        self._inference_task: Optional[asyncio.Task] = None
        self._detector_warning_sent = False
        self._uncertain_revision: Optional[int] = None
        self._retry_at = 0.0
        self._prepare_failures = 0
        self._preparation_budget_text = ""
        self._preparation_budget_epoch = 0
        self._preparation_budget_key = ""
        self._preparation_failures_by_evidence: dict[str, int] = {}
        self._preparation_failure_notice_pending = False
        self._last_server_transcript = ""
        self._covered_server_transcript = ""
        self._confirmed_preparation: Optional[asyncio.Task] = None
        self._confirmed_preparation_identity: Any = None
        self._confirmed_preparation_capture: Any = None
        self._confirmed_preparation_deadline = 0.0
        self._confirmed_preparation_attempt: Optional[_PreparationAttempt] = None
        self._preparation_tasks: set[asyncio.Task] = set()
        self.failure: Optional[CaptureFailure] = None
        self.recovery_attempt = 0
        self.max_recovery_attempts = 3
        self.recovery_timeout = max(0.01, min(15.0, recovery_timeout))
        self.recovery_backoff = max(0.0, min(0.5, recovery_backoff))

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

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
        if source == "transcript":
            self._discard_confirmed_preparation()
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
            self._inference_task.cancel()

    def observe_transcript(self, text: str) -> None:
        """Server ASR can revoke a proposal even when acoustic VAD missed speech.

        A hypothesis is only an invalidation signal here, never answer evidence
        or completion approval. Repeated cumulative subtitles do not restart
        the silence clock.
        """
        text = text.strip()
        if text and self._covered_server_transcript.startswith(text):
            return
        if text and text != self._last_server_transcript:
            self._last_server_transcript = text
            self._use_preparation_budget(text)
            self.speech_started(source="transcript")

    def cover_transcript(self, final: Any) -> None:
        """A current authoritative final covers its preceding partials.

        Advancing this watermark is not new input. A late delivery of that
        same prefix must not cancel the confirmation that used its final.
        """
        if final is not None:
            self._use_preparation_budget(final.text)
            if (self._confirmed_preparation is not None
                    and self._identity(final) != self._confirmed_preparation_identity):
                self._discard_confirmed_preparation()
            self._covered_server_transcript = final.text.strip()
            self._last_server_transcript = self._covered_server_transcript

    def request_finish(self) -> None:
        self._explicit_revision = self.revision
        self._blocked_revision = None
        self._blocked_preview = None
        self._reset_preparation_budget()
        if self._last_voice is None:
            self._last_voice = self.clock() - self.min_silence_seconds

    def continue_speaking(self) -> None:
        """An explicit user action renews retries; microphone noise does not."""
        retrying = self._prepare_failures > 0
        self._reset_preparation_budget()
        self.speech_started()
        if self.confirmation is not None and not self.confirmation.speaking:
            if not (retrying and self.confirmation.phase == "awaiting_reply"):
                self.confirmation.phase = "listening"

    def assert_current(self) -> None:
        if (self._closed or not self.capture.is_open
                or self._proposal_revision != self.revision):
            raise ApiError("TURN_DECISION_STALE", "Candidate input changed; keep listening.", status_code=409)

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
                if self._last_voice is None:
                    continue
                quiet = self.clock() - self._last_voice
                if self.confirmation is not None:
                    if (self.confirmation.phase == "listening" or (
                            self.confirmation.speaking
                            and self.confirmation._after_speech == "listening")):
                        self._discard_confirmed_preparation()
                    if self._blocked_revision == self.revision or self.clock() < self._retry_at:
                        continue
                    try:
                        await self.confirmation.step(self, quiet)
                    except ApiError as exc:
                        if exc.code != "TURN_DECISION_STALE":
                            await self._recover_capture(exc, "snapshot")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # Intent/service errors leave this capture open and
                        # retry the same reply with a bounded budget.
                        if getattr(self.capture, "recovery_required", False):
                            await self._recover_capture(exc, "snapshot")
                        else:
                            await self._understanding_failed()
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

    async def _propose(self, *, final_snapshot: Any = None) -> None:
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
                    final = await self.capture.transcript_snapshot(resume=False)
                    already_finalized = True
            else:
                # Optional provider capability: adapters without stable
                # sentences retain the existing authoritative-final path.
                final = await self.capture.transcript_snapshot()
            self.assert_current()
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
            if self._prepare_failures >= 3:
                self._blocked_revision = self.revision
                await self._understanding_failed(record_failure=False)
                return
            preparing = True
            if self._completed_confirmed_preparation(final) is None:
                await self._notify("answer_preparing")
                self.assert_current()
            stage = "understanding"
            if nonclosing and not already_finalized:
                observe_interview_agent_metric("stt_preview_prepared", 1)
            prepared = await self._prepare_transcript(final)
            self.assert_current()
            if prepared.understanding.problem is not None:
                await self._understanding_failed(record_failure=False)
                return
            if prepared.understanding.suggested_action == "continue_listening":
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
            confirmed = final if already_finalized else await self.capture.transcript_snapshot(resume=False)
            self.assert_current()
            if confirmed is None:
                self._blocked_revision = None
                return
            if self._identity(confirmed) != self._identity(final):
                self._blocked_revision = None
                if not nonclosing:
                    return
                observe_interview_agent_metric("stt_preview_final_revised", 1)
                # Reconcile a revised final on this same cut. Do not reopen
                # only to finish an empty replacement task for the same text.
                stage = "understanding"
                prepared = await self._prepare_transcript(confirmed)
                self.assert_current()
                if prepared.understanding.problem is not None:
                    await self._understanding_failed(record_failure=False)
                    return
                if prepared.understanding.suggested_action == "continue_listening":
                    return
            if self.confirmation is not None and prepared.understanding.suggested_action == "clarify":
                await self.confirmation.clarify_answer(self, prepared.understanding, confirmed)
                return
            stage = "commit"
            with measure_interview_agent_stage("decision_commit_ms"):
                await self.commit(prepared, self.assert_current)
            self._discard_confirmed_preparation()
        except ApiError as exc:
            # An unchanged transcript is insufficient when the frozen turn,
            # ownership or question budget failed the transaction's fence.
            if stage == "commit":
                self._discard_confirmed_preparation()
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
        return ("supplement_awaiting_reply" if self.confirmation and
                self.confirmation.phase in {"awaiting_reply", "classifying"} else "answer_listening")

    async def _prepare_transcript(self, transcript: Any) -> Any:
        with measure_interview_agent_stage("understanding_prepare_ms"):
            self._use_preparation_budget(transcript.text)
            if self._prepare_failures >= 3:
                raise ApiError("UNDERSTANDING_RETRY_EXHAUSTED", "New server words or an explicit retry are required.",
                               status_code=503)
            kwargs = {"completion_confirmed": True} if self.confirmation and self.confirmation.confirmed else {}
            if not kwargs or (getattr(transcript, "type", None) != "transcript.final"
                              or not getattr(transcript, "is_final", False)):
                self._discard_confirmed_preparation()
                attempt = _PreparationAttempt(self._preparation_budget_epoch, self._preparation_budget_key)
                try:
                    return await self._infer(self.capture.prepare_decision(transcript, **kwargs), timeout=45,
                                             preparation_attempt=attempt)
                except asyncio.TimeoutError:
                    self._record_preparation_failure(attempt)
                    raise
            identity = self._identity(transcript)
            if (self._confirmed_preparation_identity != identity
                    or self._confirmed_preparation_capture is not self.capture):
                self._discard_confirmed_preparation()
            task = self._confirmed_preparation
            if self._completed_confirmed_preparation(transcript) is not None:
                # The deadline bounds inference, not the lifetime of its
                # successful result. Reading ready work must not introduce a
                # cancellable waiter; the caller still fences this fresh final.
                return task.result()
            if task is None:
                attempt = _PreparationAttempt(self._preparation_budget_epoch, self._preparation_budget_key)
                task = asyncio.create_task(self._bounded_confirmed_preparation(transcript))
                self._confirmed_preparation = task
                self._confirmed_preparation_identity = identity
                self._confirmed_preparation_capture = self.capture
                self._confirmed_preparation_attempt = attempt
                self._confirmed_preparation_deadline = (
                    asyncio.get_running_loop().time() + _CONFIRMED_PREPARATION_TIMEOUT
                )
                self._preparation_tasks.add(task)
                task.add_done_callback(lambda done: self._preparation_finished(done, attempt))
            attempt = self._confirmed_preparation_attempt
            remaining = self._confirmed_preparation_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self._record_preparation_failure(attempt)
                self._discard_confirmed_preparation()
                raise asyncio.TimeoutError()
            # A sound revokes this waiter and commit eligibility immediately.
            # Keep at most one bounded, side-effect-free model preparation; a
            # fresh complete snapshot must match every field before reuse.
            try:
                return await self._infer(self._wait_confirmed_preparation(task), timeout=remaining)
            except asyncio.TimeoutError:
                self._record_preparation_failure(attempt)
                raise

    def _completed_confirmed_preparation(self, transcript: Any) -> Optional[asyncio.Task]:
        task = self._confirmed_preparation
        if (not self.confirmation or not self.confirmation.confirmed
                or getattr(transcript, "type", None) != "transcript.final"
                or not getattr(transcript, "is_final", False)
                or self._confirmed_preparation_capture is not self.capture
                or self._confirmed_preparation_identity != self._identity(transcript)
                or task is None or not task.done() or task.cancelled()
                or task.exception() is not None
                or task.result().understanding.problem is not None):
            return None
        return task

    async def _bounded_confirmed_preparation(self, transcript: Any) -> Any:
        return await asyncio.wait_for(
            self.capture.prepare_decision(transcript, completion_confirmed=True),
            timeout=_CONFIRMED_PREPARATION_TIMEOUT,
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
        failed = error is not None or task.result().understanding.problem is not None
        if failed and not (isinstance(error, ApiError) and error.code == "TURN_DECISION_STALE"):
            self._record_preparation_failure(attempt)
        return failed

    def _discard_confirmed_preparation(self) -> None:
        task, self._confirmed_preparation = self._confirmed_preparation, None
        self._confirmed_preparation_identity = None
        self._confirmed_preparation_capture = None
        self._confirmed_preparation_deadline = 0.0
        self._confirmed_preparation_attempt = None
        if task is not None and not task.done():
            task.cancel()

    async def _close_preparations(self) -> None:
        self._discard_confirmed_preparation()
        tasks = tuple(self._preparation_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _infer(self, work: Any, *, timeout: float,
                     preparation_attempt: Optional[_PreparationAttempt] = None) -> Any:
        self._inference_task = asyncio.create_task(work)
        if preparation_attempt is not None:
            self._inference_task.add_done_callback(
                lambda done: self._observe_preparation_result(done, preparation_attempt)
            )
        try:
            # Child revocation differs from whole-worker cancellation.
            done, _ = await asyncio.wait({self._inference_task}, timeout=timeout)
            if not done:
                raise asyncio.TimeoutError()
            if self._inference_task.cancelled():
                raise ApiError("TURN_DECISION_STALE", "New input cancelled preparation.", status_code=409)
            return self._inference_task.result()
        finally:
            if self._inference_task is not None and not self._inference_task.done():
                self._inference_task.cancel()
                await asyncio.gather(self._inference_task, return_exceptions=True)
            self._inference_task = None

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
            self._prepare_failures = self._preparation_failures_by_evidence.get(self._preparation_budget_key, 0)
            self._preparation_failure_notice_pending = self._prepare_failures >= 3
            self._retry_at = 0
            self._blocked_revision = None
            self._blocked_preview = None

    def _reset_preparation_budget(self) -> None:
        self._preparation_budget_epoch += 1
        self._preparation_failures_by_evidence.clear()
        self._prepare_failures = 0
        self._preparation_failure_notice_pending = False
        self._retry_at = 0
        self._blocked_revision = None
        self._blocked_preview = None

    def _record_preparation_failure(self, attempt: Optional[_PreparationAttempt] = None) -> None:
        evidence_key = self._preparation_budget_key
        if attempt is not None:
            if attempt.failure_recorded:
                return
            attempt.failure_recorded = True
            if attempt.budget_epoch != self._preparation_budget_epoch:
                return
            evidence_key = attempt.evidence_key
        failures = self._preparation_failures_by_evidence.get(evidence_key, 0) + 1
        self._preparation_failures_by_evidence[evidence_key] = failures
        if evidence_key != self._preparation_budget_key:
            return
        self._prepare_failures = failures
        self._retry_at = self.clock() + min(4, self._prepare_failures)
        self._preparation_failure_notice_pending = True
        if self._prepare_failures >= 3:
            self._blocked_revision = self.revision
        self.trace("understanding_retry_budget", failures=self._prepare_failures,
                   exhausted=self._prepare_failures >= 3, revision=self.revision)

    async def _publish_preparation_failure(self) -> None:
        if self._preparation_failure_notice_pending:
            self._preparation_failure_notice_pending = False
            await self._notify("understanding_retry_exhausted" if self._prepare_failures >= 3
                               else "understanding_unavailable")

    async def _understanding_failed(self, *, record_failure: bool = True) -> None:
        if record_failure:
            self._record_preparation_failure()
        await self._publish_preparation_failure()

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

    async def close(self) -> None:
        self._closed = True
        self.revision += 1
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._close_preparations()
        self._tail.clear()
        self._frozen_tail = None
