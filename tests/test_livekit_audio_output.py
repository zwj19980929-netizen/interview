import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from app.adapters.livekit_audio_output import LiveKitApprovedAudioPublisher
from app.adapters.livekit_media import LiveKitMediaPlane
from app.core.errors import ApiError
from test_livekit_evidence_supervisor import _configuration


def _claims(token):
    data = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))


def test_expression_grant_is_exact_audio_only_and_does_not_widen_evidence_subscriber(monkeypatch):
    media = LiveKitMediaPlane(_configuration())
    monkeypatch.setattr("app.adapters.livekit_media.time.time", lambda: 1000)
    publisher = _claims(media.issue_audio_publisher_token(room_name="room_1", performance_id="performance_1", ttl_seconds=9999))
    assert publisher["sub"] == publisher["name"] == "expression:performance_1"
    assert publisher["nbf"] == 995 and publisher["exp"] == 1300
    assert json.loads(publisher["metadata"]) == {"role": "approved_expression"}
    assert publisher["video"] == {
        "roomJoin": True, "room": "room_1", "canPublish": True,
        "canSubscribe": False, "canPublishData": False, "canPublishSources": ["microphone"],
    }
    assert _claims(media.issue_audio_publisher_token(room_name="room_1", performance_id="performance_1", ttl_seconds=1))["exp"] == 1030
    subscriber = _claims(media.issue_audio_subscriber_token(room_name="room_1", identity="evidence:owner_1"))
    assert subscriber["video"] == {
        "roomJoin": True, "room": "room_1", "canPublish": False,
        "canSubscribe": True, "canPublishData": False, "canPublishSources": [],
    }


@pytest.mark.parametrize("room,performance", [("", "performance_1"), ("*", "performance_1"), ("room_1", ""),
                                                 ("room_1", "x" * 129), ("room_1\nother", "performance_1"), ("room_1", "candidate identity")])
def test_publisher_grant_rejects_non_server_scoped_identifiers(room, performance):
    with pytest.raises(ValueError):
        LiveKitMediaPlane(_configuration()).issue_audio_publisher_token(room_name=room, performance_id=performance)


class _Source:
    def __init__(self, sample_rate, channels, queue_size_ms):
        self.sample_rate, self.channels, self.queue_size_ms = sample_rate, channels, queue_size_ms
        self.frames = []
        self.capture_entered, self.playout_entered = asyncio.Event(), asyncio.Event()
        self.capture_gate, self.playout_gate = None, None
        self.capture_error = None
        self.cleared, self.closed = 0, False

    async def capture_frame(self, frame):
        self.frames.append(frame)
        self.capture_entered.set()
        if self.capture_gate is not None:
            await self.capture_gate.wait()
        if self.capture_error is not None:
            raise self.capture_error

    async def wait_for_playout(self):
        self.playout_entered.set()
        if self.playout_gate is not None:
            await self.playout_gate.wait()

    def clear_queue(self):
        self.cleared += 1

    async def aclose(self):
        self.closed = True


class _Participant:
    identity = "expression:performance_1"

    def __init__(self):
        self.published, self.unpublished = [], []
        self.publish_entered = asyncio.Event()
        self.publish_gate = None
        self.publish_error = None

    async def publish_track(self, track, options):
        self.publish_entered.set()
        if self.publish_gate is not None:
            await self.publish_gate.wait()
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append((track, options))
        return SimpleNamespace(sid="TR_exact_output", name=track.name)

    async def unpublish_track(self, sid):
        self.unpublished.append(sid)


class _Room:
    def __init__(self):
        self.local_participant = _Participant()
        self.connected = False
        self.disconnected = 0
        self.connect_entered = asyncio.Event()
        self.connect_gate = None
        self.connect_error = None
        self.connect_cancelled = False
        self.connect_args = None

    async def connect(self, url, token, options):
        self.connect_args = (url, token, options)
        self.connect_entered.set()
        try:
            if self.connect_gate is not None:
                await self.connect_gate.wait()
            if self.connect_error is not None:
                raise self.connect_error
            self.connected = True
        except asyncio.CancelledError:
            self.connect_cancelled = True
            raise

    async def disconnect(self):
        self.connected = False
        self.disconnected += 1


class _RTC:
    def __init__(self, room):
        self.Room = lambda: room
        self.sources = []
        self.RoomOptions = lambda **kwargs: SimpleNamespace(**kwargs)
        self.TrackPublishOptions = lambda **kwargs: SimpleNamespace(**kwargs)
        self.TrackSource = SimpleNamespace(SOURCE_MICROPHONE=2)
        self.LocalAudioTrack = SimpleNamespace(create_audio_track=lambda name, source: SimpleNamespace(name=name, source=source))

    def AudioSource(self, rate, channels, queue_size_ms):
        source = _Source(rate, channels, queue_size_ms)
        self.sources.append(source)
        return source

    def AudioFrame(self, data, rate, channels, samples):
        return SimpleNamespace(data=data, sample_rate=rate, channels=channels, samples_per_channel=samples)


def _harness(*, timeout=0.2, rate=24_000):
    room = _Room()
    rtc = _RTC(room)
    calls = []
    current = {"valid": True, "checks": 0}

    def guard():
        current["checks"] += 1
        if not current["valid"]:
            raise ApiError("EXPRESSION_OWNER_STALE", "Synthetic expression authority was withdrawn.", status_code=409)

    def token(**kwargs):
        calls.append(kwargs)
        return "opaque-publisher-token"

    publisher = LiveKitApprovedAudioPublisher(
        SimpleNamespace(url="wss://local.test", issue_audio_publisher_token=token),
        room_name="room_1", performance_id="performance_1", assert_current=guard,
        rtc_module=rtc, sample_rate_hz=rate, operation_timeout_seconds=timeout,
    )
    return publisher, room, rtc, calls, current


def test_publisher_frames_exact_pcm_and_finish_preserves_remote_track_until_explicit_close():
    async def scenario():
        publisher, room, rtc, token_calls, current = _harness()
        publication = await publisher.open()
        assert publication.participant_identity == "expression:performance_1"
        assert publication.track_sid == "TR_exact_output"
        assert publication.track_name == "approved-expression:performance_1"
        assert publication.room_name == "room_1" and publication.channels == 1
        assert token_calls == [{"room_name": "room_1", "performance_id": "performance_1"}]
        assert room.connect_args[2].auto_subscribe is False
        source = rtc.sources[0]
        assert (source.sample_rate, source.channels, source.queue_size_ms) == (24000, 1, 200)
        assert room.local_participant.published[0][1].source == 2
        pcm = b"\x01\x02" * 1099
        await publisher.publish(pcm)
        assert b"".join(frame.data for frame in source.frames) == pcm
        assert [frame.samples_per_channel for frame in source.frames] == [480, 480, 139]
        assert all(frame.sample_rate == 24000 and frame.channels == 1 for frame in source.frames)
        assert publisher.total_samples == 1099 and current["checks"] >= 10
        source.playout_gate = asyncio.Event()
        draining = asyncio.create_task(publisher.finish())
        await source.playout_entered.wait()
        assert not draining.done() and room.disconnected == 0
        source.playout_gate.set()
        assert await draining == publication
        assert await publisher.finish() == publication
        assert room.connected and not source.closed and room.local_participant.unpublished == []
        with pytest.raises(ApiError):
            await publisher.publish(b"\0\0")
        await publisher.aclose()
        assert source.closed and source.cleared == 0
        assert room.disconnected == 1 and room.local_participant.unpublished == [publication.track_sid]
        await publisher.aclose()
        await publisher.abort()
        assert room.disconnected == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["connect", "publish", "capture", "drain"])
@pytest.mark.parametrize("failure", ["cancel", "timeout", "fence"])
def test_pending_sdk_operation_is_bounded_and_releases_resources(stage, failure):
    async def scenario():
        publisher, room, rtc, _calls, current = _harness(timeout=0.04)
        gate = asyncio.Event()
        if stage == "connect":
            room.connect_gate = gate
            task = asyncio.create_task(publisher.open())
            await room.connect_entered.wait()
        elif stage == "publish":
            room.local_participant.publish_gate = gate
            task = asyncio.create_task(publisher.open())
            await room.local_participant.publish_entered.wait()
        else:
            await publisher.open()
            source = rtc.sources[0]
            if stage == "capture":
                source.capture_gate = gate
                task = asyncio.create_task(publisher.publish(b"\x01\x00" * 960))
                await source.capture_entered.wait()
            else:
                source.playout_gate = gate
                task = asyncio.create_task(publisher.finish())
                await source.playout_entered.wait()
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            if failure == "fence":
                current["valid"] = False
            with pytest.raises(ApiError) as exc:
                await asyncio.wait_for(task, timeout=1)
            assert exc.value.code == ("EXPRESSION_OWNER_STALE" if failure == "fence" else "LIVEKIT_AUDIO_OUTPUT_TIMEOUT")
        assert publisher.closed and room.disconnected == 1
        assert all(source.closed and source.cleared >= 1 for source in rtc.sources)
        assert not publisher._pending_operations
        if stage == "capture":
            assert len(rtc.sources[0].frames) == 1, "No later frame can be published after invalidation"
        if stage == "connect":
            assert room.connect_cancelled

    asyncio.run(scenario())


def test_abort_clears_queued_audio_before_waiting_for_pending_capture_and_forbids_reuse():
    async def scenario():
        publisher, room, rtc, _calls, _current = _harness()
        await publisher.open()
        source = rtc.sources[0]
        source.capture_gate = asyncio.Event()
        task = asyncio.create_task(publisher.publish(b"\1\0" * 960))
        await source.capture_entered.wait()
        await publisher.abort()
        assert source.cleared >= 1 and source.closed and room.disconnected == 1
        with pytest.raises((ApiError, asyncio.CancelledError)):
            await task
        with pytest.raises(ApiError):
            await publisher.open()
        with pytest.raises(ApiError):
            await publisher.publish(b"\0\0")

    asyncio.run(scenario())


def test_concurrent_producers_are_rejected_instead_of_building_an_unbounded_pcm_queue():
    async def scenario():
        publisher, _room, rtc, _calls, _current = _harness()
        await publisher.open()
        source = rtc.sources[0]
        source.capture_gate = asyncio.Event()
        first = asyncio.create_task(publisher.publish(b"\1\0" * 480))
        await source.capture_entered.wait()
        with pytest.raises(ApiError) as exc:
            await publisher.publish(b"\2\0" * 480)
        assert exc.value.code == "LIVEKIT_AUDIO_OUTPUT_BUSY"
        assert len(source.frames) == 1 and not publisher.closed
        source.capture_gate.set()
        await first
        await publisher.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("data", [b"", b"x", b"xx" * 32769, "not-pcm", bytearray(b"\0\0")])
def test_invalid_pcm_never_enters_native_queue(data):
    async def scenario():
        publisher, _room, rtc, _calls, _current = _harness()
        await publisher.open()
        with pytest.raises(ApiError) as exc:
            await publisher.publish(data)
        assert exc.value.code == "LIVEKIT_AUDIO_OUTPUT_PCM_INVALID"
        assert rtc.sources[0].frames == []
        await publisher.abort()

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["connect", "publish", "capture"])
def test_native_errors_are_safe_and_cleanup_does_not_expose_tokens(stage):
    async def scenario():
        publisher, room, rtc, _calls, _current = _harness()
        secret_error = RuntimeError("sensitive-native-token-not-for-logs")
        if stage == "connect":
            room.connect_error = secret_error
            operation = publisher.open()
        elif stage == "publish":
            room.local_participant.publish_error = secret_error
            operation = publisher.open()
        else:
            await publisher.open()
            rtc.sources[0].capture_error = secret_error
            operation = publisher.publish(b"\0\0")
        with pytest.raises(ApiError) as exc:
            await operation
        assert exc.value.code == "LIVEKIT_AUDIO_OUTPUT_FAILED"
        assert "sensitive-native-token" not in str(exc.value)
        assert exc.value.__suppress_context__ is True
        assert publisher.closed and room.disconnected == 1

    asyncio.run(scenario())


def test_stale_fence_before_open_cannot_mint_a_media_grant():
    async def scenario():
        publisher, room, rtc, calls, current = _harness()
        current["valid"] = False
        with pytest.raises(ApiError) as exc:
            await publisher.open()
        assert exc.value.code == "EXPRESSION_OWNER_STALE"
        assert calls == [] and rtc.sources == [] and room.connect_args is None

    asyncio.run(scenario())


@pytest.mark.parametrize("kwargs", [
    {"sample_rate_hz": 44100}, {"sample_rate_hz": True}, {"assert_current": None},
    {"operation_timeout_seconds": 0}, {"operation_timeout_seconds": float("nan")},
    {"room_name": "*"}, {"performance_id": "candidate forged value"},
])
def test_invalid_publisher_configuration_is_rejected_without_rtc(kwargs):
    options = {"room_name": "room_1", "performance_id": "performance_1", "assert_current": lambda: None}
    options.update(kwargs)
    with pytest.raises(ValueError):
        LiveKitApprovedAudioPublisher(SimpleNamespace(), **options)
