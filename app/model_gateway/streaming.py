import os
from typing import Any, List, Protocol

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import StreamingSTTEvent, StreamingSTTRequest


class ProviderSTTStream(Protocol):
    stream_id: str
    ready_events: List[StreamingSTTEvent]

    async def send_audio(self, chunk: bytes) -> List[StreamingSTTEvent]: ...

    async def finish(self) -> List[StreamingSTTEvent]: ...

    async def abort(self) -> None: ...


class ValidatedSTTStream:
    """Enforces stream sequencing, size limits and single authoritative final."""

    def __init__(self, provider_stream: ProviderSTTStream, request: StreamingSTTRequest) -> None:
        self._stream = provider_stream
        self.request = request
        self.stream_id = provider_stream.stream_id
        self.max_chunk_bytes = int(os.getenv("INTERVIEWER_STT_STREAM_MAX_CHUNK_BYTES", "1048576"))
        self.max_total_bytes = int(os.getenv("INTERVIEWER_STT_STREAM_MAX_BYTES", "52428800"))
        self.byte_count = 0
        self._last_sequence = 0
        self._final_received = False
        self._closed = False
        self.ready_events = self._validate(provider_stream.ready_events)

    async def send_audio(self, chunk: bytes) -> List[StreamingSTTEvent]:
        if self._closed:
            raise ProviderError("provider_stream_closed", "STT stream is already closed.", retryable=False)
        if not chunk:
            return []
        if len(chunk) > self.max_chunk_bytes:
            raise ProviderError("provider_audio_chunk_too_large", "STT audio chunk exceeds the limit.", retryable=False)
        self.byte_count += len(chunk)
        if self.byte_count > self.max_total_bytes:
            raise ProviderError("provider_audio_stream_too_large", "STT audio stream exceeds the limit.", retryable=False)
        return self._validate(await self._stream.send_audio(chunk))

    async def finish(self) -> List[StreamingSTTEvent]:
        if self._closed:
            return []
        events = self._validate(await self._stream.finish())
        self._closed = True
        if not self._final_received:
            raise ProviderError(
                "provider_final_transcript_missing",
                "STT stream closed without an authoritative final transcript.",
                retryable=True,
            )
        return events

    async def abort(self) -> None:
        if not self._closed:
            await self._stream.abort()
            self._closed = True

    def _validate(self, events: List[Any]) -> List[StreamingSTTEvent]:
        validated: List[StreamingSTTEvent] = []
        for raw in events:
            event = raw if isinstance(raw, StreamingSTTEvent) else StreamingSTTEvent.model_validate(raw)
            if event.stream_id != self.stream_id:
                raise ProviderError("provider_schema_invalid", "STT event stream_id changed.", retryable=True)
            if event.sequence <= self._last_sequence:
                raise ProviderError("provider_schema_invalid", "STT event sequence is not monotonic.", retryable=True)
            if event.type == "transcript.final":
                if self._final_received or not event.is_final or not event.text.strip() or event.provider is None:
                    raise ProviderError("provider_schema_invalid", "STT final event is incomplete or duplicated.", retryable=True)
                self._final_received = True
            elif event.type == "transcript.partial" and event.is_final:
                raise ProviderError("provider_schema_invalid", "STT partial event cannot be final.", retryable=True)
            self._last_sequence = event.sequence
            validated.append(event)
        return validated
