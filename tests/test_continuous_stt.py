import asyncio

import pytest

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ProviderMeta, StreamingSTTEvent, StreamingSTTRequest, TranscriptSegment
from app.model_gateway.streaming import ValidatedSTTStream
from app.services.continuous_stt import ContinuousSTT


class _RawStream:
    def __init__(self, name, text="第一段。", confidence=0.9, *, finish_gate=None, send_gate=None):
        self.stream_id = name
        self.text, self.confidence = text, confidence
        self.finish_gate, self.send_gate = finish_gate, send_gate
        self.finish_started = asyncio.Event()
        self.send_started = asyncio.Event()
        self.sequence = 1
        self.ready_events = [StreamingSTTEvent(stream_id=name, sequence=1, type="stream.ready")]
        self.chunks = []
        self.aborted = False
        self.finish_calls = 0

    async def send_audio(self, chunk):
        self.chunks.append(chunk)
        self.send_started.set()
        if self.send_gate is not None:
            await self.send_gate.wait()
        self.sequence += 1
        return [StreamingSTTEvent(stream_id=self.stream_id, sequence=self.sequence,
                                 type="transcript.partial", text="未确认的临时字")]

    async def finish(self):
        self.finish_calls += 1
        self.finish_started.set()
        if self.finish_gate is not None:
            await self.finish_gate.wait()
        if self.text is None:
            return []
        self.sequence += 1
        return [StreamingSTTEvent(
            stream_id=self.stream_id, sequence=self.sequence, type="transcript.final",
            is_final=True, text=self.text, confidence=self.confidence,
            segments=[TranscriptSegment(text=self.text, start_ms=10, end_ms=200, confidence=self.confidence)],
            provider=ProviderMeta(provider_id="fake-server", model="test-stt", request_id=self.stream_id, latency_ms=1),
        )]

    async def abort(self):
        self.aborted = True


def _validated(raw):
    return ValidatedSTTStream(raw, StreamingSTTRequest(interview_id="synthetic", turn_id="turn1"))


def test_rotation_buffers_in_order_records_each_frame_once_and_keeps_partial_nonfinal():
    async def scenario():
        gate = asyncio.Event()
        first, second = _RawStream("s1", finish_gate=gate), _RawStream("s2", text="第二段。")
        recorded = []

        async def reopen():
            return _validated(second)

        capture = ContinuousSTT(_validated(first), reopen=reopen, record=recorded.append, bytes_per_second=4)
        await capture.send_audio(b"aa")
        snapshot_task = asyncio.create_task(capture.snapshot())
        await first.finish_started.wait()
        assert await capture.send_audio(b"bb") == []
        assert await capture.send_audio(b"cc") == []
        assert recorded == [b"aa", b"bb", b"cc"]
        gate.set()
        snapshot = await snapshot_task
        assert snapshot.text == "第一段。" and snapshot.is_final
        assert "临时" not in snapshot.text
        assert recorded == [b"aa", b"bb", b"cc"]
        assert first.chunks == [b"aa"] and second.chunks == [b"bb", b"cc"]
        assert capture.forwarded_bytes == 6
        assert not capture.paused
        await capture.abort()

    asyncio.run(scenario())


def test_snapshots_join_only_server_finals_keep_recording_offsets_and_lowest_confidence():
    async def scenario():
        first, second = _RawStream("s1", confidence=0.95), _RawStream("s2", text="小声尾句。", confidence=0.35)
        recorded = []

        async def reopen():
            return _validated(second)

        capture = ContinuousSTT(_validated(first), reopen=reopen, record=recorded.append, bytes_per_second=4)
        await capture.send_audio(b"aa")
        initial = await capture.snapshot()
        initial.text = "不能反向修改内部累计转写"
        await capture.send_audio(b"bb")
        final = await capture.snapshot(resume=False)
        assert final.text == "第一段。小声尾句。"
        assert final.confidence == 0.35
        assert [(s.start_ms, s.end_ms, s.confidence) for s in final.segments] == [
            (10, 200, 0.95), (510, 700, 0.35),
        ]
        assert capture.paused
        assert (await capture.finish())[0].text == final.text
        assert recorded == [b"aa", b"bb"]
        with pytest.raises(ProviderError) as exc:
            await capture.send_audio(b"cc")
        assert exc.value.code == "provider_stream_closed"

    asyncio.run(scenario())


def test_first_segment_without_final_does_not_manufacture_a_transcript():
    async def scenario():
        raw = _RawStream("empty", text=None)

        async def reopen():
            return _validated(_RawStream("next", text=None))

        capture = ContinuousSTT(_validated(raw), reopen=reopen, record=lambda _: None, bytes_per_second=4)
        await capture.send_audio(b"aa")
        assert await capture.snapshot(resume=False) is None
        with pytest.raises(ProviderError) as exc:
            await capture.finish()
        assert exc.value.code == "provider_final_transcript_missing"
        await capture.abort()

    asyncio.run(scenario())


def test_missing_final_for_continuation_cannot_silently_reuse_old_answer():
    async def scenario():
        first, next_raw = _RawStream("s1"), _RawStream("unrecognized-tail", text=None)

        async def reopen():
            return _validated(next_raw)

        capture = ContinuousSTT(_validated(first), reopen=reopen, record=lambda _: None, bytes_per_second=4)
        await capture.send_audio(b"aa")
        assert (await capture.snapshot()).text == "第一段。"
        await capture.send_audio(b"\xff\x7f")
        try:
            unresolved = await capture.snapshot(resume=False)
        except ProviderError:
            unresolved = None
        assert unresolved is None, "A missing continuation final must not certify the old prefix as complete"
        await capture.abort()

    asyncio.run(scenario())


def test_audio_and_rotation_buffer_limits_are_explicit_failures_not_silent_drops():
    async def scenario():
        first = _RawStream("s1")
        recorded = []

        async def reopen():
            return _validated(_RawStream("s2"))

        capture = ContinuousSTT(_validated(first), reopen=reopen, record=recorded.append,
                                bytes_per_second=4, max_bytes=6)
        await capture.send_audio(b"aa")
        with pytest.raises(ProviderError) as exc:
            await capture.send_audio(b"bbbbbb")
        assert exc.value.code == "provider_audio_stream_too_large"
        assert recorded == [b"aa"]
        await capture.abort()

        capture = ContinuousSTT(_validated(_RawStream("s3")), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        await capture.snapshot(resume=False)
        await capture.send_audio(b"c" * 240)
        with pytest.raises(ProviderError) as exc:
            await capture.send_audio(b"dd")
        assert exc.value.code == "provider_audio_recovery_buffer_exceeded"
        assert not exc.value.retryable
        await capture.abort()

    asyncio.run(scenario())


def test_abort_during_snapshot_does_not_reopen_or_forward_after_close():
    async def scenario():
        gate = asyncio.Event()
        first = _RawStream("s1", finish_gate=gate)
        reopens, recorded = [], []

        async def reopen():
            reopens.append(True)
            return _validated(_RawStream("s2"))

        capture = ContinuousSTT(_validated(first), reopen=reopen, record=recorded.append, bytes_per_second=4)
        await capture.send_audio(b"aa")
        task = asyncio.create_task(capture.snapshot())
        await first.finish_started.wait()
        await capture.send_audio(b"bb")
        await capture.abort()
        gate.set()
        await asyncio.gather(task, return_exceptions=True)
        assert not reopens, "Closing capture must cancel a pending recognition rotation"
        assert recorded == [b"aa", b"bb"]
        assert first.aborted, "The detached in-flight recognition stream still needs closing"

    asyncio.run(scenario())


def test_cancelled_snapshot_closes_detached_provider_stream():
    async def scenario():
        gate = asyncio.Event()
        first = _RawStream("s1", finish_gate=gate)

        async def reopen():
            return _validated(_RawStream("s2"))

        capture = ContinuousSTT(_validated(first), reopen=reopen, record=lambda _: None, bytes_per_second=4)
        await capture.send_audio(b"aa")
        task = asyncio.create_task(capture.snapshot())
        await first.finish_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert first.aborted, "Cancelled finalization must not orphan its provider stream"
        await capture.abort()

    asyncio.run(scenario())


def test_finish_never_discards_accepted_unrecorded_continuation():
    async def scenario():
        recorded = []

        async def reopen():
            return _validated(_RawStream("s2", text="补充。"))

        capture = ContinuousSTT(_validated(_RawStream("s1")), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        await capture.send_audio(b"aa")
        await capture.snapshot(resume=False)
        await capture.send_audio(b"bb")
        try:
            result = await capture.finish()
        except ProviderError:
            # Rejecting a raced finalization is safe; silently dropping audio is not.
            pass
        else:
            assert recorded == [b"aa", b"bb"]
            assert result[0].text == "第一段。补充。"
        await capture.abort()

    asyncio.run(scenario())


def test_confirmed_pure_silence_after_cut_does_not_prevent_fast_commit_forever():
    async def scenario():
        recorded = []

        async def reopen():
            raise AssertionError("A confirmed silent suffix does not need a new STT segment")

        capture = ContinuousSTT(_validated(_RawStream("s1")), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        await capture.send_audio(b"aa")
        await capture.snapshot(resume=False)
        await capture.send_audio(bytes(2))
        assert (await capture.finish())[0].text == "第一段。"
        assert recorded[0] == b"aa"
        # Implementations may retain silence in the private recording; either
        # choice must be explicit and may never silently drop voiced suffixes.
        assert all(not any(chunk) for chunk in recorded[1:])
        await capture.abort()

    asyncio.run(scenario())


def test_unresolved_segment_is_replayed_without_duplicate_recording_or_offset_shift():
    async def scenario():
        first = _RawStream("s1", text="前缀。")
        missing = _RawStream("s2", text=None)
        repaired = _RawStream("s3", text="重放尾句与补充。", confidence=0.5)
        streams = iter([missing, repaired])
        recorded = []

        async def reopen():
            return _validated(next(streams))

        capture = ContinuousSTT(_validated(first), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        await capture.send_audio(b"aa")
        assert (await capture.snapshot()).text == "前缀。"
        await capture.send_audio(b"bb")
        assert await capture.snapshot() is None
        assert repaired.chunks == [b"bb"]
        assert recorded == [b"aa", b"bb"]
        await capture.send_audio(b"cc")
        result = await capture.snapshot(resume=False)
        assert result.text == "前缀。重放尾句与补充。"
        assert result.confidence == 0.5
        assert result.segments[-1].start_ms == 510
        assert capture.forwarded_bytes == 6
        assert recorded == [b"aa", b"bb", b"cc"]
        assert repaired.chunks == [b"bb", b"cc"]
        await capture.abort()

    asyncio.run(scenario())


def test_close_during_reopen_aborts_the_newly_returned_provider():
    async def scenario():
        first, late = _RawStream("s1"), _RawStream("late-open")
        entered, release = asyncio.Event(), asyncio.Event()
        recorded = []

        async def reopen():
            entered.set()
            await release.wait()
            return _validated(late)

        capture = ContinuousSTT(_validated(first), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        await capture.send_audio(b"aa")
        task = asyncio.create_task(capture.snapshot())
        await entered.wait()
        await capture.send_audio(b"bb")
        await capture.abort()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert late.aborted and not late.chunks
        assert recorded == [b"aa", b"bb"]

    asyncio.run(scenario())


class _FaultStream(_RawStream):
    def __init__(self, name, *, send_error=None, finish_error=None, **kwargs):
        super().__init__(name, **kwargs)
        self.send_error = send_error
        self.finish_error = finish_error

    async def send_audio(self, chunk):
        events = await super().send_audio(chunk)
        if self.send_error is not None:
            raise self.send_error
        return events

    async def finish(self):
        events = await super().finish()
        if self.finish_error is not None:
            raise self.finish_error
        return events


def _network_error():
    return ProviderError("provider_timeout", "Synthetic timeout.", retryable=True)


def test_retryable_send_failure_records_before_provider_and_recovers_entire_unconfirmed_segment():
    async def scenario():
        error = _network_error()
        first, second = _FaultStream("failed", send_error=error), _RawStream("recovered", text="完整回答。")
        recorded = []

        async def reopen():
            assert first.aborted
            return _validated(second)

        capture = ContinuousSTT(_validated(first), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        assert await capture.send_audio(b"aa") == []
        assert recorded == [b"aa"]
        assert capture.recovery_required and capture.recovery_error is error
        assert await capture.send_audio(b"bb") == []
        assert recorded == [b"aa", b"bb"]
        assert first.chunks == [b"aa"]
        with pytest.raises(ProviderError) as raised:
            await capture.snapshot(resume=False)
        assert raised.value is error
        await capture.recover()
        assert not capture.recovery_required and capture.recovery_error is None
        assert second.chunks == [b"aa", b"bb"]
        assert capture.forwarded_bytes == 4 and recorded == [b"aa", b"bb"]
        assert (await capture.finish())[0].text == "完整回答。"

    asyncio.run(scenario())


def test_send_recording_and_owner_failures_propagate_without_entering_provider():
    async def scenario():
        raw = _RawStream("s1")
        failure = RuntimeError("Synthetic storage or owner fence.")

        def record(_):
            raise failure

        async def reopen():
            raise AssertionError("A recorder failure must not become a provider retry")

        capture = ContinuousSTT(_validated(raw), reopen=reopen, record=record, bytes_per_second=4)
        with pytest.raises(RuntimeError) as raised:
            await capture.send_audio(b"aa")
        assert raised.value is failure
        assert not raw.chunks and capture.forwarded_bytes == 0
        assert not capture.recovery_required
        await capture.abort()

    asyncio.run(scenario())


def test_nonretryable_provider_error_is_not_swallowed_but_accepted_pcm_is_retained():
    async def scenario():
        error = ProviderError("provider_authentication_failed", "Synthetic invalid credential.")
        raw = _FaultStream("s1", send_error=error)
        recorded = []

        async def reopen():
            raise AssertionError("Caller must not automatically retry authentication failures")

        capture = ContinuousSTT(_validated(raw), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        with pytest.raises(ProviderError) as raised:
            await capture.send_audio(b"aa")
        assert raised.value is error
        assert recorded == [b"aa"] and capture.recovery_error is error
        await capture.abort()

    asyncio.run(scenario())


def test_failed_finish_retains_tail_and_recovers_without_repeating_confirmed_prefix_or_offsets():
    async def scenario():
        error = _network_error()
        gate = asyncio.Event()
        first = _RawStream("prefix", text="前缀。")
        tail = _FaultStream("tail", finish_error=error, finish_gate=gate)
        repaired = _RawStream("repaired", text="尾句及补充。")
        streams = iter([tail, repaired])
        recorded = []

        async def reopen():
            return _validated(next(streams))

        capture = ContinuousSTT(_validated(first), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        await capture.send_audio(b"aa")
        await capture.snapshot()
        await capture.send_audio(b"bb")
        finishing = asyncio.create_task(capture.snapshot(resume=False))
        await tail.finish_started.wait()
        await capture.send_audio(b"cc")
        assert recorded == [b"aa", b"bb", b"cc"]
        gate.set()
        with pytest.raises(ProviderError) as raised:
            await finishing
        assert raised.value is error
        with pytest.raises(ProviderError):
            capture.confirmed_final()
        await capture.recover()
        assert tail.aborted and repaired.chunks == [b"bb", b"cc"]
        final = (await capture.finish())[0]
        assert final.text == "前缀。尾句及补充。"
        assert [(part.start_ms, part.end_ms) for part in final.segments] == [(10, 200), (510, 700)]
        assert capture.forwarded_bytes == 6 and recorded == [b"aa", b"bb", b"cc"]

    asyncio.run(scenario())


def test_failed_reopen_keeps_rotation_intake_and_retry_does_not_duplicate_a_confirmed_final():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        first, repaired = _RawStream("first"), _RawStream("repaired", text="补充。")
        recorded, opens = [], []

        async def reopen():
            opens.append(True)
            if len(opens) == 1:
                entered.set()
                await release.wait()
                raise _network_error()
            return _validated(repaired)

        capture = ContinuousSTT(_validated(first), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        await capture.send_audio(b"aa")
        task = asyncio.create_task(capture.snapshot())
        await entered.wait()
        await capture.send_audio(b"bb")
        release.set()
        with pytest.raises(ProviderError):
            await task
        with pytest.raises(ProviderError):
            capture.commit_final()
        await capture.recover()
        assert repaired.chunks == [b"bb"]
        assert (await capture.finish())[0].text == "第一段。补充。"
        assert recorded == [b"aa", b"bb"] and len(opens) == 2

    asyncio.run(scenario())


def test_failed_replay_preserves_all_unconfirmed_pcm_for_next_attempt():
    async def scenario():
        initial = _FaultStream("failed-send", send_error=_network_error())
        failed_replay = _FaultStream("failed-replay", send_error=_network_error())
        repaired = _RawStream("success")
        streams = iter([failed_replay, repaired])
        recorded = []

        async def reopen():
            return _validated(next(streams))

        capture = ContinuousSTT(_validated(initial), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        await capture.send_audio(b"aa")
        await capture.send_audio(b"bb")
        with pytest.raises(ProviderError):
            await capture.recover()
        await capture.send_audio(b"cc")
        assert capture.recovery_required
        await capture.recover()
        assert initial.aborted and failed_replay.aborted
        assert repaired.chunks == [b"aa", b"bb", b"cc"]
        assert recorded == [b"aa", b"bb", b"cc"] and capture.forwarded_bytes == 6
        await capture.abort()

    asyncio.run(scenario())


def test_recovery_accepts_six_seconds_of_backlog_without_starving_the_provider_sender():
    async def scenario():
        class ScheduledSender(_RawStream):
            def __init__(self):
                super().__init__("scheduled")
                self.pending_bytes = 0
                self.max_pending_bytes = 0
                self.scheduled = asyncio.Event()
                self.worker = asyncio.create_task(self.run_sender())

            async def run_sender(self):
                while True:
                    await self.scheduled.wait()
                    self.scheduled.clear()
                    self.pending_bytes = 0

            async def send_audio(self, chunk):
                self.pending_bytes += len(chunk)
                if self.pending_bytes > 32000 * 5:
                    raise ProviderError("provider_backpressure_exceeded", "Synthetic sender was starved.", retryable=True)
                self.max_pending_bytes = max(self.max_pending_bytes, self.pending_bytes)
                self.scheduled.set()
                return await super().send_audio(chunk)

            async def abort(self):
                self.worker.cancel()
                await asyncio.gather(self.worker, return_exceptions=True)
                await super().abort()

        first = _FaultStream("failed", send_error=_network_error())
        second = ScheduledSender()
        recorded = []

        async def reopen():
            return _validated(second)

        capture = ContinuousSTT(_validated(first), reopen=reopen,
                                record=recorded.append, bytes_per_second=32000)
        chunks = [bytes([index % 255 + 1]) * 640 for index in range(300)]
        for chunk in chunks:
            await capture.send_audio(chunk)
        assert sum(map(len, recorded)) == 32000 * 6
        await asyncio.wait_for(capture.recover(), timeout=1)
        assert second.chunks == chunks and recorded == chunks
        assert second.max_pending_bytes <= 1280
        assert capture.forwarded_bytes == 32000 * 6 and not capture.recovery_required
        await capture.abort()

    asyncio.run(scenario())


def test_recovery_buffer_overflow_is_sticky_even_if_an_older_reopen_finishes_later():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        initial = _FaultStream("failed", send_error=_network_error())
        late = _RawStream("late")
        recorded = []

        async def reopen():
            entered.set()
            await release.wait()
            return _validated(late)

        capture = ContinuousSTT(_validated(initial), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        await capture.send_audio(b"aa")
        task = asyncio.create_task(capture.recover())
        await entered.wait()
        await capture.send_audio(b"b" * 240)
        with pytest.raises(ProviderError) as raised:
            await capture.send_audio(b"cc")
        assert raised.value.code == "provider_audio_recovery_buffer_exceeded"
        release.set()
        with pytest.raises(ProviderError):
            await task
        for operation in (lambda: capture.send_audio(b"dd"), capture.recover, capture.finish):
            with pytest.raises(ProviderError) as raised:
                await operation()
            assert raised.value.code == "provider_audio_recovery_buffer_exceeded"
        assert recorded == [b"aa", b"b" * 240]
        assert not late.chunks
        await capture.abort()

    asyncio.run(scenario())


def test_cancelled_send_and_replay_propagate_cancellation_and_keep_audio_for_recovery():
    async def scenario():
        gate = asyncio.Event()
        first = _RawStream("cancelled-send", send_gate=gate)
        second = _RawStream("cancelled-replay", send_gate=gate)
        repaired = _RawStream("repaired")
        streams = iter([second, repaired])
        recorded = []

        async def reopen():
            return _validated(next(streams))

        capture = ContinuousSTT(_validated(first), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        task = asyncio.create_task(capture.send_audio(b"aa"))
        await first.send_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert capture.recovery_required
        await capture.send_audio(b"bb")
        recovery = asyncio.create_task(capture.recover())
        await second.send_started.wait()
        recovery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recovery
        assert capture.recovery_required
        await capture.recover()
        assert first.aborted and second.aborted and repaired.chunks == [b"aa", b"bb"]
        assert capture.forwarded_bytes == 4 and recorded == [b"aa", b"bb"]
        await capture.abort()

    asyncio.run(scenario())


def test_concurrent_recoveries_share_completed_rebuild_and_snapshot_waits_for_replay():
    async def scenario():
        first = _FaultStream("failed", send_error=_network_error())
        gate = asyncio.Event()
        repaired = _RawStream("repaired", send_gate=gate)
        opens, recorded = [], []

        async def reopen():
            opens.append(True)
            return _validated(repaired)

        capture = ContinuousSTT(_validated(first), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        await capture.send_audio(b"aa")
        recovery = asyncio.create_task(capture.recover())
        await repaired.send_started.wait()
        duplicate = asyncio.create_task(capture.recover())
        snapshot = asyncio.create_task(capture.snapshot(resume=False))
        await capture.send_audio(b"bb")
        await asyncio.sleep(0)
        assert repaired.finish_calls == 0
        gate.set()
        await asyncio.gather(recovery, duplicate)
        assert (await snapshot).text == "第一段。"
        assert len(opens) == 1 and repaired.chunks == [b"aa", b"bb"]
        assert recorded == [b"aa", b"bb"]
        await capture.abort()

    asyncio.run(scenario())


def test_snapshot_waits_for_inflight_send_and_captures_queued_audio_without_parallel_forward():
    async def scenario():
        gate = asyncio.Event()
        first = _RawStream("s1", send_gate=gate)
        recorded = []

        async def reopen():
            raise AssertionError("snapshot must remain paused")

        capture = ContinuousSTT(_validated(first), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        sending = asyncio.create_task(capture.send_audio(b"aa"))
        await first.send_started.wait()
        await capture.send_audio(b"bb")
        snapshot = asyncio.create_task(capture.snapshot(resume=False))
        await asyncio.sleep(0)
        assert not first.finish_started.is_set()
        gate.set()
        await sending
        assert (await snapshot).text == "第一段。"
        assert first.chunks == [b"aa", b"bb"] and recorded == [b"aa", b"bb"]
        assert capture.forwarded_bytes == 4
        await capture.abort()

    asyncio.run(scenario())


@pytest.mark.parametrize("held_stage", ["open", "replay"])
def test_abort_during_recover_never_resurrects_or_forwards_new_audio(held_stage):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        first = _FaultStream("failed", send_error=_network_error())
        late = _RawStream("late", send_gate=release if held_stage == "replay" else None)
        recorded = []

        async def reopen():
            if held_stage == "open":
                entered.set()
                await release.wait()
            return _validated(late)

        capture = ContinuousSTT(_validated(first), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        await capture.send_audio(b"aa")
        recovery = asyncio.create_task(capture.recover())
        await (entered if held_stage == "open" else late.send_started).wait()
        await capture.send_audio(b"bb")
        await capture.abort()
        release.set()
        with pytest.raises(ProviderError) as raised:
            await recovery
        assert raised.value.code == "provider_stream_closed"
        assert late.aborted and recorded == [b"aa", b"bb"]
        assert late.chunks == ([] if held_stage == "open" else [b"aa"])
        assert not capture.recovery_required
        with pytest.raises(ProviderError):
            await capture.send_audio(b"cc")

    asyncio.run(scenario())


def test_cancelled_snapshot_can_recover_accepted_audio_and_does_not_reuse_its_final():
    async def scenario():
        gate = asyncio.Event()
        first = _RawStream("cancelled-final", finish_gate=gate)
        recovered = _RawStream("recovered-final", text="重新确认的完整回答。")
        recorded = []

        async def reopen():
            return _validated(recovered)

        capture = ContinuousSTT(_validated(first), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        await capture.send_audio(b"aa")
        task = asyncio.create_task(capture.snapshot(resume=False))
        await first.finish_started.wait()
        await capture.send_audio(b"bb")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert first.aborted and capture.recovery_required
        with pytest.raises(ProviderError):
            capture.commit_final()
        await capture.recover()
        assert recovered.chunks == [b"aa", b"bb"]
        assert (await capture.finish())[0].text == "重新确认的完整回答。"
        assert recorded == [b"aa", b"bb"]

    asyncio.run(scenario())


def test_overflow_while_finishing_cannot_return_a_validated_but_incomplete_snapshot():
    async def scenario():
        gate = asyncio.Event()
        first = _RawStream("s1", finish_gate=gate)
        recorded = []

        async def reopen():
            raise AssertionError("A capture gap requires a new capture, not replay")

        capture = ContinuousSTT(_validated(first), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        await capture.send_audio(b"aa")
        snapshot = asyncio.create_task(capture.snapshot(resume=False))
        await first.finish_started.wait()
        await capture.send_audio(bytes(240))
        with pytest.raises(ProviderError):
            await capture.send_audio(b"bb")
        gate.set()
        with pytest.raises(ProviderError) as raised:
            await snapshot
        assert raised.value.code == "provider_audio_recovery_buffer_exceeded"
        with pytest.raises(ProviderError):
            capture.commit_final()
        assert recorded == [b"aa", bytes(240)]
        await capture.abort()

    asyncio.run(scenario())


def test_recovery_does_not_open_another_provider_until_failed_stream_cleanup_is_confirmed():
    async def scenario():
        class FailedCleanup(_FaultStream):
            async def abort(self):
                if not self.aborted:
                    self.aborted = True
                    raise ProviderError("provider_stream_close_failed", "Synthetic close failure.")

        first = FailedCleanup("failed", send_error=_network_error())
        recovered = _RawStream("recovered")
        opens, recorded = [], []

        async def reopen():
            opens.append(True)
            return _validated(recovered)

        capture = ContinuousSTT(_validated(first), reopen=reopen,
                                record=recorded.append, bytes_per_second=4)
        await capture.send_audio(b"aa")
        with pytest.raises(ProviderError) as raised:
            await capture.recover()
        assert raised.value.code == "provider_stream_close_failed"
        assert not opens and recorded == [b"aa"]
        await capture.send_audio(b"bb")
        await capture.recover()
        assert len(opens) == 1 and recovered.chunks == [b"aa", b"bb"]
        await capture.abort()

    asyncio.run(scenario())
