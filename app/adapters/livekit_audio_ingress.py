"""Receive-only LiveKit microphone ingress for authoritative interview audio.

The adapter contains only LiveKit protocol/runtime knowledge. It never opens
STT, advances a turn, or decides whether audio is evidence; callers provide a
single bounded frame sink owned by the InterviewAgent Evidence chain.
"""

from __future__ import annotations

import asyncio
import sys
from array import array
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from livekit import rtc

from app.adapters.livekit_media import LiveKitMediaPlane
from app.core.errors import ApiError


AudioFrameSink = Callable[["LiveKitIngressAudioFrame"], Awaitable[None]]
IngressStateSink = Callable[[str], Awaitable[None]]


_SAFE_TRACK_FAILURE_CODES = frozenset(
    {
        "LIVEKIT_INGRESS_AUDIO_STREAM_FAILED",
        "LIVEKIT_INGRESS_PCM_INVALID",
        "LIVEKIT_INGRESS_SINK_BACKPRESSURE",
        "PROVIDER_AUDIO_CHUNK_TOO_LARGE",
        "PROVIDER_AUDIO_STREAM_TOO_LARGE",
        "PROVIDER_AUTH_FAILED",
        "PROVIDER_BACKPRESSURE_EXCEEDED",
        "PROVIDER_BAD_REQUEST",
        "PROVIDER_CAPABILITY_MISSING",
        "PROVIDER_FINAL_TRANSCRIPT_MISSING",
        "PROVIDER_NETWORK_ERROR",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_SCHEMA_INVALID",
        "PROVIDER_SERVER_ERROR",
        "PROVIDER_STREAM_CLOSED",
        "PROVIDER_STREAM_FAILED",
        "PROVIDER_STREAM_INTERRUPTED",
        "PROVIDER_STREAM_OPEN_FAILED",
        "PROVIDER_TIMEOUT",
        "PROVIDER_TRANSPORT_UNAVAILABLE",
    }
)
_SAFE_TRACK_FAILURE_TYPES = frozenset(
    {
        "ConnectionError",
        "LiveKitAudioIngressBackpressureError",
        "OSError",
        "ProviderError",
        "RuntimeError",
        "TimeoutError",
        "ValueError",
    }
)


@dataclass(frozen=True)
class LiveKitAudioIngressFailure:
    """PII-safe failure metadata exposed to the supervising domain service."""

    code: str
    cause_type: str

    def __post_init__(self) -> None:
        code = str(self.code or "").strip().upper()
        object.__setattr__(
            self,
            "code",
            code
            if code in _SAFE_TRACK_FAILURE_CODES
            else "LIVEKIT_INGRESS_AUDIO_STREAM_FAILED",
        )
        cause_type = str(self.cause_type or "").strip()
        object.__setattr__(
            self,
            "cause_type",
            cause_type
            if cause_type in _SAFE_TRACK_FAILURE_TYPES
            else "Exception",
        )


class LiveKitAudioIngressBackpressureError(RuntimeError):
    """The lossless ingress queue could not keep up with its downstream sink."""

    code = "LIVEKIT_INGRESS_SINK_BACKPRESSURE"


@dataclass(frozen=True)
class LiveKitAudioIngressBinding:
    room_name: str
    candidate_identity: str
    subscriber_identity: str

    def __post_init__(self) -> None:
        for field_name in ("room_name", "candidate_identity", "subscriber_identity"):
            value = str(getattr(self, field_name) or "").strip()
            if not value or len(value) > 255:
                raise ValueError("LiveKit audio ingress binding fields must contain 1 to 255 characters")
            object.__setattr__(self, field_name, value)
        if self.candidate_identity == self.subscriber_identity:
            raise ValueError("Evidence subscriber identity must differ from candidate identity")


@dataclass(frozen=True)
class LiveKitIngressAudioFrame:
    sequence: int
    track_sid: str
    sample_rate_hz: int
    channels: int
    samples_per_channel: int
    captured_at: str
    pcm_s16le: bytes

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("LiveKit ingress frame sequence must be positive")
        if self.sample_rate_hz != 16_000 or self.channels != 1:
            raise ValueError("Authoritative ingress frames must be 16 kHz mono")
        if self.samples_per_channel < 1:
            raise ValueError("LiveKit ingress frame cannot be empty")
        if not self.pcm_s16le or len(self.pcm_s16le) % 2:
            raise ValueError("LiveKit ingress frame must contain PCM signed 16-bit samples")
        if len(self.pcm_s16le) > 64 * 1024:
            raise ValueError("One LiveKit ingress frame exceeds the 64 KiB safety bound")


@dataclass
class _SinkPumpState:
    queue: asyncio.Queue[Any]
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    accepted_sequence: int = 0
    delivered_sequence: int = 0
    failure: Optional[BaseException] = None
    task: Optional[asyncio.Task[None]] = None


class LiveKitCandidateAudioIngress:
    """One receive-only room connection bound to one exact candidate identity."""

    def __init__(
        self,
        media_plane: LiveKitMediaPlane,
        binding: LiveKitAudioIngressBinding,
        *,
        on_audio_frame: AudioFrameSink,
        on_state: Optional[IngressStateSink] = None,
        room_factory: Optional[Callable[[], Any]] = None,
        audio_stream_factory: Optional[Callable[[Any], Any]] = None,
        audio_track_validator: Optional[Callable[[Any], bool]] = None,
        sink_backpressure_seconds: Optional[float] = None,
    ) -> None:
        self.media_plane = media_plane
        self.binding = binding
        self.on_audio_frame = on_audio_frame
        self.on_state = on_state
        self._room_factory = room_factory or rtc.Room
        self._audio_stream_factory = audio_stream_factory or self._default_audio_stream
        self._audio_track_validator = audio_track_validator or (
            lambda value: isinstance(value, rtc.RemoteAudioTrack)
        )
        self._room: Optional[Any] = None
        self._track_task: Optional[asyncio.Task[None]] = None
        self._track_tasks: set[asyncio.Task[None]] = set()
        self._state_tasks: set[asyncio.Task[None]] = set()
        self._closed_event = asyncio.Event()
        self._closing = False
        self._connected = False
        self._sequence = 0
        self._track_sid: Optional[str] = None
        self._active_track: Optional[Any] = None
        self._active_publication: Optional[Any] = None
        self._requested_sids: set[str] = set()
        self._last_track_failure: Optional[LiveKitAudioIngressFailure] = None
        self._sink_state: Optional[_SinkPumpState] = None
        configured_backpressure_seconds = (
            sink_backpressure_seconds
            if sink_backpressure_seconds is not None
            else getattr(
                media_plane.configuration,
                "authoritative_ingress_backpressure_seconds",
                5,
            )
        )
        try:
            parsed_backpressure_seconds = float(configured_backpressure_seconds)
        except (TypeError, ValueError):
            parsed_backpressure_seconds = 5.0
        self._sink_backpressure_seconds = max(
            0.25, min(30.0, parsed_backpressure_seconds)
        )
        # The receive-only LiveKit stream is normalized to 20 ms frames.
        self._sink_queue_capacity_frames = max(
            13, int(round(self._sink_backpressure_seconds * 50))
        )

    @property
    def connected(self) -> bool:
        return self._connected and not self._closing

    @property
    def active_track_sid(self) -> Optional[str]:
        return self._track_sid

    @property
    def last_track_failure(self) -> Optional[LiveKitAudioIngressFailure]:
        return self._last_track_failure

    async def drain(self, *, timeout_seconds: Optional[float] = None) -> int:
        """Wait until every frame accepted before this call crossed the sink.

        The watermark is deliberately captured at call time: a formal endpoint
        may finalize after this prefix has reached recording/STT while later
        room ambience observes the now-closed Evidence gate.  No accepted frame
        can be overtaken by ``evidence.seal``.
        """

        state = self._sink_state
        if state is None:
            return self._sequence
        target = state.accepted_sequence
        if target <= state.delivered_sequence:
            return target
        timeout = (
            max(5.0, self._sink_backpressure_seconds * 2.0)
            if timeout_seconds is None
            else max(0.05, float(timeout_seconds))
        )

        async def wait_for_watermark() -> None:
            async with state.condition:
                while state.delivered_sequence < target:
                    if state.failure is not None:
                        raise state.failure
                    await state.condition.wait()

        try:
            await asyncio.wait_for(wait_for_watermark(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            failure = LiveKitAudioIngressBackpressureError(
                "LiveKit evidence sink did not drain before the seal deadline"
            )
            state.failure = failure
            self._last_track_failure = _classify_track_failure(failure)
            task = self._track_task
            if task is not None and not task.done():
                task.cancel()
            async with state.condition:
                state.condition.notify_all()
            raise failure from exc
        return target

    async def connect(self) -> None:
        if self._room is not None:
            raise ApiError(
                "LIVEKIT_INGRESS_ALREADY_CONNECTED",
                "LiveKit evidence ingress is already connected.",
                status_code=409,
            )
        self.media_plane.require_ready(recording=False)
        room = self._room_factory()
        self._room = room
        room.on("track_published", self._on_track_published)
        room.on("track_subscribed", self._on_track_subscribed)
        room.on("track_unsubscribed", self._on_track_unsubscribed)
        room.on("track_unpublished", self._on_track_unpublished)
        room.on("track_muted", self._on_track_muted)
        room.on("participant_disconnected", self._on_participant_disconnected)
        room.on("disconnected", self._on_room_disconnected)
        token = self.media_plane.issue_audio_subscriber_token(
            room_name=self.binding.room_name,
            identity=self.binding.subscriber_identity,
            ttl_seconds=3600,
        )
        try:
            await room.connect(
                self.media_plane.url,
                token,
                rtc.RoomOptions(auto_subscribe=False),
            )
            # Publications that existed before this receive-only participant
            # joined are not guaranteed to emit track_published again.
            for participant in getattr(room, "remote_participants", {}).values():
                for publication in participant.track_publications.values():
                    self._request_subscription(publication, participant)
        except BaseException:
            await self.close()
            raise
        self._connected = True
        await self._report_state("connected")

    async def wait_closed(self) -> None:
        await self._closed_event.wait()

    async def close(self) -> None:
        if self._closing:
            await self._closed_event.wait()
            return
        self._closing = True
        self._connected = False
        track_task = self._track_task
        self._track_task = None
        if track_task is not None:
            track_task.cancel()
        track_tasks = list(self._track_tasks)
        for task in track_tasks:
            task.cancel()
        if track_tasks:
            await asyncio.gather(*track_tasks, return_exceptions=True)
        room = self._room
        self._room = None
        if room is not None:
            try:
                await room.disconnect()
            except Exception:
                pass
        state_tasks = list(self._state_tasks)
        if state_tasks:
            await asyncio.gather(*state_tasks, return_exceptions=True)
        self._track_sid = None
        self._active_track = None
        self._active_publication = None
        self._sink_state = None
        self._requested_sids.clear()
        self._closed_event.set()

    def _eligible(self, publication: Any, participant: Any) -> bool:
        return bool(
            str(getattr(participant, "identity", ""))
            == self.binding.candidate_identity
            and getattr(publication, "kind", None) == rtc.TrackKind.KIND_AUDIO
            and getattr(publication, "source", None)
            == rtc.TrackSource.SOURCE_MICROPHONE
        )

    def _on_track_published(self, publication: Any, participant: Any) -> None:
        try:
            self._request_subscription(publication, participant)
        except Exception:
            self._schedule_state("microphone_subscription_failed")

    def _request_subscription(self, publication: Any, participant: Any) -> None:
        if self._closing or not self._eligible(publication, participant):
            return
        track_sid = str(getattr(publication, "sid", "") or "")
        if not track_sid or track_sid in self._requested_sids:
            return
        self._requested_sids.add(track_sid)
        track = getattr(publication, "track", None)
        if track is not None:
            if not self._audio_track_validator(track):
                publication.set_subscribed(False)
                raise TypeError(
                    "Candidate microphone publication did not expose a RemoteAudioTrack"
                )
            self._start_track(track, publication, participant)
            return
        publication.set_subscribed(True)

    def _on_track_subscribed(
        self, track: Any, publication: Any, participant: Any
    ) -> None:
        if self._closing:
            return
        if not self._eligible(publication, participant) or not self._audio_track_validator(
            track
        ):
            # auto_subscribe is disabled, but an adapter/server regression must
            # still fail closed if an unrelated track appears subscribed.
            try:
                publication.set_subscribed(False)
            except Exception:
                pass
            return
        self._start_track(track, publication, participant)

    def _start_track(self, track: Any, publication: Any, participant: Any) -> None:
        track_sid = str(getattr(publication, "sid", "") or "")
        if not track_sid or track_sid == self._track_sid:
            return
        self._requested_sids.add(track_sid)
        previous = self._track_task
        previous_publication = self._active_publication
        self._track_sid = track_sid
        self._active_track = track
        self._active_publication = publication
        self._last_track_failure = None
        if previous is not None:
            previous.cancel()
        if previous_publication is not None and previous_publication is not publication:
            try:
                previous_publication.set_subscribed(False)
            except Exception:
                pass
        self._track_task = asyncio.create_task(
            self._consume_track(track, track_sid, previous)
        )
        self._track_tasks.add(self._track_task)
        self._track_task.add_done_callback(self._track_done)

    def _on_track_unsubscribed(
        self, _track: Any, publication: Any, participant: Any
    ) -> None:
        if str(getattr(participant, "identity", "")) != self.binding.candidate_identity:
            return
        self._requested_sids.discard(str(getattr(publication, "sid", "") or ""))
        if str(getattr(publication, "sid", "") or "") != self._track_sid:
            return
        self._cancel_active_track("track_unsubscribed")

    def _on_track_unpublished(self, publication: Any, participant: Any) -> None:
        self._requested_sids.discard(str(getattr(publication, "sid", "") or ""))
        if str(getattr(participant, "identity", "")) != self.binding.candidate_identity:
            return
        if str(getattr(publication, "sid", "") or "") == self._track_sid:
            self._cancel_active_track("track_unpublished")

    def _on_track_muted(self, publication: Any, participant: Any) -> None:
        """Report a server-observed candidate gap without tearing down its track.

        The browser recovery adapter deliberately mutes the LiveKit publication
        while it fences and replays a short disconnected interval.  Keeping the
        receive stream alive lets normal WebRTC audio resume after unmute; the
        state signal only authorizes the bounded browser backfill window.
        """
        if not self._eligible(publication, participant):
            return
        if str(getattr(publication, "sid", "") or "") != self._track_sid:
            return
        self._schedule_state("track_muted")

    def _on_participant_disconnected(self, participant: Any) -> None:
        if str(getattr(participant, "identity", "")) != self.binding.candidate_identity:
            return
        self._cancel_active_track("candidate_disconnected")

    def _on_room_disconnected(self, _reason: Any) -> None:
        if self._closing:
            return
        self._connected = False
        self._cancel_active_track("room_disconnected")
        self._closed_event.set()

    def _cancel_active_track(self, state: str) -> None:
        task = self._track_task
        self._track_task = None
        self._track_sid = None
        self._active_track = None
        self._active_publication = None
        if task is not None:
            task.cancel()
        self._schedule_state(state)

    def _track_done(self, task: asyncio.Task[None]) -> None:
        self._track_tasks.discard(task)
        if task.cancelled() or self._closing:
            return
        try:
            failure = task.exception()
        except (asyncio.CancelledError, Exception):
            return
        if failure is not None:
            self._last_track_failure = _classify_track_failure(failure)
            self._schedule_state("audio_stream_failed")

    async def recover_audio_stream(self) -> bool:
        """Restart the lossless iterator for the currently published mic track.

        A downstream warm-up STT failure terminates the iterator task even
        though the LiveKit room and microphone publication remain healthy.
        The domain supervisor may call this only after it has aborted the
        disposable warm-up stream and received an explicit candidate retry.
        """

        if self._closing or not self._connected:
            return False
        track = self._active_track
        publication = self._active_publication
        track_sid = str(getattr(publication, "sid", "") or "")
        if (
            track is None
            or publication is None
            or not track_sid
            or not self._audio_track_validator(track)
        ):
            return False
        previous = self._track_task
        if previous is not None and not previous.done():
            return True
        if previous is not None:
            await asyncio.gather(previous, return_exceptions=True)
        if (
            self._closing
            or not self._connected
            or self._active_track is not track
            or self._active_publication is not publication
        ):
            return False
        self._track_task = None
        self._track_sid = None
        self._last_track_failure = None
        self._start_track(track, publication, None)
        return True

    async def _consume_track(
        self,
        track: Any,
        track_sid: str,
        previous: Optional[asyncio.Task[None]],
    ) -> None:
        if previous is not None:
            await asyncio.gather(previous, return_exceptions=True)
        stream = self._audio_stream_factory(track)
        await self._report_state("microphone_subscribed")
        state = _SinkPumpState(
            queue=asyncio.Queue(maxsize=self._sink_queue_capacity_frames),
            accepted_sequence=self._sequence,
            delivered_sequence=self._sequence,
        )
        self._sink_state = state
        end_of_stream = object()

        async def pump_sink() -> None:
            try:
                while True:
                    item = await state.queue.get()
                    if item is end_of_stream:
                        return
                    await self.on_audio_frame(item)
                    async with state.condition:
                        state.delivered_sequence = item.sequence
                        state.condition.notify_all()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                state.failure = exc
                raise
            finally:
                async with state.condition:
                    state.condition.notify_all()

        sink_task = asyncio.create_task(pump_sink())
        state.task = sink_task
        try:
            async for event in stream:
                if self._closing or self._track_sid != track_sid:
                    return
                frame = event.frame
                pcm = _pcm_s16le_bytes(frame.data)
                expected_bytes = (
                    int(frame.samples_per_channel)
                    * int(frame.num_channels)
                    * 2
                )
                if len(pcm) != expected_bytes:
                    raise ValueError(
                        "LiveKit ingress returned a malformed PCM16 frame"
                    )
                self._sequence += 1
                value = LiveKitIngressAudioFrame(
                        sequence=self._sequence,
                        track_sid=track_sid,
                        sample_rate_hz=int(frame.sample_rate),
                        channels=int(frame.num_channels),
                        samples_per_channel=int(frame.samples_per_channel),
                        captured_at=datetime.now(timezone.utc).isoformat().replace(
                            "+00:00", "Z"
                        ),
                        pcm_s16le=pcm,
                    )
                if sink_task.done():
                    await sink_task
                try:
                    state.queue.put_nowait(value)
                    state.accepted_sequence = value.sequence
                except asyncio.QueueFull as exc:
                    raise LiveKitAudioIngressBackpressureError(
                        "LiveKit evidence sink exceeded its configured backpressure budget"
                    ) from exc
                # LiveKit 的本地缓冲可能让 __anext__ 连续立即返回。显式让出
                # 调度权，避免接收任务在 pump_sink 获得首次运行机会前就把
                # 有界队列一次性填满；真实慢消费仍会按配置预算失败关闭。
                await asyncio.sleep(0)
            try:
                await asyncio.wait_for(
                    state.queue.put(end_of_stream),
                    timeout=max(5.0, self._sink_backpressure_seconds),
                )
            except asyncio.TimeoutError as exc:
                raise LiveKitAudioIngressBackpressureError(
                    "LiveKit evidence sink could not drain its bounded queue"
                ) from exc
            await asyncio.wait_for(
                sink_task,
                timeout=max(5.0, self._sink_backpressure_seconds),
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            state.failure = exc
            async with state.condition:
                state.condition.notify_all()
            raise
        finally:
            sink_task.cancel()
            await asyncio.gather(sink_task, return_exceptions=True)
            try:
                await stream.aclose()
            except Exception:
                pass

    def _schedule_state(self, state: str) -> None:
        if self.on_state is None:
            return
        task = asyncio.create_task(self._report_state(state))
        self._state_tasks.add(task)
        task.add_done_callback(self._state_done)

    def _state_done(self, task: asyncio.Task[None]) -> None:
        self._state_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            return

    async def _report_state(self, state: str) -> None:
        if self.on_state is not None:
            await self.on_state(state)

    @staticmethod
    def _default_audio_stream(track: Any) -> Any:
        # LiveKit's positive capacity drops oldest frames when full. Evidence
        # may never be silently truncated, so the native iterator stays
        # lossless while _consume_track drains it into an explicit bounded pump
        # that fails closed on downstream congestion.
        return rtc.AudioStream.from_track(
            track=track,
            capacity=0,
            sample_rate=16_000,
            num_channels=1,
            frame_size_ms=20,
        )


def _pcm_s16le_bytes(value: Any) -> bytes:
    raw = bytes(value)
    if sys.byteorder == "little":
        return raw
    samples = array("h")
    samples.frombytes(raw)
    samples.byteswap()
    return samples.tobytes()


def _classify_track_failure(exc: BaseException) -> LiveKitAudioIngressFailure:
    if isinstance(exc, LiveKitAudioIngressBackpressureError):
        code = "LIVEKIT_INGRESS_SINK_BACKPRESSURE"
    elif isinstance(exc, ValueError):
        code = "LIVEKIT_INGRESS_PCM_INVALID"
    else:
        candidate = str(getattr(exc, "code", "") or "").strip().upper()
        code = candidate if candidate in _SAFE_TRACK_FAILURE_CODES else (
            "LIVEKIT_INGRESS_AUDIO_STREAM_FAILED"
        )
    cause_type = type(exc).__name__
    if cause_type not in _SAFE_TRACK_FAILURE_TYPES:
        cause_type = "Exception"
    return LiveKitAudioIngressFailure(code=code, cause_type=cause_type)
