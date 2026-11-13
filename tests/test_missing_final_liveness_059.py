"""Unresolved provider tails cannot buy endless same-origin finalization."""
import asyncio

import pytest

from app.services.continuous_stt import ContinuousSTT
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint, _until
from test_continuous_stt import _RawStream, _validated


class ContinuousCapture(_Capture):
    def __init__(self, stream):
        super().__init__()
        self.stream = stream

    @property
    def recovery_origin(self):
        return self.stream.recovery_origin

    async def transcript_snapshot(self, *, resume=True):
        self.snapshots.append(resume)
        return await self.stream.snapshot(resume=resume)

    async def resume_capture(self):
        self.resumes += 1
        await self.stream.resume()


@pytest.mark.anyio
@pytest.mark.parametrize("resume_source", ["audio", "continue", "finish"])
async def test_missing_final_waits_after_three_attempts_without_losing_audio_or_trusting_partial(resume_source):
    streams = [_RawStream("initial", text="已确定的合成回答。")]
    recorded = []

    async def reopen():
        raw = _RawStream("retry-%s" % len(streams), text=None)
        streams.append(raw)
        return _validated(raw)

    continuous = ContinuousSTT(_validated(streams[0]), reopen=reopen,
                               record=recorded.append, bytes_per_second=32000)
    capture = ContinuousCapture(continuous)
    endpoint, clock, notices, commits = _endpoint(capture=capture)
    spoken = []

    async def speak(kind, guard):
        guard()
        spoken.append(kind)
        return True

    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak, semantic_first=False)
    try:
        await continuous.send_audio(bytes(640))
        endpoint.speech_started(source="audio")
        endpoint.start()
        clock.value = 5
        await _until(lambda: spoken == ["check"])
        endpoint.confirmation.floor_returned(endpoint)
        # A provider can emit a nonempty hypothesis during pure silence and
        # fail to finalize it. It stays unresolved; it is never an answer.
        await continuous.send_audio(bytes(640))
        endpoint.observe_transcript("已确定的合成回答。未确认的临时字")
        for attempt in range(3):
            clock.value += 3
            await _until(lambda: sum(raw.finish_calls for raw in streams) >= attempt + 2)
            await _until(lambda: not continuous.paused)
        await _until(lambda: "transcript_retry_required" in notices)
        attempts, opened = sum(raw.finish_calls for raw in streams), len(streams)
        for index in range(5):
            # Only revised ASR text, not new acoustic speech. These revisions
            # must not reset the finalization budget or erase the old tail.
            endpoint.observe_transcript("已确定的合成回答。临时修订%d" % index)
            await continuous.send_audio(bytes(640))
            clock.value += 20
            await asyncio.sleep(.06)
        assert sum(raw.finish_calls for raw in streams) == attempts
        assert len(streams) == opened and continuous._segment_start_bytes == 640
        assert continuous._segment_text_seen and continuous._unresolved
        assert continuous._final.text == "已确定的合成回答。"
        assert b"".join(recorded) == bytes(640 * 7)
        assert not commits and capture.is_open and not endpoint._closed
        assert notices.count("transcript_retry_required") == 1

        if resume_source == "audio":
            # New VAD boundaries cannot renew finalization of the same old
            # unresolved origin. The candidate must explicitly retry it.
            for source in ["audio", "client", "audio"]:
                endpoint.speech_started(source=source)
                clock.value += 6
                await asyncio.sleep(.06)
            assert endpoint.awaiting_transcript
            assert sum(raw.finish_calls for raw in streams) == attempts
            assert notices.count("transcript_retry_required") == 1
            endpoint.request_finish()
        elif resume_source == "continue":
            endpoint.continue_speaking()
        else:
            endpoint.request_finish()
        clock.value += 6
        await _until(lambda: sum(raw.finish_calls for raw in streams) > attempts)
        assert not commits and continuous._unresolved
    finally:
        await endpoint.close()
        await continuous.abort()


@pytest.mark.anyio
async def test_accepted_provider_final_advances_origin_and_renews_wait_without_committing_answer():
    raw = _RawStream("unresolved", text=None)
    async def reopen():
        return _validated(_RawStream("next", text=None))
    continuous = ContinuousSTT(_validated(raw), reopen=reopen, record=lambda _pcm: None, bytes_per_second=32000)
    capture = ContinuousCapture(continuous)
    endpoint, _, _, commits = _endpoint(capture=capture)
    try:
        await continuous.send_audio(bytes(640))
        for _ in range(3):
            endpoint._observe_final_snapshot(None)
        assert endpoint.awaiting_transcript and capture.recovery_origin == 0
        endpoint.speech_started(source="audio")
        assert endpoint.awaiting_transcript
        # Only an accepted server final advances the audio origin; a browser
        # boundary, revised partial or new PCM length cannot manufacture it.
        raw.text = "服务端最终确认的合成片段。"
        final = await continuous.snapshot()
        assert final is not None and capture.recovery_origin == 640
        endpoint.speech_started(source="audio")
        assert not endpoint.awaiting_transcript and endpoint._missing_final_attempts == 0
        assert not commits and capture.is_open
    finally:
        await endpoint.close()
        await continuous.abort()
