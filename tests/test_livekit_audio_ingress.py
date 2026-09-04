import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from livekit import rtc

from app.adapters.livekit_audio_ingress import (
    LiveKitAudioIngressBinding,
    LiveKitCandidateAudioIngress,
)
from app.adapters.livekit_media import LiveKitConfiguration, LiveKitMediaPlane


def _media_plane() -> LiveKitMediaPlane:
    return LiveKitMediaPlane(
        LiveKitConfiguration(
            url="wss://livekit.internal.example",
            api_key="test-key",
            api_secret="secret-at-least-sixteen-characters",
            egress_url="https://livekit.internal.example",
            storage_endpoint="",
            storage_bucket="",
            storage_region="auto",
            storage_access_key="",
            storage_secret="",
        )
    )


class _FakeRoom:
    def __init__(self) -> None:
        self.handlers = {}
        self.connect_args = None
        self.disconnected = False
        self.remote_participants = {}

    def on(self, name, callback=None):
        if callback is not None:
            self.handlers[name] = callback
            return callback

        def register(value):
            self.handlers[name] = value
            return value

        return register

    async def connect(self, url, token, options):
        self.connect_args = (url, token, options)

    async def disconnect(self):
        self.disconnected = True

    def emit(self, name, *args):
        self.handlers[name](*args)


@dataclass
class _FakeFrame:
    data: bytes
    sample_rate: int = 16_000
    num_channels: int = 1
    samples_per_channel: int = 320


class _FakeAudioStream:
    def __init__(self, frames) -> None:
        self.frames = list(frames)
        self.closed = False

    def __aiter__(self):
        self.iterator = iter(self.frames)
        return self

    async def __anext__(self):
        try:
            frame = next(self.iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc
        return SimpleNamespace(frame=frame)

    async def aclose(self):
        self.closed = True


def _publication(*, sid="mic_1", source=rtc.TrackSource.SOURCE_MICROPHONE, kind=rtc.TrackKind.KIND_AUDIO, track=None):
    publication = SimpleNamespace(
        sid=sid,
        source=source,
        kind=kind,
        track=track,
        subscription_requests=[],
    )
    publication.set_subscribed = publication.subscription_requests.append
    return publication


def _participant(identity="candidate:connection_1"):
    return SimpleNamespace(identity=identity)


def test_livekit_receive_only_token_cannot_publish_media_or_data() -> None:
    media = _media_plane()
    token = media.issue_audio_subscriber_token(
        room_name="interview-1",
        identity="evidence:interview-1",
    )
    import base64
    import json

    encoded = token.split(".")[1]
    encoded += "=" * (-len(encoded) % 4)
    claims = json.loads(base64.urlsafe_b64decode(encoded))
    assert claims["video"] == {
        "roomJoin": True,
        "room": "interview-1",
        "canPublish": False,
        "canSubscribe": True,
        "canPublishData": False,
        "canPublishSources": [],
    }


def test_authoritative_ingress_readiness_requires_database_fenced_mode() -> None:
    base = _media_plane().configuration
    disabled = LiveKitConfiguration(
        **{
            **base.__dict__,
            "authoritative_ingress_enabled": True,
            "authoritative_ingress_mode": "disabled",
        }
    )
    database_fenced = LiveKitConfiguration(
        **{
            **disabled.__dict__,
            "authoritative_ingress_mode": "database_fenced",
        }
    )
    assert disabled.authoritative_audio_ingress_ready() is False
    assert database_fenced.authoritative_audio_ingress_ready() is True


def test_livekit_ingress_accepts_only_bound_candidate_microphone_and_sequences_pcm() -> None:
    async def scenario() -> None:
        room = _FakeRoom()
        streams = []
        frames = []
        states = []

        async def collect_frame(frame):
            frames.append(frame)

        async def collect_state(state):
            states.append(state)

        def stream_factory(_track):
            stream = _FakeAudioStream(
                [_FakeFrame(b"\x01\x00" * 320), _FakeFrame(b"\x02\x00" * 320)]
            )
            streams.append(stream)
            return stream

        ingress = LiveKitCandidateAudioIngress(
            _media_plane(),
            LiveKitAudioIngressBinding(
                room_name="interview-1",
                candidate_identity="candidate:connection_1",
                subscriber_identity="evidence:interview-1",
            ),
            on_audio_frame=collect_frame,
            on_state=collect_state,
            room_factory=lambda: room,
            audio_stream_factory=stream_factory,
            audio_track_validator=lambda _track: True,
        )
        await ingress.connect()
        assert ingress.connected is True
        assert room.connect_args[0] == "wss://livekit.internal.example"
        assert room.connect_args[2].auto_subscribe is False

        room.emit(
            "track_subscribed",
            object(),
            _publication(sid="other_participant"),
            _participant("candidate:someone_else"),
        )
        room.emit(
            "track_subscribed",
            object(),
            _publication(sid="camera", source=rtc.TrackSource.SOURCE_CAMERA, kind=rtc.TrackKind.KIND_VIDEO),
            _participant(),
        )
        assert ingress.active_track_sid is None

        room.emit(
            "track_subscribed",
            object(),
            _publication(),
            _participant(),
        )
        task = ingress._track_task
        assert task is not None
        await task
        assert ingress.active_track_sid == "mic_1"
        assert [item.sequence for item in frames] == [1, 2]
        assert all(item.sample_rate_hz == 16_000 and item.channels == 1 for item in frames)
        assert frames[0].pcm_s16le == b"\x01\x00" * 320
        assert streams[0].closed is True
        assert states[:2] == ["connected", "microphone_subscribed"]

        await ingress.close()
        assert room.disconnected is True
        assert ingress.connected is False

    asyncio.run(scenario())


def test_livekit_ingress_yields_to_sink_when_audio_iterator_is_eager() -> None:
    async def scenario() -> None:
        room = _FakeRoom()
        received = []
        frame_count = 150

        async def collect_frame(frame):
            received.append(frame.sequence)

        ingress = LiveKitCandidateAudioIngress(
            _media_plane(),
            LiveKitAudioIngressBinding(
                room_name="interview-1",
                candidate_identity="candidate:connection_1",
                subscriber_identity="evidence:interview-1",
            ),
            on_audio_frame=collect_frame,
            room_factory=lambda: room,
            audio_stream_factory=lambda _track: _FakeAudioStream(
                [_FakeFrame(b"\x01\x00" * 320) for _ in range(frame_count)]
            ),
            audio_track_validator=lambda _track: True,
        )
        await ingress.connect()
        room.emit("track_subscribed", object(), _publication(), _participant())
        assert ingress._track_task is not None
        await ingress._track_task

        assert received == list(range(1, frame_count + 1))
        await ingress.close()

    asyncio.run(scenario())


def test_livekit_ingress_track_replacement_and_candidate_disconnect_are_bounded() -> None:
    async def scenario() -> None:
        room = _FakeRoom()
        release = asyncio.Event()

        class BlockingStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                await release.wait()
                raise StopAsyncIteration

            async def aclose(self):
                return None

        ingress = LiveKitCandidateAudioIngress(
            _media_plane(),
            LiveKitAudioIngressBinding(
                room_name="interview-1",
                candidate_identity="candidate:connection_1",
                subscriber_identity="evidence:interview-1",
            ),
            on_audio_frame=lambda _frame: asyncio.sleep(0),
            room_factory=lambda: room,
            audio_stream_factory=lambda _track: BlockingStream(),
            audio_track_validator=lambda _track: True,
        )
        await ingress.connect()
        room.emit("track_subscribed", object(), _publication(sid="mic_1"), _participant())
        first = ingress._track_task
        await asyncio.sleep(0)
        room.emit("track_subscribed", object(), _publication(sid="mic_2"), _participant())
        second = ingress._track_task
        await asyncio.gather(first, return_exceptions=True)
        assert first is not second
        assert first.cancelled() or first.done()
        assert ingress.active_track_sid == "mic_2"

        room.emit("participant_disconnected", _participant())
        await asyncio.gather(second, return_exceptions=True)
        assert ingress.active_track_sid is None
        assert second.cancelled() or second.done()
        release.set()
        await ingress.close()

    asyncio.run(scenario())


def test_candidate_microphone_mute_reports_gap_without_tearing_down_track() -> None:
    async def scenario() -> None:
        room = _FakeRoom()
        states = []
        release = asyncio.Event()

        class BlockingStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                await release.wait()
                raise StopAsyncIteration

            async def aclose(self):
                return None

        ingress = LiveKitCandidateAudioIngress(
            _media_plane(),
            LiveKitAudioIngressBinding(
                room_name="interview-1",
                candidate_identity="candidate:connection_1",
                subscriber_identity="evidence:interview-1",
            ),
            on_audio_frame=lambda _frame: asyncio.sleep(0),
            on_state=lambda state: _append_async(states, state),
            room_factory=lambda: room,
            audio_stream_factory=lambda _track: BlockingStream(),
            audio_track_validator=lambda _track: True,
        )
        await ingress.connect()
        publication = _publication(sid="mic_1")
        room.emit("track_subscribed", object(), publication, _participant())
        task = ingress._track_task
        await asyncio.sleep(0)
        room.emit("track_muted", publication, _participant())
        await asyncio.sleep(0)
        assert "track_muted" in states
        assert ingress.active_track_sid == "mic_1"
        assert ingress._track_task is task
        assert task is not None and not task.done()
        release.set()
        await asyncio.gather(task)
        await ingress.close()

    asyncio.run(scenario())


async def _append_async(values, value) -> None:
    values.append(value)


def test_livekit_ingress_scans_existing_tracks_and_requests_only_candidate_mic() -> None:
    async def scenario() -> None:
        room = _FakeRoom()
        candidate_mic = _publication(sid="existing_mic")
        candidate_camera = _publication(
            sid="existing_camera",
            source=rtc.TrackSource.SOURCE_CAMERA,
            kind=rtc.TrackKind.KIND_VIDEO,
        )
        other_mic = _publication(sid="other_mic")
        room.remote_participants = {
            "candidate:connection_1": SimpleNamespace(
                identity="candidate:connection_1",
                track_publications={
                    candidate_mic.sid: candidate_mic,
                    candidate_camera.sid: candidate_camera,
                },
            ),
            "candidate:other": SimpleNamespace(
                identity="candidate:other",
                track_publications={other_mic.sid: other_mic},
            ),
        }
        ingress = LiveKitCandidateAudioIngress(
            _media_plane(),
            LiveKitAudioIngressBinding(
                room_name="interview-1",
                candidate_identity="candidate:connection_1",
                subscriber_identity="evidence:interview-1",
            ),
            on_audio_frame=lambda _frame: asyncio.sleep(0),
            room_factory=lambda: room,
        )
        await ingress.connect()
        assert candidate_mic.subscription_requests == [True]
        assert candidate_camera.subscription_requests == []
        assert other_mic.subscription_requests == []
        await ingress.close()

    asyncio.run(scenario())


def test_livekit_ingress_rejects_invalid_bindings_and_oversized_frames() -> None:
    with pytest.raises(ValueError):
        LiveKitAudioIngressBinding(
            room_name="room",
            candidate_identity="same",
            subscriber_identity="same",
        )

    async def scenario() -> None:
        room = _FakeRoom()
        ingress = LiveKitCandidateAudioIngress(
            _media_plane(),
            LiveKitAudioIngressBinding(
                room_name="interview-1",
                candidate_identity="candidate:connection_1",
                subscriber_identity="evidence:interview-1",
            ),
            on_audio_frame=lambda _frame: asyncio.sleep(0),
            room_factory=lambda: room,
            audio_stream_factory=lambda _track: _FakeAudioStream(
                [_FakeFrame(b"\x00\x00" * 40_000, samples_per_channel=40_000)]
            ),
            audio_track_validator=lambda _track: True,
        )
        await ingress.connect()
        room.emit("track_subscribed", object(), _publication(), _participant())
        task = ingress._track_task
        assert task is not None
        with pytest.raises(ValueError, match="64 KiB"):
            await task
        await ingress.close()

    asyncio.run(scenario())


def test_livekit_ingress_fails_closed_instead_of_silently_dropping_on_backpressure() -> None:
    async def scenario() -> None:
        room = _FakeRoom()
        never_release = asyncio.Event()
        states = []

        async def blocked_sink(_frame):
            await never_release.wait()

        async def collect_state(state):
            states.append(state)

        ingress = LiveKitCandidateAudioIngress(
            _media_plane(),
            LiveKitAudioIngressBinding(
                room_name="interview-1",
                candidate_identity="candidate:connection_1",
                subscriber_identity="evidence:interview-1",
            ),
            on_audio_frame=blocked_sink,
            on_state=collect_state,
            room_factory=lambda: room,
            audio_stream_factory=lambda _track: _FakeAudioStream(
                [_FakeFrame(b"\x00\x00" * 320) for _ in range(102)]
            ),
            audio_track_validator=lambda _track: True,
        )
        await ingress.connect()
        room.emit("track_subscribed", object(), _publication(), _participant())
        task = ingress._track_task
        assert task is not None
        with pytest.raises(RuntimeError, match="backpressure budget"):
            await task
        await asyncio.sleep(0)
        assert "audio_stream_failed" in states
        never_release.set()
        await ingress.close()

    asyncio.run(scenario())
