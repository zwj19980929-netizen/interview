"""One approved act's bounded, revocable speech output and private archive.

The provider, local sender and remote player have distinct completion points.
Nothing in this module creates an answer or grants a client decision authority.
"""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any, Awaitable, Callable, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.interview_agent_metrics import measure_interview_agent_stage, observe_interview_agent_metric
from app.domain.interview_agent import LiveSpeechBinding


class ApprovedSpeechOutput:
    def __init__(
        self, *, performance_id: str, stream: Any, publisher: Any,
        assert_current: Callable[[], None], archive: Callable[..., dict],
        ready_timeout: float = 5.0, drain_timeout: float = 5.0,
        total_timeout: float = 300.0, max_bytes: int = 20 * 1024 * 1024 - 44,
    ) -> None:
        self.performance_id = performance_id
        self.output_id = new_id("speech_output")
        self.stream, self.publisher = stream, publisher
        self._fence, self._archive = assert_current, archive
        self._ready_timeout, self._drain_timeout = ready_timeout, drain_timeout
        self._deadline = monotonic() + total_timeout
        self._created_at = monotonic()
        self._max_bytes = max_bytes
        self._ready, self._drained = asyncio.Event(), asyncio.Event()
        self._closed = False
        self._running = False
        self.producer_finished = False
        self.archive_reference: Optional[str] = None
        self.total_samples = 0

    async def open(self) -> LiveSpeechBinding:
        try:
            self._assert_current()
            if (self.stream.sample_rate_hz != 24_000 or self.stream.channels != 1
                    or self.stream.ready_event.provider.provider_id == "mock"):
                raise ApiError("AGENT_STREAM_AUDIO_INVALID", "Formal streamed speech requires real mono 24 kHz PCM.", status_code=503)
            publication = await self.publisher.open()
            self._assert_current()
            return LiveSpeechBinding(
                output_id=self.output_id,
                publisher_identity=publication.participant_identity,
                track_sid=publication.track_sid, track_name=publication.track_name,
                sample_rate_hz=publication.sample_rate_hz, channels=publication.channels,
            )
        except BaseException:
            await self.abort()
            raise

    def acknowledge_ready(self, performance_id: str, output_id: str) -> bool:
        if not self.matches(performance_id, output_id) or self.producer_finished:
            return False
        self._assert_current()
        self._ready.set()
        return True

    def acknowledge_drained(self, performance_id: str, output_id: str, reason: str) -> bool:
        if not self.matches(performance_id, output_id) or not self.producer_finished or reason != "drained":
            return False
        self._assert_current()
        self._drained.set()
        return True

    def matches(self, performance_id: str, output_id: str) -> bool:
        return not self._closed and performance_id == self.performance_id and output_id == self.output_id

    async def run(self, on_producer_finished: Callable[[dict], Awaitable[None]]) -> None:
        if self._running:
            raise ApiError("AGENT_STREAM_ALREADY_RUNNING", "Approved speech permits one producer.", status_code=409)
        self._running = True
        pcm = bytearray()
        final = None
        try:
            with measure_interview_agent_stage("tts_playback_ready_ms"):
                await self._wait(self._ready, self._ready_timeout, "AGENT_AUDIO_SUBSCRIPTION_TIMEOUT")
            # A monitor fences even a stalled provider read. Cancelling it
            # reaches the validated provider stream and clears the audio source.
            consumer = asyncio.create_task(self._consume(pcm))
            try:
                while not consumer.done():
                    self._assert_current()
                    if monotonic() >= self._deadline:
                        raise ApiError("AGENT_AUDIO_OUTPUT_TIMEOUT", "Approved speech exceeded its time budget.", status_code=504)
                    await asyncio.wait({consumer}, timeout=0.05)
                self._assert_current()
                final = consumer.result()
            finally:
                if not consumer.done():
                    consumer.cancel()
                    await asyncio.gather(consumer, return_exceptions=True)
            self._assert_current()
            if final is None or not pcm:
                raise ApiError("AGENT_AUDIO_FINAL_REQUIRED", "Approved speech requires a complete provider final.", status_code=502)
            # Archive only complete validated output. Provider URLs never enter
            # events or playback, and interrupted bytes never masquerade as final.
            with measure_interview_agent_stage("tts_asset_import_ms"):
                stored = self._archive(pcm_s16le=bytes(pcm), sample_rate_hz=24_000, channels=1)
            self.archive_reference = stored["audio_uri"]
            pcm.clear()
            await asyncio.wait_for(self.publisher.finish(), timeout=max(0.001, self._deadline - monotonic()))
            self._assert_current()
            self.producer_finished = True
            await asyncio.wait_for(on_producer_finished({
                "performance_id": self.performance_id, "output_id": self.output_id,
                "total_samples": self.total_samples, "sample_rate_hz": 24_000,
            }), timeout=max(0.001, self._deadline - monotonic()))
            with measure_interview_agent_stage("tts_remote_drain_ms"):
                await self._wait(self._drained, self._drain_timeout, "AGENT_AUDIO_PLAYBACK_TIMEOUT")
            # The ACK handler may already have moved Floor. Normal cleanup no
            # longer requires the speaking fence; stale cleanup owns only its track.
            self._closed = True
            await self.publisher.aclose()
        finally:
            pcm.clear()
            await self.abort()

    async def _consume(self, pcm: bytearray) -> Any:
        final = None
        async for event in self.stream.events():
            self._assert_current()
            if event.type == "audio.chunk":
                chunk = event.pcm_s16le
                if self.total_samples == 0:
                    observe_interview_agent_metric("tts_first_pcm_ms", (monotonic() - self._created_at) * 1000)
                if len(pcm) + len(chunk) > self._max_bytes:
                    raise ApiError("AGENT_AUDIO_OUTPUT_TOO_LARGE", "Approved speech exceeds the private archive budget.", status_code=502)
                pcm.extend(chunk)
                # A provider event can be larger than one adapter write, never
                # larger than the gateway's independent bounded chunk contract.
                for offset in range(0, len(chunk), 65_536):
                    self._assert_current()
                    await self.publisher.publish(chunk[offset:offset + 65_536])
                self.total_samples += len(chunk) // 2
            elif event.type == "audio.final":
                final = event
        return final

    async def _wait(self, event: asyncio.Event, timeout: float, code: str) -> None:
        deadline = min(monotonic() + timeout, self._deadline)
        while not event.is_set():
            self._assert_current()
            if monotonic() >= deadline:
                raise ApiError(code, "Approved speech playback did not acknowledge in time.", status_code=504)
            try:
                await asyncio.wait_for(event.wait(), timeout=min(0.05, max(0.001, deadline - monotonic())))
            except asyncio.TimeoutError:
                pass

    def _assert_current(self) -> None:
        if self._closed:
            raise ApiError("AGENT_SPEECH_OUTPUT_STALE", "Approved speech output is no longer current.", status_code=409)
        self._fence()
        if monotonic() >= self._deadline:
            raise ApiError("AGENT_AUDIO_OUTPUT_TIMEOUT", "Approved speech exceeded its time budget.", status_code=504)

    async def abort(self) -> None:
        self._closed = True
        try:
            await self.publisher.abort()
        finally:
            await self.stream.abort()
