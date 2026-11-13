"""Abandoned native joins finish safely without reviving media authority."""
import asyncio
import gc

import pytest

from app.adapters.livekit_audio_ingress import LiveKitAudioIngressBinding, LiveKitCandidateAudioIngress
from app.adapters.livekit_connection import _ABANDONED_JOINS, connect_room
from app.core.errors import ApiError
from test_livekit_audio_ingress import _FakeRoom, _media_plane, _participant, _publication
from test_livekit_audio_output import _harness


async def _until(predicate):
    deadline = asyncio.get_running_loop().time() + .75
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "Synthetic join did not settle"
        await asyncio.sleep(0)


class _JoiningRoom(_FakeRoom):
    def __init__(self, *, fail=False, disconnect_fail=False):
        super().__init__()
        self.entered, self.release, self.finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.connected = False
        self.cancelled = False
        self.fail = fail
        self.disconnect_fail = disconnect_fail
        self.disconnects = 0
        self.late_callback = None

    async def connect(self, url, token, options):
        self.connect_args = url, token, options
        self.entered.set()
        try:
            await self.release.wait()
            if self.fail:
                raise RuntimeError("PRIVATE_JOIN_TOKEN_AND_URL")
            self.connected = True
            if self.late_callback:
                self.late_callback()
            return "joined"
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.finished.set()

    async def disconnect(self):
        self.disconnects += 1
        self.connected = False
        self.disconnected = True
        if self.disconnect_fail:
            raise RuntimeError("PRIVATE_DISCONNECT_TOKEN_AND_URL")


def test_normal_join_keeps_connection_owned_by_its_caller():
    async def scenario():
        room = _JoiningRoom()
        task = asyncio.create_task(connect_room(room, "wss://synthetic", "opaque-token", object()))
        await room.entered.wait()
        room.release.set()
        assert await task == "joined"
        assert room.connected and not room.cancelled and room.disconnects == 0
        assert not _ABANDONED_JOINS
        await room.disconnect()
    asyncio.run(scenario())


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
@pytest.mark.parametrize("fail", [False, True])
def test_cancellation_or_timeout_does_not_cancel_native_join_and_reaps_late_outcome(stop, fail, caplog):
    async def scenario():
        loop, errors = asyncio.get_running_loop(), []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, event: errors.append(event))
        room = _JoiningRoom(fail=fail)
        operation = connect_room(room, "wss://synthetic", "opaque-token", object())
        task = asyncio.create_task(asyncio.wait_for(operation, .02) if stop == "timeout" else operation)
        try:
            await room.entered.wait()
            if stop == "cancel":
                task.cancel()
            with pytest.raises(asyncio.CancelledError if stop == "cancel" else asyncio.TimeoutError):
                await task
            assert not room.cancelled and not room.finished.is_set()
            assert room.disconnects == 0 and _ABANDONED_JOINS
            room.release.set()
            await _until(lambda: room.finished.is_set() and not _ABANDONED_JOINS)
            assert not room.connected and not room.cancelled and room.disconnects == 1
            del task
            gc.collect()
            await asyncio.sleep(0)
            assert errors == []
            assert "PRIVATE_JOIN" not in caplog.text
        finally:
            room.release.set()
            await _until(lambda: not _ABANDONED_JOINS)
            loop.set_exception_handler(previous)
    asyncio.run(scenario())


def test_failed_late_disconnect_is_consumed_and_only_reports_fixed_error_type(caplog):
    async def scenario():
        room = _JoiningRoom(disconnect_fail=True)
        task = asyncio.create_task(connect_room(room, "wss://synthetic", "opaque-token", object()))
        await room.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        room.release.set()
        await _until(lambda: not _ABANDONED_JOINS)
        assert room.disconnects == 1 and not room.cancelled
        assert "error_type=RuntimeError" in caplog.text
        assert "PRIVATE_DISCONNECT" not in caplog.text
    asyncio.run(scenario())


def test_cancellation_at_the_native_ready_boundary_still_reclaims_the_room():
    async def scenario():
        room = _JoiningRoom()
        task = asyncio.create_task(connect_room(room, "wss://synthetic", "opaque-token", object()))
        await room.entered.wait()
        # The SDK has joined but the shield waiter has not resumed. Withdrawal
        # at this boundary must not orphan the newly connected native room.
        room.late_callback = task.cancel
        room.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _until(lambda: not _ABANDONED_JOINS)
        assert room.finished.is_set() and not room.cancelled
        assert not room.connected and room.disconnects == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("stop", ["cancel", "timeout", "fence"])
@pytest.mark.parametrize("fail", [False, True])
def test_abandoned_publisher_join_never_creates_a_source_or_publishes_a_track(stop, fail, caplog):
    async def scenario():
        publisher, room, rtc, _, current = _harness(timeout=.03)
        room.connect_gate = asyncio.Event()
        if fail:
            room.connect_error = RuntimeError("PRIVATE_OUTPUT_JOIN")
        task = asyncio.create_task(publisher.open())
        try:
            await room.connect_entered.wait()
            if stop == "cancel":
                task.cancel()
            elif stop == "fence":
                current["valid"] = False
            with pytest.raises(asyncio.CancelledError if stop == "cancel" else ApiError) as caught:
                await task
            if stop != "cancel":
                assert caught.value.code == ("EXPRESSION_OWNER_STALE" if stop == "fence" else "LIVEKIT_AUDIO_OUTPUT_TIMEOUT")
            assert publisher.closed and not room.connect_cancelled
            assert rtc.sources == [] and room.local_participant.published == []
            room.connect_gate.set()
            await _until(lambda: not _ABANDONED_JOINS)
            assert not room.connected and not room.connect_cancelled
            assert rtc.sources == [] and room.local_participant.published == []
            assert not publisher._pending_operations
            assert "PRIVATE_OUTPUT_JOIN" not in caplog.text
        finally:
            room.connect_gate.set()
            await _until(lambda: not _ABANDONED_JOINS)
            await publisher.abort()
    asyncio.run(scenario())


def _ingress(room):
    states, frames, streams = [], [], []
    async def state(value):
        states.append(value)
    async def audio(value):
        frames.append(value)
    def stream(track):
        streams.append(track)
        raise AssertionError("An abandoned join must not start a microphone stream")
    ingress = LiveKitCandidateAudioIngress(_media_plane(),
        LiveKitAudioIngressBinding(room_name="synthetic_room", candidate_identity="candidate:connection_1",
                                  subscriber_identity="evidence:synthetic"),
        on_audio_frame=audio, on_state=state, room_factory=lambda: room,
        audio_stream_factory=stream, audio_track_validator=lambda _track: True)
    publication, participant = _publication(), _participant()
    participant.track_publications = {publication.sid: publication}
    room.remote_participants = {participant.identity: participant}
    def late_callback():
        room.emit("track_published", publication, participant)
        room.emit("track_subscribed", object(), publication, participant)
    room.late_callback = late_callback
    return ingress, publication, states, frames, streams


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
@pytest.mark.parametrize("fail", [False, True])
def test_abandoned_ingress_join_ignores_late_publications_and_subscription_callbacks(stop, fail, caplog):
    async def scenario():
        room = _JoiningRoom(fail=fail)
        ingress, publication, states, frames, streams = _ingress(room)
        operation = ingress.connect()
        task = asyncio.create_task(asyncio.wait_for(operation, .02) if stop == "timeout" else operation)
        try:
            await room.entered.wait()
            if stop == "cancel":
                task.cancel()
            with pytest.raises(asyncio.CancelledError if stop == "cancel" else asyncio.TimeoutError):
                await task
            assert not room.cancelled and not ingress.connected
            assert ingress._closing and ingress._closed_event.is_set()
            assert room.connect_args[2].auto_subscribe is False
            room.release.set()
            await _until(lambda: not _ABANDONED_JOINS)
            assert not room.connected and not room.cancelled
            assert publication.subscription_requests == [] and states == []
            assert frames == [] and streams == [] and ingress.active_track_sid is None
            assert not ingress._track_tasks and not ingress._state_tasks
            assert "PRIVATE_JOIN" not in caplog.text
        finally:
            room.release.set()
            await _until(lambda: not _ABANDONED_JOINS)
            await ingress.close()
    asyncio.run(scenario())


def test_closing_ingress_while_joining_reclaims_late_connection_without_connected_state():
    async def scenario():
        room = _JoiningRoom()
        ingress, publication, states, frames, streams = _ingress(room)
        task = asyncio.create_task(ingress.connect())
        try:
            await room.entered.wait()
            await ingress.close()
            room.release.set()
            try:
                await task
            except (ApiError, asyncio.CancelledError):
                pass
            await _until(lambda: not _ABANDONED_JOINS)
            assert not room.connected, "close must reclaim a native join that completes after its first disconnect"
            assert publication.subscription_requests == []
            assert states == [] and frames == [] and streams == []
        finally:
            room.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await room.disconnect()
    asyncio.run(scenario())
