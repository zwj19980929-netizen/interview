import asyncio
import base64
import binascii
import os
from typing import Any, Awaitable, Callable, List, Optional, Protocol

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    RealtimeSpeechDialogueEvent,
    RealtimeSpeechDialogueRequest,
    RealtimeSpeechResponseCommand,
)


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

    def __init__(self, stream: Any, *, max_pending_bytes: int, send_timeout: float = 0.5):
        self.stream = stream
        self.stream_id = stream.stream_id
        self.ready_events = stream.ready_events
        self._limit = max(1, max_pending_bytes)
        self._send_timeout = max(0.01, send_timeout)
        self._pending = 0
        self._queue: asyncio.Queue = asyncio.Queue()
        self._events: list = []
        self._failure: Optional[ProviderError] = None
        self._closed = False
        self._abort_started = False
        self._worker: Optional[asyncio.Task] = None

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
            raise self._failure
        if chunk:
            self._pending += len(chunk)
            self._queue.put_nowait(bytes(chunk))
            if self._worker is None:
                self._worker = asyncio.create_task(self._pump())
        events, self._events = self._events, []
        return events

    async def _pump(self):
        try:
            while not self._closed:
                chunk = await self._queue.get()
                try:
                    events = await self._bounded(self.stream.send_audio(chunk), self._send_timeout)
                    if not self._closed:
                        if len(self._events) + len(events) > 64:
                            raise ProviderError("provider_dialogue_event_overflow", "Optional speech events exceeded their limit.", retryable=False)
                        self._events.extend(events)
                finally:
                    self._pending -= len(chunk)
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure = exc if isinstance(exc, ProviderError) else ProviderError(
                "provider_dialogue_input_failed", "Optional speech input failed.", retryable=False)
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
