import asyncio
import base64
import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlparse

import websockets

from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    BatchSTTRequest,
    BatchSTTResponse,
    ProviderContext,
    ProviderMeta,
    RealtimeSpeechDialogueRequest,
    StreamingSTTEvent,
    StreamingSTTRequest,
    TTSSynthesizeRequest,
    TTSSynthesizeResponse,
    TranscriptSegment,
)
from app.providers.openai_compatible.provider import OpenAICompatibleProvider
from app.providers.realtime_speech import OpenAIStyleRealtimeSpeechStream


class DashScopeProvider(OpenAICompatibleProvider):
    """DashScope adapter for Qwen LLM/TTS and authoritative Chinese ASR."""

    provider_id = "dashscope"

    def __init__(self, client_factory: Any = None, websocket_connect: Any = None) -> None:
        super().__init__(client_factory=client_factory) if client_factory is not None else super().__init__()
        self.websocket_connect = websocket_connect or websockets.connect

    async def invoke(self, capability: str, request: Any, context: ProviderContext) -> Any:
        if capability == cap.STT_BATCH and isinstance(request, BatchSTTRequest):
            return await self._transcribe_batch(request, context)
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
        return await super().invoke(capability, request, context)

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
        self.committed: List[TranscriptSegment] = []
        self.partial_text = ""
        self.request_id = ""
        self.ready_events = [self._event("stream.ready")]
        # 厂商下行事件必须由独立任务持续接收。候选人上行每 20ms 一帧，
        # 不能在 send_audio 中为每一帧等待 WebSocket 下行，否则会把
        # LiveKit 的两秒证据缓冲耗尽并触发失败关闭。
        self._reader_queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._reader_task: Optional[asyncio.Task] = None
        self._reader_error: Optional[Exception] = None
        self._task_finished = False
        # WebSocket send 可能偶发阻塞几十毫秒。用独立发送任务承接 PCM，
        # 并用“最多两秒原始音频字节”限制内存；LiveKit 收帧路径只入队。
        self._sender_queue: asyncio.Queue = asyncio.Queue()
        self._sender_task: Optional[asyncio.Task] = None
        self._sender_error: Optional[Exception] = None
        self._pending_audio_bytes = 0
        backpressure_seconds = max(
            0.25,
            float(context.config.get("stream_send_backpressure_seconds", 2.0)),
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

    @classmethod
    async def open(cls, provider: DashScopeProvider, request: StreamingSTTRequest, context: ProviderContext) -> "DashScopeSTTStream":
        audio_format = _stream_audio_format(request.audio.content_type)
        url = _asr_stream_endpoint(context.config)
        headers = {"Authorization": "Bearer %s" % _api_key(context.credentials), "User-Agent": "Interviewer/0.1"}
        workspace_id = str(context.config.get("workspace_id") or "").strip()
        if workspace_id:
            headers["X-DashScope-WorkSpace"] = workspace_id
        try:
            socket = await provider.websocket_connect(
                url,
                additional_headers=headers,
                open_timeout=max(1, float(context.timeout_s)),
                max_size=int(context.config.get("websocket_max_message_bytes", 4 * 1024 * 1024)),
            )
        except TypeError:
            # Injectable test clients and websockets<14 use extra_headers.
            socket = await provider.websocket_connect(url, extra_headers=headers)
        except asyncio.TimeoutError as exc:
            raise ProviderError("provider_stream_open_failed", "DashScope ASR WebSocket timed out.", retryable=True) from exc
        except Exception as exc:
            raise ProviderError("provider_stream_open_failed", "DashScope ASR WebSocket could not be opened.", retryable=True) from exc
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
        await socket.send(json.dumps({
            "header": {"action": "run-task", "task_id": stream.task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio", "task": "asr", "function": "recognition", "model": context.model,
                "parameters": parameters, "input": {},
            },
        }, ensure_ascii=False))
        first = await stream._receive_one(timeout=max(1, float(context.timeout_s)))
        if first != "task-started":
            await stream.abort()
            raise ProviderError("provider_stream_open_failed", "DashScope ASR did not acknowledge task start.", retryable=True)
        stream._reader_task = asyncio.create_task(stream._read_vendor_events())
        stream._sender_task = asyncio.create_task(stream._send_audio_frames())
        return stream

    async def send_audio(self, chunk: bytes) -> List[StreamingSTTEvent]:
        if self.closed:
            raise ProviderError("provider_stream_closed", "DashScope ASR stream is already closed.", retryable=False)
        self._raise_reader_error()
        self._raise_sender_error()
        pending = self._pending_audio_bytes + len(chunk)
        if pending > self._max_pending_audio_bytes:
            raise ProviderError(
                "provider_backpressure_exceeded",
                "DashScope ASR audio send backlog exceeded the two-second safety budget.",
                retryable=True,
            )
        self._pending_audio_bytes = pending
        self._sender_drained.clear()
        self._sender_queue.put_nowait(chunk)
        # 不在持有 Evidence 锁的收帧路径中主动等待调度或网络；后台发送、
        # 接收任务会在下一次事件循环机会运行，partial 由后续帧或 finish 排出。
        self._raise_sender_error()
        events = self._drain_available()
        if self._task_finished:
            raise ProviderError("provider_stream_closed", "DashScope ASR stream ended before finish was requested.", retryable=True)
        return events

    async def finish(self) -> List[StreamingSTTEvent]:
        if self.closed:
            return []
        try:
            self._raise_reader_error()
            self._raise_sender_error()
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
            text = "".join(item.text for item in self.committed).strip() or self.partial_text.strip()
            if not text:
                raise ProviderError("provider_final_transcript_missing", "DashScope ASR returned no final transcript.", retryable=True)
            events.append(self._event("transcript.final", text=text, is_final=True, segments=self.committed or [TranscriptSegment(text=text)]))
            events.append(self._event("stream.closed"))
            self.closed = True
            await self._stop_sender()
            await self._stop_reader()
            await self.socket.close()
            return events
        except Exception:
            await self.abort()
            raise

    async def abort(self) -> None:
        if not self.closed:
            self.closed = True
            await self._stop_sender()
            await self._stop_reader()
            await self.socket.close()

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
                    if self._pending_audio_bytes == 0:
                        self._sender_drained.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._sender_error = exc
            self._sender_drained.set()

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

    async def _stop_reader(self) -> None:
        task = self._reader_task
        self._reader_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _stop_sender(self) -> None:
        task = self._sender_task
        self._sender_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
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
            if not sentence.get("heartbeat"):
                text = str(sentence.get("text") or "")
                self.partial_text = text
                if sentence.get("sentence_end") and text.strip():
                    self.committed.append(TranscriptSegment(
                        text=text.strip(),
                        start_ms=max(0, int(sentence.get("begin_time") or 0)),
                        end_ms=max(0, int(sentence.get("end_time") or 0)),
                        confidence=1.0,
                    ))
                    self.partial_text = ""
        return event

    def _project_result(self) -> List[StreamingSTTEvent]:
        text = ("".join(item.text for item in self.committed) + self.partial_text).strip()
        if not text or not self.request.enable_partial:
            return []
        return [self._event("transcript.partial", text=text)]

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
