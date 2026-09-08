import asyncio
import base64
import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlparse

import websockets

from app.core.speech_diagnostics import record_stt_sentence

from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    BatchSTTRequest,
    BatchSTTResponse,
    ChatJSONRequest,
    ProviderContext,
    ProviderMeta,
    RealtimeSpeechDialogueRequest,
    StableTranscriptPreview,
    StreamingSTTEvent,
    StreamingSTTRequest,
    TTSSynthesizeRequest,
    TTSSynthesizeResponse,
    TranscriptSegment,
)
from app.providers.openai_compatible.provider import OpenAICompatibleProvider
from app.providers.dashscope.tts_streaming import DashScopeTTSStream
from app.providers.realtime_speech import OpenAIStyleRealtimeSpeechStream


class DashScopeProvider(OpenAICompatibleProvider):
    """DashScope adapter for Qwen LLM/TTS and authoritative Chinese ASR."""

    provider_id = "dashscope"

    def __init__(self, client_factory: Any = None, websocket_connect: Any = None) -> None:
        super().__init__(client_factory=client_factory) if client_factory is not None else super().__init__()
        self.websocket_connect = websocket_connect or websockets.connect

    async def invoke(self, capability: str, request: Any, context: ProviderContext) -> Any:
        original_request = request
        if capability == cap.STT_BATCH and isinstance(request, BatchSTTRequest):
            return await self._transcribe_batch(request, context)
        if (
            capability == cap.LLM_CHAT_JSON
            and isinstance(request, ChatJSONRequest)
            and str(context.config.get("structured_output_mode") or self.structured_output_mode) == "json_schema"
        ):
            # DashScope rejects array.uniqueItems in constrained decoding.
            # Only the vendor wire schema omits it: ModelGateway still checks
            # the caller's original schema, including uniqueness, after return.
            request = request.model_copy(update={
                "json_schema": _dashscope_wire_schema(request.json_schema),
            })
        if (
            capability in {cap.LLM_CHAT_JSON, cap.LLM_CHAT_TEXT}
            and context.purpose
            in {"interview_turn_understanding", "controlled_followup"}
            and context.model.lower().startswith("qwen")
            and "enable_thinking" not in context.config
        ):
            # 实时轮转优先确定性低延迟。Qwen 混合思考模型若使用默认深度
            # 思考，短 JSON 合同也可能超过 30 秒；管理员显式配置时仍尊重其值。
            context = context.model_copy(
                update={
                    "model_settings": {
                        **context.model_settings,
                        "enable_thinking": False,
                    }
                }
            )
        try:
            return await super().invoke(capability, request, context)
        except ProviderError as exc:
            if (capability != cap.LLM_CHAT_JSON or context.purpose != "interview_turn_understanding"
                    or str(context.config.get("structured_output_mode") or self.structured_output_mode) != "json_schema"
                    or exc.code != "provider_bad_request" or exc.details.get("http_status") != 400):
                raise
            # One changed transport attempt, never a loop with an identical
            # rejected request. The gateway still validates the FULL original
            # schema, including references/uniqueness and forbidden fields.
            logging.getLogger(__name__).warning(
                "structured_output_transport_fallback http_status=400 category=%s",
                exc.details.get("rejection_category", "request_rejected"),
            )
            fallback_context = context.model_copy(update={"model_settings": {
                **context.model_settings, "structured_output_mode": "json_object",
            }})
            return await super().invoke(capability, original_request, fallback_context)

    def _chat_request_options(
        self, config: Dict[str, Any], model: str
    ) -> Dict[str, Any]:
        if model.lower().startswith("qwen") and "enable_thinking" in config:
            return {"enable_thinking": bool(config["enable_thinking"])}
        return {}

    async def open_stream(self, request: StreamingSTTRequest, context: ProviderContext) -> Any:
        if context.capability != cap.STT_STREAMING:
            raise ProviderError("provider_capability_missing", "DashScope stream capability is invalid.", retryable=False)
        return await DashScopeSTTStream.open(self, request, context)

    async def open_tts_stream(self, request: TTSSynthesizeRequest, context: ProviderContext) -> DashScopeTTSStream:
        if context.capability != cap.TTS_SYNTHESIZE:
            raise ProviderError("provider_capability_missing", "DashScope TTS stream capability is invalid.", retryable=False)
        config = context.config
        endpoint = _tts_endpoint(config, context.model)
        if not context.model.lower().startswith("qwen3-tts") or _uses_speech_synthesizer(context.model, endpoint):
            raise ProviderError("provider_streaming_not_supported", "This DashScope model does not implement Qwen3 HTTP SSE PCM streaming.", retryable=False)
        if not request.text.strip() or len(request.text) > 600:
            raise ProviderError("provider_bad_request", "Qwen3 HTTP TTS requires between 1 and 600 text characters.", retryable=False)
        if request.speaking_rate != 1.0:
            raise ProviderError("provider_bad_request", "Qwen3 HTTP TTS does not expose a speaking-rate parameter.", retryable=False)
        if request.format not in {"audio/wav", "wav", "audio/pcm", "pcm"} or config.get("sample_rate_hz", 24000) not in (24000, "24000"):
            raise ProviderError("provider_audio_format_unsupported", "Qwen3 HTTP streaming requires 24 kHz mono PCM16 output.", retryable=False)
        voice = (config.get("voice_map") or {}).get(request.voice_profile_id)
        if not voice:
            voice = config.get("default_voice") if request.voice_profile_id.startswith("voice_default") else request.voice_profile_id
        return await DashScopeTTSStream.open(
            self.client_factory, context, endpoint=endpoint, api_key=_api_key(context.credentials),
            payload={"model": context.model, "input": {
                "text": request.text, "voice": str(voice or _default_voice(context.model)),
                "language_type": _language_type(request.language),
            }},
        )

    async def open_dialogue(
        self,
        request: RealtimeSpeechDialogueRequest,
        context: ProviderContext,
    ) -> OpenAIStyleRealtimeSpeechStream:
        """Open Qwen Realtime behind the provider-neutral speech dialogue seam."""
        if context.capability != cap.SPEECH_DIALOGUE_REALTIME:
            raise ProviderError(
                "provider_capability_missing",
                "DashScope realtime speech capability is invalid.",
                retryable=False,
            )
        api_key = _api_key(context.credentials)
        base = _dialogue_endpoint(context.config)
        separator = "&" if "?" in base else "?"
        url = "%s%s%s" % (base, separator, urlencode({"model": context.model}))
        voice_map = context.config.get("voice_map") or {}
        configured_voice = str(
            voice_map.get(request.voice)
            or context.config.get("dialogue_default_voice")
            or context.config.get("default_voice")
            or request.voice
        )
        voice = _dialogue_voice(context.model, configured_voice)
        transcription_model = str(
            context.config.get("dialogue_input_transcription_model")
            or "qwen3-asr-flash-realtime"
        )
        if transcription_model == "qwen3-asr-flash":
            transcription_model = "qwen3-asr-flash-realtime"
        session: Dict[str, Any] = {
            "modalities": ["text", "audio"],
            "instructions": request.session_instructions,
            "voice": voice,
            # The interviewer owns turn boundaries. Vendor VAD is deliberately
            # disabled so a premature endpoint cannot become scoring evidence.
            "turn_detection": None,
        }
        if context.model.startswith("qwen3.5-omni-"):
            session.update(
                {
                    "model": context.model,
                    "audio": {
                        "input": {"format": {"type": "pcm", "sample_rate": 16000}},
                        "output": {"format": {"type": "pcm", "sample_rate": 24000}},
                    },
                    "input_audio_transcription": {
                        "model": transcription_model,
                        "language": _language_hint(request.language),
                    },
                }
            )
        else:
            session.update(
                {
                    "input_audio_format": "pcm",
                    "output_audio_format": "pcm",
                }
            )
        response_payload = {
            "modalities": ["text", "audio"],
            "voice": voice,
        }
        headers = {
            "Authorization": "Bearer %s" % api_key,
            "User-Agent": "Interviewer/0.1",
        }
        workspace_id = str(context.config.get("workspace_id") or "").strip()
        if workspace_id:
            headers["X-DashScope-WorkSpace"] = workspace_id
        return await OpenAIStyleRealtimeSpeechStream.open(
            provider_id=self.provider_id,
            connector=self.websocket_connect,
            url=url,
            headers=headers,
            request=request,
            context=context,
            session=session,
            vendor_input_rate=16000,
            vendor_output_rate=24000,
            response_payload=response_payload,
            response_instruction_mode="session_update",
        )

    async def _transcribe_batch(self, request: BatchSTTRequest, context: ProviderContext) -> BatchSTTResponse:
        audio = request.audio_bytes
        if not audio:
            raise ProviderError(
                "provider_audio_unavailable",
                "DashScope batch ASR requires server-resolved private audio bytes.",
                retryable=False,
            )
        if len(audio) > int(context.config.get("batch_max_bytes", 10 * 1024 * 1024)):
            raise ProviderError("provider_audio_stream_too_large", "DashScope batch ASR audio exceeds the configured limit.", retryable=False)
        endpoint = _asr_batch_endpoint(context.config)
        data_uri = "data:%s;base64,%s" % (request.content_type.split(";", 1)[0], base64.b64encode(audio).decode("ascii"))
        payload = {
            "model": context.model,
            "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": data_uri}}]}],
            "stream": False,
            "asr_options": {"language": _language_hint(request.language), "enable_itn": True},
        }
        vendor_response = await self._post(
            endpoint,
            payload,
            api_key=_api_key(context.credentials),
            timeout_s=max(1, int(context.timeout_s)),
            use_environment_proxy=bool(context.config.get("use_environment_proxy", False)),
        )
        choices = vendor_response.get("choices") or []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
        text = str((message or {}).get("content") or "").strip()
        if not text:
            raise ProviderError("provider_final_transcript_missing", "DashScope batch ASR returned no transcript.", retryable=True)
        return BatchSTTResponse(
            text=text,
            language=request.language,
            confidence=1.0,
            segments=[TranscriptSegment(text=text, confidence=1.0)],
            source="server_batch_repair" if request.purpose == "candidate_answer_repair" else "server_batch",
            provider=ProviderMeta(
                provider_id=self.provider_id,
                model=context.model,
                request_id=str(vendor_response.get("request_id") or vendor_response.get("id") or ""),
                latency_ms=0,
            ),
        )

    async def synthesize_speech(
        self,
        request: TTSSynthesizeRequest,
        *,
        config: Dict[str, Any],
        credentials: Dict[str, Any],
        model: str,
        timeout_s: int,
    ) -> TTSSynthesizeResponse:
        response_format, content_type = _dashscope_format(request.format)
        voice_map = config.get("voice_map") or {}
        voice = voice_map.get(request.voice_profile_id)
        if not voice:
            voice = config.get("default_voice") if request.voice_profile_id.startswith("voice_default") else request.voice_profile_id
        voice = str(voice or _default_voice(model))
        endpoint = _tts_endpoint(config, model)
        uses_speech_synthesizer = _uses_speech_synthesizer(model, endpoint)
        input_payload: Dict[str, Any] = {
            "text": request.text,
            "voice": voice,
        }
        if uses_speech_synthesizer:
            input_payload.update(
                {
                    "format": response_format,
                    "sample_rate": int(config.get("sample_rate_hz", 24000)),
                }
            )
            if request.speaking_rate != 1.0:
                input_payload["rate"] = request.speaking_rate
        else:
            if request.speaking_rate != 1.0:
                raise ProviderError(
                    "provider_bad_request",
                    "DashScope Qwen3-TTS basic HTTP integration does not expose a speaking-rate parameter.",
                    retryable=False,
                )
            if response_format != "wav":
                raise ProviderError(
                    "provider_audio_format_unsupported",
                    "DashScope Qwen3-TTS basic HTTP integration currently returns WAV audio.",
                    retryable=False,
                )
            input_payload["language_type"] = _language_type(request.language)
        vendor_response = await self._post(
            endpoint,
            {"model": model, "input": input_payload},
            api_key=_api_key(credentials),
            timeout_s=timeout_s,
            use_environment_proxy=bool(config.get("use_environment_proxy", False)),
        )
        output = vendor_response.get("output") or {}
        audio = output.get("audio") or {}
        audio_uri = str(audio.get("url") or "").strip()
        if audio_uri.startswith("http://") and bool(config.get("force_https_assets", True)):
            audio_uri = "https://%s" % audio_uri.removeprefix("http://")
        if not audio_uri.startswith(("https://", "http://")):
            raise ProviderError(
                "provider_schema_invalid",
                "DashScope TTS response did not contain a downloadable audio URL.",
                retryable=True,
            )
        locator_hash = hashlib.sha256(audio_uri.encode("utf-8")).hexdigest()
        return TTSSynthesizeResponse(
            audio_uri=audio_uri,
            content_type=content_type,
            duration_ms=max(250, int(len(request.text.strip()) * 180 / request.speaking_rate)),
            content_hash="sha256:%s" % locator_hash,
            provider=ProviderMeta(
                provider_id=self.provider_id,
                model=model,
                request_id=str(vendor_response.get("request_id") or audio.get("id") or ""),
                latency_ms=0,
            ),
        )


def _api_key(credentials: Dict[str, Any]) -> str:
    api_key = str(credentials.get("api_key") or "")
    if not api_key:
        raise ProviderError("provider_auth_failed", "DashScope api_key is required.", retryable=False)
    return api_key


def _dashscope_wire_schema(schema):
    if isinstance(schema, list):
        return [_dashscope_wire_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    return {
        key: value if key in {"enum", "const", "default", "examples"} else _dashscope_wire_schema(value)
        for key, value in schema.items()
        if not (key == "uniqueItems" and schema.get("type") == "array")
    }


class DashScopeSTTStream:
    """Maps DashScope duplex ASR WebSocket events to the single-final stream interface."""

    def __init__(self, provider: DashScopeProvider, request: StreamingSTTRequest, context: ProviderContext, socket: Any) -> None:
        self.provider = provider
        self.request = request
        self.context = context
        self.socket = socket
        self.task_id = str(uuid.uuid4())
        self.stream_id = "stt_stream_%s" % self.task_id.replace("-", "")
        self.sequence = 1
        self.closed = False
        self._finish_lock = asyncio.Lock()
        self._abort_task: Optional[asyncio.Task] = None
        self._transport_aborted = False
        self.committed: List[TranscriptSegment] = []
        self.partial_segment: Optional[TranscriptSegment] = None
        self._committed_sentences: Dict[Any, Optional[TranscriptSegment]] = {}
        self._committed_sentence_keys: List[Any] = []
        self._pending_sentences: Dict[Any, Optional[TranscriptSegment]] = {}
        self._partial_sentence_key: Any = None
        self._has_unstable_tail = False
        self._text_seen = False
        self._preview_revision = 0
        self._preview_timestamps_valid = True
        self._preview_segment_count = 0
        self._last_sentence_id = 0
        self._preview_last_sentence_id = 0
        self.request_id = ""
        self.ready_events = [self._event("stream.ready")]
        # 厂商下行事件必须由独立任务持续接收。候选人上行每 20ms 一帧，
        # 不能在 send_audio 中为每一帧等待 WebSocket 下行，否则会把
        # LiveKit 的两秒证据缓冲耗尽并触发失败关闭。
        self._reader_queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._reader_task: Optional[asyncio.Task] = None
        self._reader_error: Optional[Exception] = None
        self._task_finished = False
        # WebSocket send 可能偶发阻塞。用独立发送任务承接 PCM，并用有界
        # 原始音频时长限制内存；LiveKit 收帧路径只入队。默认五秒覆盖
        # 事件循环/网络短抖动，超过预算仍然失败关闭，绝不静默丢帧。
        self._sender_queue: asyncio.Queue = asyncio.Queue()
        self._sender_task: Optional[asyncio.Task] = None
        self._sender_error: Optional[Exception] = None
        self._pending_audio_bytes = 0
        self._audio_send_buffer = bytearray()
        normalized_content_type = request.audio.content_type.split(";", 1)[0].strip().lower()
        if normalized_content_type == "audio/pcm":
            # DashScope recommends audio packets near 100 ms. LiveKit supplies
            # 20 ms PCM frames, so coalesce them before writing to the vendor
            # socket instead of producing 50 WebSocket messages per second.
            packet_ms = _bounded_stream_audio_packet_ms(context.config)
            bytes_per_sample_frame = request.audio.channels * 2
            packet_sample_frames = max(
                1,
                round(request.audio.sample_rate_hz * packet_ms / 1000),
            )
            self._audio_send_target_bytes = (
                packet_sample_frames * bytes_per_sample_frame
            )
        else:
            # Encoded formats already have their own packet boundaries.
            self._audio_send_target_bytes = 0
        backpressure_seconds = _bounded_stream_send_backpressure_seconds(
            context.config
        )
        self._max_pending_audio_bytes = max(
            1024,
            int(
                request.audio.sample_rate_hz
                * request.audio.channels
                * 2
                * backpressure_seconds
            ),
        )
        self._sender_drained = asyncio.Event()
        self._sender_drained.set()
        self._sender_progress = asyncio.Event()

    @classmethod
    async def open(cls, provider: DashScopeProvider, request: StreamingSTTRequest, context: ProviderContext) -> "DashScopeSTTStream":
        audio_format = _stream_audio_format(request.audio.content_type)
        url = _asr_stream_endpoint(context.config)
        headers = {"Authorization": "Bearer %s" % _api_key(context.credentials), "User-Agent": "Interviewer/0.1"}
        workspace_id = str(context.config.get("workspace_id") or "").strip()
        if workspace_id:
            headers["X-DashScope-WorkSpace"] = workspace_id
        try:
            try:
                socket = await provider.websocket_connect(
                    url,
                    additional_headers=headers,
                    open_timeout=max(1, float(context.timeout_s)),
                    max_size=int(context.config.get("websocket_max_message_bytes", 4 * 1024 * 1024)),
                    # websockets>=15 discovers HTTP(S)/SOCKS proxies from the
                    # process environment by default. Provider configuration is
                    # authoritative: an inherited developer proxy must not
                    # silently enter the formal evidence path.
                    proxy=(
                        True
                        if bool(context.config.get("use_environment_proxy", False))
                        else None
                    ),
                )
            except TypeError as exc:
                if _is_unexpected_keyword(exc, "additional_headers"):
                    socket = await provider.websocket_connect(
                        url, extra_headers=headers
                    )
                elif not _is_unexpected_keyword(exc, "proxy"):
                    raise
                else:
                    try:
                        # websockets 14 uses additional_headers but predates the
                        # explicit proxy argument.
                        socket = await provider.websocket_connect(
                            url,
                            additional_headers=headers,
                            open_timeout=max(1, float(context.timeout_s)),
                            max_size=int(
                                context.config.get(
                                    "websocket_max_message_bytes", 4 * 1024 * 1024
                                )
                            ),
                        )
                    except TypeError as legacy_exc:
                        if not _is_unexpected_keyword(
                            legacy_exc, "additional_headers"
                        ):
                            raise
                        # websockets<14 and minimal injectable clients use
                        # extra_headers and have no environment-proxy discovery.
                        socket = await provider.websocket_connect(
                            url, extra_headers=headers
                        )
        except asyncio.TimeoutError as exc:
            raise ProviderError("provider_stream_open_failed", "DashScope ASR WebSocket timed out.", retryable=True) from exc
        except Exception as exc:
            raise ProviderError("provider_stream_open_failed", "DashScope ASR WebSocket could not be opened.", retryable=True) from exc
        stream = None
        try:
            stream = cls(provider, request, context, socket)
            parameters: Dict[str, Any] = {
                "format": audio_format,
                "sample_rate": request.audio.sample_rate_hz,
                "language_hints": [_language_hint(request.language)],
                "semantic_punctuation_enabled": False,
                "max_sentence_silence": int(context.config.get("max_sentence_silence_ms", 800)),
                "heartbeat": True,
            }
            vocabulary_id = str(context.config.get("vocabulary_id") or "").strip()
            if vocabulary_id:
                parameters["vocabulary_id"] = vocabulary_id
            # Native per-session word bias, not a generated answer/context.
            # Other models do not accept this vendor parameter.
            if context.model == "qwen-audio-3.0-asr-flash-streaming" and request.recognition_terms:
                parameters["vocabulary"] = {term: 2 for term in request.recognition_terms}
                if _language_hint(request.language) == "zh":
                    parameters["language_hints"] = ["zh", "en"]
            await socket.send(json.dumps({
                "header": {"action": "run-task", "task_id": stream.task_id, "streaming": "duplex"},
                "payload": {
                    "task_group": "audio", "task": "asr", "function": "recognition", "model": context.model,
                    "parameters": parameters, "input": {},
                },
            }, ensure_ascii=False))
            first = await stream._receive_one(timeout=max(1, float(context.timeout_s)))
            if first != "task-started":
                raise ProviderError("provider_stream_open_failed", "DashScope ASR did not acknowledge task start.", retryable=True)
            stream._reader_task = asyncio.create_task(stream._read_vendor_events())
            stream._sender_task = asyncio.create_task(stream._send_audio_frames())
            return stream
        except BaseException:
            # Once connected, this adapter owns the socket until a complete
            # handshake is handed off. CancelledError is not an Exception.
            async def cleanup() -> None:
                try:
                    await asyncio.wait_for(
                        stream.abort() if stream is not None else _close_aborted_socket(socket, float(context.timeout_s)),
                        timeout=max(0.01, min(2.0, float(context.timeout_s))),
                    )
                except (Exception, asyncio.CancelledError):
                    pass

            task = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass  # Cleanup keeps its own bounded deadline on repeated cancellation.
            raise

    async def wait_audio_capacity(self, byte_count: int) -> None:
        """Bounded replay flow control; live send_audio stays nonblocking."""
        if byte_count > self._max_pending_audio_bytes:
            raise ProviderError("provider_audio_chunk_too_large", "Replay chunk exceeds sender capacity.", retryable=False)
        while True:
            if self.closed:
                raise ProviderError("provider_stream_closed", "DashScope ASR stream is already closed.", retryable=False)
            self._raise_sender_error()
            self._raise_reader_error()
            if self._pending_audio_bytes + byte_count <= self._max_pending_audio_bytes:
                return
            # No await between the capacity check and clear: progress cannot
            # be lost. Abort and transport failure also wake this waiter.
            self._sender_progress.clear()
            try:
                await asyncio.wait_for(self._sender_progress.wait(), timeout=max(1, min(5, float(self.context.timeout_s))))
            except asyncio.TimeoutError as exc:
                raise ProviderError("provider_timeout", "ASR replay sender made no progress.", retryable=True) from exc

    async def send_audio(self, chunk: bytes) -> List[StreamingSTTEvent]:
        if self.closed:
            raise ProviderError("provider_stream_closed", "DashScope ASR stream is already closed.", retryable=False)
        self._raise_reader_error()
        self._raise_sender_error()
        if not chunk:
            events = self._drain_available()
            if self._task_finished:
                raise ProviderError("provider_stream_closed", "DashScope ASR stream ended before finish was requested.", retryable=True)
            return events
        pending = self._pending_audio_bytes + len(chunk)
        if pending > self._max_pending_audio_bytes:
            raise ProviderError(
                "provider_backpressure_exceeded",
                "DashScope ASR audio send backlog exceeded its configured safety budget.",
                retryable=True,
            )
        self._pending_audio_bytes = pending
        self._sender_drained.clear()
        self._queue_audio(chunk)
        # 不在持有 Evidence 锁的收帧路径中主动等待调度或网络；后台发送、
        # 接收任务会在下一次事件循环机会运行，partial 由后续帧或 finish 排出。
        self._raise_sender_error()
        events = self._drain_available()
        if self._task_finished:
            raise ProviderError("provider_stream_closed", "DashScope ASR stream ended before finish was requested.", retryable=True)
        return events

    async def finish(self) -> List[StreamingSTTEvent]:
        # Concurrent finalization callers may not send finish-task twice or
        # manufacture a second authoritative final from the same projection.
        async with self._finish_lock:
            return await self._finish_once()

    @property
    def has_observed_text(self) -> bool:
        # The reader may have seen text whose queue is lost to an exception.
        # This local fact only prohibits an empty replacement from erasing it.
        return self._text_seen

    async def preview(self) -> Optional[StableTranscriptPreview]:
        """Read sentence-final facts without ending or flushing the task.

        This is not an acknowledgement of all input audio. The independent
        reader owns sentence updates; this method has no await, so it observes
        one coherent projection without consuming realtime event sequences.
        """
        if self.closed:
            raise ProviderError("provider_stream_closed", "DashScope ASR stream is already closed.", retryable=False)
        self._raise_reader_error()
        self._raise_sender_error()
        if self._task_finished:
            raise ProviderError("provider_stream_closed", "DashScope ASR stream ended before finish was requested.", retryable=True)
        if not self._preview_segment_count:
            return None
        segments = self.committed[:self._preview_segment_count]
        return StableTranscriptPreview(
            stream_id=self.stream_id,
            revision=self._preview_revision,
            text="".join(segment.text for segment in segments),
            language=self.request.language,
            confidence=1.0,
            segments=[segment.model_copy(deep=True) for segment in segments],
            provider=ProviderMeta(provider_id=self.provider.provider_id, model=self.context.model,
                                  request_id=self.request_id, latency_ms=0),
            has_unstable_tail=self._has_unstable_tail or not self._preview_timestamps_valid,
        )

    async def _finish_once(self) -> List[StreamingSTTEvent]:
        if self.closed:
            return []
        try:
            self._raise_reader_error()
            self._raise_sender_error()
            self._flush_audio_send_buffer()
            try:
                await asyncio.wait_for(
                    self._sender_drained.wait(),
                    timeout=max(1, float(self.context.timeout_s)),
                )
            except asyncio.TimeoutError as exc:
                raise ProviderError(
                    "provider_timeout",
                    "DashScope ASR audio send backlog did not drain before finish.",
                    retryable=True,
                ) from exc
            self._raise_sender_error()
            await self.socket.send(json.dumps({
                "header": {"action": "finish-task", "task_id": self.task_id, "streaming": "duplex"},
                "payload": {"input": {}},
            }))
            deadline = asyncio.get_running_loop().time() + max(1, float(self.context.timeout_s))
            events = self._drain_available()
            while not self._task_finished:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise ProviderError("provider_timeout", "DashScope ASR final transcript timed out.", retryable=True)
                try:
                    item = await asyncio.wait_for(self._reader_queue.get(), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise ProviderError("provider_timeout", "DashScope ASR final transcript timed out.", retryable=True) from exc
                events.extend(self._consume_reader_item(item))
            self._raise_reader_error()
            text, segments = self._transcript_projection()
            if not text and (self._text_seen or self._has_unstable_tail or self._pending_sentences):
                raise ProviderError("provider_final_transcript_missing", "DashScope ASR returned no final transcript.", retryable=True)
            for segment in segments:
                if segment not in self.committed:
                    self._trace_sentence(segment, final=False)
            events.append(
                self._event(
                    # task-finished is the vendor's normal completion ACK.
                    # With every audio frame sent and no text ever observed or
                    # hypothesis pending, report recognition without words.
                    # This does not certify silence or candidate intent.
                    "transcript.final" if text else "transcript.empty",
                    text=text,
                    is_final=True,
                    segments=segments,
                )
            )
            events.append(self._event("stream.closed"))
            # The final is already authoritative. A slow peer close handshake
            # must not hold the next recognition segment behind a ten-second
            # socket timeout. Reuse the independently owned, bounded cleanup;
            # it only releases transport after audio and final have drained.
            await self.abort()
            return events
        except BaseException:
            # Cancellation before/during finalization still owns this socket.
            # Cleanup failure (or repeated caller cancellation) must never
            # replace the original finalization error or CancelledError.
            try:
                await self.abort()
            except (Exception, asyncio.CancelledError):
                pass
            raise

    async def abort(self) -> None:
        # ``closed`` stops audio acceptance immediately; it must not mean that
        # a partially cancelled socket close has already completed. All abort
        # callers join one independently owned cleanup instead of skipping it.
        if self._abort_task is None:
            self.closed = True
            self._abort_task = asyncio.create_task(self._abort_cleanup())
            self._abort_task.add_done_callback(_consume_cleanup_result)
        try:
            await asyncio.shield(self._abort_task)
        except asyncio.CancelledError:
            # An outer probe timeout cannot merely cancel close(): websockets
            # waits for the peer's handshake/TCP teardown. Reclaim the transport
            # now; shielded cleanup continues to settle its tasks independently.
            self._force_transport_abort()
            raise

    def _force_transport_abort(self) -> bool:
        if self._transport_aborted:
            return True
        self._transport_aborted = _abort_socket_transport(self.socket)
        return self._transport_aborted

    async def _abort_cleanup(self) -> None:
        workers = [task for task in (self._sender_task, self._reader_task) if task is not None]
        self._sender_task = self._reader_task = None
        for task in workers:
            if not task.done():
                task.cancel()
        self._discard_pending_audio()
        try:
            if workers:
                _, pending = await asyncio.wait(workers, timeout=0.1)
                if pending:
                    self._force_transport_abort()
            await _close_aborted_socket(
                self.socket, float(self.context.timeout_s),
                force_abort=self._force_transport_abort,
            )
            if workers:
                await asyncio.wait(workers, timeout=0.1)
        except BaseException:
            self._force_transport_abort()
            raise
        finally:
            for task in workers:
                if not task.done():
                    task.cancel()
                task.add_done_callback(_consume_cleanup_result)

    def _queue_audio(self, chunk: bytes) -> None:
        target = self._audio_send_target_bytes
        if target <= 0:
            self._sender_queue.put_nowait(chunk)
            return
        self._audio_send_buffer.extend(chunk)
        while len(self._audio_send_buffer) >= target:
            packet = bytes(self._audio_send_buffer[:target])
            del self._audio_send_buffer[:target]
            self._sender_queue.put_nowait(packet)

    def _flush_audio_send_buffer(self) -> None:
        if not self._audio_send_buffer:
            return
        self._sender_queue.put_nowait(bytes(self._audio_send_buffer))
        self._audio_send_buffer.clear()

    def _discard_pending_audio(self) -> None:
        self._audio_send_buffer.clear()
        while True:
            try:
                self._sender_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._sender_queue.task_done()
        self._pending_audio_bytes = 0
        self._sender_drained.set()
        self._sender_progress.set()

    async def _send_audio_frames(self) -> None:
        try:
            while not self.closed:
                chunk = await self._sender_queue.get()
                try:
                    await self.socket.send(chunk)
                finally:
                    self._pending_audio_bytes = max(
                        0, self._pending_audio_bytes - len(chunk)
                    )
                    self._sender_queue.task_done()
                    self._sender_progress.set()
                    if self._pending_audio_bytes == 0:
                        self._sender_drained.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._sender_error = exc
            self._sender_drained.set()
            self._sender_progress.set()

    async def _read_vendor_events(self) -> None:
        try:
            while not self.closed:
                vendor_event = await self._receive_one(timeout=None)
                if vendor_event == "result-generated":
                    projected = self._project_result()
                    # heartbeat 或禁用 partial 时没有可见事件，无需占用有界队列。
                    if projected:
                        await self._reader_queue.put((vendor_event, projected))
                elif vendor_event == "task-finished":
                    await self._reader_queue.put((vendor_event, []))
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._reader_error = exc
            try:
                self._reader_queue.put_nowait(("reader-error", []))
            except asyncio.QueueFull:
                # 消费方还会在下一次 send/finish 前直接读取 _reader_error。
                pass

    def _drain_available(self) -> List[StreamingSTTEvent]:
        events: List[StreamingSTTEvent] = []
        while True:
            try:
                item = self._reader_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            events.extend(self._consume_reader_item(item))
        return events

    def _consume_reader_item(self, item: Any) -> List[StreamingSTTEvent]:
        vendor_event, projected = item
        if vendor_event == "reader-error":
            self._raise_reader_error()
        if vendor_event == "task-finished":
            self._task_finished = True
        return projected

    def _raise_reader_error(self) -> None:
        if self._reader_error is None:
            return
        error = self._reader_error
        if isinstance(error, ProviderError):
            raise error
        raise ProviderError("provider_stream_failed", "DashScope ASR WebSocket receive failed.", retryable=True) from error

    def _raise_sender_error(self) -> None:
        if self._sender_error is None:
            return
        error = self._sender_error
        if isinstance(error, ProviderError):
            raise error
        raise ProviderError("provider_stream_failed", "DashScope ASR WebSocket send failed.", retryable=True) from error

    async def _receive_one(self, timeout: Optional[float]) -> str:
        if timeout is None:
            raw = await self.socket.recv()
        else:
            raw = await asyncio.wait_for(self.socket.recv(), timeout=timeout)
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProviderError("provider_schema_invalid", "DashScope ASR returned an invalid event.", retryable=True) from exc
        header = payload.get("header") or {}
        event = str(header.get("event") or "")
        self.request_id = str(header.get("task_id") or self.request_id)
        if event == "task-failed":
            raise ProviderError(
                "provider_server_error",
                "DashScope ASR task failed: %s" % str(header.get("error_code") or "unknown"),
                retryable=True,
            )
        if event == "result-generated":
            sentence = ((payload.get("payload") or {}).get("output") or {}).get("sentence") or {}
            if not isinstance(sentence, dict) or any(
                name in sentence and type(sentence[name]) is not bool
                for name in ("heartbeat", "sentence_end", "sentence_begin")
            ):
                raise ProviderError("provider_schema_invalid", "ASR sentence flags are invalid.", retryable=True)
            if not sentence.get("heartbeat"):
                self._consume_sentence(sentence)
        return event

    def _consume_sentence(self, sentence: Dict[str, Any]) -> None:
        # Fun-ASR/Qwen-Audio identify a sentence before its real begin_time is
        # known. Paraformer has no sentence_id; only its valid time key can be
        # used as a conservative identity. Never key a Fun-ASR sentence by the
        # provisional zero in sentence_begin.
        sentence_id = sentence.get("sentence_id")
        if sentence_id is not None and (type(sentence_id) is not int or sentence_id < 1):
            raise ProviderError("provider_schema_invalid", "ASR sentence identity is invalid.", retryable=True)
        begin = sentence.get("begin_time")
        valid_begin = type(begin) is int and begin >= 0
        key = ("id", sentence_id) if sentence_id is not None else (("time", begin) if valid_begin else None)
        raw_text = sentence.get("text", "")
        if not isinstance(raw_text, str) or len(raw_text) > 100_000:
            raise ProviderError("provider_schema_invalid", "ASR sentence text is invalid or too large.", retryable=True)
        text = raw_text.strip()
        self._text_seen |= bool(text)
        end = sentence.get("end_time")
        valid_end = type(end) is int and end >= 0
        # Paraformer can omit the sentence end while providing word timings.
        # Only an actual, well-ordered provider word range is a safe fallback.
        if end is None and valid_begin:
            words = sentence.get("words") or []
            if not isinstance(words, list) or any(not isinstance(word, dict) for word in words):
                words = []
            last_end = begin
            for word in words:
                word_begin, word_end = word.get("begin_time"), word.get("end_time")
                if (type(word_begin) is not int or type(word_end) is not int
                        or word_begin < last_end or word_end < word_begin):
                    break
                last_end = word_end
            else:
                if words and last_end > begin:
                    end, valid_end = last_end, True
        valid_timing = valid_begin and valid_end and end >= begin
        segment = TranscriptSegment(text=text, start_ms=begin if valid_begin else 0,
                                    end_ms=end if valid_end else 0, confidence=1.0) if text else None
        previous = self._committed_sentences.get(key) if key is not None else None
        if key is not None and key in self._committed_sentences:
            if sentence.get("sentence_end") and segment != previous:
                raise ProviderError("provider_schema_invalid", "ASR changed an already final sentence.", retryable=True)
            # Late copies of old results must not replace or clear a newer tail.
            return
        if (sentence_id is not None and sentence_id < self._last_sentence_id
                and key not in self._pending_sentences):
            raise ProviderError("provider_schema_invalid", "ASR sentence order regressed.", retryable=True)
        if sentence.get("sentence_end"):
            if sentence_id is not None:
                # A later sentence may finish while an earlier hypothesis is
                # still pending. Keep its final for lossless finish, but never
                # advertise a prefix with a hole or reorder an exposed prefix.
                self._preview_timestamps_valid &= sentence_id == self._preview_last_sentence_id + 1
                self._preview_last_sentence_id = max(self._preview_last_sentence_id, sentence_id)
            elif self._partial_sentence_key is not None and key != self._partial_sentence_key:
                self._preview_timestamps_valid = False
            if segment is not None:
                if (self._preview_timestamps_valid and self.committed and valid_timing
                        and begin < self.committed[-1].end_ms):
                    raise ProviderError("provider_schema_invalid", "ASR final sentence ranges overlap.", retryable=True)
                self.committed.append(segment)
                self._trace_sentence(segment, final=True)
                self._committed_sentence_keys.append(key)
                self._preview_timestamps_valid &= valid_timing and key is not None
                if self._preview_timestamps_valid:
                    self._preview_segment_count = len(self.committed)
                self._preview_revision += 1
            if key is not None:
                self._committed_sentences[key] = segment
                self._pending_sentences.pop(key, None)
            if sentence_id is not None:
                self._last_sentence_id = max(self._last_sentence_id, sentence_id)
            # A delayed earlier sentence_end cannot erase a later sentence's
            # hypothesis (including an empty sentence_begin).
            if (self._partial_sentence_key is None or key == self._partial_sentence_key
                    or sentence_id is None):
                if self._has_unstable_tail:
                    self._preview_revision += 1
                self.partial_segment = None
                self._partial_sentence_key = None
                self._has_unstable_tail = bool(self._pending_sentences)
        else:
            if sentence_id is not None:
                pending = dict(self._pending_sentences)
                pending[key] = segment
                if len(pending) > 64 or sum(len(part.text) for part in pending.values() if part is not None) > 100_000:
                    raise ProviderError("provider_schema_invalid", "ASR unresolved sentences exceeded the safety limit.", retryable=True)
                self._pending_sentences = pending
                # Older hypotheses remain retained, but cannot replace the
                # newest realtime tail or clear its unfinished marker.
                if (self._partial_sentence_key is not None and self._partial_sentence_key[0] == "id"
                        and sentence_id < self._partial_sentence_key[1]):
                    self._preview_revision += 1
                    return
            elif self._partial_sentence_key is not None and key != self._partial_sentence_key:
                # Without an explicit ID there is no proof whether this is a
                # revised start time or another sentence. Preserve the legacy
                # latest hypothesis for finish, but do not certify a preview.
                self._preview_timestamps_valid = False
            if (not self._has_unstable_tail or key != self._partial_sentence_key
                    or segment != self.partial_segment):
                self._preview_revision += 1
            self.partial_segment = segment
            self._partial_sentence_key = key
            self._has_unstable_tail = True

    def _trace_sentence(self, segment: TranscriptSegment, *, final: bool) -> None:
        record_stt_sentence(interview_id=self.request.interview_id, turn_id=self.request.turn_id,
            stream_id=self.stream_id, provider_id=self.provider.provider_id, model=self.context.model,
            text=segment.text, start_ms=segment.start_ms, end_ms=segment.end_ms, final=final)

    def _project_result(self) -> List[StreamingSTTEvent]:
        text, _segments = self._transcript_projection()
        if not text or not self.request.enable_partial:
            return []
        return [self._event("transcript.partial", text=text)]

    def _transcript_projection(self) -> tuple[str, List[TranscriptSegment]]:
        """Project one lossless view for both realtime partial and final.

        DashScope may acknowledge ``task-finished`` while the last sentence is
        still a non-``sentence_end`` hypothesis.  That hypothesis is still the
        provider's latest delivered text and must remain in the authoritative
        final whenever earlier committed sentences exist.
        """

        entries = list(zip(self._committed_sentence_keys, self.committed))
        entries.extend((key, segment) for key, segment in self._pending_sentences.items() if segment is not None)
        if (self.partial_segment is not None and self.partial_segment.text.strip()
                and self._partial_sentence_key not in self._pending_sentences):
            entries.append((self._partial_sentence_key, self.partial_segment))
        if entries and all(key is not None and key[0] == "id" for key, _ in entries):
            entries.sort(key=lambda entry: entry[0][1])
        segments = [segment for _, segment in entries]
        return "".join(item.text for item in segments).strip(), segments

    def _event(self, event_type: str, *, text: str = "", is_final: bool = False, segments: Optional[List[TranscriptSegment]] = None) -> StreamingSTTEvent:
        event = StreamingSTTEvent(
            stream_id=self.stream_id,
            sequence=self.sequence,
            type=event_type,
            text=text,
            language=self.request.language,
            confidence=1.0 if text else 0.0,
            segments=segments or [],
            is_final=is_final,
            provider=ProviderMeta(provider_id=self.provider.provider_id, model=self.context.model, request_id=self.request_id, latency_ms=0),
        )
        self.sequence += 1
        return event


def _consume_cleanup_result(task: asyncio.Task) -> None:
    """Detached cleanup is bounded and must not emit unhandled task warnings."""
    if not task.cancelled():
        task.exception()


def _abort_socket_transport(socket: Any) -> bool:
    """Both current and legacy websockets expose their asyncio transport."""
    abort = getattr(getattr(socket, "transport", None), "abort", None)
    if not callable(abort):
        return False
    try:
        abort()
        return True
    except Exception:
        return False


async def _close_aborted_socket(socket: Any, timeout_s: float, *, force_abort: Any = None) -> None:
    """Try a short close handshake; force-release before cancelling any wait.

    Abort may use this immediately; formal finish uses it only after draining
    accepted audio and receiving the authoritative final transcript.
    """
    abort_transport = force_abort or (lambda: _abort_socket_transport(socket))
    transport_aborted = False

    def force() -> bool:
        nonlocal transport_aborted
        if not transport_aborted:
            transport_aborted = bool(abort_transport())
        return transport_aborted
    close_task = asyncio.create_task(socket.close())
    tasks = [close_task]
    gracefully_closed = False
    try:
        done, _ = await asyncio.wait(tasks, timeout=max(0.01, min(0.5, timeout_s)))
        if done:
            try:
                close_task.result()
                gracefully_closed = True
                return
            except (Exception, asyncio.CancelledError):
                pass
        forced = force()
        close_task.cancel()
        wait_closed = getattr(socket, "wait_closed", None)
        if callable(wait_closed):
            tasks.append(asyncio.create_task(wait_closed()))
        await asyncio.wait(tasks, timeout=0.1)
        if not forced:
            raise ProviderError(
                "provider_stream_close_failed",
                "STT socket could not be released after its close deadline.",
                retryable=True,
            )
    except asyncio.CancelledError:
        force()
        raise
    finally:
        if not gracefully_closed and not close_task.done():
            # Releasing the OS transport precedes task cancellation, even if
            # cleanup itself is interrupted during process shutdown.
            force()
        for task in tasks:
            if not task.done():
                task.cancel()
            task.add_done_callback(_consume_cleanup_result)


def _asr_batch_endpoint(config: Dict[str, Any]) -> str:
    configured = str(config.get("asr_batch_endpoint") or "").strip()
    if configured:
        return configured
    workspace_id = str(config.get("workspace_id") or "").strip()
    if workspace_id:
        return "https://%s.%s.maas.aliyuncs.com/compatible-mode/v1/chat/completions" % (
            workspace_id, _workspace_region(config)
        )
    base_url = str(config.get("base_url") or "").rstrip("/")
    if not base_url.endswith("/compatible-mode/v1"):
        raise ProviderError("provider_bad_request", "DashScope ASR requires a workspace compatible-mode base_url.", retryable=False)
    return "%s/chat/completions" % base_url


def _asr_stream_endpoint(config: Dict[str, Any]) -> str:
    configured = str(config.get("asr_websocket_url") or "").strip()
    if configured:
        parsed = urlparse(configured)
        if parsed.scheme != "wss" or not parsed.hostname or parsed.username or parsed.password:
            raise ProviderError("provider_bad_request", "DashScope ASR WebSocket URL must be secure.", retryable=False)
        return configured
    workspace_id = str(config.get("workspace_id") or "").strip()
    if workspace_id:
        return "wss://%s.%s.maas.aliyuncs.com/api-ws/v1/inference" % (
            workspace_id, _workspace_region(config)
        )
    base_url = str(config.get("base_url") or "").rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ProviderError("provider_bad_request", "DashScope base_url must be HTTPS.", retryable=False)
    return "wss://%s/api-ws/v1/inference" % parsed.hostname


def _dialogue_endpoint(config: Dict[str, Any]) -> str:
    configured = str(config.get("realtime_websocket_url") or "").strip()
    if configured:
        parsed = urlparse(configured)
        if parsed.scheme != "wss" or not parsed.hostname or parsed.username or parsed.password:
            raise ProviderError(
                "provider_bad_request",
                "DashScope Realtime WebSocket URL must be secure.",
                retryable=False,
            )
        return configured
    workspace_id = str(config.get("workspace_id") or "").strip()
    if not workspace_id:
        raise ProviderError(
            "provider_bad_request",
            "DashScope Realtime requires workspace_id or realtime_websocket_url.",
            retryable=False,
        )
    return "wss://%s.%s.maas.aliyuncs.com/api-ws/v1/realtime" % (
        workspace_id,
        _workspace_region(config),
    )


def _workspace_region(config: Dict[str, Any]) -> str:
    region = str(config.get("workspace_region") or "cn-beijing").strip()
    if region not in {"cn-beijing", "ap-southeast-1"}:
        raise ProviderError("provider_bad_request", "DashScope workspace_region is not supported.", retryable=False)
    return region


def _stream_audio_format(content_type: str) -> str:
    normalized = content_type.split(";", 1)[0].lower()
    result = {
        "audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3", "audio/mp3": "mp3",
        "audio/ogg": "opus", "audio/opus": "opus", "audio/aac": "aac", "audio/pcm": "pcm",
    }.get(normalized)
    if not result:
        raise ProviderError(
            "provider_audio_format_unsupported",
            "DashScope real-time ASR requires PCM/WAV/MP3/Ogg-Opus/AAC; WebM must use batch repair or browser PCM capture.",
            retryable=False,
        )
    return result


def _bounded_stream_audio_packet_ms(config: Dict[str, Any]) -> float:
    try:
        configured = float(config.get("stream_audio_packet_ms", 100))
    except (TypeError, ValueError):
        configured = 100.0
    return max(20.0, min(200.0, configured))


def _bounded_stream_send_backpressure_seconds(config: Dict[str, Any]) -> float:
    try:
        configured = float(config.get("stream_send_backpressure_seconds", 5))
    except (TypeError, ValueError):
        configured = 5.0
    return max(0.25, min(30.0, configured))


def _is_unexpected_keyword(exc: TypeError, name: str) -> bool:
    message = str(exc)
    return (
        "unexpected keyword argument" in message
        and "'%s'" % name in message
    )


def _language_hint(language: str) -> str:
    normalized = language.lower()
    if normalized.startswith("zh"):
        return "zh"
    if normalized.startswith("en"):
        return "en"
    return normalized.split("-", 1)[0]


def _tts_endpoint(config: Dict[str, Any], model: str) -> str:
    configured = str(config.get("tts_endpoint") or "").rstrip("/")
    if configured:
        return configured
    base_url = str(config.get("base_url") or "").rstrip("/")
    suffix = "/compatible-mode/v1"
    if not base_url.endswith(suffix):
        raise ProviderError(
            "provider_bad_request",
            "DashScope tts_endpoint is required when base_url is not an official compatible-mode URL.",
            retryable=False,
        )
    native_root = "%s/api/v1" % base_url[: -len(suffix)]
    if _uses_speech_synthesizer(model, ""):
        return "%s/services/audio/tts/SpeechSynthesizer" % native_root
    return "%s/services/aigc/multimodal-generation/generation" % native_root


def _uses_speech_synthesizer(model: str, endpoint: str) -> bool:
    normalized = model.lower()
    return "SpeechSynthesizer" in endpoint or normalized.startswith(("cosyvoice", "qwen-audio-"))


def _default_voice(model: str) -> str:
    normalized = model.lower()
    if normalized.startswith("cosyvoice"):
        return "longanyang"
    if normalized.startswith("qwen-audio-"):
        return "longanhuan_v3.6"
    return "Cherry"


def _dialogue_voice(model: str, configured: str) -> str:
    value = configured.strip()
    normalized = model.lower()
    if normalized.startswith("qwen3.5-omni-") and value in {"", "default", "Cherry"}:
        return "Tina"
    if normalized.startswith("qwen-audio-3.0-realtime-") and value in {
        "",
        "default",
        "Cherry",
    }:
        return "longanqian"
    return value or "Cherry"


def _language_type(language: str) -> str:
    normalized = language.lower()
    if normalized.startswith("zh"):
        return "Chinese"
    if normalized.startswith("en"):
        return "English"
    return language


def _dashscope_format(content_type: str) -> tuple[str, str]:
    normalized = content_type.split(";", 1)[0].strip().lower()
    formats = {
        "audio/wav": ("wav", "audio/wav"),
        "audio/x-wav": ("wav", "audio/wav"),
        "audio/mpeg": ("mp3", "audio/mpeg"),
        "audio/mp3": ("mp3", "audio/mpeg"),
        "audio/pcm": ("pcm", "audio/pcm"),
        "audio/ogg": ("opus", "audio/ogg"),
    }
    resolved: Optional[tuple[str, str]] = formats.get(normalized)
    if resolved is None:
        raise ProviderError(
            "provider_audio_format_unsupported",
            "DashScope TTS does not support the requested basic HTTP audio format.",
            retryable=False,
        )
    return resolved
