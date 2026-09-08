import asyncio

import pytest

from app.model_gateway.schemas import StableTranscriptPreview
from app.model_gateway.errors import ProviderError
from app.services.continuous_stt import ContinuousSTT
from test_continuous_stt import _RawStream, _validated


class PreviewStream(_RawStream):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stable = True
        self.tail = False

    async def preview(self):
        if not self.stable or not self.chunks:
            return None
        from app.model_gateway.schemas import ProviderMeta, TranscriptSegment
        return StableTranscriptPreview(
            stream_id=self.stream_id, revision=1, text=self.text, confidence=self.confidence,
            language="zh-CN", has_unstable_tail=self.tail,
            segments=[TranscriptSegment(text=self.text, start_ms=10, end_ms=200, confidence=self.confidence)],
            provider=ProviderMeta(provider_id="fake-server", model="test-stt", request_id=self.stream_id, latency_ms=1),
        )


def test_many_previews_do_not_finish_or_reopen_or_record_audio_twice():
    async def scenario():
        first = PreviewStream("s1")
        recorded, opened = [], []
        async def reopen():
            opened.append(True)
            return _validated(PreviewStream("s2"))
        capture = ContinuousSTT(_validated(first), reopen=reopen, record=recorded.append, bytes_per_second=32000)
        voice, silence = b"\0\x20" * 320, bytes(640)
        await capture.send_audio(voice)
        for _ in range(60):
            await capture.send_audio(silence)
            preview = await capture.preview()
            assert preview.text == first.text and not preview.has_unstable_tail
            assert not hasattr(preview, "is_final")
        assert first.finish_calls == 0 and not opened and len(recorded) == 61
        final = await capture.snapshot(resume=False)
        assert final.text == preview.text and final.is_final
        assert first.finish_calls == 1 and not opened
        assert capture.commit_final().text == first.text
        assert recorded == first.chunks
    asyncio.run(scenario())


def test_cumulative_preview_offsets_and_nested_objects_are_isolated():
    async def scenario():
        first, second = PreviewStream("s1"), PreviewStream("s2", text="第二句。", confidence=0.8)
        async def reopen():
            return _validated(second)
        capture = ContinuousSTT(_validated(first), reopen=reopen, record=lambda _: None, bytes_per_second=32000)
        await capture.send_audio(b"\0\x20" * 16000)
        await capture.snapshot()
        await capture.send_audio(b"\0\x20" * 320)
        preview = await capture.preview()
        assert preview.text == "第一段。第二句。"
        assert preview.confidence == 0.8
        assert [s.start_ms for s in preview.segments] == [10, 1010]
        preview.segments[0].text = "caller mutation"
        preview.provider.model = "caller mutation"
        again = await capture.preview()
        assert again.segments[0].text == "第一段。" and again.provider.model == "test-stt"
        await capture.abort()
    asyncio.run(scenario())


def test_reopen_does_not_hide_confirmed_prefix_while_waiting_for_inference_retry():
    async def scenario():
        first, second = PreviewStream("s1"), PreviewStream("s2")
        second.stable = False
        async def reopen():
            return _validated(second)
        capture = ContinuousSTT(_validated(first), reopen=reopen, record=lambda _: None, bytes_per_second=32000)
        await capture.send_audio(b"\0\x20" * 320)
        final = await capture.snapshot(resume=False)
        await capture.resume()
        await capture.send_audio(bytes(640))
        preview = await capture.preview()
        assert preview.text == final.text and preview.provider == final.provider
        assert not preview.has_unstable_tail
        # A new unrecognized voiced suffix must prevent submitting this old
        # prefix, even if it has not yet produced a provider partial.
        await capture.send_audio(b"\0\x20" * 320)
        assert (await capture.preview()).has_unstable_tail
        assert first.finish_calls == 1 and second.finish_calls == 0
        await capture.abort()
    asyncio.run(scenario())


@pytest.mark.parametrize("pcm,expected_unstable", [(b"\0\x20" * 320, True), (bytes(640), False)])
def test_inflight_voice_blocks_preview_but_silent_pcm_does_not_repeat_understanding(pcm, expected_unstable):
    async def scenario():
        gate = asyncio.Event()
        raw = PreviewStream("s1", send_gate=gate)
        async def reopen():
            raise AssertionError("no reopen expected")
        capture = ContinuousSTT(_validated(raw), reopen=reopen, record=lambda _: None, bytes_per_second=32000)
        sending = asyncio.create_task(capture.send_audio(pcm))
        await raw.send_started.wait()
        assert (await capture.preview()).has_unstable_tail is expected_unstable
        gate.set()
        await sending
        assert not (await capture.preview()).has_unstable_tail
        assert raw.finish_calls == 0
        await capture.abort()
    asyncio.run(scenario())


def test_preview_transport_failure_marks_failed_stream_and_recovery_really_rebuilds():
    async def scenario():
        first, second = PreviewStream("s1"), PreviewStream("s2")
        opened, recorded = [], []
        async def reopen():
            opened.append(True)
            return _validated(second)
        async def broken_preview():
            raise ProviderError("provider_timeout", "synthetic failure", retryable=True)
        first.preview = broken_preview
        capture = ContinuousSTT(_validated(first), reopen=reopen, record=recorded.append, bytes_per_second=32000)
        await capture.send_audio(b"\0\x20" * 320)
        with pytest.raises(ProviderError):
            await capture.preview()
        assert capture.recovery_required
        await capture.recover()
        assert not capture.recovery_required and opened == [True] and first.aborted
        assert second.chunks == recorded and len(recorded) == 1
        assert (await capture.preview()).text == second.text
        await capture.abort()
    asyncio.run(scenario())


def test_corrected_final_understanding_retry_can_use_saved_prefix_without_new_speech():
    from types import SimpleNamespace
    from test_answer_endpoint import _endpoint, _voice_then_pause, _until

    async def scenario():
        first, second = PreviewStream("s1"), PreviewStream("s2", text=None)
        original_preview = first.preview
        async def prefix_preview():
            preview = await original_preview()
            if preview:
                preview.text = "第一"
                preview.segments[0].text = "第一"
            return preview
        first.preview = prefix_preview
        second.stable = False
        opened, prepared, commits = [], [], []
        async def reopen():
            opened.append(True)
            return _validated(second)
        stream = ContinuousSTT(_validated(first), reopen=reopen, record=lambda _: None, bytes_per_second=32000)
        class Capture:
            is_open = True
            supports_stable_preview = True
            async def transcript_preview(self):
                return await stream.preview()
            async def transcript_snapshot(self, *, resume=True):
                return await stream.snapshot(resume=resume)
            async def resume_capture(self):
                await stream.resume()
            async def prepare_decision(self, transcript):
                prepared.append(transcript.text)
                return SimpleNamespace(understanding=SimpleNamespace(
                    problem={"temporary": True} if len(prepared) == 2 else None,
                    suggested_action="accept"))
        capture = Capture()
        async def commit(decision, guard):
            guard()
            commits.append(stream.commit_final())
        endpoint, clock, _, _ = _endpoint(capture=capture, commit_impl=commit)
        await stream.send_audio(b"\0\x20" * 320)
        _voice_then_pause(endpoint, clock)
        endpoint.start()
        await _until(lambda: len(prepared) == 2 and len(opened) == 1)
        assert not commits
        clock.value += 2
        await _until(lambda: commits)
        assert prepared == ["第一", "第一段。", "第一段。"]
        assert commits[0].text == "第一段。" and first.finish_calls == 1
        assert second.finish_calls == 0 and second.aborted
        await endpoint.close()
    asyncio.run(scenario())
