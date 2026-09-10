"""Continuous capture across bounded, individually validated STT segments.

A provider final closes a recognition segment, not the candidate's answer.
Every accepted frame enters the recorder before any provider call. Recognition
may be rebuilt independently, replaying only unconfirmed audio. Only validated
server finals are joined; recovery never certifies an old prefix as complete.
"""

from __future__ import annotations
from app.domain.speech_quality import minimum_reported_confidence

import asyncio
import math
import logging
from array import array
from collections import deque
from typing import Awaitable, Callable, Optional

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import StableTranscriptPreview, StreamingSTTEvent
from app.model_gateway.streaming import ValidatedSTTStream
from app.core.interview_agent_metrics import observe_interview_agent_metric
from app.core.speech_diagnostics import record_stt_audio_origin

_LOG = logging.getLogger(__name__)


class ContinuousSTT:
    def __init__(self, stream: ValidatedSTTStream, *,
                 reopen: Callable[[], Awaitable[ValidatedSTTStream]],
                 record: Callable[[bytes], None], bytes_per_second: int,
                 speech_activity: object = None,
                 max_bytes: int = 52_428_800, final_timeout_seconds: float = 10.0) -> None:
        self.stream_id = stream.stream_id
        self.ready_events = stream.ready_events
        self._stream: Optional[ValidatedSTTStream] = stream
        self._preview_supported = stream.supports_stable_preview
        observe_interview_agent_metric("stt_recognition_opened", 1)
        self._finishing_stream: Optional[ValidatedSTTStream] = None
        self._retired_stream: Optional[ValidatedSTTStream] = None
        self._reopen = reopen
        self._record = record
        self._speech_activity = speech_activity
        self._bytes_per_second = max(1, bytes_per_second)
        record_stt_audio_origin(stream, start_byte=0, bytes_per_second=self._bytes_per_second)
        self._max_bytes = max_bytes
        self._final_timeout_seconds = max(0.01, min(10.0, final_timeout_seconds))
        self._received_bytes = 0
        self._forwarded_bytes = 0
        self._segment_start_bytes = 0
        self._pending: deque[bytes] = deque()
        self._pending_bytes = 0
        self._rotating = False
        self._closed = False
        self._abort_task: Optional[asyncio.Task] = None
        self._recovery_error: Optional[BaseException] = None
        self._lock = asyncio.Lock()
        self._final: Optional[StreamingSTTEvent] = None
        self._segment_audio: list[bytes] = []
        self._unresolved = False
        self._segment_text_seen = False
        self._missing_final_logged = False
        self._forward_idle = asyncio.Event()
        self._inflight_has_speech = False
        self._forward_idle.set()

    @property
    def paused(self) -> bool:
        return self._stream is None and not self._rotating

    @property
    def supports_stable_preview(self) -> bool:
        return self._preview_supported

    async def preview(self) -> Optional[StableTranscriptPreview]:
        """Read stable provider sentences without cutting or draining capture.

        Local send progress is not a provider ACK. Even a preview without a
        visible tail must be reconciled against a real final before commit.
        """
        async with self._lock:
            self._assert_open()
            self._assert_recognition_current()
            stream = self._stream
            if stream is None or not self.supports_stable_preview:
                return None
            try:
                preview = await stream.preview()
                if preview is not None and preview.text.strip():
                    self._segment_text_seen = True
            except asyncio.CancelledError:
                self._mark_interrupted()
                raise
            except Exception as exc:
                # Recovery must rebuild this stream, not report success after
                # recover() sees a falsely healthy local state and does nothing.
                self._remember_failure(exc)
                raise
            finally:
                self._observe_stream_text(stream)
            self._assert_open()
            self._assert_recognition_current()
            if stream is not self._stream:
                return None
            if preview is None:
                # A final revised at the commit cut may still need semantic
                # preparation (including a bounded inference retry). Reopening
                # must not hide that durable prefix forever in pure silence.
                # Any unconfirmed voiced suffix blocks reuse until recognized.
                previous = self._final
                if previous is None or not previous.segments:
                    return None
                return StableTranscriptPreview(
                    stream_id=previous.stream_id, revision=previous.sequence,
                    text=previous.text, language=previous.language,
                    confidence=previous.confidence,
                    segments=[s.model_copy(deep=True) for s in previous.segments],
                    provider=previous.provider.model_copy(deep=True),
                    has_unstable_tail=(self._unresolved or self._inflight_has_speech
                                       or self._segment_has_speech() or self._pending_has_speech()),
                )
            offset = int(self._segment_start_bytes * 1000 / self._bytes_per_second)
            previous = self._final
            return preview.model_copy(deep=True, update={
                "text": (previous.text if previous else "") + preview.text,
                "segments": ([s.model_copy(deep=True) for s in previous.segments] if previous else []) + [
                    segment.model_copy(update={"start_ms": segment.start_ms + offset,
                                               "end_ms": segment.end_ms + offset})
                    for segment in preview.segments
                ],
                "confidence": minimum_reported_confidence(previous.confidence, preview.confidence) if previous else preview.confidence,
                "has_unstable_tail": (preview.has_unstable_tail or self._inflight_has_speech
                                      or self._pending_has_speech()),
            })

    @property
    def forwarded_bytes(self) -> int:
        return self._forwarded_bytes

    @property
    def recovery_required(self) -> bool:
        return self._recovery_error is not None and not self._closed

    @property
    def recovery_error(self) -> Optional[BaseException]:
        # Process-local cause for the recovery policy, never a wire/log payload.
        return self._recovery_error

    async def send_audio(self, chunk: bytes) -> list[StreamingSTTEvent]:
        self._assert_open()
        self._assert_no_capture_gap()
        if not chunk:
            return []
        if self._received_bytes + len(chunk) > self._max_bytes:
            self._recovery_error = ProviderError("provider_audio_stream_too_large",
                                                 "Capture size limit exceeded.", retryable=False)
            raise self._recovery_error
        # One consistent intake budget covers finalization (<=10s) and the
        # owner's three bounded recovery attempts (<=15s each). Entering
        # recovery must not shrink an already accepted 60s preparation buffer.
        if self._pending_bytes + len(chunk) > self._bytes_per_second * 60:
            error = ProviderError("provider_audio_recovery_buffer_exceeded",
                                  "Unconfirmed audio intake buffer exhausted.", retryable=False)
            self._recovery_error = error
            raise error
        chunk = bytes(chunk)
        # The recorder can enforce ownership and durable Evidence integrity.
        # Its exceptions are not provider failures and must propagate unchanged.
        self._record(chunk)
        if self._speech_activity is not None:
            self._speech_activity.observe(chunk)
        self._received_bytes += len(chunk)
        self._pending.append(chunk)
        self._pending_bytes += len(chunk)
        if (self._rotating or self._stream is None or self.recovery_required
                or not self._forward_idle.is_set()):
            return []
        self._forward_idle.clear()
        events: list[StreamingSTTEvent] = []
        try:
            while self._pending and not self._rotating and not self.recovery_required:
                self._assert_open()
                events.extend(await self._forward_pending(suppress_retryable=True))
                # Provider enqueue often completes synchronously. A backlog must
                # not fill its sender queue before that sender gets scheduled.
                await asyncio.sleep(0)
            return events
        finally:
            self._forward_idle.set()

    async def _forward_pending(self, *, suppress_retryable: bool = False,
                               wait_for_capacity: bool = False) -> list[StreamingSTTEvent]:
        self._assert_no_capture_gap()
        stream = self._stream
        assert stream is not None
        chunk = self._pending.popleft()
        self._pending_bytes -= len(chunk)
        # Move before send: even a failed/cancelled send may have reached the
        # provider. It belongs to the unconfirmed segment until a valid final.
        self._segment_audio.append(chunk)
        self._inflight_has_speech = (
            self._speech_activity.since(self._forwarded_bytes) if self._speech_activity is not None
            else self._has_signal([chunk])
        )
        self._forwarded_bytes += len(chunk)
        try:
            events = await stream.send_audio(chunk, wait_for_capacity=wait_for_capacity)
        except ProviderError as exc:
            self._remember_failure(exc)
            if suppress_retryable and exc.retryable:
                return []
            raise
        except asyncio.CancelledError:
            self._mark_interrupted()
            raise
        except Exception as exc:
            self._remember_failure(exc)
            raise
        finally:
            self._observe_stream_text(stream)
            self._inflight_has_speech = False
        prefix = self._final.text if self._final else ""
        self._segment_text_seen |= any(
            event.type in {"transcript.partial", "transcript.final"} and event.text.strip()
            for event in events
        )
        return [event.model_copy(update={"text": prefix + event.text})
                if event.type == "transcript.partial" else event for event in events]

    async def snapshot(self, *, resume: bool = True) -> Optional[StreamingSTTEvent]:
        async with self._lock:
            self._assert_open()
            self._rotating = True
            stream = None
            segment_finalized = False
            try:
                await self._forward_idle.wait()
                self._assert_open()
                self._assert_recognition_current()
                # Frames queued behind an in-flight send predate this cut and
                # must be included. New frames during finish remain pending.
                pending_count = len(self._pending) if self._stream is not None else 0
                for _ in range(pending_count):
                    await self._forward_pending(wait_for_capacity=True)
                    await asyncio.sleep(0)
                    self._assert_open()
                    self._assert_no_capture_gap()
                stream, self._stream = self._stream, None
                self._finishing_stream = stream
                end_bytes = self._forwarded_bytes
                if stream is not None:
                    try:
                        if end_bytes == self._segment_start_bytes:
                            # A fresh, empty stream has no new evidence. Some
                            # development providers otherwise repeat a final.
                            self._observe_stream_text(stream)
                            await stream.abort()
                            events = []
                        else:
                            observe_interview_agent_metric("stt_recognition_finished", 1)
                            try:
                                events = await asyncio.wait_for(
                                    stream.finish(), timeout=self._final_timeout_seconds,
                                )
                            except asyncio.TimeoutError as exc:
                                raise ProviderError("provider_timeout", "STT finalization deadline exceeded.",
                                                    retryable=True) from exc
                    except ProviderError as exc:
                        self._observe_stream_text(stream)
                        if exc.code != "provider_final_transcript_missing":
                            raise
                        await stream.abort()
                        events = []
                    self._observe_stream_text(stream)
                    self._assert_open()
                    self._assert_no_capture_gap()
                    final = next((e for e in events if e.type == "transcript.final"), None)
                    empty = any(e.type == "transcript.empty" for e in events)
                    # Empty is an explicit, validated provider completion,
                    # not an inference from local VAD or a missing result.
                    # Earlier text from a failed attempt still needs a final;
                    # a replacement stream cannot erase that evidence.
                    self._unresolved = final is None and (
                        self._segment_text_seen or (not empty and self._segment_has_speech())
                    )
                    if self._unresolved and not self._missing_final_logged:
                        _LOG.warning("stt_snapshot_unresolved prior_final=%s segment_bytes=%d",
                                     self._final is not None, end_bytes - self._segment_start_bytes)
                        self._missing_final_logged = True
                    if final is not None:
                        self._missing_final_logged = False
                        offset = int(self._segment_start_bytes * 1000 / self._bytes_per_second)
                        segments = [s.model_copy(update={"start_ms": s.start_ms + offset,
                                                         "end_ms": s.end_ms + offset})
                                    for s in final.segments]
                        previous = self._final
                        self._final = final.model_copy(update={
                            "text": (previous.text if previous else "") + final.text,
                            "segments": (list(previous.segments) if previous else []) + segments,
                            "confidence": minimum_reported_confidence(previous.confidence, final.confidence) if previous else final.confidence,
                        })
                    if not self._unresolved:
                        self._segment_start_bytes = end_bytes
                        self._segment_audio.clear()
                        self._segment_text_seen = False
                        segment_finalized = True
                result = self._final.model_copy(deep=True) if self._final and not self._unresolved else None
                if resume:
                    await self._resume_locked()
                return result
            except BaseException as exc:
                if stream is not None and not segment_finalized:
                    self._observe_stream_text(stream)
                if isinstance(exc, asyncio.CancelledError):
                    self._mark_interrupted()
                elif not (isinstance(exc, ProviderError) and exc.code == "provider_stream_closed"):
                    self._remember_failure(exc)
                if stream is not None and not segment_finalized:
                    self._retired_stream = stream
                    try:
                        await self._dispose_retired()
                    except Exception:
                        # Preserve the primary failure. A subsequent recover
                        # must confirm cleanup before opening another stream.
                        pass
                raise
            finally:
                self._finishing_stream = None
                self._rotating = False

    async def _resume_locked(self) -> None:
        self._assert_open()
        self._assert_recognition_current()
        await self._open_and_replay()

    async def _open_and_replay(self) -> None:
        self._assert_open()
        self._assert_no_capture_gap()
        if self._stream is None:
            stream = await self._reopen()
            if self._closed:
                await stream.abort()
                self._assert_open()
            self._stream = stream
            record_stt_audio_origin(stream, start_byte=self._segment_start_bytes,
                                    bytes_per_second=self._bytes_per_second)
            self._preview_supported = stream.supports_stable_preview
            observe_interview_agent_metric("stt_recognition_opened", 1)
            # Missing final is unresolved evidence, not permission to use an
            # old prefix. Replay this segment without recording it twice.
            for chunk in self._segment_audio:
                self._assert_no_capture_gap()
                try:
                    events = await stream.send_audio(chunk, wait_for_capacity=True)
                    self._segment_text_seen |= any(
                        event.type in {"transcript.partial", "transcript.final"} and event.text.strip()
                        for event in events
                    )
                finally:
                    self._observe_stream_text(stream)
                await asyncio.sleep(0)
                self._assert_open()
        while self._pending:
            self._assert_open()
            await self._forward_pending(wait_for_capacity=True)
            await asyncio.sleep(0)
        self._assert_open()
        self._assert_no_capture_gap()

    async def _dispose_retired(self) -> None:
        stream = self._retired_stream
        if stream is not None:
            self._observe_stream_text(stream)
            await asyncio.wait_for(stream.abort(), timeout=2.0)
            if self._retired_stream is stream:
                self._retired_stream = None

    def _observe_stream_text(self, stream: ValidatedSTTStream) -> None:
        # Monotonic for this unconfirmed audio segment, across every provider
        # attempt. Empty completion can never erase previously seen words.
        self._segment_text_seen |= bool(getattr(stream, "has_observed_text", False))

    def _mark_interrupted(self) -> None:
        if self._recovery_error is None:
            self._recovery_error = ProviderError("provider_stream_interrupted",
                                                 "Recognition operation was interrupted.", retryable=True)

    def _assert_recognition_current(self) -> None:
        if self._recovery_error is not None:
            raise self._recovery_error

    def _assert_no_capture_gap(self) -> None:
        if self._capture_gap:
            raise self._recovery_error

    @property
    def _capture_gap(self) -> bool:
        return (isinstance(self._recovery_error, ProviderError)
                and self._recovery_error.code in {
                    "provider_audio_recovery_buffer_exceeded", "provider_audio_stream_too_large",
                })

    def _remember_failure(self, exc: BaseException) -> None:
        if not self._capture_gap:
            self._recovery_error = exc

    async def recover(self) -> None:
        """One fenced rebuild and ordered replay; callers own retry budgets."""
        async with self._lock:
            self._assert_open()
            self._assert_no_capture_gap()
            if not self.recovery_required:
                return
            self._rotating = True
            try:
                await self._forward_idle.wait()
                self._assert_open()
                # No failed stream is reused, even if its send reached the
                # provider before the error. The full segment is replayed once.
                await self._dispose_retired()
                self._assert_open()
                self._retired_stream, self._stream = self._stream, None
                await self._dispose_retired()
                self._assert_open()
                await self._open_and_replay()
                self._recovery_error = None
            except asyncio.CancelledError:
                self._mark_interrupted()
                raise
            except BaseException as exc:
                self._remember_failure(exc)
                raise
            finally:
                self._rotating = False

    async def resume(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._rotating = True
            try:
                await self._forward_idle.wait()
                await self._resume_locked()
            except asyncio.CancelledError:
                self._mark_interrupted()
                raise
            except BaseException as exc:
                self._remember_failure(exc)
                raise
            finally:
                self._rotating = False

    async def finish(self) -> list[StreamingSTTEvent]:
        # The caller must have reconciled the snapshot and the input revision
        # before using this paused fast path for the answer transaction.
        if not self.paused:
            await self.snapshot(resume=False)
        return [self.commit_final()]

    @staticmethod
    def _has_signal(chunks: object) -> bool:
        # This conservative energy gate is only an extra stale-input fence,
        # not the turn detector. Retain even the quiet suffix in Evidence.
        for chunk in chunks:
            if len(chunk) % 2:
                return True
            values = array("h", chunk)
            if values and math.sqrt(sum(x*x for x in values) / len(values)) / 32768 >= 0.001:
                return True
        return False

    def _assert_open(self) -> None:
        if self._closed:
            raise ProviderError("provider_stream_closed", "Capture is closed.", retryable=False)

    def _segment_has_speech(self) -> bool:
        if self._speech_activity is not None:
            return self._speech_activity.since(self._segment_start_bytes)
        return self._has_signal(self._segment_audio)

    def _pending_has_speech(self) -> bool:
        if self._speech_activity is not None:
            return self._speech_activity.since(self._forwarded_bytes)
        return self._has_signal(self._pending)

    def assert_can_commit(self) -> None:
        self._assert_open()
        self._assert_recognition_current()
        if not self.paused or self._pending_has_speech():
            raise ProviderError("provider_snapshot_stale", "Audio arrived after the final snapshot.", retryable=True)
        if self._final is None or self._unresolved:
            raise ProviderError("provider_final_transcript_missing", "No server final.", retryable=True)

    def confirmed_final(self) -> StreamingSTTEvent:
        self.assert_can_commit()
        return self._final.model_copy(deep=True)

    def commit_final(self) -> StreamingSTTEvent:
        """Synchronous commit cut: no audio can interleave before sealing."""
        self.assert_can_commit()
        # Preserve quiet PCM in the private recording; never silently discard
        # frames accepted while the provider was finishing its final.
        while self._pending:
            chunk = self._pending.popleft()
            self._forwarded_bytes += len(chunk)
        self._closed = True
        self._pending_bytes = 0
        self._segment_audio.clear()
        return self._final.model_copy(deep=True)

    async def abort(self) -> None:
        if self._abort_task is None:
            self._closed = True
            stream, self._stream = self._stream, None
            self._pending.clear()
            self._pending_bytes = 0
            self._segment_audio.clear()
            streams = {id(item): item for item in
                       (stream, self._finishing_stream, self._retired_stream) if item is not None}
            # A revoked owner can cancel its caller immediately. This capture
            # still owns every detached transport until bounded cleanup ends;
            # subsequent close callers join the same operation.
            self._abort_task = asyncio.create_task(self._abort_streams(list(streams.values())))
            self._abort_task.add_done_callback(self._consume_abort_result)
        await asyncio.shield(self._abort_task)

    async def _abort_streams(self, streams: list[ValidatedSTTStream]) -> None:
        # One failed close must not prevent closing another detached stream.
        # Create owned Tasks first: cancelling a wait_for coroutine before its
        # first step must not abandon an unawaited inner abort coroutine.
        tasks = [asyncio.create_task(stream.abort()) for stream in streams]
        results = await asyncio.gather(
            *(asyncio.wait_for(task, timeout=2.0) for task in tasks),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        self._retired_stream = None

    @staticmethod
    def _consume_abort_result(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()
