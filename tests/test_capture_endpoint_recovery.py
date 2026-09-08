"""014: exercise the real endpoint with synthetic, in-memory capture failures."""

import asyncio
import logging
from collections import deque
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ProviderMeta, StreamingSTTEvent, TranscriptSegment
from app.services.answer_endpoint import AnswerEndpoint


def final(text="合成回答。"):
    return StreamingSTTEvent(
        stream_id="synthetic_capture", sequence=2, type="transcript.final",
        is_final=True, text=text, confidence=0.9,
        segments=[TranscriptSegment(text=text, start_ms=0, end_ms=1000, confidence=0.9)],
        provider=ProviderMeta(provider_id="synthetic", model="synthetic-stt", request_id="synthetic-request", latency_ms=1),
    )


async def until(predicate, timeout=2):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), timeout=timeout)


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


class Detector:
    def __init__(self, probability=0.9):
        self.calls = 0
        self.probability = probability

    async def predict(self, _pcm, **_kwargs):
        self.calls += 1
        return SimpleNamespace(probability=self.probability)


class Capture:
    def __init__(self):
        self.is_open = True
        self.current_final = final()
        self.snapshots = []
        self.snapshot_outcomes = deque()
        self.prepares = []
        self.prepare_error = None
        self.resumes = 0
        self.resume_errors = deque()
        self.recovery_required = False
        self.recovery_error = None
        self.recovery_calls = 0
        self.recovery_outcomes = deque()
        self.recovery_entered = asyncio.Event()
        self.recovery_cancelled = 0
        self.aborts = 0
        self.checkpoints = 0

    async def transcript_snapshot(self, *, resume=True):
        self.snapshots.append(resume)
        if self.snapshot_outcomes:
            outcome = self.snapshot_outcomes.popleft()
            if isinstance(outcome, BaseException):
                raise outcome
            self.current_final = outcome
        return self.current_final.model_copy(deep=True) if self.current_final is not None else None

    async def prepare_decision(self, transcript):
        self.prepares.append(transcript.model_copy(deep=True))
        if self.prepare_error is not None:
            raise self.prepare_error
        return SimpleNamespace(
            text=transcript.text, final=transcript.model_copy(deep=True),
            understanding=SimpleNamespace(problem=None, suggested_action="accept"),
        )

    async def resume_capture(self):
        self.resumes += 1
        if self.resume_errors:
            raise self.resume_errors.popleft()

    async def recover_capture(self):
        self.recovery_calls += 1
        self.recovery_entered.set()
        outcome = self.recovery_outcomes.popleft() if self.recovery_outcomes else None
        try:
            if isinstance(outcome, BaseException):
                raise outcome
            if isinstance(outcome, asyncio.Event):
                await outcome.wait()
        except asyncio.CancelledError:
            self.recovery_cancelled += 1
            raise
        self.recovery_required = False
        self.recovery_error = None

    async def abort(self):
        self.aborts += 1
        self.is_open = False

    def checkpoint_incomplete(self):
        self.checkpoints += 1


class Harness:
    def __init__(self, *, probability=0.9, recovery_timeout=0.02):
        self.capture, self.detector, self.clock = Capture(), Detector(probability), Clock()
        self.notices, self.notice_facts, self.commits = [], [], []
        self.paused = False
        self.retry_required = False
        self.commit_error = None
        self.commit_calls = 0
        self.endpoint = AnswerEndpoint(
            detector=self.detector, capture=self.capture, commit=self.commit,
            notify=self.notify, clock=self.clock, recovery_timeout=recovery_timeout,
            recovery_backoff=0,
        )

    async def notify(self, state):
        self.notices.append(state)
        self.notice_facts.append({
            "state": state, "attempt": self.endpoint.recovery_attempt,
            "failure": asdict(self.endpoint.failure) if self.endpoint.failure else None,
            "capture_open": self.capture.is_open, "aborts": self.capture.aborts,
        })
        # Mimic only the domain effects of the endpoint's public notice seam.
        # A transient notice cannot destroy the recorder or pause the session.
        if state == "capture_failed":
            self.paused = True
            await self.capture.abort()
        elif state == "capture_retry_required":
            self.retry_required = True
            self.capture.checkpoint_incomplete()
            await self.capture.abort()

    async def commit(self, prepared, guard):
        self.commit_calls += 1
        guard()
        if self.commit_error is not None:
            raise self.commit_error
        self.commits.append(prepared)
        self.capture.is_open = False

    def voice_then_pause(self):
        self.endpoint.observe_audio(b"\x00\x40" * 320)
        self.clock.value += 0.8

    async def notice(self, state):
        await until(lambda: state in self.notices)


@pytest.mark.anyio
@pytest.mark.parametrize("error", [
    asyncio.TimeoutError(),
    ProviderError("provider_timeout", "synthetic timeout", retryable=True),
    ApiError("PROVIDER_TIMEOUT", "synthetic timeout", status_code=502),
])
async def test_snapshot_timeout_recovers_before_any_abort_and_can_commit_automatically(error):
    case = Harness()
    case.capture.snapshot_outcomes.append(error)
    case.voice_then_pause()
    case.endpoint.start()
    try:
        await case.notice("capture_recovered")
        assert case.capture.recovery_calls == 1 and case.capture.aborts == 0
        assert case.capture.is_open and not case.paused and not case.commits
        assert case.capture.snapshots == [True] and not case.capture.prepares
        assert case.notices.index("capture_recovering") < case.notices.index("capture_recovered")
        assert all(fact["capture_open"] and fact["aborts"] == 0 for fact in case.notice_facts)
        case.clock.value += 1
        await until(lambda: case.commits)
        assert len(case.commits) == 1 and case.capture.snapshots == [True, True, False]
        assert "capture_failed" not in case.notices and "capture_retry_required" not in case.notices
    finally:
        await case.endpoint.close()


@pytest.mark.anyio
async def test_transient_resume_failure_uses_same_recovery_without_closing_recorder():
    case = Harness()
    case.capture.current_final = None
    case.capture.resume_errors.append(ProviderError("provider_network_error", "synthetic", retryable=True))
    case.voice_then_pause()
    case.endpoint.start()
    try:
        await case.notice("capture_recovered")
        assert case.endpoint.failure.stage == "resume"
        assert case.capture.recovery_calls == 1 and not case.capture.aborts
        assert not case.paused and not case.commits
        case.capture.current_final = final()
        case.clock.value += 1
        await until(lambda: case.commits)
        assert len(case.commits) == 1
    finally:
        await case.endpoint.close()


@pytest.mark.anyio
async def test_speech_during_recovery_revokes_old_preparation_and_requires_new_final():
    case = Harness(recovery_timeout=1)
    gate = asyncio.Event()
    case.capture.snapshot_outcomes.extend([
        final("合成前句。"), ProviderError("provider_timeout", "synthetic", retryable=True),
    ])
    case.capture.recovery_outcomes.append(gate)
    case.voice_then_pause()
    case.endpoint.start()
    try:
        await case.capture.recovery_entered.wait()
        assert [item.text for item in case.capture.prepares] == ["合成前句。"]
        case.clock.value += 1
        case.endpoint.observe_audio(b"\x00\x40" * 320)
        case.capture.current_final = final("合成前句。恢复时继续补充。")
        gate.set()
        await case.notice("capture_recovered")
        assert not case.commits and not case.capture.aborts
        case.clock.value += 1
        await until(lambda: case.commits)
        assert [item.text for item in case.commits] == ["合成前句。恢复时继续补充。"]
        assert [item.text for item in case.capture.prepares] == ["合成前句。", "合成前句。恢复时继续补充。"]
    finally:
        await case.endpoint.close()


@pytest.mark.anyio
async def test_send_failure_recovers_without_prior_speech_or_end_of_turn_prediction():
    case = Harness(probability=None)
    case.capture.recovery_required = True
    case.capture.recovery_error = ProviderError("provider_stream_closed", "synthetic", retryable=False)
    case.endpoint.start()
    try:
        await case.notice("capture_recovered")
        assert case.capture.recovery_calls == 1 and not case.capture.recovery_required
        assert case.endpoint.failure.stage == "send"
        assert not case.detector.calls and not case.capture.snapshots and not case.commits
        assert case.capture.is_open and not case.capture.aborts and not case.paused
    finally:
        await case.endpoint.close()


@pytest.mark.anyio
async def test_second_recovery_attempt_can_succeed_without_early_abort_or_submission():
    case = Harness()
    case.capture.recovery_required = True
    case.capture.recovery_error = asyncio.TimeoutError()
    case.capture.recovery_outcomes.extend([
        ProviderError("provider_network_error", "synthetic", retryable=True), None,
    ])
    case.endpoint.start()
    try:
        await case.notice("capture_recovered")
        attempts = [fact for fact in case.notice_facts if fact["state"] == "capture_recovering"]
        assert [fact["attempt"] for fact in attempts] == [1, 2]
        assert [fact["failure"]["cause_code"] for fact in attempts] == ["PROVIDER_TIMEOUT", "PROVIDER_NETWORK_ERROR"]
        assert case.capture.recovery_calls == 2 and case.capture.is_open
        assert not case.capture.aborts and not case.commits and not case.paused
        assert "capture_retry_required" not in case.notices
    finally:
        await case.endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("blocked", [False, True])
async def test_recovery_exhaustion_preserves_incomplete_evidence_and_never_submits(blocked):
    case = Harness()
    case.capture.recovery_required = True
    case.capture.recovery_error = asyncio.TimeoutError()
    case.capture.recovery_outcomes.extend(
        [asyncio.Event() for _ in range(3)] if blocked else
        [ProviderError("provider_timeout", "synthetic", retryable=True) for _ in range(3)]
    )
    case.endpoint.start()
    try:
        await case.notice("capture_retry_required")
        assert case.capture.recovery_calls == 3
        assert [fact["attempt"] for fact in case.notice_facts if fact["state"] == "capture_recovering"] == [1, 2, 3]
        assert case.capture.checkpoints == 1 and case.capture.aborts == 1
        assert not case.paused and case.retry_required and not case.commits and not case.capture.snapshots
        assert case.capture.recovery_cancelled == (3 if blocked else 0)
        case.clock.value += 100
        case.endpoint.request_finish()
        case.endpoint.observe_audio(b"\x00\x40" * 320)
        await asyncio.sleep(0.08)
        assert case.capture.recovery_calls == 3 and not case.commit_calls
        assert "capture_failed" not in case.notices
    finally:
        await case.endpoint.close()


@pytest.mark.anyio
async def test_commit_timeout_is_never_replayed_as_recognition_recovery():
    case = Harness()
    case.commit_error = ProviderError("provider_timeout", "synthetic commit timeout", retryable=True)
    case.voice_then_pause()
    case.endpoint.start()
    try:
        await case.notice("capture_failed")
        assert case.commit_calls == 1 and not case.commits
        assert case.endpoint.failure.stage == "commit" and not case.endpoint.failure.retryable
        assert case.capture.recovery_calls == 0 and case.capture.resumes == 0
        assert case.paused and case.capture.aborts == 1
        assert "capture_recovering" not in case.notices
    finally:
        await case.endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["snapshot", "understanding", "send", "recovery"])
async def test_owner_fence_is_fatal_at_every_endpoint_stage(stage):
    case = Harness()
    error = ApiError("EVIDENCE_OWNER_FENCED", "synthetic owner lost", status_code=409)
    if stage == "snapshot":
        case.capture.snapshot_outcomes.append(error)
    elif stage == "understanding":
        case.capture.prepare_error = error
    elif stage == "send":
        case.capture.recovery_required, case.capture.recovery_error = True, error
    else:
        case.capture.snapshot_outcomes.append(asyncio.TimeoutError())
        case.capture.recovery_outcomes.append(error)
    case.voice_then_pause()
    case.endpoint.start()
    try:
        await case.notice("capture_failed")
        assert case.paused and case.capture.aborts == 1 and not case.commits
        assert case.endpoint.failure.cause_code == "EVIDENCE_OWNER_FENCED"
        assert not case.endpoint.failure.retryable
        assert case.capture.recovery_calls == (1 if stage == "recovery" else 0)
        assert "capture_retry_required" not in case.notices and "capture_recovered" not in case.notices
    finally:
        await case.endpoint.close()


@pytest.mark.anyio
async def test_close_cancels_inflight_recovery_and_does_not_reopen_again():
    case = Harness(recovery_timeout=1)
    gate = asyncio.Event()
    case.capture.recovery_required, case.capture.recovery_error = True, asyncio.TimeoutError()
    case.capture.recovery_outcomes.append(gate)
    case.endpoint.start()
    await case.capture.recovery_entered.wait()
    await case.endpoint.close()
    assert case.capture.recovery_cancelled == 1 and case.capture.recovery_calls == 1
    gate.set()
    case.clock.value += 100
    await asyncio.sleep(0.08)
    assert case.capture.recovery_calls == 1 and not case.commits
    assert not {"capture_recovered", "capture_retry_required", "capture_failed"}.intersection(case.notices)


@pytest.mark.anyio
async def test_capture_replaced_during_recovery_cannot_report_old_capture_as_recovered():
    case = Harness(recovery_timeout=1)
    gate = asyncio.Event()
    case.capture.recovery_required, case.capture.recovery_error = True, asyncio.TimeoutError()
    case.capture.recovery_outcomes.append(gate)
    case.endpoint.start()
    try:
        await case.capture.recovery_entered.wait()
        case.capture.is_open = False
        gate.set()
        await asyncio.sleep(0.08)
        assert case.capture.recovery_calls == 1 and not case.commits
        assert not {"capture_recovered", "capture_retry_required", "capture_failed"}.intersection(case.notices)
    finally:
        await case.endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("terminal", ["capture_retry_required", "capture_failed"])
@pytest.mark.parametrize("fault", ["exception", "timeout"])
async def test_terminal_notice_failure_cannot_leave_unowned_open_capture(terminal, fault, caplog):
    case = Harness()
    secret = "SYNTHETIC_PRIVATE_TERMINAL_NOTICE_BODY"
    notice_entered = asyncio.Event()
    notice_cancelled = asyncio.Event()
    failure_callbacks = []
    previous_notify = case.endpoint.notify

    async def failed_terminal_notice(state):
        if state != terminal:
            await previous_notify(state)
            return
        notice_entered.set()
        if fault == "exception":
            raise RuntimeError(secret)
        try:
            await asyncio.Event().wait()
        finally:
            notice_cancelled.set()

    case.endpoint.notify = failed_terminal_notice

    async def failing_failure_callback(error):
        assert not case.capture.is_open, "Revoke the local gate before attempting another external effect"
        failure_callbacks.append(type(error).__name__)
        raise RuntimeError(secret)

    case.endpoint.on_failure = failing_failure_callback
    case.capture.recovery_required = True
    if terminal == "capture_retry_required":
        case.capture.recovery_error = asyncio.TimeoutError()
        case.capture.recovery_outcomes.extend([asyncio.TimeoutError() for _ in range(3)])
    else:
        case.capture.recovery_error = ApiError("EVIDENCE_OWNER_FENCED", secret, status_code=409)
    caplog.set_level(logging.INFO, logger="app.services.answer_endpoint")
    case.endpoint.start()
    try:
        await asyncio.wait_for(notice_entered.wait(), timeout=1)
        # Unlike an optional UI hint, a failed terminal transition must revoke
        # the capture locally even when its durable notification cannot commit.
        await until(lambda: not case.capture.is_open, timeout=7)
        await until(lambda: failure_callbacks)
        assert case.capture.aborts == 1 and not case.commits
        assert len(failure_callbacks) == 1
        assert case.capture.recovery_calls == (3 if terminal == "capture_retry_required" else 0)
        assert "capture_recovered" not in case.notices
        if fault == "timeout":
            assert notice_cancelled.is_set()
        assert secret not in caplog.text and secret not in repr(case.notice_facts)
        attempts = case.capture.recovery_calls
        case.clock.value += 100
        case.endpoint.request_finish()
        await asyncio.sleep(0.08)
        assert case.capture.recovery_calls == attempts and not case.commit_calls
    finally:
        await case.endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["api_unknown_code", "provider_unknown_code", "provider_message", "runtime_message"])
async def test_failure_projection_and_logs_do_not_disclose_raw_messages_or_unknown_codes(kind, caplog):
    secret = "SYNTHETIC_PRIVATE_CANDIDATE_VALUE"
    errors = {
        "api_unknown_code": ApiError(secret, secret, status_code=502, details={"response": secret}),
        "provider_unknown_code": ProviderError(secret, secret, retryable=True, details={"response": secret}),
        "provider_message": ProviderError("provider_timeout", secret, retryable=True, details={"response": secret}),
        "runtime_message": RuntimeError(secret),
    }
    case = Harness()
    case.capture.snapshot_outcomes.append(errors[kind])
    case.voice_then_pause()
    caplog.set_level(logging.INFO, logger="app.services.answer_endpoint")
    case.endpoint.start()
    try:
        await until(lambda: "capture_failed" in case.notices or "capture_recovered" in case.notices)
        assert secret not in caplog.text
        assert secret not in repr(case.notice_facts)
        assert set(asdict(case.endpoint.failure)) == {"stage", "cause_code", "cause_type", "retryable"}
        assert case.endpoint.failure.cause_code == ("PROVIDER_TIMEOUT" if kind == "provider_message" else "CAPTURE_INTERNAL_ERROR")
        assert not case.commits
    finally:
        await case.endpoint.close()


@pytest.mark.anyio
async def test_recovery_budget_is_bounded_even_if_constructor_overrides_are_invalid():
    case = Harness(recovery_timeout=999)
    assert case.endpoint.recovery_timeout == 15.0 and case.endpoint.max_recovery_attempts == 3
    other = Harness(recovery_timeout=-10)
    assert other.endpoint.recovery_timeout == 0.01
