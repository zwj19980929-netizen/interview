import asyncio
import base64
import json
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.core.ids import new_id
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    ProviderContext,
    ProviderMeta,
    RealtimeSpeechDialogueEvent,
    RealtimeSpeechDialogueRequest,
    RealtimeSpeechResponseCommand,
)


class OpenAIStyleRealtimeSpeechStream:
    """Shared WebSocket protocol engine for OpenAI and Qwen Realtime APIs."""

    def __init__(
        self,
        *,
        provider_id: str,
        request: RealtimeSpeechDialogueRequest,
        context: ProviderContext,
        socket: Any,
        vendor_input_rate: int,
        vendor_output_rate: int,
        response_payload: Dict[str, Any],
    ) -> None:
        self.provider_id = provider_id
        self.request = request
        self.context = context
        self.socket = socket
        self.vendor_input_rate = vendor_input_rate
        self.vendor_output_rate = vendor_output_rate
        self.response_payload = response_payload
        self.stream_id = new_id("dialogue_stream")
        self.sequence = 1
        self.closed = False
        self.request_id = ""
        self.ready_events = [self._event("dialogue.ready")]

    @classmethod
    async def open(
        cls,
        *,
        provider_id: str,
        connector: Any,
        url: str,
        headers: Dict[str, str],
        request: RealtimeSpeechDialogueRequest,
        context: ProviderContext,
        session: Dict[str, Any],
        vendor_input_rate: int,
        vendor_output_rate: int,
        response_payload: Dict[str, Any],
    ) -> "OpenAIStyleRealtimeSpeechStream":
        try:
            socket = await connector(
                url,
                additional_headers=headers,
                open_timeout=max(1, float(context.timeout_s)),
                max_size=int(context.config.get("websocket_max_message_bytes", 8 * 1024 * 1024)),
            )
        except TypeError:
            socket = await connector(url, extra_headers=headers)
        except asyncio.TimeoutError as exc:
            raise ProviderError(
                "provider_stream_open_failed",
                "%s realtime WebSocket timed out." % provider_id,
                retryable=True,
            ) from exc
        except Exception as exc:
            raise ProviderError(
                "provider_stream_open_failed",
                "%s realtime WebSocket could not be opened." % provider_id,
                retryable=True,
            ) from exc
        stream = cls(
            provider_id=provider_id,
            request=request,
            context=context,
            socket=socket,
            vendor_input_rate=vendor_input_rate,
            vendor_output_rate=vendor_output_rate,
            response_payload=response_payload,
        )
        try:
            await stream._wait_for({"session.created"})
            await stream._send(
                {"type": "session.update", "session": session},
                ensure_ascii=False,
            )
            await stream._wait_for({"session.updated"})
        except Exception:
            await stream.abort()
            raise
        return stream

    async def send_audio(self, chunk: bytes) -> List[RealtimeSpeechDialogueEvent]:
        if self.closed:
            raise ProviderError(
                "provider_stream_closed", "Realtime speech stream is closed.", retryable=False
            )
        audio = _resample_pcm16(
            chunk,
            self.request.input_audio.sample_rate_hz,
            self.vendor_input_rate,
        )
        await self._send(
            {
                "event_id": new_id("vendor_event"),
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(audio).decode("ascii"),
            }
        )
        return await self._drain(0.001)

    async def commit(
        self,
        command: RealtimeSpeechResponseCommand,
        on_event: Optional[Callable[[RealtimeSpeechDialogueEvent], Awaitable[None]]] = None,
    ) -> List[RealtimeSpeechDialogueEvent]:
        if self.closed:
            return []
        await self._send(
            {"event_id": new_id("vendor_event"), "type": "input_audio_buffer.commit"}
        )
        response = {
            **self.response_payload,
            "instructions": command.response_instructions,
        }
        await self._send(
            {
                "event_id": new_id("vendor_event"),
                "type": "response.create",
                "response": response,
            },
            ensure_ascii=False,
        )
        deadline = asyncio.get_running_loop().time() + max(1, float(self.context.timeout_s))
        events: List[RealtimeSpeechDialogueEvent] = []
        while asyncio.get_running_loop().time() < deadline:
            try:
                payload = await self._receive(
                    max(0.01, deadline - asyncio.get_running_loop().time())
                )
            except asyncio.TimeoutError as exc:
                await self.abort()
                raise ProviderError(
                    "provider_timeout", "Realtime speech response timed out.", retryable=True
                ) from exc
            except ProviderError:
                await self.abort()
                raise
            except Exception as exc:
                await self.abort()
                raise ProviderError(
                    "provider_stream_interrupted",
                    "Realtime speech stream was interrupted.",
                    retryable=False,
                ) from exc
            projected = self._project(payload)
            if on_event:
                for event in projected:
                    await on_event(event)
            else:
                events.extend(projected)
            if payload.get("type") == "response.done":
                break
        else:
            await self.abort()
            raise ProviderError(
                "provider_timeout", "Realtime speech response timed out.", retryable=True
            )
        self.closed = True
        await self.socket.close()
        closed = self._event("dialogue.closed")
        if on_event:
            await on_event(closed)
        else:
            events.append(closed)
        return events

    async def interrupt(self) -> List[RealtimeSpeechDialogueEvent]:
        if self.closed:
            return []
        await self._send(
            {"event_id": new_id("vendor_event"), "type": "response.cancel"}
        )
        return [self._event("output.interrupted")]

    async def abort(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                await self.socket.close()
            except Exception:
                return

    async def _wait_for(self, accepted: set[str]) -> Dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + max(1, float(self.context.timeout_s))
        while asyncio.get_running_loop().time() < deadline:
            try:
                payload = await self._receive(
                    max(0.01, deadline - asyncio.get_running_loop().time())
                )
            except asyncio.TimeoutError as exc:
                raise ProviderError(
                    "provider_timeout",
                    "Realtime session acknowledgement timed out.",
                    retryable=True,
                ) from exc
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError(
                    "provider_stream_interrupted",
                    "Realtime session was interrupted before acknowledgement.",
                    retryable=True,
                ) from exc
            if payload.get("type") in accepted:
                return payload
        raise ProviderError(
            "provider_timeout", "Realtime session acknowledgement timed out.", retryable=True
        )

    async def _drain(self, timeout: float) -> List[RealtimeSpeechDialogueEvent]:
        events: List[RealtimeSpeechDialogueEvent] = []
        while True:
            try:
                payload = await self._receive(timeout)
            except asyncio.TimeoutError:
                break
            events.extend(self._project(payload))
            timeout = 0.001
        return events

    async def _receive(self, timeout: float) -> Dict[str, Any]:
        raw = await asyncio.wait_for(self.socket.recv(), timeout=timeout)
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProviderError(
                "provider_schema_invalid",
                "Realtime provider returned invalid JSON.",
                retryable=True,
            ) from exc
        self.request_id = str(
            payload.get("event_id") or payload.get("response_id") or self.request_id
        )
        if payload.get("type") == "error":
            error = payload.get("error") or {}
            raise ProviderError(
                "provider_server_error",
                "Realtime provider error: %s" % str(error.get("code") or "unknown"),
                retryable=str(error.get("type") or "") not in {"invalid_request_error"},
            )
        return payload

    async def _send(self, payload: Dict[str, Any], *, ensure_ascii: bool = True) -> None:
        try:
            await self.socket.send(json.dumps(payload, ensure_ascii=ensure_ascii))
        except Exception as exc:
            raise ProviderError(
                "provider_stream_interrupted",
                "Realtime speech stream could not send a provider event.",
                retryable=False,
            ) from exc

    def _project(self, payload: Dict[str, Any]) -> List[RealtimeSpeechDialogueEvent]:
        event_type = str(payload.get("type") or "")
        if event_type == "input_audio_buffer.speech_started":
            return [self._event("input.speech.started")]
        if event_type == "input_audio_buffer.speech_stopped":
            return [self._event("input.speech.stopped")]
        if event_type in {
            "conversation.item.input_audio_transcription.delta",
            "conversation.item.input_audio_transcription.partial",
        }:
            return [
                self._event(
                    "input.transcript.partial",
                    text=str(payload.get("delta") or payload.get("text") or ""),
                )
            ]
        if event_type == "conversation.item.input_audio_transcription.completed":
            return [
                self._event(
                    "input.transcript.final",
                    text=str(payload.get("transcript") or payload.get("text") or ""),
                    is_final=True,
                )
            ]
        if event_type in {"response.output_audio_transcript.delta", "response.audio_transcript.delta"}:
            return [
                self._event(
                    "output.transcript.delta", text=str(payload.get("delta") or "")
                )
            ]
        if event_type in {"response.output_audio_transcript.done", "response.audio_transcript.done"}:
            return [
                self._event(
                    "output.transcript.final",
                    text=str(payload.get("transcript") or payload.get("text") or ""),
                    is_final=True,
                )
            ]
        if event_type in {"response.output_audio.delta", "response.audio.delta"}:
            return [
                self._event(
                    "output.audio.delta",
                    audio_base64=str(payload.get("delta") or ""),
                )
            ]
        if event_type in {"response.output_audio.done", "response.audio.done"}:
            return [self._event("output.audio.done")]
        return []

    def _event(self, event_type: str, **values: Any) -> RealtimeSpeechDialogueEvent:
        event = RealtimeSpeechDialogueEvent(
            stream_id=self.stream_id,
            sequence=self.sequence,
            type=event_type,
            audio_content_type="audio/pcm",
            sample_rate_hz=self.vendor_output_rate,
            provider=ProviderMeta(
                provider_id=self.provider_id,
                model=self.context.model,
                request_id=self.request_id,
                latency_ms=0,
            ),
            **values,
        )
        self.sequence += 1
        return event


def _resample_pcm16(audio: bytes, source_rate: int, target_rate: int) -> bytes:
    if source_rate == target_rate or not audio:
        return audio
    sample_count = len(audio) // 2
    if sample_count <= 1:
        return audio
    samples = [
        int.from_bytes(audio[index : index + 2], "little", signed=True)
        for index in range(0, sample_count * 2, 2)
    ]
    output_count = max(1, int(sample_count * target_rate / source_rate))
    output = bytearray(output_count * 2)
    ratio = source_rate / target_rate
    for index in range(output_count):
        position = min(sample_count - 1, index * ratio)
        left = int(position)
        right = min(sample_count - 1, left + 1)
        fraction = position - left
        value = int(samples[left] * (1 - fraction) + samples[right] * fraction)
        output[index * 2 : index * 2 + 2] = value.to_bytes(2, "little", signed=True)
    return bytes(output)
