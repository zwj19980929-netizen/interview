"""Continuous captions stay live while a separate turn decision is pending."""

import asyncio

import pytest

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ProviderMeta, StreamingSTTEvent, StreamingSTTRequest, TranscriptSegment
from app.model_gateway.streaming import ValidatedSTTStream
from app.services.continuous_stt import ContinuousSTT


class Segment:
    def __init__(self, name, final, *, finish_gate=None):
        self.stream_id = name
        self.text = final
        self.finish_gate = finish_gate
        self.finish_entered = asyncio.Event()
        self.sequence = 1
        self.chunks = []
        self.aborted = False
        self.ready_events = [StreamingSTTEvent(stream_id=name, sequence=1, type="stream.ready")]

    async def send_audio(self, chunk):
        self.chunks.append(chunk)
        self.sequence += 1
        return [StreamingSTTEvent(stream_id=self.stream_id, sequence=self.sequence,
                                  type="transcript.partial", text="补充字幕%d" % len(self.chunks))]

    async def finish(self):
        self.finish_entered.set()
        if self.finish_gate is not None:
            await self.finish_gate.wait()
        if self.text is None:
            return []
        self.sequence += 1
        return [StreamingSTTEvent(stream_id=self.stream_id, sequence=self.sequence,
                                  type="transcript.final", is_final=True, text=self.text, confidence=.9,
                                  segments=[TranscriptSegment(text=self.text, start_ms=0, end_ms=100)],
                                  provider=ProviderMeta(provider_id="synthetic", model="stt", request_id=self.stream_id, latency_ms=1))]

    async def abort(self):
        self.aborted = True


def validated(raw):
    return ValidatedSTTStream(raw, StreamingSTTRequest(interview_id="synthetic", turn_id="turn"))


def test_rotation_replayed_caption_is_delivered_before_slow_decision_finishes():
    async def scenario():
        final_gate, decision_gate = asyncio.Event(), asyncio.Event()
        first = Segment("first", "原回答。", finish_gate=final_gate)
        second = Segment("continuation", "追加说明。")
        recorded = []

        async def reopen():
            return validated(second)

        capture = ContinuousSTT(validated(first), reopen=reopen, record=recorded.append, bytes_per_second=32)
        await capture.send_audio(b"aa")
        cut = asyncio.create_task(capture.snapshot(resume=True))
        await first.finish_entered.wait()
        assert await capture.send_audio(b"bb") == []
        assert await capture.send_audio(b"cc") == []
        final_gate.set()
        initial = await cut
        decision = asyncio.create_task(decision_gate.wait())
        assert initial.text == "原回答。"
        # Only the latest display hypothesis needs buffering. It must not
        # disappear merely because the audio arrived during provider rotation.
        updates = await capture.send_audio(b"")
        assert len(updates) == 1
        assert updates[0].text == "原回答。补充字幕2"
        assert updates[0].type == "transcript.partial" and not updates[0].is_final
        assert not decision.done()
        assert await capture.send_audio(b"") == []
        assert second.chunks == [b"bb", b"cc"]
        with pytest.raises(ProviderError) as error:
            capture.commit_final()
        assert error.value.code == "provider_snapshot_stale"
        final = await capture.snapshot(resume=False)
        assert final.text == "原回答。追加说明。"
        assert capture.commit_final().text == final.text
        assert recorded == [b"aa", b"bb", b"cc"]
        decision_gate.set()
        await decision
    asyncio.run(scenario())


def test_next_audio_delivery_includes_the_caption_produced_while_resuming():
    async def scenario():
        first, second = Segment("first", "原回答。"), Segment("next", "追加说明。")
        recorded = []

        async def reopen():
            return validated(second)

        capture = ContinuousSTT(validated(first), reopen=reopen, record=recorded.append, bytes_per_second=32)
        await capture.send_audio(b"aa")
        await capture.snapshot(resume=False)
        await capture.send_audio(b"bb")
        await capture.resume()
        updates = await capture.send_audio(b"cc")
        assert [event.text for event in updates] == ["原回答。补充字幕1", "原回答。补充字幕2"]
        assert all(event.type == "transcript.partial" for event in updates)
        assert await capture.send_audio(b"") == []
        assert recorded == [b"aa", b"bb", b"cc"]
        await capture.abort()
    asyncio.run(scenario())


def test_rotated_partial_never_certifies_unrecognized_suffix_as_complete():
    async def scenario():
        first, second = Segment("first", "原回答。"), Segment("next", None)

        async def reopen():
            return validated(second)

        capture = ContinuousSTT(validated(first), reopen=reopen, record=lambda _: None, bytes_per_second=32)
        await capture.send_audio(b"aa")
        await capture.snapshot(resume=False)
        await capture.send_audio(b"bb")
        await capture.resume()
        assert (await capture.send_audio(b""))[0].text == "原回答。补充字幕1"
        assert await capture.snapshot(resume=False) is None
        with pytest.raises(ProviderError) as error:
            capture.commit_final()
        assert error.value.code == "provider_final_transcript_missing"
        await capture.abort()
    asyncio.run(scenario())


def test_final_and_abort_remove_obsolete_pending_caption():
    async def scenario():
        for closing in ("final", "abort"):
            first, second = Segment("first", "原回答。"), Segment("next", "最终修正。")

            async def reopen():
                return validated(second)

            capture = ContinuousSTT(validated(first), reopen=reopen, record=lambda _: None, bytes_per_second=32)
            await capture.send_audio(b"aa")
            await capture.snapshot(resume=False)
            await capture.send_audio(b"bb")
            await capture.resume()
            if closing == "final":
                assert (await capture.snapshot(resume=False)).text == "原回答。最终修正。"
            else:
                await capture.abort()
            if closing == "final":
                assert await capture.send_audio(b"") == []
            else:
                with pytest.raises(ProviderError):
                    await capture.send_audio(b"")
                assert second.aborted
            await capture.abort()
    asyncio.run(scenario())
