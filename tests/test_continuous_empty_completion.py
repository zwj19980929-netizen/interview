"""Normal no-word completion must not replay a noisy suffix indefinitely."""
import asyncio

import pytest

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ProviderMeta, StableTranscriptPreview, StreamingSTTEvent
from app.services.continuous_stt import ContinuousSTT
from test_continuous_stt import _RawStream, _validated


class EmptyStream(_RawStream):
    async def send_audio(self, chunk):
        self.chunks.append(chunk)
        return []

    async def finish(self):
        self.finish_calls += 1
        self.sequence += 1
        return [StreamingSTTEvent(
            stream_id=self.stream_id, sequence=self.sequence, type="transcript.empty", is_final=True,
            provider=ProviderMeta(provider_id="fake-server", model="test-stt", request_id=self.stream_id, latency_ms=1),
        )]


def test_acknowledged_empty_suffix_covers_audio_without_replaying_or_inventing_words():
    async def scenario():
        first, empty, next_stream = _RawStream("answer", text="合成回答。没有要补充的了。"), EmptyStream("empty"), _RawStream("next")
        opened, recorded = [], []
        async def reopen():
            raw = [empty, next_stream][len(opened)]
            opened.append(raw)
            return _validated(raw)
        capture = ContinuousSTT(_validated(first), reopen=reopen, record=recorded.append, bytes_per_second=32000)
        voice = b"\x00\x20" * 320
        await capture.send_audio(voice)
        final = await capture.snapshot()
        # Conservatively classified as voice locally, but server acknowledges
        # full processing with no words. Neither PCM nor a prior reply is lost.
        await capture.send_audio(voice)
        fresh = await capture.snapshot(resume=False)
        assert fresh == final and empty.finish_calls == 1
        assert not capture._unresolved and capture._segment_start_bytes == 1280
        await capture.resume()
        assert next_stream.chunks == [], "Confirmed no-word audio must never be replayed"
        await capture.snapshot(resume=False)  # no audio: abort the unused stream
        assert capture.commit_final().text == final.text
        assert recorded == [voice, voice] and len(opened) == 2
    asyncio.run(scenario())


def test_empty_first_segment_cannot_create_an_answer_or_confirm_finish():
    async def scenario():
        empty = EmptyStream("empty")
        async def reopen():
            raise AssertionError("No replay expected")
        capture = ContinuousSTT(_validated(empty), reopen=reopen, record=lambda _: None, bytes_per_second=32000)
        await capture.send_audio(b"\x00\x20" * 320)
        assert await capture.snapshot(resume=False) is None
        assert not capture._unresolved and not capture._segment_audio
        with pytest.raises(ProviderError, match="No server final"):
            capture.commit_final()
        await capture.abort()
    asyncio.run(scenario())


def test_empty_replacement_cannot_erase_words_observed_before_a_missing_final():
    async def scenario():
        lost = _RawStream("lost", text=None)
        empty = EmptyStream("replacement")
        async def reopen():
            return _validated(empty)
        capture = ContinuousSTT(_validated(lost), reopen=reopen, record=lambda _: None, bytes_per_second=32000)
        await capture.send_audio(b"\x00\x20" * 320)  # emits a nonempty partial
        assert await capture.snapshot() is None
        assert await capture.snapshot(resume=False) is None
        assert capture._unresolved and capture._segment_audio
        with pytest.raises(ProviderError):
            capture.commit_final()
        await capture.abort()
    asyncio.run(scenario())


def test_final_deadline_releases_failed_stream_and_recovers_all_accepted_pcm():
    async def scenario():
        gate = asyncio.Event()
        first, recovered = _RawStream("slow", finish_gate=gate), _RawStream("recovered")
        recorded = []
        async def reopen():
            return _validated(recovered)
        capture = ContinuousSTT(_validated(first), reopen=reopen, record=recorded.append,
                                bytes_per_second=32000, final_timeout_seconds=.03)
        voice, suffix = b"\x00\x20" * 320, bytes(640)
        await capture.send_audio(voice)
        finishing = asyncio.create_task(capture.snapshot(resume=False))
        await first.finish_started.wait()
        await capture.send_audio(suffix)
        with pytest.raises(ProviderError) as error:
            await asyncio.wait_for(finishing, timeout=1)
        assert error.value.code == "provider_timeout" and first.aborted
        assert capture.recovery_required and recorded == [voice, suffix]
        await capture.recover()
        assert recovered.chunks == recorded and recorded == [voice, suffix]
        assert (await capture.snapshot(resume=False)).text == recovered.text
        capture.commit_final()
    asyncio.run(scenario())


def test_recovery_never_shrinks_already_accepted_preparation_buffer():
    async def scenario():
        async def reopen():
            return _validated(EmptyStream("new"))
        capture = ContinuousSTT(_validated(_RawStream("first")), reopen=reopen,
                                record=lambda _: None, bytes_per_second=4)
        await capture.send_audio(b"aa")
        await capture.snapshot(resume=False)
        await capture.send_audio(bytes(160))  # forty seconds held during preparation
        capture._remember_failure(ProviderError("provider_timeout", "synthetic", retryable=True))
        await capture.send_audio(bytes(2))
        assert capture._pending_bytes == 162
        await capture.abort()
    asyncio.run(scenario())


class FailingObservedStream(EmptyStream):
    async def finish(self):
        raise ProviderError("provider_timeout", "Synthetic final failure.", retryable=True)


class PreviewOnlyStream(FailingObservedStream):
    async def preview(self):
        return StableTranscriptPreview(
            stream_id=self.stream_id, revision=1, text="稳定句也不能丢。", language="zh-CN",
            confidence=1.0, segments=[{"text": "稳定句也不能丢。", "start_ms": 0, "end_ms": 20}],
            provider=ProviderMeta(provider_id="fake-server", model="test-stt", request_id=self.stream_id, latency_ms=1),
            has_unstable_tail=False,
        )


def test_empty_recovery_cannot_erase_preview_that_never_emitted_partial():
    async def scenario():
        raw, replacement = PreviewOnlyStream("preview"), EmptyStream("replacement")
        async def reopen():
            return _validated(replacement)
        recorded = []
        capture = ContinuousSTT(_validated(raw), reopen=reopen, record=recorded.append, bytes_per_second=32000)
        pcm = bytes(640)
        await capture.send_audio(pcm)
        assert (await capture.preview()).text == "稳定句也不能丢。"
        with pytest.raises(ProviderError):
            await capture.snapshot(resume=False)
        await capture.recover()
        assert await capture.snapshot(resume=False) is None
        assert capture._unresolved and capture._segment_audio == [pcm]
        assert recorded == [pcm] and replacement.chunks == [pcm]
        with pytest.raises(ProviderError):
            capture.commit_final()
        await capture.abort()
    asyncio.run(scenario())


def test_empty_recovery_cannot_erase_partial_seen_only_during_replay():
    class ReplayPartialStream(FailingObservedStream):
        async def send_audio(self, chunk):
            self.chunks.append(chunk)
            self.sequence += 1
            return [StreamingSTTEvent(stream_id=self.stream_id, sequence=self.sequence,
                type="transcript.partial", text="重放时识别到了文字")]

    async def scenario():
        first, replay, empty = FailingObservedStream("first"), ReplayPartialStream("replay"), EmptyStream("empty")
        opened = []
        async def reopen():
            raw = [replay, empty][len(opened)]
            opened.append(raw)
            return _validated(raw)
        pcm, recorded = bytes(640), []
        capture = ContinuousSTT(_validated(first), reopen=reopen, record=recorded.append, bytes_per_second=32000)
        await capture.send_audio(pcm)
        with pytest.raises(ProviderError):
            await capture.snapshot(resume=False)
        await capture.recover()
        with pytest.raises(ProviderError):
            await capture.snapshot(resume=False)
        await capture.recover()
        assert await capture.snapshot(resume=False) is None
        assert capture._unresolved and capture._segment_audio == [pcm]
        assert recorded == [pcm] and replay.chunks == empty.chunks == [pcm]
        with pytest.raises(ProviderError):
            capture.commit_final()
        await capture.abort()
    asyncio.run(scenario())


def test_empty_recovery_cannot_erase_dashscope_reader_text_before_finish_timeout():
    from test_stt_empty_completion import EmptySocket, open_socket

    async def scenario():
        socket = EmptySocket([{"text": "下行已经识别到文字", "sentence_end": False,
                               "begin_time": 0}], acknowledge=False)
        validated = await open_socket(socket)
        empty = EmptyStream("empty")
        async def reopen():
            return _validated(empty)
        pcm, recorded = bytes(640), []
        capture = ContinuousSTT(validated, reopen=reopen, record=recorded.append,
                                bytes_per_second=32000, final_timeout_seconds=.05)
        await capture.send_audio(pcm)
        with pytest.raises(ProviderError) as error:
            await capture.snapshot(resume=False)
        assert error.value.code == "provider_timeout"
        # No send/finish call returned this partial to the business layer.
        assert validated.has_observed_text and capture._segment_text_seen
        await capture.recover()
        assert await capture.snapshot(resume=False) is None
        assert capture._unresolved and capture._segment_audio == [pcm]
        assert recorded == [pcm] and empty.chunks == [pcm]
        with pytest.raises(ProviderError):
            capture.commit_final()
        await capture.abort()
    asyncio.run(scenario())


def test_failed_reopen_does_not_move_confirmed_old_words_into_the_new_segment():
    async def scenario():
        first, empty = _RawStream("answer", text="已确认回答。"), EmptyStream("empty")
        attempts = []
        async def reopen():
            attempts.append(1)
            if len(attempts) == 1:
                raise ProviderError("provider_timeout", "Synthetic reopen failure.", retryable=True)
            return _validated(empty)
        capture = ContinuousSTT(_validated(first), reopen=reopen, record=lambda _: None, bytes_per_second=32000)
        await capture.send_audio(bytes(640))
        with pytest.raises(ProviderError):
            await capture.snapshot()
        await capture.send_audio(bytes(640))
        await capture.recover()
        final = await capture.snapshot(resume=False)
        assert final.text == "已确认回答。" and not capture._unresolved
        assert capture._segment_start_bytes == 1280 and not capture._segment_audio
        assert capture.commit_final().text == final.text
    asyncio.run(scenario())
