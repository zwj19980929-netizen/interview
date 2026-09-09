import asyncio
from collections import deque
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.model_gateway.schemas import ProviderMeta, StreamingSTTEvent, TranscriptSegment
from app.services.answer_endpoint import AnswerEndpoint


def _final(text="合成回答结束。", confidence=0.9):
    return StreamingSTTEvent(
        stream_id="synthetic", sequence=2, type="transcript.final", is_final=True,
        text=text, confidence=confidence,
        segments=[TranscriptSegment(text=text, start_ms=0, end_ms=1000, confidence=confidence)],
        provider=ProviderMeta(provider_id="fake", model="stt-test", request_id="req", latency_ms=1),
    )


async def _until(predicate, timeout=1):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), timeout)


class _Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class _Detector:
    def __init__(self, probability=0.9):
        self.probability = probability
        self.calls = []
        self.exception = None

    async def predict(self, pcm, **kwargs):
        self.calls.append((pcm, kwargs))
        if self.exception is not None:
            raise self.exception
        return SimpleNamespace(probability=self.probability,
                               status="ready" if self.probability is not None else "unavailable")


class _Capture:
    def __init__(self, finals=None):
        self.is_open = True
        self.finals = deque(finals or [])
        self.current_final = _final()
        self.snapshots = []
        self.prepares = []
        self.resumes = 0
        self.prepare_impl = None
        self.problem = None
        self.action = "accept"

    async def transcript_snapshot(self, *, resume=True):
        self.snapshots.append(resume)
        if self.finals:
            self.current_final = self.finals.popleft()
        return self.current_final.model_copy(deep=True) if self.current_final is not None else None

    async def prepare_decision(self, final):
        self.prepares.append(final.model_copy(deep=True))
        if self.prepare_impl is not None:
            return await self.prepare_impl(final)
        return self.prepared(final)

    def prepared(self, final):
        return SimpleNamespace(text=final.text, final=final.model_copy(deep=True),
                               understanding=SimpleNamespace(problem=self.problem, suggested_action=self.action))

    async def resume_capture(self):
        self.resumes += 1


def _endpoint(*, detector=None, capture=None, commit_impl=None):
    detector, capture = detector or _Detector(), capture or _Capture()
    clock, notifications, commits = _Clock(), [], []

    async def notify(state):
        notifications.append(state)

    async def commit(prepared, guard):
        if commit_impl is not None:
            await commit_impl(prepared, guard)
        guard()
        commits.append(prepared)
        capture.is_open = False

    endpoint = AnswerEndpoint(detector=detector, capture=capture, commit=commit,
                              notify=notify, clock=clock)
    return endpoint, clock, notifications, commits


def _voice_then_pause(endpoint, clock):
    endpoint.observe_audio(b"\0\x40" * 320)
    clock.value += 0.3
    endpoint.observe_audio(bytes(9600))
    clock.value += 0.5


def test_silence_without_prior_voice_never_invokes_detector_or_submits():
    async def scenario():
        endpoint, clock, _, commits = _endpoint()
        endpoint.start()
        clock.value = 100
        endpoint.observe_audio(bytes(38_400))
        await asyncio.sleep(0.12)
        assert not endpoint.detector.calls and not endpoint.capture.snapshots and not commits
        await endpoint.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("probability", [None, 0.1])
def test_a_pause_and_unknown_or_low_probability_never_submit(probability):
    async def scenario():
        endpoint, clock, notices, commits = _endpoint(detector=_Detector(probability))
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await _until(lambda: endpoint.detector.calls)
        await asyncio.sleep(0.1)
        assert not endpoint.capture.snapshots and not commits
        if probability is None:
            assert notices == ["detector_unavailable"]
            clock.value += 1
            await asyncio.sleep(0.07)
            assert len(endpoint.detector.calls) == 1
            clock.value += 2
            await _until(lambda: len(endpoint.detector.calls) == 2)
            assert notices == ["detector_unavailable"]
        await endpoint.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("probability", [float("nan"), float("inf"), 1.2, True])
def test_malformed_detector_probability_cannot_authorize_a_commit(probability):
    async def scenario():
        endpoint, clock, _, commits = _endpoint(detector=_Detector(probability))
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await _until(lambda: endpoint.detector.calls)
        await asyncio.sleep(0.07)
        assert not commits and not endpoint.capture.snapshots
        await endpoint.close()

    asyncio.run(scenario())


def test_automatic_commit_requires_prepared_and_reconfirmed_server_final():
    async def scenario():
        endpoint, clock, notices, commits = _endpoint()
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await _until(lambda: commits)
        assert endpoint.capture.snapshots == [True, False]
        assert len(endpoint.capture.prepares) == 1
        assert commits[0].text == "合成回答结束。"
        assert "answer_preparing" in notices
        await endpoint.close()

    asyncio.run(scenario())


def test_explicit_finish_is_optional_override_not_required_for_automatic_flow():
    async def scenario():
        endpoint, clock, _, commits = _endpoint(detector=_Detector(None))
        _voice_then_pause(endpoint, clock)
        endpoint.request_finish()
        endpoint.start()
        await _until(lambda: commits)
        assert not endpoint.detector.calls
        await endpoint.close()

    asyncio.run(scenario())


def test_new_voice_cancels_slow_prepare_and_discards_late_result():
    async def scenario():
        capture = _Capture()
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def slow_prepare(final):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                # A provider may return a result after cancellation raced it.
                return capture.prepared(final)

        capture.prepare_impl = slow_prepare
        endpoint, clock, notices, commits = _endpoint(capture=capture)
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await entered.wait()
        clock.value += 1
        endpoint.observe_audio(b"\0\x40" * 320)
        await cancelled.wait()
        await _until(lambda: capture.resumes and "answer_listening" in notices)
        assert not commits and "answer_listening" in notices
        capture.prepare_impl = None
        capture.current_final = _final("合成回答结束。还有补充。")
        clock.value += 0.8
        await _until(lambda: commits)
        assert len(commits) == 1 and commits[0].text.endswith("还有补充。")
        assert len(capture.prepares) == 2
        await endpoint.close()

    asyncio.run(scenario())


def test_second_final_adds_quiet_tail_so_first_preparation_is_never_committed():
    async def scenario():
        capture = _Capture([_final("第一句。"), _final("第一句。小声补充。"),
                            _final("第一句。小声补充。"), _final("第一句。小声补充。")])
        endpoint, clock, _, commits = _endpoint(capture=capture)
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await _until(lambda: commits)
        assert [item.text for item in capture.prepares] == ["第一句。", "第一句。小声补充。"]
        assert [item.text for item in commits] == ["第一句。小声补充。"]
        assert capture.resumes >= 1
        await endpoint.close()

    asyncio.run(scenario())


def test_same_text_with_lower_final_confidence_requires_fresh_understanding():
    async def scenario():
        capture = _Capture([_final(confidence=0.95), _final(confidence=0.3),
                            _final(confidence=0.3), _final(confidence=0.3)])
        endpoint, clock, _, commits = _endpoint(capture=capture)
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await _until(lambda: commits)
        assert [item.confidence for item in capture.prepares] == [0.95, 0.3]
        await endpoint.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["no_final", "problem", "continue", "timeout", "exception"])
def test_preparation_failures_keep_listening_without_repeated_calls_or_commit(fault):
    async def scenario():
        capture = _Capture()
        if fault == "no_final":
            capture.current_final = None
        elif fault == "problem":
            capture.problem = SimpleNamespace(code="schema_invalid")
        elif fault == "continue":
            capture.action = "continue_listening"
        else:
            async def fail(_):
                raise asyncio.TimeoutError() if fault == "timeout" else RuntimeError("private model body")
            capture.prepare_impl = fail
        endpoint, clock, notices, commits = _endpoint(capture=capture)
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await _until(lambda: capture.resumes)
        if fault in {"problem", "timeout", "exception"}:
            assert len(capture.prepares) == 1
            clock.value += 0.99
            await asyncio.sleep(0.07)
            assert len(capture.prepares) == 1
            clock.value += 0.02
            await _until(lambda: capture.resumes >= 2)
            assert len(capture.prepares) == 2
            clock.value += 1.99
            await asyncio.sleep(0.07)
            assert len(capture.prepares) == 2
            clock.value += 0.02
            await _until(lambda: capture.resumes >= 3)
            assert len(capture.prepares) == 3
            clock.value += 60
            await asyncio.sleep(0.15)
            assert len(capture.prepares) == 3, "Three failures exhaust this input's retry budget"
            _voice_then_pause(endpoint, clock)
            await _until(lambda: capture.resumes >= 4)
            assert len(capture.prepares) == 3, "Audio alone cannot restart the same server evidence budget"
            capture.current_final = _final(capture.current_final.text + "新增服务端补充内容。")
            endpoint.observe_transcript(capture.current_final.text)
            clock.value += .8
            await _until(lambda: len(capture.prepares) == 4)
            assert len(capture.prepares) == 4, "New server words start a fresh bounded budget"
        else:
            count = len(capture.prepares)
            clock.value += 60
            await asyncio.sleep(0.15)
            assert len(capture.prepares) == count
        assert not commits and capture.is_open and "answer_listening" in notices
        assert all("private model" not in item for item in notices)
        assert "capture_failed" not in notices, "A model failure must not become a fatal Evidence failure"
        if fault in {"problem", "timeout", "exception"}:
            assert "understanding_unavailable" in notices
        await endpoint.close()

    asyncio.run(scenario())


def test_recoverable_prepare_failure_can_retry_and_commit_without_user_button():
    async def scenario():
        capture = _Capture()

        async def transient_failure(final):
            if len(capture.prepares) == 1:
                raise asyncio.TimeoutError()
            return capture.prepared(final)

        capture.prepare_impl = transient_failure
        endpoint, clock, notices, commits = _endpoint(capture=capture)
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await _until(lambda: capture.resumes == 1)
        assert not commits
        clock.value += 1.1
        await _until(lambda: commits)
        assert len(capture.prepares) == 2 and len(commits) == 1
        assert notices.count("understanding_unavailable") == 1
        assert "capture_failed" not in notices
        await endpoint.close()

    asyncio.run(scenario())


def test_detector_exception_does_not_kill_endpoint_or_force_a_commit():
    async def scenario():
        detector = _Detector()
        detector.exception = RuntimeError("native worker failure")
        endpoint, clock, notices, commits = _endpoint(detector=detector)
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await _until(lambda: detector.calls)
        await asyncio.sleep(0.08)
        assert "detector_unavailable" in notices
        assert not commits and endpoint.capture.is_open
        detector.exception = None
        clock.value += 3
        await _until(lambda: commits)
        await endpoint.close()

    asyncio.run(scenario())


def test_input_revision_is_rechecked_inside_fast_commit():
    async def scenario():
        endpoint = None

        async def before_guard(_prepared, guard):
            endpoint.speech_started()
            with pytest.raises(ApiError) as exc:
                guard()
            assert exc.value.code == "TURN_DECISION_STALE"

        endpoint, clock, notices, commits = _endpoint(commit_impl=before_guard)
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await _until(lambda: endpoint.capture.resumes and "answer_listening" in notices)
        assert not commits and endpoint.capture.is_open
        assert "answer_listening" in notices
        await endpoint.close()

    asyncio.run(scenario())


def test_close_cancels_pending_inference_without_resuming_or_committing():
    async def scenario():
        capture = _Capture()
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def blocked(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        capture.prepare_impl = blocked
        endpoint, clock, _, commits = _endpoint(capture=capture)
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await entered.wait()
        await endpoint.close()
        assert cancelled.is_set() and not capture.resumes and not commits
        with pytest.raises(ApiError):
            endpoint.assert_current()

    asyncio.run(scenario())


def test_transient_notice_failure_does_not_kill_automatic_endpoint():
    async def scenario():
        endpoint, clock, _, commits = _endpoint()

        async def unavailable_notice(_reason):
            raise RuntimeError("synthetic unavailable event transport")

        endpoint.notify = unavailable_notice
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await _until(lambda: commits)
        assert len(commits) == 1
        await endpoint.close()

    asyncio.run(scenario())


def test_application_shutdown_cancellation_is_not_confused_with_new_speech():
    async def scenario():
        capture = _Capture()
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def blocked(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        capture.prepare_impl = blocked
        endpoint, clock, _, commits = _endpoint(capture=capture)
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await entered.wait()
        # Simulate the loop cancelling tasks before the normal close hook.
        worker = endpoint._task
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, timeout=1)
        assert cancelled.is_set() and not capture.resumes and not commits
        await endpoint.close()

    asyncio.run(scenario())
