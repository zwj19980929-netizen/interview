"""Synthetic end-to-end transport orchestration, without rooms or providers."""

import asyncio
from types import SimpleNamespace

import pytest

from app.adapters.livekit_audio_output import LiveKitApprovedAudioPublication
from app.core.errors import ApiError
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ProviderMeta
from app.model_gateway.tts_streaming import TTSStreamEvent, ValidatedTTSStream
from app.services.approved_speech_output import ApprovedSpeechOutput
from app.services import approved_speech_output


class _RawTTS:
    def __init__(self, chunks, *, final=True, error=None, rate=24000, provider_id="synthetic_tts"):
        self.stream_id = "synthetic_tts_stream"
        self.meta = ProviderMeta(provider_id=provider_id, model="synthetic_tts_model", request_id="synthetic_request", latency_ms=0)
        self.ready_event = self.event(1, "stream.ready", sample_rate_hz=rate)
        self.chunks, self.final, self.error = chunks, final, error
        self.started, self.chunk_reached, self.eof_reached = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.read_gate = None
        self.eof_gate = None
        self.aborted = 0
        self.read_cancelled = False

    def event(self, sequence, kind, **kwargs):
        return TTSStreamEvent(stream_id=self.stream_id, sequence=sequence, type=kind,
                              provider=self.meta, **{"sample_rate_hz": 24000, **kwargs})

    async def events(self):
        self.started.set()
        sequence = 1
        try:
            for chunk in self.chunks:
                self.chunk_reached.set()
                if self.read_gate is not None:
                    await self.read_gate.wait()
                sequence += 1
                yield self.event(sequence, "audio.chunk", pcm_s16le=chunk)
            if self.error is not None:
                raise self.error
            if self.final:
                yield self.event(sequence + 1, "audio.final", total_audio_bytes=sum(map(len, self.chunks)))
            self.eof_reached.set()
            if self.eof_gate is not None:
                await self.eof_gate.wait()
        except asyncio.CancelledError:
            self.read_cancelled = True
            raise

    async def abort(self):
        self.aborted += 1


class _Publisher:
    def __init__(self):
        self.publication = LiveKitApprovedAudioPublication(
            "synthetic_room", "performance_synthetic", "expression:performance_synthetic",
            "TR_synthetic_exact", "approved-expression:performance_synthetic", 24000, 1,
        )
        self.opened = 0
        self.published = []
        self.publish_reached, self.finish_reached = asyncio.Event(), asyncio.Event()
        self.publish_gate, self.finish_gate = None, None
        self.publish_delay = 0
        self.publish_error = None
        self.finish_calls = 0
        self.close_calls = 0
        self.abort_calls = 0

    def bind_output(self, output_id):
        self.output_id = output_id

    async def open(self):
        self.opened += 1
        return self.publication

    async def publish(self, chunk):
        self.published.append(chunk)
        self.publish_reached.set()
        if self.publish_gate is not None:
            await self.publish_gate.wait()
        if self.publish_delay:
            await asyncio.sleep(self.publish_delay)
        if self.publish_error is not None:
            raise self.publish_error

    async def finish(self):
        self.finish_calls += 1
        self.finish_reached.set()
        if self.finish_gate is not None:
            await self.finish_gate.wait()
        return self.publication

    async def aclose(self):
        self.close_calls += 1

    async def abort(self):
        self.abort_calls += 1


def _harness(chunks=None, *, final=True, error=None, rate=24000, provider_id="synthetic_tts", **limits):
    raw = _RawTTS(chunks if chunks is not None else [b"\1\0" * 480, b"\2\0" * 240],
                  final=final, error=error, rate=rate, provider_id=provider_id)
    stream = ValidatedTTSStream(raw, provider_id=provider_id, model=raw.meta.model, read_timeout_s=1)
    publisher = _Publisher()
    current = {"valid": True}
    archives = []
    notifications = []
    producer_notified = asyncio.Event()

    def fence():
        if not current["valid"]:
            raise ApiError("EXPRESSION_OWNER_STALE", "Synthetic owner replaced", status_code=409)

    def archive(**payload):
        archives.append(payload)
        return {"audio_uri": "private-file://synthetic-complete-expression"}

    async def on_finished(payload):
        notifications.append(payload)
        producer_notified.set()

    output = ApprovedSpeechOutput(
        performance_id="performance_synthetic", stream=stream, publisher=publisher,
        assert_current=fence, archive=archive, ready_timeout=0.5, drain_timeout=0.5,
        total_timeout=limits.pop("total_timeout", 2), **limits,
    )
    return SimpleNamespace(output=output, raw=raw, publisher=publisher, archives=archives,
                           current=current, notifications=notifications, on_finished=on_finished,
                           producer_notified=producer_notified)


def _ready(h):
    return h.output.acknowledge_ready("performance_synthetic", h.output.output_id)


def _drained(h):
    return h.output.acknowledge_drained("performance_synthetic", h.output.output_id, "drained")


@pytest.mark.parametrize("publish_fails", [False, True])
def test_supply_and_transport_timing_stay_separate_including_failure(monkeypatch, publish_fails):
    async def scenario():
        clock, measurements = [100.0], {}
        monkeypatch.setattr(approved_speech_output, "monotonic", lambda: clock[0])
        monkeypatch.setattr(approved_speech_output, "observe_interview_agent_metric",
                            lambda name, value: measurements.setdefault(name, []).append(value))
        h = _harness()

        async def events():
            for number, chunk in enumerate(h.raw.chunks, start=2):
                clock[0] += 0.025
                yield h.raw.event(number, "audio.chunk", pcm_s16le=chunk)
            clock[0] += 0.010
            yield h.raw.event(4, "audio.final", total_audio_bytes=1440)

        async def publish(chunk):
            clock[0] += 0.100
            if publish_fails:
                raise RuntimeError("synthetic transport failure")

        h.stream = SimpleNamespace(events=events)
        h.output.stream = h.stream
        h.publisher.publish = publish
        if publish_fails:
            with pytest.raises(RuntimeError, match="synthetic transport"):
                await h.output._consume(bytearray())
        else:
            await h.output._consume(bytearray())
        assert measurements["tts_provider_read_wait_ms"] == pytest.approx([25 if publish_fails else 60])
        assert measurements["tts_transport_publish_ms"] == pytest.approx([100 if publish_fails else 200])
        assert measurements["tts_content_duration_ms"] == [0 if publish_fails else 30]

    asyncio.run(scenario())


def test_no_pcm_before_ready_and_only_exact_private_binding_is_exposed():
    async def scenario():
        h = _harness()
        binding = await h.output.open()
        assert binding.model_dump() == {
            "output_id": h.output.output_id, "publisher_identity": "expression:performance_synthetic",
            "track_sid": "TR_synthetic_exact", "track_name": "approved-expression:performance_synthetic",
            "sample_rate_hz": 24000, "channels": 1,
        }
        task = asyncio.create_task(h.output.run(h.on_finished))
        try:
            await asyncio.sleep(0.01)
            assert not h.raw.started.is_set() and h.publisher.published == [] and h.archives == []
            assert not h.output.acknowledge_ready("wrong_performance", h.output.output_id)
            assert not h.output.acknowledge_ready("performance_synthetic", "old_output")
            assert not _drained(h)
            assert _ready(h)
            await h.producer_notified.wait()
            assert not task.done() and h.publisher.close_calls == 0
            assert _drained(h)
            await task
            assert h.publisher.close_calls == 1 and h.publisher.abort_calls >= 1 and h.raw.aborted >= 1
            assert not _ready(h) and not _drained(h)
        finally:
            await h.output.abort()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_provider_eof_local_drain_and_remote_drain_are_three_separate_barriers():
    async def scenario():
        h = _harness()
        h.raw.eof_gate = asyncio.Event()
        h.publisher.finish_gate = asyncio.Event()
        await h.output.open()
        _ready(h)
        task = asyncio.create_task(h.output.run(h.on_finished))
        try:
            await h.raw.eof_reached.wait()
            assert h.publisher.published and h.archives == []
            assert not h.output.producer_finished and not _drained(h)
            h.raw.eof_gate.set()
            await h.publisher.finish_reached.wait()
            assert not h.output.producer_finished and h.notifications == [] and not _drained(h)
            h.publisher.finish_gate.set()
            await h.producer_notified.wait()
            assert h.output.producer_finished and not task.done()
            for reason in ["completed", "error", "interrupted", "", "ended"]:
                assert not h.output.acknowledge_drained("performance_synthetic", h.output.output_id, reason)
            assert not h.output.acknowledge_drained("old_performance", h.output.output_id, "drained")
            assert not h.output.acknowledge_drained("performance_synthetic", "old_output", "drained")
            assert not h.output.acknowledge_ready("performance_synthetic", h.output.output_id)
            assert _drained(h)
            await task
            assert h.publisher.close_calls == 1
        finally:
            h.raw.eof_gate.set()
            h.publisher.finish_gate.set()
            await h.output.abort()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_all_chunks_are_published_and_archived_once_without_urls_or_pcm_in_events():
    async def scenario():
        chunks = [b"\1\0" * 40000, b"\2\0" * 1501, b"\3\0" * 9]
        h = _harness(chunks)
        binding = await h.output.open()
        _ready(h)

        async def finish_and_ack(payload):
            await h.on_finished(payload)
            assert _drained(h)

        await h.output.run(finish_and_ack)
        pcm = b"".join(chunks)
        assert b"".join(h.publisher.published) == pcm
        assert all(0 < len(chunk) <= 65536 and len(chunk) % 2 == 0 for chunk in h.publisher.published)
        assert h.archives == [{"pcm_s16le": pcm, "sample_rate_hz": 24000, "channels": 1}]
        assert h.output.archive_reference == "private-file://synthetic-complete-expression"
        assert h.notifications == [{"performance_id": "performance_synthetic", "output_id": h.output.output_id,
                                   "total_samples": len(pcm) // 2, "sample_rate_hz": 24000}]
        assert "audio_uri" not in binding.model_dump() and "pcm_s16le" not in binding.model_dump()
        assert not any("url" in key or "uri" in key or "pcm" in key for key in h.notifications[0])

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["missing_final", "empty_final", "oversize", "provider_error", "publish_error", "cancel", "fence", "timeout"])
def test_incomplete_or_revoked_speech_is_never_archived_as_complete(failure):
    async def scenario():
        error = ProviderError("provider_transport_failed", "Synthetic provider error", retryable=False) if failure == "provider_error" else None
        h = _harness([] if failure == "empty_final" else None, final=failure != "missing_final", error=error,
                     **({"max_bytes": 4} if failure == "oversize" else {}),
                     **({"total_timeout": 0.025} if failure == "timeout" else {}))
        if failure in {"cancel", "fence", "timeout"}:
            h.raw.read_gate = asyncio.Event()
        if failure == "publish_error":
            h.publisher.publish_error = ApiError("SYNTHETIC_PUBLISH_FAILED", "Synthetic failure", status_code=503)
        await h.output.open()
        _ready(h)
        task = asyncio.create_task(h.output.run(h.on_finished))
        if failure in {"cancel", "fence", "timeout"}:
            await h.raw.chunk_reached.wait()
            if failure == "cancel":
                task.cancel()
            elif failure == "fence":
                h.current["valid"] = False
        with pytest.raises((ApiError, ProviderError, asyncio.CancelledError)):
            await asyncio.wait_for(task, timeout=1)
        assert h.archives == [] and h.output.archive_reference is None
        assert not h.output.producer_finished and h.notifications == []
        assert h.publisher.abort_calls >= 1 and h.raw.aborted >= 1 and h.publisher.close_calls == 0
        if failure in {"cancel", "fence", "timeout"}:
            assert h.raw.read_cancelled, "A stalled provider read must be cancelled by the output's fence/budget monitor"

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["ready", "remote_drain"])
def test_missing_client_ack_times_out_and_releases_output(phase):
    async def scenario():
        h = _harness()
        h.output._ready_timeout = h.output._drain_timeout = 0.025
        await h.output.open()
        if phase == "remote_drain":
            _ready(h)
        with pytest.raises(ApiError) as exc:
            await h.output.run(h.on_finished)
        assert exc.value.code == ("AGENT_AUDIO_SUBSCRIPTION_TIMEOUT" if phase == "ready" else "AGENT_AUDIO_PLAYBACK_TIMEOUT")
        assert h.publisher.abort_calls >= 1 and h.raw.aborted >= 1 and h.publisher.close_calls == 0
        if phase == "ready":
            assert h.archives == [] and h.publisher.published == []
        else:
            assert len(h.archives) == 1, "A full validated provider output is an archive, not proof of remote playback"

    asyncio.run(scenario())


def test_remote_ack_can_move_floor_without_invalidating_cleanup_of_its_exact_track():
    async def scenario():
        h = _harness()
        await h.output.open()
        _ready(h)

        async def finish_and_change_floor(payload):
            await h.on_finished(payload)
            assert _drained(h)
            h.current["valid"] = False

        await h.output.run(finish_and_change_floor)
        assert h.publisher.close_calls == 1

    asyncio.run(scenario())


def test_concurrent_consumer_failure_and_revocation_retrieve_the_consumer_error():
    async def scenario():
        import gc
        h = _harness()
        await h.output.open()
        _ready(h)
        loop = asyncio.get_running_loop()
        errors = []
        old_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))

        async def fail_and_revoke(_pcm):
            h.current["valid"] = False
            raise ApiError("SYNTHETIC_CONSUMER_FAILED", "Synthetic failure", status_code=503)

        h.output._consume = fail_and_revoke
        try:
            try:
                await h.output.run(h.on_finished)
            except ApiError as error:
                assert error.code == "EXPRESSION_OWNER_STALE"
            else:
                pytest.fail("Revoked output must fail closed")
            gc.collect()
            await asyncio.sleep(0)
            assert errors == []
            assert h.archives == [] and h.publisher.abort_calls >= 1
        finally:
            loop.set_exception_handler(old_handler)

    asyncio.run(scenario())


@pytest.mark.parametrize("provider_id,rate", [("mock", 24000), ("synthetic_tts", 16000)])
def test_open_rejects_development_audio_or_wrong_format_before_publishing(provider_id, rate):
    async def scenario():
        h = _harness(provider_id=provider_id, rate=rate)
        with pytest.raises(ApiError) as exc:
            await h.output.open()
        assert exc.value.code == "AGENT_STREAM_AUDIO_INVALID"
        assert h.publisher.opened == 0 and h.publisher.abort_calls >= 1 and h.raw.aborted >= 1

    asyncio.run(scenario())


def test_external_abort_interrupts_a_stalled_read_without_a_complete_archive():
    async def scenario():
        h = _harness()
        h.raw.read_gate = asyncio.Event()
        await h.output.open()
        _ready(h)
        task = asyncio.create_task(h.output.run(h.on_finished))
        await h.raw.chunk_reached.wait()
        await h.output.abort()
        with pytest.raises(ApiError) as exc:
            await asyncio.wait_for(task, timeout=1)
        assert exc.value.code == "AGENT_SPEECH_OUTPUT_STALE"
        assert h.archives == [] and h.publisher.abort_calls >= 1 and h.raw.read_cancelled
        assert not _ready(h) and not _drained(h)

    asyncio.run(scenario())


def test_consumer_finishing_after_total_deadline_must_not_create_a_complete_archive(monkeypatch):
    async def scenario():
        clock = {"now": 100.0}
        monkeypatch.setattr(approved_speech_output, "monotonic", lambda: clock["now"])
        h = _harness([b"\1\0" * 480], total_timeout=0.01)

        async def publish_crossing_deadline(chunk):
            h.publisher.published.append(chunk)
            clock["now"] += 0.025

        h.publisher.publish = publish_crossing_deadline
        await h.output.open()
        _ready(h)

        async def finish_and_ack(payload):
            await h.on_finished(payload)
            _drained(h)

        with pytest.raises(ApiError) as exc:
            await h.output.run(finish_and_ack)
        assert exc.value.code == "AGENT_AUDIO_OUTPUT_TIMEOUT"
        assert h.archives == [] and not h.output.producer_finished

    asyncio.run(scenario())


def test_first_audio_is_published_while_provider_is_still_generating_the_remainder():
    async def scenario():
        h = _harness([b"\1\0" * 480, b"\2\0" * 480])
        reached_second, finish_generation = asyncio.Event(), asyncio.Event()
        original = h.raw.events

        async def delayed_events():
            index = 0
            async for event in original():
                if event.type == "audio.chunk":
                    index += 1
                    if index == 2:
                        reached_second.set()
                        await finish_generation.wait()
                yield event

        h.raw.events = delayed_events
        await h.output.open()
        _ready(h)
        task = asyncio.create_task(h.output.run(h.on_finished))
        await reached_second.wait()
        assert h.publisher.published == [b"\1\0" * 480]
        assert not h.raw.eof_reached.is_set() and not h.output.producer_finished
        assert h.archives == [] and h.notifications == []
        finish_generation.set()
        await h.producer_notified.wait()
        assert _drained(h)
        await task
        assert b"".join(h.publisher.published) == b"\1\0" * 480 + b"\2\0" * 480

    asyncio.run(scenario())
