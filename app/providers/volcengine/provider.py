from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
from io import BytesIO
import json
import uuid
import wave
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
import websockets

from app.core.ids import new_id
from app.core.prompt.contracts import prompt_contract
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    BatchSTTRequest,
    BatchSTTResponse,
    ChatTextRequest,
    ProviderContext,
    ProviderMeta,
    RealtimeSpeechDialogueEvent,
    RealtimeSpeechDialogueRequest,
    RealtimeSpeechResponseCommand,
    StreamingSTTEvent,
    StreamingSTTRequest,
    TTSSynthesizeRequest,
    TTSSynthesizeResponse,
    TranscriptSegment,
)
from app.providers.openai_compatible.provider import OpenAICompatibleProvider
from app.providers.realtime_speech import _resample_pcm16
from app.providers.volcengine.protocol import (
    VolcengineSpeechFrame,
    decode_server_frame,
    encode_audio_only_request,
    encode_full_client_request,
)


class VolcengineProvider(OpenAICompatibleProvider):
    """Doubao/Ark adapter behind the existing provider-neutral gateway seam."""

    provider_id = "volcengine"
    structured_output_mode = "json_schema"

    def __init__(self, client_factory: Any = None, websocket_connect: Any = None) -> None:
        super().__init__(client_factory=client_factory) if client_factory is not None else super().__init__()
        self.websocket_connect = websocket_connect or websockets.connect

    async def validate_credentials(
        self, config: Dict[str, Any], credentials: Dict[str, Any], *, timeout_s: int = 10
    ) -> Dict[str, str]:
        # Ark has no portable list-models contract.  A one-token centralized
        # probe validates both authentication and the configured endpoint/model.
        _speech_api_key(credentials)
        contract = prompt_contract("provider_credential_probe", {})
        model = str(config.get("test_model") or "doubao-seed-2-1-lite-260628")
        await super().chat_text(
            ChatTextRequest(
                purpose="provider_credential_validation",
                messages=contract.messages,
                temperature=0,
                max_output_tokens=1,
            ),
            config=config,
            credentials={"api_key": _ark_api_key(credentials)},
            model=model,
            timeout_s=timeout_s,
        )
        return {
            "status": "valid",
            "message": (
                "Ark API Key authenticated successfully; the Doubao Speech API Key is configured "
                "and will be exercised by each speech model probe."
            ),
        }

    async def invoke(self, capability: str, request: Any, context: ProviderContext) -> Any:
        if capability == cap.STT_BATCH and isinstance(request, BatchSTTRequest):
            return await self._transcribe_batch(request, context)
        normalized = context.model_copy(
            update={
                "credentials": {
                    **context.credentials,
                    "api_key": _ark_api_key(context.credentials),
                }
            }
        )
        return await super().invoke(capability, request, normalized)

    async def open_stream(self, request: StreamingSTTRequest, context: ProviderContext) -> Any:
        if context.capability != cap.STT_STREAMING:
            raise ProviderError(
                "provider_capability_missing",
                "Volcengine streaming ASR capability is invalid.",
                retryable=False,
            )
        return await VolcengineSTTStream.open(self, request, context)

    async def open_dialogue(
        self,
        request: RealtimeSpeechDialogueRequest,
        context: ProviderContext,
    ) -> "VolcengineRealtimeSpeechStream":
        if context.capability != cap.SPEECH_DIALOGUE_REALTIME:
            raise ProviderError(
                "provider_capability_missing",
                "Volcengine realtime speech capability is invalid.",
                retryable=False,
            )
        return await VolcengineRealtimeSpeechStream.open(self, request, context)

    async def synthesize_speech(
        self,
        request: TTSSynthesizeRequest,
        *,
        config: Dict[str, Any],
        credentials: Dict[str, Any],
        model: str,
        timeout_s: int,
    ) -> TTSSynthesizeResponse:
        audio_format, content_type = _tts_format(request.format)
        sample_rate = 48_000 if audio_format == "ogg_opus" else int(config.get("sample_rate_hz", 24_000))
        voice_map = config.get("voice_map") or {}
        voice = voice_map.get(request.voice_profile_id)
        if not voice and request.voice_profile_id.startswith("voice_default"):
            voice = config.get("default_voice")
        voice = str(voice or request.voice_profile_id).strip()
        if not voice:
            raise ProviderError("provider_bad_request", "Volcengine TTS voice is required.", retryable=False)

        request_id = str(uuid.uuid4())
        payload = {
            "req_params": {
                "text": request.text,
                "speaker": voice,
                "audio_params": {
                    "format": audio_format,
                    "sample_rate": sample_rate,
                    "speech_rate": round((request.speaking_rate - 1.0) * 100),
                    "enable_subtitle": bool(config.get("enable_subtitle", True)),
                },
            }
        }
        headers = {
            "X-Api-Key": _speech_api_key(credentials),
            "X-Api-Resource-Id": str(config.get("tts_resource_id") or _tts_resource(model)),
            "X-Api-Request-Id": request_id,
            "X-Control-Require-Usage-Tokens-Return": "*",
            "Content-Type": "application/json",
        }
        response = await self._speech_post(
            _secure_http_endpoint(
                config.get("tts_endpoint"),
                "https://openspeech.bytedance.com/api/v3/tts/unidirectional",
                name="Volcengine TTS endpoint",
            ),
            payload,
            headers=headers,
            timeout_s=timeout_s,
            use_environment_proxy=bool(config.get("use_environment_proxy", False)),
        )
        messages = _decode_json_messages(response.content)
        audio_parts: List[bytes] = []
        words: List[Dict[str, Any]] = []
        for message in messages:
            code = message.get("code")
            if code not in {None, 0, 20_000_000}:
                raise ProviderError(
                    "provider_server_error",
                    "Volcengine TTS error %s." % code,
                    retryable=int(code) >= 55_000_000 if isinstance(code, int) else True,
                    details={"vendor_error_code": code},
                )
            encoded = message.get("data")
            if encoded:
                try:
                    audio_parts.append(base64.b64decode(str(encoded), validate=True))
                except (ValueError, binascii.Error) as exc:
                    raise ProviderError(
                        "provider_schema_invalid",
                        "Volcengine TTS returned invalid base64 audio.",
                        retryable=True,
                    ) from exc
            sentence = message.get("sentence") or {}
            if isinstance(sentence, dict) and isinstance(sentence.get("words"), list):
                words.extend(item for item in sentence["words"] if isinstance(item, dict))
        audio = b"".join(audio_parts)
        if not audio:
            raise ProviderError(
                "provider_schema_invalid",
                "Volcengine TTS returned no audio data.",
                retryable=True,
            )
        digest = hashlib.sha256(audio).hexdigest()
        request_id = str(response.headers.get("x-tt-logid") or request_id)
        return TTSSynthesizeResponse(
            audio_uri="data:%s;base64,%s" % (content_type, base64.b64encode(audio).decode("ascii")),
            content_type=content_type,
            duration_ms=_audio_duration_ms(
                audio,
                audio_format=audio_format,
                sample_rate=sample_rate,
                words=words,
                text=request.text,
                speaking_rate=request.speaking_rate,
            ),
            content_hash="sha256:%s" % digest,
            provider=ProviderMeta(
                provider_id=self.provider_id,
                model=model,
                request_id=request_id,
                latency_ms=0,
            ),
        )

    async def _transcribe_batch(
        self, request: BatchSTTRequest, context: ProviderContext
    ) -> BatchSTTResponse:
        if not request.audio_bytes:
            raise ProviderError(
                "provider_audio_unavailable",
                "Volcengine batch ASR requires server-resolved private audio bytes.",
                retryable=False,
            )
        max_bytes = int(context.config.get("batch_max_bytes", 20 * 1024 * 1024))
        if len(request.audio_bytes) > max_bytes:
            raise ProviderError(
                "provider_audio_stream_too_large",
                "Volcengine batch ASR audio exceeds the configured limit.",
                retryable=False,
            )
        request_id = str(uuid.uuid4())
        payload = {
            "user": {"uid": "interviewer-server"},
            "audio": {"data": base64.b64encode(request.audio_bytes).decode("ascii")},
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "show_utterances": True,
            },
        }
        headers = {
            "X-Api-Key": _speech_api_key(context.credentials),
            "X-Api-Resource-Id": str(
                context.config.get("asr_batch_resource_id") or "volc.bigasr.auc_turbo"
            ),
            "X-Api-Request-Id": request_id,
            "X-Api-Sequence": "-1",
            "Content-Type": "application/json",
        }
        response = await self._speech_post(
            _secure_http_endpoint(
                context.config.get("asr_batch_endpoint"),
                "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
                name="Volcengine batch ASR endpoint",
            ),
            payload,
            headers=headers,
            timeout_s=max(1, int(context.timeout_s)),
            use_environment_proxy=bool(context.config.get("use_environment_proxy", False)),
        )
        status_code = str(response.headers.get("x-api-status-code") or "20000000")
        if status_code != "20000000":
            raise ProviderError(
                "provider_server_error",
                "Volcengine batch ASR error %s." % status_code,
                retryable=status_code.startswith("55"),
                details={"vendor_error_code": status_code},
            )
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(
                "provider_schema_invalid",
                "Volcengine batch ASR response was not valid JSON.",
                retryable=True,
            ) from exc
        result = body.get("result") or {}
        text = str(result.get("text") or "").strip()
        if not text:
            raise ProviderError(
                "provider_final_transcript_missing",
                "Volcengine batch ASR returned no transcript.",
                retryable=True,
            )
        segments = _transcript_segments(result.get("utterances"))
        return BatchSTTResponse(
            text=text,
            language=request.language,
            confidence=_segment_confidence(segments),
            segments=segments,
            source="server_batch_repair" if request.purpose == "candidate_answer_repair" else "server_batch",
            provider=ProviderMeta(
                provider_id=self.provider_id,
                model=context.model,
                request_id=str(response.headers.get("x-tt-logid") or request_id),
                latency_ms=0,
            ),
        )

    async def _speech_post(
        self,
        url: str,
        payload: Dict[str, Any],
        *,
        headers: Dict[str, str],
        timeout_s: int,
        use_environment_proxy: bool,
    ) -> httpx.Response:
        try:
            async with self.client_factory(timeout=timeout_s, trust_env=use_environment_proxy) as client:
                response = await client.post(url, json=payload, headers=headers)
        except ImportError as exc:
            raise ProviderError(
                "provider_transport_unavailable",
                "Volcengine HTTP transport or configured proxy dependency is unavailable.",
                retryable=False,
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderError("provider_timeout", "Volcengine request timed out.", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("provider_server_error", "Volcengine request failed.", retryable=True) from exc
        if response.status_code in {401, 403}:
            raise ProviderError("provider_auth_failed", "Volcengine credentials were rejected.", retryable=False)
        if response.status_code == 429:
            raise ProviderError("provider_rate_limited", "Volcengine rate limited the request.", retryable=True)
        if response.status_code >= 500:
            raise ProviderError("provider_server_error", "Volcengine returned a server error.", retryable=True)
        if response.status_code >= 400:
            raise ProviderError("provider_bad_request", "Volcengine rejected the request.", retryable=False)
        return response


class VolcengineSTTStream:
    """Seed ASR binary WebSocket adapter with exactly one authoritative final."""

    def __init__(
        self,
        provider: VolcengineProvider,
        request: StreamingSTTRequest,
        context: ProviderContext,
        socket: Any,
        request_id: str,
    ) -> None:
        self.provider = provider
        self.request = request
        self.context = context
        self.socket = socket
        self.request_id = request_id
        self.stream_id = new_id("stt_stream")
        self.sequence = 1
        self.closed = False
        self.latest_text = ""
        self.latest_segments: List[TranscriptSegment] = []
        self.ready_events = [self._event("stream.ready")]

    @classmethod
    async def open(
        cls,
        provider: VolcengineProvider,
        request: StreamingSTTRequest,
        context: ProviderContext,
    ) -> "VolcengineSTTStream":
        audio_format, codec = _asr_stream_format(request.audio.content_type)
        if request.audio.sample_rate_hz != 16_000:
            raise ProviderError(
                "provider_audio_format_unsupported",
                "Volcengine streaming ASR requires 16 kHz audio.",
                retryable=False,
            )
        request_id = str(uuid.uuid4())
        headers = {
            "X-Api-Key": _speech_api_key(context.credentials),
            "X-Api-Resource-Id": str(
                context.config.get("asr_resource_id") or "volc.seedasr.sauc.duration"
            ),
            "X-Api-Request-Id": request_id,
            "X-Api-Connect-Id": request_id,
        }
        endpoint = _secure_ws_endpoint(
            context.config.get("asr_websocket_url"),
            "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
            name="Volcengine streaming ASR endpoint",
        )
        socket = await _open_socket(
            provider.websocket_connect,
            endpoint,
            headers,
            timeout_s=context.timeout_s,
            max_size=int(context.config.get("websocket_max_message_bytes", 8 * 1024 * 1024)),
            provider_label="Volcengine ASR",
        )
        stream = cls(provider, request, context, socket, request_id)
        request_payload: Dict[str, Any] = {
            "user": {"uid": "interviewer-server"},
            "audio": {
                "format": audio_format,
                "codec": codec,
                "rate": 16_000,
                "bits": 16,
                "channel": request.audio.channels,
            },
            "request": {
                "model_name": "bigmodel",
                "enable_nonstream": True,
                "enable_itn": True,
                "enable_punc": True,
                "show_utterances": True,
                "result_type": "full",
                "end_window_size": int(context.config.get("end_window_size_ms", 800)),
            },
        }
        hotwords_context = str(context.config.get("hotwords_context") or "").strip()
        if hotwords_context:
            request_payload["request"]["corpus"] = {"context": hotwords_context}
        try:
            await socket.send(encode_full_client_request(request_payload))
            # The protocol acknowledges the full client request before audio.
            await stream._receive_frame(max(1, float(context.timeout_s)))
        except Exception:
            await stream.abort()
            raise
        return stream

    async def send_audio(self, chunk: bytes) -> List[StreamingSTTEvent]:
        if self.closed:
            raise ProviderError("provider_stream_closed", "Volcengine ASR stream is closed.", retryable=False)
        if not chunk:
            return []
        try:
            await self.socket.send(encode_audio_only_request(chunk))
        except Exception as exc:
            raise ProviderError(
                "provider_stream_interrupted",
                "Volcengine ASR stream could not send audio.",
                retryable=False,
            ) from exc
        return await self._drain()

    async def finish(self) -> List[StreamingSTTEvent]:
        if self.closed:
            return []
        try:
            await self.socket.send(encode_audio_only_request(b"", last=True))
            deadline = asyncio.get_running_loop().time() + max(1, float(self.context.timeout_s))
            while asyncio.get_running_loop().time() < deadline:
                frame = await self._receive_frame(
                    max(0.01, deadline - asyncio.get_running_loop().time())
                )
                self._remember(frame)
                if frame.is_last:
                    break
            else:
                raise asyncio.TimeoutError
        except asyncio.TimeoutError as exc:
            await self.abort()
            raise ProviderError(
                "provider_timeout",
                "Volcengine ASR final transcript timed out.",
                retryable=True,
            ) from exc
        except ProviderError:
            await self.abort()
            raise
        except Exception as exc:
            await self.abort()
            raise ProviderError(
                "provider_stream_interrupted",
                "Volcengine ASR stream ended before final transcript.",
                retryable=False,
            ) from exc
        text = self.latest_text.strip()
        if not text:
            await self.abort()
            raise ProviderError(
                "provider_final_transcript_missing",
                "Volcengine ASR returned no final transcript.",
                retryable=True,
            )
        final = self._event(
            "transcript.final",
            text=text,
            confidence=_segment_confidence(self.latest_segments),
            segments=self.latest_segments,
            is_final=True,
        )
        self.closed = True
        await self.socket.close()
        return [final, self._event("stream.closed")]

    async def abort(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                await self.socket.close()
            except Exception:
                return

    async def _drain(self) -> List[StreamingSTTEvent]:
        events: List[StreamingSTTEvent] = []
        timeout = 0.003
        while True:
            try:
                frame = await self._receive_frame(timeout)
            except asyncio.TimeoutError:
                break
            self._remember(frame)
            if self.latest_text:
                events.append(
                    self._event(
                        "transcript.partial",
                        text=self.latest_text,
                        confidence=_segment_confidence(self.latest_segments),
                        segments=self.latest_segments,
                    )
                )
            timeout = 0.001
        return events

    async def _receive_frame(self, timeout: float) -> VolcengineSpeechFrame:
        raw = await asyncio.wait_for(self.socket.recv(), timeout=timeout)
        return decode_server_frame(raw)

    def _remember(self, frame: VolcengineSpeechFrame) -> None:
        result = frame.payload.get("result") or {}
        if not isinstance(result, dict):
            return
        text = str(result.get("text") or "").strip()
        if text:
            self.latest_text = text
        segments = _transcript_segments(result.get("utterances"))
        if segments:
            self.latest_segments = segments

    def _event(self, event_type: str, **values: Any) -> StreamingSTTEvent:
        event = StreamingSTTEvent(
            stream_id=self.stream_id,
            sequence=self.sequence,
            type=event_type,
            language=self.request.language,
            provider=ProviderMeta(
                provider_id=self.provider.provider_id,
                model=self.context.model,
                request_id=self.request_id,
                latency_ms=0,
            ),
            **values,
        )
        self.sequence += 1
        return event


class VolcengineRealtimeSpeechStream:
    """Seeduplex JSON-event S2S adapter with approved-text replacement."""

    def __init__(
        self,
        provider: VolcengineProvider,
        request: RealtimeSpeechDialogueRequest,
        context: ProviderContext,
        socket: Any,
    ) -> None:
        self.provider = provider
        self.request = request
        self.context = context
        self.socket = socket
        self.stream_id = new_id("dialogue_stream")
        self.sequence = 1
        self.closed = False
        self.request_id = ""
        self.approved_text = ""
        self._output_final_emitted = False
        self.ready_events = [self._event("dialogue.ready")]

    @classmethod
    async def open(
        cls,
        provider: VolcengineProvider,
        request: RealtimeSpeechDialogueRequest,
        context: ProviderContext,
    ) -> "VolcengineRealtimeSpeechStream":
        endpoint = _secure_ws_endpoint(
            context.config.get("realtime_websocket_url"),
            "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue",
            name="Volcengine realtime speech endpoint",
        )
        socket = await _open_socket(
            provider.websocket_connect,
            endpoint,
            {"X-Api-Key": _speech_api_key(context.credentials)},
            timeout_s=context.timeout_s,
            max_size=int(context.config.get("websocket_max_message_bytes", 8 * 1024 * 1024)),
            provider_label="Volcengine realtime speech",
        )
        stream = cls(provider, request, context, socket)
        voice_map = context.config.get("voice_map") or {}
        voice = str(
            voice_map.get(request.voice)
            or context.config.get("dialogue_default_voice")
            or request.voice
        )
        session = {
            "model": str(context.config.get("dialogue_model_version") or context.model or "1.2.6.1"),
            "instructions": request.session_instructions,
            "audio": {
                "input": {"format": {"type": "pcm", "rate": 16_000}},
                "output": {"format": {"type": "pcm", "rate": 24_000}},
                "voice": voice,
            },
        }
        try:
            await stream._send({"type": "session.create", "event_id": new_id("vendor_event"), "session": session})
            await stream._wait_for({"session.created"})
        except Exception:
            await stream.abort()
            raise
        return stream

    async def send_audio(self, chunk: bytes) -> List[RealtimeSpeechDialogueEvent]:
        if self.closed:
            raise ProviderError(
                "provider_stream_closed", "Volcengine realtime speech stream is closed.", retryable=False
            )
        audio = _resample_pcm16(chunk, self.request.input_audio.sample_rate_hz, 16_000)
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "event_id": new_id("vendor_event"),
                "audio": base64.b64encode(audio).decode("ascii"),
            }
        )
        return await self._drain()

    async def commit(
        self,
        command: RealtimeSpeechResponseCommand,
        on_event: Optional[Callable[[RealtimeSpeechDialogueEvent], Awaitable[None]]] = None,
    ) -> List[RealtimeSpeechDialogueEvent]:
        if self.closed:
            return []
        self.approved_text = command.spoken_text
        await self._send({"type": "input_audio_buffer.commit", "event_id": new_id("vendor_event")})
        # Seeduplex replacement mode makes the audio expression track speak the
        # already-approved act instead of an autonomous model answer.
        await self._send(
            {
                "type": "speech_text_buffer.replacement.append",
                "event_id": new_id("vendor_event"),
                "text": command.spoken_text,
            }
        )
        await self._send(
            {
                "type": "speech_text_buffer.replacement.commit",
                "event_id": new_id("vendor_event"),
            }
        )
        deadline = asyncio.get_running_loop().time() + max(1, float(self.context.timeout_s))
        events: List[RealtimeSpeechDialogueEvent] = []
        try:
            while asyncio.get_running_loop().time() < deadline:
                payload = await self._receive(max(0.01, deadline - asyncio.get_running_loop().time()))
                projected = self._project(payload)
                if on_event:
                    for event in projected:
                        await on_event(event)
                else:
                    events.extend(projected)
                if payload.get("type") == "response.done":
                    break
            else:
                raise asyncio.TimeoutError
        except asyncio.TimeoutError as exc:
            await self.abort()
            raise ProviderError(
                "provider_timeout", "Volcengine realtime speech response timed out.", retryable=True
            ) from exc
        except ProviderError:
            await self.abort()
            raise
        except Exception as exc:
            await self.abort()
            raise ProviderError(
                "provider_stream_interrupted",
                "Volcengine realtime speech stream was interrupted.",
                retryable=False,
            ) from exc

        if not self._output_final_emitted:
            final = self._event("output.transcript.final", text=self.approved_text, is_final=True)
            self._output_final_emitted = True
            if on_event:
                await on_event(final)
            else:
                events.append(final)
        try:
            await self._send({"type": "session.close", "event_id": new_id("vendor_event")})
        finally:
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
        await self._send({"type": "response.cancel", "event_id": new_id("vendor_event")})
        return [self._event("output.interrupted")]

    async def abort(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                await self._send({"type": "session.close", "event_id": new_id("vendor_event")})
            except Exception:
                pass
            try:
                await self.socket.close()
            except Exception:
                return

    async def _wait_for(self, accepted: set[str]) -> Dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + max(1, float(self.context.timeout_s))
        try:
            while asyncio.get_running_loop().time() < deadline:
                payload = await self._receive(
                    max(0.01, deadline - asyncio.get_running_loop().time())
                )
                if payload.get("type") in accepted:
                    return payload
        except asyncio.TimeoutError as exc:
            raise ProviderError(
                "provider_timeout",
                "Volcengine realtime session acknowledgement timed out.",
                retryable=True,
            ) from exc
        raise ProviderError(
            "provider_timeout",
            "Volcengine realtime session acknowledgement timed out.",
            retryable=True,
        )

    async def _drain(self) -> List[RealtimeSpeechDialogueEvent]:
        events: List[RealtimeSpeechDialogueEvent] = []
        timeout = 0.003
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
                "Volcengine realtime speech returned invalid JSON.",
                retryable=True,
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(
                "provider_schema_invalid",
                "Volcengine realtime speech event must be an object.",
                retryable=True,
            )
        self.request_id = str(payload.get("event_id") or self.request_id)
        if payload.get("type") == "error":
            code = payload.get("status_code") or (payload.get("error") or {}).get("code")
            raise ProviderError(
                "provider_server_error",
                "Volcengine realtime speech error %s." % (code or "unknown"),
                retryable=str(code or "").startswith("55"),
                details={"vendor_error_code": code},
            )
        return payload

    async def _send(self, payload: Dict[str, Any]) -> None:
        try:
            await self.socket.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        except Exception as exc:
            raise ProviderError(
                "provider_stream_interrupted",
                "Volcengine realtime speech could not send an event.",
                retryable=False,
            ) from exc

    def _project(self, payload: Dict[str, Any]) -> List[RealtimeSpeechDialogueEvent]:
        event_type = str(payload.get("type") or "")
        if event_type == "conversation.item.input_audio_transcription.started":
            return [self._event("input.speech.started")]
        if event_type == "input_audio_buffer.committed":
            return [self._event("input.speech.stopped")]
        if event_type == "conversation.item.input_audio_transcription.delta":
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
        if event_type == "response.output_audio.delta":
            return [
                self._event(
                    "output.audio.delta",
                    audio_base64=str(payload.get("delta") or payload.get("audio") or ""),
                )
            ]
        if event_type == "response.output_audio.done":
            events: List[RealtimeSpeechDialogueEvent] = []
            if self.approved_text and not self._output_final_emitted:
                events.append(
                    self._event("output.transcript.final", text=self.approved_text, is_final=True)
                )
                self._output_final_emitted = True
            events.append(self._event("output.audio.done"))
            return events
        if event_type == "response.canceled":
            return [self._event("output.interrupted")]
        return []

    def _event(self, event_type: str, **values: Any) -> RealtimeSpeechDialogueEvent:
        event = RealtimeSpeechDialogueEvent(
            stream_id=self.stream_id,
            sequence=self.sequence,
            type=event_type,
            audio_content_type="audio/pcm",
            sample_rate_hz=24_000,
            provider=ProviderMeta(
                provider_id=self.provider.provider_id,
                model=self.context.model,
                request_id=self.request_id,
                latency_ms=0,
            ),
            **values,
        )
        self.sequence += 1
        return event


async def _open_socket(
    connector: Any,
    url: str,
    headers: Dict[str, str],
    *,
    timeout_s: float,
    max_size: int,
    provider_label: str,
) -> Any:
    try:
        try:
            return await connector(
                url,
                additional_headers=headers,
                open_timeout=max(1, float(timeout_s)),
                max_size=max_size,
            )
        except TypeError:
            # websockets < 14 used extra_headers.  Keep compatibility inside
            # the vendor adapter without leaking the library version upward.
            return await connector(url, extra_headers=headers)
    except asyncio.TimeoutError as exc:
        raise ProviderError(
            "provider_stream_open_failed", "%s WebSocket timed out." % provider_label, retryable=True
        ) from exc
    except Exception as exc:
        raise ProviderError(
            "provider_stream_open_failed",
            "%s WebSocket could not be opened." % provider_label,
            retryable=True,
        ) from exc


def _ark_api_key(credentials: Dict[str, Any]) -> str:
    api_key = str(credentials.get("ark_api_key") or credentials.get("api_key") or "").strip()
    if not api_key:
        raise ProviderError("provider_auth_failed", "Volcengine Ark API Key is required.", retryable=False)
    return api_key


def _speech_api_key(credentials: Dict[str, Any]) -> str:
    api_key = str(credentials.get("speech_api_key") or "").strip()
    if not api_key:
        raise ProviderError(
            "provider_auth_failed", "Volcengine Speech API Key is required.", retryable=False
        )
    return api_key


def _secure_http_endpoint(value: Any, default: str, *, name: str) -> str:
    endpoint = str(value or default).strip().rstrip("/")
    if not endpoint.startswith("https://"):
        raise ProviderError("provider_bad_request", "%s must use HTTPS." % name, retryable=False)
    return endpoint


def _secure_ws_endpoint(value: Any, default: str, *, name: str) -> str:
    endpoint = str(value or default).strip()
    if not endpoint.startswith("wss://"):
        raise ProviderError("provider_bad_request", "%s must use WSS." % name, retryable=False)
    return endpoint


def _asr_stream_format(content_type: str) -> tuple[str, str]:
    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized in {"audio/pcm", "audio/l16", "audio/raw"}:
        return "pcm", "raw"
    if normalized in {"audio/wav", "audio/x-wav"}:
        return "wav", "raw"
    if normalized in {"audio/ogg", "application/ogg"}:
        return "ogg", "opus"
    if normalized in {"audio/mpeg", "audio/mp3"}:
        return "mp3", "raw"
    raise ProviderError(
        "provider_audio_format_unsupported",
        "Volcengine streaming ASR supports PCM, WAV, Ogg-Opus, or MP3 audio.",
        retryable=False,
    )


def _tts_format(content_type: str) -> tuple[str, str]:
    normalized = content_type.split(";", 1)[0].strip().lower()
    mapping = {
        "audio/wav": ("wav", "audio/wav"),
        "audio/x-wav": ("wav", "audio/wav"),
        "audio/pcm": ("pcm", "audio/pcm"),
        "audio/mpeg": ("mp3", "audio/mpeg"),
        "audio/mp3": ("mp3", "audio/mpeg"),
        "audio/ogg": ("ogg_opus", "audio/ogg"),
    }
    if normalized not in mapping:
        raise ProviderError(
            "provider_audio_format_unsupported",
            "Volcengine TTS supports WAV, PCM, MP3, or Ogg-Opus output.",
            retryable=False,
        )
    return mapping[normalized]


def _tts_resource(model: str) -> str:
    if model in {"seed-tts-2.0", "seed-icl-2.0"}:
        return model
    if model == "doubao-seed-icl-2.0":
        return "seed-icl-2.0"
    return "seed-tts-2.0"


def _decode_json_messages(raw: bytes) -> List[Dict[str, Any]]:
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ProviderError(
            "provider_schema_invalid", "Volcengine TTS response was not UTF-8 JSON.", retryable=True
        ) from exc
    if not text:
        return []
    decoder = json.JSONDecoder()
    messages: List[Dict[str, Any]] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if text.startswith("data:", index):
            index += 5
            while index < len(text) and text[index].isspace():
                index += 1
        if index >= len(text):
            break
        try:
            value, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "provider_schema_invalid", "Volcengine TTS response was not valid JSON.", retryable=True
            ) from exc
        if not isinstance(value, dict):
            raise ProviderError(
                "provider_schema_invalid", "Volcengine TTS response item must be an object.", retryable=True
            )
        messages.append(value)
    return messages


def _transcript_segments(raw: Any) -> List[TranscriptSegment]:
    if not isinstance(raw, list):
        return []
    segments: List[TranscriptSegment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        confidences = [
            float(word.get("confidence"))
            for word in (item.get("words") or [])
            if isinstance(word, dict) and isinstance(word.get("confidence"), (int, float))
        ]
        confidence = min(confidences) if confidences else None
        segments.append(
            TranscriptSegment(
                text=text,
                start_ms=max(0, int(item.get("start_time") or 0)),
                end_ms=max(0, int(item.get("end_time") or 0)),
                confidence=confidence,
            )
        )
    return segments


def _segment_confidence(segments: List[TranscriptSegment]) -> Optional[float]:
    known = [item.confidence for item in segments if item.confidence is not None]
    return min(known) if known else None


def _audio_duration_ms(
    audio: bytes,
    *,
    audio_format: str,
    sample_rate: int,
    words: List[Dict[str, Any]],
    text: str,
    speaking_rate: float,
) -> int:
    if audio_format == "pcm":
        return max(1, round(len(audio) * 1000 / max(1, sample_rate * 2)))
    if audio_format == "wav":
        try:
            with wave.open(BytesIO(audio), "rb") as reader:
                return max(1, round(reader.getnframes() * 1000 / max(1, reader.getframerate())))
        except (wave.Error, EOFError):
            pass
    end_times = [
        float(item.get("endTime"))
        for item in words
        if isinstance(item.get("endTime"), (int, float))
    ]
    if end_times:
        return max(1, round(max(end_times) * 1000))
    return max(250, round(len(text.strip()) * 180 / max(0.5, speaking_rate)))
