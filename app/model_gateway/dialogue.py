import asyncio
import base64
import binascii
import json
import logging
import math
import os
import re
import time
from typing import Any, Awaitable, Callable, List, Optional, Protocol

from app.model_gateway.errors import ProviderError
from app.core.errors import ApiError
from app.core.time import utc_now
from app.model_gateway.schemas import (
    RealtimeSpeechDialogueEvent,
    RealtimeSpeechDialogueRequest,
    RealtimeSpeechResponseCommand,
)


DIALOGUE_DEGRADATION_REASONS = frozenset({
    "provider_dialogue_input_overflow", "provider_dialogue_input_timeout", "provider_dialogue_input_failed",
    "provider_dialogue_event_overflow", "provider_dialogue_response_mismatch", "provider_timeout",
    "provider_unavailable", "provider_route_missing", "provider_configuration_invalid", "provider_auth_failed",
    "provider_billing_required", "provider_rate_limited", "provider_circuit_open", "provider_schema_invalid",
    "provider_audio_unavailable", "provider_stream_closed",
})


def dialogue_degradation_reason(code: Any) -> str:
    return code if isinstance(code, str) and code in DIALOGUE_DEGRADATION_REASONS else "provider_unavailable"


class ProviderSpeechDialogueStream(Protocol):
    stream_id: str
    ready_events: List[RealtimeSpeechDialogueEvent]

    async def send_audio(self, chunk: bytes) -> List[RealtimeSpeechDialogueEvent]: ...

    async def commit(
        self,
        command: RealtimeSpeechResponseCommand,
        on_event: Optional[Callable[[RealtimeSpeechDialogueEvent], Awaitable[None]]] = None,
    ) -> List[RealtimeSpeechDialogueEvent]: ...

    async def interrupt(self) -> List[RealtimeSpeechDialogueEvent]: ...

    async def abort(self) -> None: ...


class OptionalSpeechDialogueInput:
    """An optional expression stream can never backpressure authoritative PCM.

    Enqueue is nonblocking and memory bounded. A slow, failed or overflowing
    expression stream is disabled as a whole; its audio is never retried and
    its late events cannot escape. The caller's independent STT/recorder owns
    evidence and continues normally.
    """

    def __init__(self, stream: Any, *, max_pending_bytes: int, send_timeout: float = 0.5,
                 diagnostic_context: Optional[dict] = None):
        self.stream = stream
        self.stream_id = stream.stream_id
        self.ready_events = stream.ready_events
        self._limit = max(1, max_pending_bytes)
        self._send_timeout = max(0.01, send_timeout)
        self._pending = 0
        self._queue: asyncio.Queue = asyncio.Queue()
        self._events: list = []
        self._failure: Optional[Exception] = None
        self._closed = False
        self._abort_started = False
        self._worker: Optional[asyncio.Task] = None
        self._diagnostic_context = {key: value for key, value in (diagnostic_context or {}).items()
            if key in {"interview_id", "turn_id"} and isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)}
        self._diagnosed = False
        self._input_started_at: Optional[float] = None
        self._send_started_at: Optional[float] = None
        self._enqueued_bytes = self._sent_bytes = self._peak_pending = 0
        self._last_send_ms = self._max_send_ms = 0

    def _diagnose_failure(self, code: str, *, chunk_bytes: int) -> None:
        # One summary per optional stream, never a per-frame logger/DB path.
        # Values are generated here, not copied from exceptions or Provider data.
        if self._diagnosed:
            return
        self._diagnosed = True
        try:
            now = time.monotonic()
            def bounded(value, maximum):
                if type(value) is int:
                    return max(0, min(maximum, value))
                return max(0, min(maximum, int(value))) if type(value) is float and math.isfinite(value) else 0
            byte_fields = {"pending_bytes": self._pending, "chunk_bytes": chunk_bytes,
                "limit_bytes": self._limit, "peak_pending_bytes": self._peak_pending,
                "enqueued_bytes": self._enqueued_bytes, "sent_bytes": self._sent_bytes}
            time_fields = {"last_send_ms": self._last_send_ms, "max_send_ms": self._max_send_ms,
                "input_elapsed_ms": (now - self._input_started_at) * 1000 if self._input_started_at is not None else 0,
                "inflight_send_ms": (now - self._send_started_at) * 1000 if self._send_started_at is not None else 0}
            logging.getLogger("interviewer.speech_evidence").info(json.dumps({
                "event": "dialogue.input_degraded", "at": utc_now(), **self._diagnostic_context,
                "reason_code": dialogue_degradation_reason(code),
                **{key: bounded(value, 1_073_741_824) for key, value in byte_fields.items()},
                **{key: bounded(value, 86_400_000) for key, value in time_fields.items()},
                "queued_chunks": bounded(self._queue.qsize(), 1_000_000),
            }))
        except Exception:
            # Diagnostics cannot become backpressure or a recognition failure.
            pass

    @staticmethod
    def _retrieve(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    async def _bounded(self, operation, timeout: float):
        task = asyncio.ensure_future(operation)
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if not done:
                raise ProviderError("provider_dialogue_input_timeout", "Optional speech input timed out.", retryable=False)
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                task.add_done_callback(self._retrieve)

    async def send_audio(self, chunk: bytes) -> List[RealtimeSpeechDialogueEvent]:
        if self._failure is not None:
            raise self._failure
        if self._closed:
            raise ProviderError("provider_stream_closed", "Optional speech input is closed.", retryable=False)
        if self._pending + len(chunk) > self._limit:
            self._failure = ProviderError("provider_dialogue_input_overflow", "Optional speech input buffer is full.", retryable=False)
            self._diagnose_failure(self._failure.code, chunk_bytes=len(chunk))
            raise self._failure
        if chunk:
            if self._input_started_at is None:
                self._input_started_at = time.monotonic()
            self._pending += len(chunk)
            self._peak_pending = max(self._peak_pending, self._pending)
            self._enqueued_bytes = min(1_073_741_824, self._enqueued_bytes + len(chunk))
            self._queue.put_nowait(bytes(chunk))
            if self._worker is None:
                self._worker = asyncio.create_task(self._pump())
        events, self._events = self._events, []
        return events

    async def _pump(self):
        chunk = b""
        try:
            while not self._closed:
                chunk = await self._queue.get()
                self._send_started_at = time.monotonic()
                try:
                    events = await self._bounded(self.stream.send_audio(chunk), self._send_timeout)
                    self._sent_bytes = min(1_073_741_824, self._sent_bytes + len(chunk))
                    if not self._closed:
                        if len(self._events) + len(events) > 64:
                            raise ProviderError("provider_dialogue_event_overflow", "Optional speech events exceeded their limit.", retryable=False)
                        self._events.extend(events)
                finally:
                    self._last_send_ms = (time.monotonic() - self._send_started_at) * 1000
                    self._max_send_ms = max(self._max_send_ms, self._last_send_ms)
                    self._send_started_at = None
                    self._pending -= len(chunk)
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure = exc if isinstance(exc, (ApiError, ProviderError)) else ProviderError(
                "provider_dialogue_input_failed", "Optional speech input failed.", retryable=False)
            if not isinstance(exc, ApiError):
                self._diagnose_failure(self._failure.code, chunk_bytes=len(chunk))
            await self.abort()

    async def commit(self, command, on_event=None):
        if self._failure is not None:
            raise self._failure
        await self._bounded(self._queue.join(), self._send_timeout)
        if self._failure is not None:
            raise self._failure
        if self._closed:
            raise ProviderError("provider_stream_closed", "Optional speech input is closed.", retryable=False)
        return await self._bounded(self.stream.commit(command, on_event=on_event), 8.0)

    async def abort(self):
        if self._abort_started:
            return
        self._abort_started = True
        self._closed = True
        self._events.clear()
        while not self._queue.empty():
            chunk = self._queue.get_nowait()
            self._pending -= len(chunk)
            self._queue.task_done()
        worker = self._worker
        if worker is not None and worker is not asyncio.current_task():
            worker.cancel()
            await asyncio.wait({worker}, timeout=0.1)
            worker.add_done_callback(self._retrieve)
        try:
            await self._bounded(self.stream.abort(), 1.0)
        except Exception:
            pass


class ValidatedSpeechDialogueStream:
    """Deep stream seam: bounds audio and validates normalized S2S event order."""

    def __init__(
        self,
        provider_stream: ProviderSpeechDialogueStream,
        request: RealtimeSpeechDialogueRequest,
    ) -> None:
        self._stream = provider_stream
        self.request = request
        self.stream_id = provider_stream.stream_id
        self.max_chunk_bytes = int(
            os.getenv("INTERVIEWER_DIALOGUE_STREAM_MAX_CHUNK_BYTES", "1048576")
        )
        self.max_total_bytes = int(
            os.getenv("INTERVIEWER_DIALOGUE_STREAM_MAX_BYTES", "52428800")
        )
        self.max_output_bytes = int(
            os.getenv("INTERVIEWER_DIALOGUE_OUTPUT_MAX_BYTES", "20971520")
        )
        self.byte_count = 0
        self.output_byte_count = 0
        self._last_sequence = 0
        self._closed = False
        self.ready_events = self._validate(provider_stream.ready_events)

    async def send_audio(self, chunk: bytes) -> List[RealtimeSpeechDialogueEvent]:
        if self._closed:
            raise ProviderError(
                "provider_stream_closed", "Speech dialogue stream is closed.", retryable=False
            )
        if not chunk:
            return []
        if len(chunk) > self.max_chunk_bytes:
            raise ProviderError(
                "provider_audio_chunk_too_large",
                "Speech dialogue audio chunk exceeds the limit.",
                retryable=False,
            )
        self.byte_count += len(chunk)
        if self.byte_count > self.max_total_bytes:
            raise ProviderError(
                "provider_audio_stream_too_large",
                "Speech dialogue audio stream exceeds the limit.",
                retryable=False,
            )
        return self._validate(await self._stream.send_audio(chunk))

    async def commit(
        self,
        command: RealtimeSpeechResponseCommand,
        on_event: Optional[Callable[[RealtimeSpeechDialogueEvent], Awaitable[None]]] = None,
    ) -> List[RealtimeSpeechDialogueEvent]:
        if self._closed:
            raise ProviderError(
                "provider_stream_closed", "Speech dialogue stream is closed.", retryable=False
            )
        if self.byte_count <= 0:
            raise ProviderError(
                "provider_audio_unavailable",
                "Speech dialogue requires candidate audio before commit.",
                retryable=False,
            )
        events: List[RealtimeSpeechDialogueEvent] = []

        async def validate_and_collect(raw: RealtimeSpeechDialogueEvent) -> None:
            validated = self._validate([raw])
            events.extend(validated)

        returned = await self._stream.commit(command, on_event=validate_and_collect)
        if returned:
            validated = self._validate(returned)
            events.extend(validated)
        final_text = "".join(
            item.text for item in events if item.type == "output.transcript.final"
        ).strip()
        if any(item.type == "output.audio.delta" for item in events) and not final_text:
            raise ProviderError(
                "provider_final_transcript_missing",
                "Realtime speech provider returned audio without a verifiable final transcript.",
                retryable=False,
            )
        if final_text and self._normalize(final_text) != self._normalize(command.spoken_text):
            raise ProviderError(
                "provider_dialogue_response_mismatch",
                "Realtime speech provider did not preserve the approved follow-up text.",
                retryable=False,
            )
        if any(item.type == "dialogue.closed" for item in events):
            self._closed = True
        # Provider audio is withheld until its final transcript has been
        # checked against the exact ApprovedConversationAct.  This deliberately
        # trades speculative playback for the guarantee that unapproved speech
        # can never reach the candidate.
        if on_event:
            for event in events:
                await on_event(event)
        return events

    async def interrupt(self) -> List[RealtimeSpeechDialogueEvent]:
        if self._closed:
            return []
        return self._validate(await self._stream.interrupt())

    async def abort(self) -> None:
        if not self._closed:
            await self._stream.abort()
            self._closed = True

    def _validate(self, values: List[Any]) -> List[RealtimeSpeechDialogueEvent]:
        events: List[RealtimeSpeechDialogueEvent] = []
        for raw in values:
            event = (
                raw
                if isinstance(raw, RealtimeSpeechDialogueEvent)
                else RealtimeSpeechDialogueEvent.model_validate(raw)
            )
            if event.stream_id != self.stream_id or event.sequence <= self._last_sequence:
                raise ProviderError(
                    "provider_schema_invalid",
                    "Speech dialogue event identity or sequence is invalid.",
                    retryable=True,
                )
            if event.type == "output.audio.delta":
                try:
                    decoded = base64.b64decode(event.audio_base64, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise ProviderError(
                        "provider_schema_invalid",
                        "Speech dialogue audio delta is not valid base64.",
                        retryable=True,
                    ) from exc
                if not decoded:
                    raise ProviderError(
                        "provider_schema_invalid",
                        "Speech dialogue audio delta is empty.",
                        retryable=True,
                    )
                self.output_byte_count += len(decoded)
                if self.output_byte_count > self.max_output_bytes:
                    raise ProviderError(
                        "provider_output_audio_too_large",
                        "Speech dialogue output audio exceeds the limit.",
                        retryable=False,
                    )
            if event.type == "output.transcript.final" and (not event.is_final or not event.text.strip()):
                raise ProviderError(
                    "provider_schema_invalid",
                    "Speech dialogue final transcript is incomplete.",
                    retryable=True,
                )
            self._last_sequence = event.sequence
            events.append(event)
        return events

    @staticmethod
    def _normalize(value: str) -> str:
        return "".join(character.casefold() for character in value if character.isalnum())
