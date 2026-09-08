import os
from typing import Any, Dict, List, Optional, Protocol

from pydantic import ValidationError

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import StableTranscriptPreview, StreamingSTTEvent, StreamingSTTRequest


class ProviderSTTStream(Protocol):
    stream_id: str
    ready_events: List[StreamingSTTEvent]

    async def send_audio(self, chunk: bytes) -> List[StreamingSTTEvent]: ...

    async def finish(self) -> List[StreamingSTTEvent]: ...

    async def abort(self) -> None: ...


class ValidatedSTTStream:
    """Enforces sequencing, limits and one transcript or empty completion."""

    def __init__(self, provider_stream: ProviderSTTStream, request: StreamingSTTRequest) -> None:
        self._stream = provider_stream
        self.request = request
        self.stream_id = provider_stream.stream_id
        self.max_chunk_bytes = int(os.getenv("INTERVIEWER_STT_STREAM_MAX_CHUNK_BYTES", "1048576"))
        self.max_total_bytes = int(os.getenv("INTERVIEWER_STT_STREAM_MAX_BYTES", "52428800"))
        self.byte_count = 0
        self._last_sequence = 0
        self._final_received = False
        self._completion_received = False
        self._text_received = False
        self._closed = False
        self._preview: Optional[StableTranscriptPreview] = None
        self._provider_identity: Dict[str, str] = {}
        self.ready_events = self._validate(provider_stream.ready_events)

    @property
    def supports_stable_preview(self) -> bool:
        # Optional extension: existing ProviderSTTStream adapters still work.
        return callable(getattr(self._stream, "preview", None))

    @property
    def has_observed_text(self) -> bool:
        """Sticky veto against empty completion, never permission to commit.

        An adapter reader can receive words before finish raises or returns
        its queued events. Its optional local fact must survive stream repair.
        Malformed or failed optional facts conservatively veto empty results.
        """
        if self._text_received or self._preview is not None:
            return True
        try:
            observed = getattr(self._stream, "has_observed_text", False)
        except Exception:
            observed = True
        self._text_received |= observed is not False
        return self._text_received

    async def preview(self) -> Optional[StableTranscriptPreview]:
        """Read the adapter's local stable prefix, without flushing or closing.

        Adapters must implement this as a local read, never a network request.
        None means no stable sentence yet; supports_stable_preview separately
        identifies adapters that need the legacy final-snapshot path.
        """
        if self._closed:
            raise ProviderError("provider_stream_closed", "STT stream is already closed.", retryable=False)
        if not self.supports_stable_preview:
            return None
        try:
            raw = await self._stream.preview()
            if self._closed:
                raise ProviderError("provider_stream_closed", "STT stream is already closed.", retryable=False)
            if raw is None:
                if self._preview is not None:
                    self._invalid_preview()
                return None
            preview = StableTranscriptPreview.model_validate(raw)
        except (ValidationError, ValueError, TypeError):
            # Never expose pydantic's raw input/transcript in the exception.
            raise ProviderError(
                "provider_schema_invalid", "STT stable preview contract is invalid.", retryable=True,
            ) from None
        if preview.stream_id != self.stream_id:
            self._invalid_preview()
        for name, expected in self._provider_identity.items():
            if getattr(preview.provider, name) != expected:
                self._invalid_preview()
        previous = self._preview
        if previous is not None:
            if preview.revision < previous.revision or preview.language != previous.language:
                self._invalid_preview()
            before = previous.segments
            after = preview.segments
            if len(after) < len(before) or after[:len(before)] != before:
                self._invalid_preview()
            if preview.revision == previous.revision and after != before:
                self._invalid_preview()
            if after == before and preview.confidence != previous.confidence:
                self._invalid_preview()
        for name in ("provider_id", "model", "request_id"):
            identity = getattr(preview.provider, name)
            if identity:
                self._provider_identity.setdefault(name, identity)
        self._preview = preview.model_copy(deep=True)
        return preview.model_copy(deep=True)

    @staticmethod
    def _invalid_preview() -> None:
        raise ProviderError(
            "provider_schema_invalid", "STT stable preview identity or revision is invalid.", retryable=True,
        )

    async def send_audio(self, chunk: bytes, *, wait_for_capacity: bool = False) -> List[StreamingSTTEvent]:
        if self._closed:
            raise ProviderError("provider_stream_closed", "STT stream is already closed.", retryable=False)
        if not chunk:
            return []
        if len(chunk) > self.max_chunk_bytes:
            raise ProviderError("provider_audio_chunk_too_large", "STT audio chunk exceeds the limit.", retryable=False)
        # Replay owns a separate intake buffer, so it can wait for actual
        # transport capacity without blocking live evidence acceptance.
        capacity = getattr(self._stream, "wait_audio_capacity", None)
        if wait_for_capacity and callable(capacity):
            await capacity(len(chunk))
            if self._closed:
                raise ProviderError("provider_stream_closed", "STT stream is already closed.", retryable=False)
        self.byte_count += len(chunk)
        if self.byte_count > self.max_total_bytes:
            raise ProviderError("provider_audio_stream_too_large", "STT audio stream exceeds the limit.", retryable=False)
        return self._validate(await self._stream.send_audio(chunk))

    async def finish(self) -> List[StreamingSTTEvent]:
        if self._closed:
            return []
        events = self._validate(await self._stream.finish(), completion_requested=True)
        self._closed = True
        if not self._completion_received:
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

    def _validate(self, events: List[Any], *, completion_requested: bool = False) -> List[StreamingSTTEvent]:
        validated: List[StreamingSTTEvent] = []
        for raw in events:
            try:
                # Provider-owned model instances can be mutated or constructed
                # without validation; validate a fresh value at this seam.
                if isinstance(raw, StreamingSTTEvent):
                    raw = raw.model_dump(warnings=False)
                event = StreamingSTTEvent.model_validate(raw)
            except (ValidationError, ValueError, TypeError):
                raise ProviderError("provider_schema_invalid", "STT event contract is invalid.", retryable=True) from None
            if event.stream_id != self.stream_id:
                raise ProviderError("provider_schema_invalid", "STT event stream_id changed.", retryable=True)
            if event.sequence <= self._last_sequence:
                raise ProviderError("provider_schema_invalid", "STT event sequence is not monotonic.", retryable=True)
            if event.type == "transcript.final":
                if self._completion_received or not event.is_final or not event.text.strip() or event.provider is None:
                    raise ProviderError("provider_schema_invalid", "STT final event is incomplete or duplicated.", retryable=True)
                self._final_received = True
                self._completion_received = True
            elif event.type == "transcript.empty":
                if (not completion_requested or self._completion_received
                        or self.has_observed_text):
                    raise ProviderError("provider_schema_invalid", "STT empty completion conflicts with prior recognition.", retryable=True)
                for name, expected in self._provider_identity.items():
                    if getattr(event.provider, name) != expected:
                        raise ProviderError("provider_schema_invalid", "STT empty completion provider changed.", retryable=True)
                self._completion_received = True
            elif event.type == "transcript.partial":
                if event.is_final or self._completion_received:
                    raise ProviderError("provider_schema_invalid", "STT partial event cannot be final or follow completion.", retryable=True)
                self._text_received |= bool(event.text.strip()) or any(segment.text.strip() for segment in event.segments)
            self._last_sequence = event.sequence
            if event.provider is not None:
                for name in ("provider_id", "model", "request_id"):
                    identity = getattr(event.provider, name)
                    if identity:
                        self._provider_identity.setdefault(name, identity)
            validated.append(event)
        return validated
