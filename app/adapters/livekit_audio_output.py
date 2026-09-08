"""Bounded PCM publication for one server-approved expression.

Only the domain caller can supply the current-expression fence and resolved
room/performance IDs. This adapter accepts no candidate authorization fields
and does not modify the independent receive-only Evidence participant.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from livekit import rtc

from app.adapters.livekit_media import LiveKitMediaPlane
from app.core.errors import ApiError


@dataclass(frozen=True)
class LiveKitApprovedAudioPublication:
    room_name: str
    performance_id: str
    participant_identity: str
    track_sid: str
    track_name: str
    sample_rate_hz: int
    channels: int = 1


class LiveKitApprovedAudioPublisher:
    """A single-use audio publisher with a 200 ms native playout queue.

    ``publish`` is sequential/backpressured (no additional Python audio queue)
    and divides bounded input chunks into at most 20 ms PCM frames. The
    synchronous ``assert_current`` callback must raise when the exact approved
    act, turn, or ownership fence is stale. It is checked before each frame
    and while SDK operations are pending, not merely when the room opens.

    ``finish`` drains the *local* source only and keeps the track published.
    The caller must await its remote playback acknowledgement before ``aclose``;
    interruption/owner loss uses ``abort`` to discard queued audio immediately.
    """

    def __init__(
        self,
        media_plane: LiveKitMediaPlane,
        *,
        room_name: str,
        performance_id: str,
        assert_current: Callable[[], None],
        sample_rate_hz: int = 24_000,
        rtc_module: Any = None,
        room_factory: Optional[Callable[[], Any]] = None,
        operation_timeout_seconds: float = 5.0,
    ) -> None:
        if not isinstance(room_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}", room_name):
            raise ValueError("An exact server-issued room identifier is required")
        if not isinstance(performance_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", performance_id):
            raise ValueError("A server-issued performance identifier is required")
        if not callable(assert_current) or inspect.iscoroutinefunction(assert_current):
            raise ValueError("A synchronous current-expression fence is required")
        if type(sample_rate_hz) is not int or sample_rate_hz not in {16_000, 24_000, 48_000}:
            raise ValueError("Approved PCM output supports 16, 24, or 48 kHz mono")
        if (type(operation_timeout_seconds) not in (int, float)
                or not math.isfinite(operation_timeout_seconds) or not 0 < operation_timeout_seconds <= 30):
            raise ValueError("Output operation timeout must be between 0 and 30 seconds")
        self.media_plane = media_plane
        self.room_name, self.performance_id = room_name, performance_id
        self.participant_identity = "expression:%s" % performance_id
        self.track_name = "approved-expression:%s" % performance_id
        self.sample_rate_hz = sample_rate_hz
        self.total_samples = 0
        self._fence = assert_current
        self._rtc = rtc if rtc_module is None else rtc_module
        self._room_factory = room_factory or self._rtc.Room
        self._timeout = float(operation_timeout_seconds)
        self._room: Any = None
        self._source: Any = None
        self._publication: Optional[LiveKitApprovedAudioPublication] = None
        self._state = "new"
        self._operation_lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()
        self._pending_operations: set[asyncio.Task] = set()

    @property
    def publication(self) -> Optional[LiveKitApprovedAudioPublication]:
        return self._publication

    @property
    def closed(self) -> bool:
        return self._state == "closed"

    def _assert_current(self) -> None:
        if self.closed:
            raise ApiError("LIVEKIT_AUDIO_OUTPUT_CLOSED", "Expression audio output is closed.", status_code=409)
        try:
            result = self._fence()
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise ValueError("Expression fence must be synchronous")
            if result is False:
                raise ValueError("Expression authority was withdrawn")
        except ApiError:
            raise
        except Exception:
            raise ApiError("LIVEKIT_AUDIO_OUTPUT_STALE", "Expression authority is no longer current.", status_code=409) from None

    async def _current_operation(self, awaitable: Any) -> Any:
        """Bound SDK waits and detect withdrawn authority during backpressure."""
        task = asyncio.ensure_future(awaitable)
        self._pending_operations.add(task)
        deadline = asyncio.get_running_loop().time() + self._timeout
        try:
            while True:
                self._assert_current()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise ApiError("LIVEKIT_AUDIO_OUTPUT_TIMEOUT", "Expression audio transport timed out.", status_code=503)
                done, _ = await asyncio.wait({task}, timeout=min(0.02, remaining))
                self._assert_current()
                if done:
                    return task.result()
        finally:
            self._pending_operations.discard(task)
            if not task.done():
                task.cancel()
                await self._reap(task)
            elif not task.cancelled():
                task.exception()  # A concurrent fence rejection must still consume a failed SDK task.

    async def _reap(self, task: asyncio.Task) -> None:
        done, _ = await asyncio.wait({task}, timeout=min(0.1, self._timeout))
        if done:
            if not task.cancelled():
                task.exception()
        else:
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())

    async def open(self) -> LiveKitApprovedAudioPublication:
        async with self._operation_lock:
            if self._state != "new":
                raise ApiError("LIVEKIT_AUDIO_OUTPUT_ALREADY_OPENED", "Expression audio output is single-use.", status_code=409)
            self._state = "opening"
            try:
                self._assert_current()
                token = self.media_plane.issue_audio_publisher_token(
                    room_name=self.room_name, performance_id=self.performance_id,
                )
                self._room = self._room_factory()
                await self._current_operation(self._room.connect(
                    self.media_plane.url, token, self._rtc.RoomOptions(auto_subscribe=False),
                ))
                if self._room.local_participant.identity != self.participant_identity:
                    raise ValueError("LiveKit publisher identity differs from its server-issued grant")
                self._source = self._rtc.AudioSource(self.sample_rate_hz, 1, queue_size_ms=200)
                track = self._rtc.LocalAudioTrack.create_audio_track(self.track_name, self._source)
                options = self._rtc.TrackPublishOptions(source=self._rtc.TrackSource.SOURCE_MICROPHONE)
                publication = await self._current_operation(self._room.local_participant.publish_track(track, options))
                sid = str(publication.sid or "")
                if not sid or len(sid) > 255 or publication.name != self.track_name:
                    raise ValueError("LiveKit did not return an exact audio track SID")
                self._publication = LiveKitApprovedAudioPublication(
                    self.room_name, self.performance_id, self.participant_identity,
                    sid, self.track_name, self.sample_rate_hz,
                )
                self._state = "open"
                return self._publication
            except BaseException as exc:
                await self.abort()
                self._raise_safe(exc)

    async def publish(self, pcm_s16le: bytes) -> None:
        if not isinstance(pcm_s16le, bytes) or not pcm_s16le or len(pcm_s16le) % 2 or len(pcm_s16le) > 65_536:
            raise ApiError("LIVEKIT_AUDIO_OUTPUT_PCM_INVALID", "Output chunks must contain 1 to 32768 complete PCM16 mono samples.", status_code=422)
        # Reject overlapping producers rather than accepting an unbounded
        # number of caller-owned chunks behind a lock. The domain has one
        # sequential decoder for this exact performance.
        if self._operation_lock.locked():
            raise ApiError("LIVEKIT_AUDIO_OUTPUT_BUSY", "Await the previous audio operation before publishing another chunk.", status_code=409)
        async with self._operation_lock:
            if self._state != "open":
                raise ApiError("LIVEKIT_AUDIO_OUTPUT_NOT_OPEN", "Expression audio output is not accepting PCM.", status_code=409)
            try:
                frame_bytes = self.sample_rate_hz // 50 * 2
                for offset in range(0, len(pcm_s16le), frame_bytes):
                    self._assert_current()
                    data = pcm_s16le[offset:offset + frame_bytes]
                    frame = self._rtc.AudioFrame(data, self.sample_rate_hz, 1, len(data) // 2)
                    await self._current_operation(self._source.capture_frame(frame))
                    self.total_samples += len(data) // 2
            except BaseException as exc:
                await self.abort()
                self._raise_safe(exc)

    async def finish(self) -> LiveKitApprovedAudioPublication:
        async with self._operation_lock:
            if self._state == "finished":
                self._assert_current()
                return self._publication
            if self._state != "open":
                raise ApiError("LIVEKIT_AUDIO_OUTPUT_NOT_OPEN", "Expression audio output is not open.", status_code=409)
            self._state = "finishing"
            try:
                await self._current_operation(self._source.wait_for_playout())
                self._state = "finished"
                return self._publication
            except BaseException as exc:
                await self.abort()
                self._raise_safe(exc)

    async def aclose(self) -> None:
        if self.closed:
            return
        if self._state == "open":
            await self.finish()
        await self._cleanup(clear_queue=False)

    async def abort(self) -> None:
        await self._cleanup(clear_queue=True)

    async def _cleanup(self, *, clear_queue: bool) -> None:
        # Fence future writes and empty local playout before any awaited room
        # cleanup. In-flight operations wake on cancellation or their next
        # 20 ms authority check and cannot republish after this cut.
        self._state = "closed"
        source = self._source
        if clear_queue and source is not None:
            try:
                source.clear_queue()
            except Exception:
                pass
        for task in tuple(self._pending_operations):
            if not task.done():
                task.cancel()
        async with self._cleanup_lock:
            source, self._source = self._source, None
            room, self._room = self._room, None
            if room is not None and self._publication is not None:
                await self._best_effort(room.local_participant.unpublish_track(self._publication.track_sid))
            if source is not None:
                await self._best_effort(source.aclose())
            if room is not None:
                await self._best_effort(room.disconnect())

    async def _best_effort(self, awaitable: Any) -> None:
        task = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait({task}, timeout=min(1.0, self._timeout))
            if done:
                if not task.cancelled():
                    task.exception()
            else:
                task.cancel()
                await self._reap(task)
        except asyncio.CancelledError:
            task.cancel()
            await self._reap(task)
            raise

    @staticmethod
    def _raise_safe(exc: BaseException) -> None:
        if isinstance(exc, (ApiError, asyncio.CancelledError)):
            raise exc
        raise ApiError("LIVEKIT_AUDIO_OUTPUT_FAILED", "Expression audio transport failed.", status_code=503) from None
