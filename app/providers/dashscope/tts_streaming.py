"""Qwen3-TTS HTTP SSE adapter; vendor URLs never cross the PCM seam.

The documented HTTP streaming payload is base64 PCM, signed 16-bit little
endian, 24 kHz, mono. The terminal payload has finish_reason=stop and an
optional downloadable URL; that URL is neither fetched nor returned here.
"""

import base64
import asyncio
import binascii
import json
import logging
import struct
from contextlib import AsyncExitStack
from typing import Any, AsyncIterator, Dict

import httpx

from app.core.ids import new_id
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ProviderContext, ProviderMeta, Usage
from app.model_gateway.tts_streaming import TTSStreamEvent
from app.providers.dashscope.tts_diagnostics import (
    TransportTrace, annotate_cancellation, bounded_error_payload, response_error, vendor_fields,
)
from app.providers.dashscope.tts_http_pool import tts_http_client


_CLOSE_TIMEOUT = 1.0
_LOG = logging.getLogger(__name__)


# Observed Qwen HTTP SSE WAVE prefix. Only this exact PCM16/24k/mono
# streaming container is removed; arbitrary RIFF headers fail closed.
_STREAM_WAVE_HEADER = struct.pack(
    "<4sI4s4sIHHIIHH4sI", b"RIFF", 0x7FFFFFBF, b"WAVE", b"fmt ",
    16, 1, 1, 24000, 48000, 2, 16, b"data", 0x7FFFFF9B,
)


def _usage(payload: Dict[str, Any]) -> Usage:
    raw = payload.get("usage") or {}
    if not isinstance(raw, dict):
        raise _invalid("DashScope TTS usage is invalid.")
    counts = {key: raw.get(key, 0) for key in ("input_tokens", "output_tokens", "total_tokens")}
    if any(type(value) is not int or value < 0 for value in [*counts.values(), raw.get("characters", 0)]):
        raise _invalid("DashScope TTS usage counts are invalid.")
    counts["total_tokens"] = max(counts["total_tokens"], counts["input_tokens"] + counts["output_tokens"])
    return Usage(**counts)


def _invalid(message: str) -> ProviderError:
    return ProviderError("provider_schema_invalid", message, retryable=True)


async def _sse_data(response: httpx.Response, *, max_event_bytes: int = 524288) -> AsyncIterator[str]:
    """Bound lines AND complete events before decoding JSON; ignore SSE comments."""
    pending = bytearray()
    data = []
    event_bytes = 0

    def line(raw: bytes):
        nonlocal event_bytes, data
        event_bytes += len(raw) + 1
        if event_bytes > max_event_bytes:
            raise _invalid("DashScope TTS SSE event exceeds the limit.")
        raw = raw.removesuffix(b"\r")
        if not raw:
            value = b"\n".join(data) if data else None
            data = []
            event_bytes = 0
            return value
        if raw.startswith(b"data:"):
            value = raw[5:]
            data.append(value[1:] if value.startswith(b" ") else value)
        return None

    async for packet in response.aiter_bytes():
        # Do not allow an arbitrarily long line or an allocation proportional to
        # an untrusted packet. Iterating bounded slices also handles coalesced SSE.
        for offset in range(0, len(packet), 16384):
            pending.extend(packet[offset:offset + 16384])
            while b"\n" in pending:
                raw, _, remaining = pending.partition(b"\n")
                pending = bytearray(remaining)
                value = line(bytes(raw))
                if value is not None:
                    try:
                        yield value.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise _invalid("DashScope TTS SSE data is not UTF-8.") from exc
            if len(pending) + event_bytes > max_event_bytes:
                raise _invalid("DashScope TTS SSE line exceeds the limit.")
    # SSE data without its terminating blank line is truncated, not a final.
    if pending or data:
        raise _invalid("DashScope TTS SSE event was truncated.")


class DashScopeTTSStream:
    def __init__(self, context: ProviderContext) -> None:
        self.stream_id = new_id("tts_stream")
        self._context = context
        self._stack = AsyncExitStack()
        self._closed = False
        self._consumed = False
        self._sequence = 1
        self._request_id = ""
        self._bytes = 0
        self._trace = TransportTrace()
        self._close_task = None
        self._terminal_error = None
        self.ready_event = self._event("stream.ready")

    @property
    def terminal_error(self):
        return self._terminal_error

    @property
    def cleanup_confirmed(self) -> bool:
        task = self._close_task
        return (task is not None and task.done() and not task.cancelled()
                and task.exception() is None)

    @classmethod
    async def open(cls, client_factory, context: ProviderContext, *, endpoint: str, api_key: str, payload: Dict[str, Any]):
        caller_task = asyncio.current_task()
        previous = getattr(caller_task, "_dashscope_tts_open_error", None)
        if previous is not None:
            if getattr(caller_task, "provider_error", None) is previous:
                del caller_task.provider_error
            del caller_task._dashscope_tts_open_error
        self = cls(context)
        try:
            client = await self._stack.enter_async_context(tts_http_client(client_factory, context, endpoint))
            self._response = await self._stack.enter_async_context(client.stream(
                "POST", endpoint,
                headers={"Authorization": "Bearer %s" % api_key, "Content-Type": "application/json", "X-DashScope-SSE": "enable", "Accept-Encoding": "identity"},
                json=payload,
                timeout=context.timeout_s,
                extensions={"trace": self._trace},
            ))
            if self._response.status_code != 200:
                fields = {}
                self._trace.enter("response_body")
                body_outcome = "http_error"
                try:
                    fields = await bounded_error_payload(self._response)
                except (httpx.TimeoutException, asyncio.TimeoutError):
                    body_outcome = "timeout"
                except httpx.HTTPError:
                    # A broken error body cannot hide the already known status.
                    body_outcome = "network_error"
                failure = response_error(self._response.status_code, fields)
                failure.details.update(self._trace.snapshot(body_outcome))
                raise failure
            if self._response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                raise ProviderError("provider_streaming_not_supported", "DashScope did not return the requested SSE transport.", retryable=False)
            return self
        except BaseException as exc:
            outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "timeout" if isinstance(exc, httpx.TimeoutException) else "network_error" if isinstance(exc, httpx.HTTPError) else "http_error"
            diagnostics = self._trace.snapshot(outcome)
            status = getattr(getattr(self, "_response", None), "status_code", None)
            if type(status) is int and 100 <= status <= 599:
                diagnostics["http_status"] = status
            if isinstance(exc, ProviderError):
                exc.details = {**diagnostics, **exc.details}
                self._terminal_error = exc
                # The enclosing deadline may leave zero cleanup wait budget.
                # Expose the known rejection before the first cleanup await.
                caller_task.provider_error = exc
                caller_task._dashscope_tts_open_error = exc
            elif isinstance(exc, asyncio.CancelledError):
                # Headers already prove a rejection even when its body stalls.
                # Retain the adapter's standard classification for a gateway
                # deadline without replacing the caller's cancellation.
                failure = response_error(status, {}) if "http_status" in diagnostics and status != 200 else None
                if failure is not None:
                    failure.details.update(diagnostics)
                annotate_cancellation(exc, diagnostics, provider_error=failure)
            try:
                await self.abort()
            except asyncio.CancelledError as cancelled:
                # A known HTTP rejection can be waiting for transport cleanup
                # when the caller's deadline arrives. Keep its standard cause
                # on the cancellation; the unopened stream cannot be observed.
                if isinstance(exc, ProviderError):
                    annotate_cancellation(cancelled, diagnostics, provider_error=exc)
                raise
            if isinstance(exc, httpx.TimeoutException):
                raise ProviderError("provider_timeout", "DashScope TTS stream open timed out.", retryable=True, details=diagnostics) from exc
            if isinstance(exc, httpx.HTTPError):
                raise ProviderError("provider_network_error", "DashScope TTS stream could not connect.", retryable=True, details=diagnostics) from exc
            raise

    def _event(self, kind: str, **kwargs) -> TTSStreamEvent:
        return TTSStreamEvent(
            stream_id=self.stream_id, sequence=self._sequence, type=kind,
            sample_rate_hz=24000,
            provider=ProviderMeta(provider_id="dashscope", model=self._context.model, request_id=self._request_id, latency_ms=0),
            **kwargs,
        )

    async def events(self) -> AsyncIterator[TTSStreamEvent]:
        if self._closed or self._consumed:
            raise ProviderError("provider_stream_closed", "DashScope TTS stream is closed.", retryable=False)
        self._consumed = True
        final = None
        done_received = False
        metadata_received = False
        try:
            self._trace.enter("response_body")
            async for data in _sse_data(self._response):
                if self._closed:
                    raise ProviderError("provider_stream_closed", "DashScope TTS stream was cancelled.", retryable=False)
                if data == "[DONE]":
                    if final is None or done_received:
                        raise _invalid("DashScope TTS end marker is premature or duplicated.")
                    done_received = True
                    continue
                if final is not None:
                    raise _invalid("DashScope TTS emitted data after the terminal payload.")
                try:
                    payload = json.loads(data)
                except (ValueError, TypeError) as exc:
                    raise _invalid("DashScope TTS SSE payload is not JSON.") from exc
                if not isinstance(payload, dict):
                    raise _invalid("DashScope TTS SSE payload must be an object.")
                status = payload.get("status_code", 200)
                if type(status) is not int:
                    raise _invalid("DashScope TTS status is invalid.")
                if status != 200 or payload.get("code"):
                    raise response_error(status, vendor_fields(payload), stream=True)
                request_id = payload.get("request_id")
                if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
                    raise _invalid("DashScope TTS request identity is missing.")
                if self._request_id and request_id != self._request_id:
                    raise _invalid("DashScope TTS request identity changed.")
                self._request_id = request_id
                output = payload.get("output")
                audio = output.get("audio") if isinstance(output, dict) else None
                if not isinstance(audio, dict) or not isinstance(audio.get("data"), str):
                    raise _invalid("DashScope TTS audio payload is missing.")
                encoded = audio["data"]
                reason = output.get("finish_reason")
                if reason == "stop":
                    if encoded or self._bytes == 0:
                        raise _invalid("DashScope TTS terminal payload is empty or contains unexpected PCM.")
                    self._sequence += 1
                    final = self._event("audio.final", total_audio_bytes=self._bytes, usage=_usage(payload))
                    continue
                # Qwen HTTP SSE uses the literal string "null" in current
                # intermediate chunks; older responses use JSON null.
                # Neither is terminal, and both still require valid PCM.
                if reason not in (None, "null"):
                    raise _invalid("DashScope TTS intermediate audio is empty or has an unsupported finish reason.")
                if not encoded:
                    # Current Qwen sends one usage-only event before stop.
                    # This never authorizes a final, nor changes PCM sequence.
                    if self._bytes == 0 or metadata_received or not isinstance(payload.get("usage"), dict):
                        raise _invalid("DashScope TTS intermediate audio is empty.")
                    _usage(payload)
                    metadata_received = True
                    continue
                if metadata_received:
                    raise _invalid("DashScope TTS emitted PCM after its usage summary.")
                try:
                    pcm = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise _invalid("DashScope TTS PCM is not valid base64.") from exc
                if self._bytes == 0 and pcm.startswith(b"RIFF"):
                    if not pcm.startswith(_STREAM_WAVE_HEADER):
                        raise _invalid("DashScope TTS streaming WAVE format is unsupported.")
                    pcm = pcm[len(_STREAM_WAVE_HEADER):]
                if not pcm or len(pcm) % 2 or len(pcm) > 262144:
                    raise _invalid("DashScope TTS PCM chunk is empty, unaligned or too large.")
                self._bytes += len(pcm)
                self._sequence += 1
                yield self._event("audio.chunk", pcm_s16le=pcm)
            if final is None:
                raise ProviderError("provider_audio_final_missing", "DashScope TTS disconnected before its final payload.", retryable=True)
            yield final
        except httpx.TimeoutException as exc:
            raise ProviderError("provider_timeout", "DashScope TTS stream read timed out.", retryable=True,
                                details=self._trace.snapshot("timeout")) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("provider_network_error", "DashScope TTS stream disconnected.", retryable=True,
                                details=self._trace.snapshot("network_error")) from exc
        except ProviderError as exc:
            exc.details.update(self._trace.snapshot("stream_error"))
            self._terminal_error = exc
            raise
        except asyncio.CancelledError as exc:
            annotate_cancellation(exc, self._trace.snapshot("cancelled"))
            raise
        finally:
            await self.abort()

    async def abort(self) -> None:
        if not self._closed:
            self._closed = True
            self._close_task = asyncio.create_task(self._stack.aclose())
            task = self._close_task

            def consume(done):
                if not done.cancelled():
                    done.exception()

            try:
                done, _ = await asyncio.wait({task}, timeout=_CLOSE_TIMEOUT)
                if done:
                    task.result()
                else:
                    task.cancel()
                    task.add_done_callback(consume)
                    _LOG.warning("DashScope TTS cleanup exceeded its time limit.")
            except asyncio.CancelledError:
                task.cancel()
                task.add_done_callback(consume)
                raise
            except Exception as exc:
                _LOG.warning("DashScope TTS cleanup failed: error_type=%s", type(exc).__name__)
