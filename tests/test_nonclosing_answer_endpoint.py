"""Stable sentence preparation must not rotate a healthy recognition stream."""
import asyncio

import pytest

from app.model_gateway.schemas import StableTranscriptPreview
from test_answer_endpoint import _Capture, _endpoint, _final, _until, _voice_then_pause


def _preview(final=None, *, revision=1, tail=False):
    final = final or _final()
    return StableTranscriptPreview(
        **{key: getattr(final, key) for key in
           ("stream_id", "text", "confidence", "language", "segments", "provider")},
        revision=revision, has_unstable_tail=tail,
    )


class PreviewCapture(_Capture):
    supports_stable_preview = True

    def __init__(self):
        super().__init__()
        self.preview = _preview()
        self.preview_calls = 0
        self.finish_impl = None

    async def transcript_preview(self):
        self.preview_calls += 1
        return self.preview.model_copy(deep=True) if self.preview else None

    async def transcript_snapshot(self, *, resume=True):
        if self.finish_impl:
            await self.finish_impl()
        return await super().transcript_snapshot(resume=resume)


def test_many_continue_listening_pauses_do_not_finish_or_reopen_or_repeat_inference():
    async def scenario():
        capture = PreviewCapture()
        capture.action = "continue_listening"
        endpoint, clock, _, commits = _endpoint(capture=capture)
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await _until(lambda: capture.prepares)
        for _ in range(6):
            clock.value += 2
            await asyncio.sleep(0.11)
        assert len(capture.prepares) == 1
        assert capture.preview_calls > 5
        assert capture.snapshots == [] and capture.resumes == 0 and commits == []
        # A late stable sentence is a new proposal even without another VAD
        # revision. No fixed silence threshold needs to guess completeness.
        capture.current_final = _final("合成回答补充结束。")
        capture.preview = _preview(capture.current_final, revision=2)
        capture.action = "accept"
        await _until(lambda: commits)
        assert len(capture.prepares) == 2 and capture.snapshots == [False]
        assert capture.resumes == 0
        await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("unavailable", [None, _preview(tail=True)])
def test_absent_or_unstable_preview_waits_on_same_connection_then_finishes_once(unavailable):
    async def scenario():
        capture = PreviewCapture()
        capture.preview = unavailable
        endpoint, clock, notices, commits = _endpoint(capture=capture)
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await _until(lambda: capture.preview_calls >= 3)
        assert not capture.prepares and not capture.snapshots and capture.resumes == 0
        assert "capture_recovering" not in notices and "transcript_unavailable" not in notices
        capture.preview = _preview()
        await _until(lambda: commits)
        assert capture.snapshots == [False] and len(capture.prepares) == 1
        await endpoint.close()
    asyncio.run(scenario())


def test_new_voice_cancels_speculation_without_finishing():
    async def scenario():
        capture = PreviewCapture()
        started = asyncio.Event()
        async def slow_prepare(final):
            started.set()
            await asyncio.Event().wait()
        capture.prepare_impl = slow_prepare
        endpoint, clock, _, commits = _endpoint(capture=capture)
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await started.wait()
        endpoint.speech_started()
        await _until(lambda: endpoint._inference_task is None)
        assert capture.snapshots == [] and capture.resumes == 0 and not commits
        await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["append", "partial_tail"])
def test_late_provider_evidence_invalidates_preparation_before_cut(change):
    async def scenario():
        capture = PreviewCapture()
        gate, started = asyncio.Event(), asyncio.Event()
        async def prepare(final):
            started.set()
            await gate.wait()
            return capture.prepared(final)
        capture.prepare_impl = prepare
        endpoint, clock, _, commits = _endpoint(capture=capture)
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await started.wait()
        if change == "append":
            capture.preview = _preview(_final("合成回答又增加一句。"), revision=2)
            capture.action = "continue_listening"
        else:
            capture.preview = _preview(tail=True)
        gate.set()
        await _until(lambda: endpoint._inference_task is None)
        await asyncio.sleep(0.12)
        assert capture.snapshots == [] and capture.resumes == 0 and not commits
        await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["text", "confidence", "timestamps", "provider"])
def test_final_correction_reprepares_without_an_empty_reopen_and_second_finish(change):
    async def scenario():
        capture = PreviewCapture()
        corrected = _final()
        if change == "text":
            corrected = _final("合成回答还有最后一句。")
        elif change == "confidence":
            corrected = _final(confidence=0.8)
        elif change == "timestamps":
            corrected.segments[0].end_ms = 1050
        else:
            corrected.provider.latency_ms = 5
        capture.current_final = corrected
        endpoint, clock, _, commits = _endpoint(capture=capture)
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await _until(lambda: commits)
        assert capture.snapshots == [False] and capture.resumes == 0
        assert len(capture.prepares) == 2
        assert commits[0].final.model_dump() == corrected.model_dump()
        await endpoint.close()
    asyncio.run(scenario())


def test_voice_during_final_confirmation_resumes_once_and_never_commits_old_preview():
    async def scenario():
        capture = PreviewCapture()
        endpoint, clock, _, commits = _endpoint(capture=capture)
        async def resume_voice():
            endpoint.speech_started()
        capture.finish_impl = resume_voice
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await _until(lambda: capture.resumes == 1)
        assert capture.snapshots == [False] and not commits
        await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("unavailable", [None, _preview(tail=True)])
def test_explicit_early_finish_remains_a_single_final_fallback_when_no_stable_preview(unavailable):
    async def scenario():
        capture = PreviewCapture()
        capture.preview = unavailable
        endpoint, clock, _, commits = _endpoint(capture=capture)
        _voice_then_pause(endpoint, clock)
        endpoint.request_finish()
        endpoint.start()
        await _until(lambda: commits)
        assert capture.snapshots == [False] and len(capture.prepares) == 1
        assert capture.prepares[0].is_final
        assert capture.resumes == 0
        await endpoint.close()
    asyncio.run(scenario())
