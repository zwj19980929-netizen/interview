"""Provider-neutral, bounded PCM transport for an already-approved TTS text.

This is a transport variant of tts.synthesize, not a generative dialogue
capability. Once a PCM chunk escapes this module it must never be replayed by
an automatic provider/batch fallback.
"""

import asyncio
from time import monotonic
from typing import Any, AsyncIterator, Callable, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ProviderMeta, Usage


class TTSStreamEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stream_id: str = Field(min_length=1, max_length=200, strict=True)
    sequence: int = Field(ge=1, strict=True)
    type: Literal["stream.ready", "audio.chunk", "audio.final"]
    sample_rate_hz: int = Field(ge=8000, le=48000, strict=True)
    channels: Literal[1] = 1
    sample_format: Literal["pcm_s16le"] = "pcm_s16le"
    # Raw audio cannot accidentally enter JSON events, logs or model records.
    pcm_s16le: bytes = Field(default=b"", strict=True, exclude=True, repr=False)
    total_audio_bytes: Optional[int] = Field(default=None, ge=0, strict=True)
    provider: ProviderMeta
    usage: Optional[Usage] = None


class ProviderTTSStream(Protocol):
    stream_id: str
    ready_event: TTSStreamEvent

    def events(self) -> AsyncIterator[TTSStreamEvent]: ...

    async def abort(self) -> None: ...


def tts_stream_error(code: str, message: str, *, retryable: bool = True) -> ProviderError:
    return ProviderError(code, message, retryable=retryable)


class ValidatedTTSStream:
    """Single-consumer stream, with one validated terminal event and no retry.

    The route timeout bounds opening/each provider read, not total spoken
    duration. A separate wall-clock bound includes downstream backpressure.
    Callers must abort on early exit (including a superseded performance).
    """

    def __init__(
        self,
        provider_stream: ProviderTTSStream,
        *,
        provider_id: str,
        model: str,
        read_timeout_s: float,
        max_chunk_bytes: int = 262144,
        max_total_bytes: int = 20 * 1024 * 1024,
        max_audio_seconds: float = 180.0,
        max_wall_seconds: float = 300.0,
        on_terminal: Optional[Callable[[str, Optional[TTSStreamEvent], Optional[ProviderError]], None]] = None,
    ) -> None:
        if (
            not isinstance(getattr(provider_stream, "stream_id", None), str)
            or not hasattr(provider_stream, "ready_event")
            or not callable(getattr(provider_stream, "events", None))
            or not callable(getattr(provider_stream, "abort", None))
        ):
            raise tts_stream_error("provider_schema_invalid", "Provider does not expose the TTS stream protocol.")
        self._stream = provider_stream
        self.stream_id = provider_stream.stream_id
        self._provider_id = provider_id
        self._model = model
        self._read_timeout_s = max(0.01, read_timeout_s)
        self._max_chunk_bytes = max_chunk_bytes
        self._max_total_bytes = max_total_bytes
        self._max_audio_seconds = max_audio_seconds
        self._deadline = monotonic() + max_wall_seconds
        self._on_terminal = on_terminal
        self._notified = False
        self._closed = False
        self._consumed = False
        self._read_task = None
        self._sequence = 0
        self._request_id = ""
        self.byte_count = 0
        self.audio_started = False
        self.ready_event = self._validate(provider_stream.ready_event, ready=True)
        self.sample_rate_hz = self.ready_event.sample_rate_hz
        self.channels = self.ready_event.channels
        self.sample_format = self.ready_event.sample_format

    def _error(self, error: ProviderError) -> ProviderError:
        error.details = {**error.details, "audio_started": self.audio_started, "fallback_allowed": not self.audio_started}
        return error

    def _validate(self, raw: Any, *, ready: bool = False) -> TTSStreamEvent:
        try:
            if isinstance(raw, TTSStreamEvent):
                raw = {**raw.model_dump(), "pcm_s16le": raw.pcm_s16le}
            if not isinstance(raw, dict) or type(raw.get("channels", 1)) is not int:
                raise ValueError("Invalid TTS channel type.")
            provider = raw.get("provider")
            if isinstance(provider, ProviderMeta):
                provider = provider.model_dump()
            if (
                not isinstance(provider, dict)
                or set(provider) != {"provider_id", "model", "request_id", "latency_ms"}
                or any(not isinstance(provider[key], str) or len(provider[key]) > 200 for key in ("provider_id", "model", "request_id"))
                or not provider["provider_id"] or not provider["model"]
                or type(provider["latency_ms"]) is not int or provider["latency_ms"] < 0
            ):
                raise ValueError("Invalid TTS provider metadata.")
            usage = raw.get("usage")
            if isinstance(usage, Usage):
                usage = usage.model_dump()
            if usage is not None and (
                not isinstance(usage, dict)
                or set(usage) - {"input_tokens", "output_tokens", "total_tokens"}
                or any(type(value) is not int or value < 0 for value in usage.values())
            ):
                raise ValueError("Invalid TTS usage metadata.")
            event = TTSStreamEvent.model_validate(raw)
        except (ValidationError, TypeError, ValueError) as exc:
            raise self._error(tts_stream_error("provider_schema_invalid", "TTS event does not match the PCM stream contract.")) from exc
        invalid = (
            event.stream_id != self.stream_id
            or event.sequence != self._sequence + 1
            or event.provider.provider_id != self._provider_id
            or event.provider.model != self._model
            or (self._request_id and event.provider.request_id != self._request_id)
            or (ready and event.type != "stream.ready")
            or (not ready and event.type == "stream.ready")
            or (not ready and event.sample_rate_hz != self.sample_rate_hz)
        )
        if invalid:
            raise self._error(tts_stream_error("provider_schema_invalid", "TTS stream identity, order or PCM format changed."))
        if event.type == "audio.chunk":
            if not event.pcm_s16le or len(event.pcm_s16le) % 2 or event.total_audio_bytes is not None or event.usage is not None:
                raise self._error(tts_stream_error("provider_schema_invalid", "TTS PCM chunk is empty, unaligned or contains terminal fields."))
            if len(event.pcm_s16le) > self._max_chunk_bytes:
                raise self._error(tts_stream_error("provider_audio_chunk_too_large", "TTS PCM chunk exceeds the limit.", retryable=False))
            next_bytes = self.byte_count + len(event.pcm_s16le)
            if next_bytes > self._max_total_bytes or next_bytes > self.sample_rate_hz * 2 * self._max_audio_seconds:
                raise self._error(tts_stream_error("provider_audio_stream_too_large", "TTS PCM stream exceeds the byte or audio-duration limit.", retryable=False))
            self.byte_count = next_bytes
        elif event.pcm_s16le:
            raise self._error(tts_stream_error("provider_schema_invalid", "TTS non-chunk event contains PCM."))
        elif event.type == "audio.final":
            if self.byte_count == 0 or event.total_audio_bytes != self.byte_count:
                raise self._error(tts_stream_error("provider_schema_invalid", "TTS final byte count is empty or inconsistent."))
            if event.usage and any(value < 0 for value in event.usage.model_dump().values()):
                raise self._error(tts_stream_error("provider_schema_invalid", "TTS usage counts cannot be negative."))
        elif event.total_audio_bytes is not None or event.usage is not None:
            raise self._error(tts_stream_error("provider_schema_invalid", "TTS ready event contains terminal fields."))
        self._sequence = event.sequence
        self._request_id = event.provider.request_id or self._request_id
        return event

    async def _next(self, iterator: AsyncIterator[TTSStreamEvent]) -> TTSStreamEvent:
        remaining = self._deadline - monotonic()
        if remaining <= 0:
            raise tts_stream_error("provider_timeout", "TTS stream exceeded the wall-clock limit.")
        task = asyncio.ensure_future(iterator.__anext__())
        self._read_task = task
        try:
            return await asyncio.wait_for(task, timeout=min(self._read_timeout_s, remaining))
        except asyncio.TimeoutError as exc:
            raise tts_stream_error("provider_timeout", "TTS stream exceeded the read or wall-clock timeout.") from exc
        finally:
            if self._read_task is task:
                self._read_task = None

    def _notify(self, status: str, event: Optional[TTSStreamEvent] = None, error: Optional[ProviderError] = None) -> None:
        if not self._notified:
            self._notified = True
            if self._on_terminal:
                try:
                    self._on_terminal(status, event, error)
                except ProviderError:
                    # A terminal policy check (for example cost) can reject a
                    # nominal final; its failure must still receive an audit.
                    self._notified = False
                    raise

    async def events(self) -> AsyncIterator[TTSStreamEvent]:
        if self._closed or self._consumed:
            raise self._error(tts_stream_error("provider_stream_closed", "TTS stream permits only one consumer.", retryable=False))
        self._consumed = True
        try:
            iterator = self._stream.events().__aiter__()
            while True:
                try:
                    raw = await self._next(iterator)
                except StopAsyncIteration as exc:
                    raise tts_stream_error("provider_audio_final_missing", "TTS stream closed without a complete final event.") from exc
                if self._closed:
                    raise tts_stream_error("provider_stream_closed", "TTS stream was cancelled.", retryable=False)
                event = self._validate(raw)
                if event.type == "audio.final":
                    # Do not expose final success before rejecting duplicate finals
                    # or trailing PCM. Providers must end the stream at this point.
                    try:
                        await self._next(iterator)
                    except StopAsyncIteration:
                        self._notify("success", event)
                        yield event
                        return
                    raise tts_stream_error("provider_schema_invalid", "TTS stream contains data after its final event.")
                self.audio_started = True
                yield event
        except asyncio.CancelledError:
            try:
                self._notify("cancelled")
            except Exception:
                pass
            raise
        except ProviderError as exc:
            error = self._error(exc)
            try:
                self._notify("failed", error=error)
            except Exception:
                pass
            raise error
        except Exception as exc:
            error = self._error(tts_stream_error("provider_stream_failed", "TTS stream transport failed."))
            try:
                self._notify("failed", error=error)
            except Exception:
                pass
            raise error from exc
        finally:
            await self.abort()

    def __aiter__(self) -> AsyncIterator[TTSStreamEvent]:
        return self.events()

    async def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._read_task is not None:
            self._read_task.cancel()
        try:
            self._notify("cancelled")
        except Exception:
            # Observability cannot prevent releasing a cancelled transport.
            pass
        try:
            await asyncio.wait_for(self._stream.abort(), timeout=min(5.0, self._read_timeout_s))
        except Exception:
            # A cleanup failure cannot turn cancellation into another response.
            pass
