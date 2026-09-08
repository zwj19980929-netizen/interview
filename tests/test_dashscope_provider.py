import json
import asyncio
import base64

import httpx
import pytest

from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    ChatJSONRequest,
    ChatMessage,
    BatchSTTRequest,
    ProviderContext,
    RealtimeSpeechDialogueRequest,
    RealtimeSpeechResponseCommand,
    StreamingAudioConfig,
    StreamingSTTRequest,
    TTSSynthesizeRequest,
)
from app.providers.dashscope.provider import DashScopeProvider


def make_provider(handler):
    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=transport, **kwargs)

    return DashScopeProvider(client_factory=client_factory)


def context(capability: str, model: str, *, config=None) -> ProviderContext:
    return ProviderContext(
        organization_id="org_default",
        invocation_id="invocation_test",
        route_id="route_test",
        provider_connection_id="provider_conn_dashscope",
        model_configuration_id="model_cfg_dashscope",
        model_type="tts" if capability == cap.TTS_SYNTHESIZE else "realtime_speech" if capability == cap.SPEECH_DIALOGUE_REALTIME else "stt" if capability.startswith("stt.") else "llm",
        capability=capability,
        purpose="provider_test",
        model=model,
        timeout_s=5,
        attempt=1,
        fallback_index=0,
        connection_config=config or {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
        credentials={"api_key": "dashscope-key"},
    )


@pytest.mark.anyio
async def test_dashscope_qwen_chat_uses_openai_compatible_contract() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "qwen_req_1",
                "choices": [{"message": {"content": json.dumps({"score": 90})}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
            },
        )

    response = await make_provider(handler).invoke(
        cap.LLM_CHAT_JSON,
        ChatJSONRequest(
            purpose="answer_evaluation",
            messages=[ChatMessage(role="user", content="请按 JSON 输出")],
            json_schema={
                "type": "object",
                "required": ["score"],
                "properties": {"score": {"type": "integer"}},
            },
        ),
        context(cap.LLM_CHAT_JSON, "qwen-plus"),
    )

    assert seen["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert seen["authorization"] == "Bearer dashscope-key"
    assert seen["payload"]["response_format"]["type"] == "json_schema"
    assert response.data == {"score": 90}
    assert response.provider.provider_id == "dashscope"


@pytest.mark.anyio
async def test_dashscope_wire_subset_keeps_original_gateway_uniqueness_validation() -> None:
    from app.model_gateway.gateway import ModelGateway
    from app.repositories.memory import InMemoryStore
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ids":["E1","E1"]}'}}], "usage": {},
        })

    request = ChatJSONRequest(purpose="interview_turn_understanding", messages=[ChatMessage(role="user", content="JSON")], json_schema={
        "type": "object", "required": ["ids"], "additionalProperties": False,
        "properties": {"ids": {"type": "array", "uniqueItems": True, "maxItems": 2, "items": {"type": "string", "enum": ["E1"]}}},
    })
    response = await make_provider(handler).invoke(cap.LLM_CHAT_JSON, request, context(cap.LLM_CHAT_JSON, "qwen3.7-plus"))
    wire = seen["response_format"]["json_schema"]["schema"]["properties"]["ids"]
    assert "uniqueItems" not in wire
    assert wire["maxItems"] == 2 and wire["items"]["enum"] == ["E1"]
    assert request.json_schema["properties"]["ids"]["uniqueItems"] is True
    with pytest.raises(ProviderError) as rejected:
        ModelGateway(InMemoryStore())._validate_response(cap.LLM_CHAT_JSON, request, response)
    assert rejected.value.code == "provider_schema_invalid"


@pytest.mark.anyio
async def test_dashscope_realtime_understanding_disables_qwen_thinking_by_default() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "qwen_realtime_understanding",
                "choices": [{"message": {"content": json.dumps({"intent": "answer"})}}],
                "usage": {},
            },
        )

    request = ChatJSONRequest(
        purpose="interview_turn_understanding",
        messages=[ChatMessage(role="user", content="理解本轮回答")],
        json_schema={
            "type": "object",
            "required": ["intent"],
            "properties": {"intent": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    await make_provider(handler).invoke(
        cap.LLM_CHAT_JSON,
        request,
        context(cap.LLM_CHAT_JSON, "qwen3.7-plus").model_copy(
            update={"purpose": "interview_turn_understanding"}
        ),
    )

    assert seen["payload"]["enable_thinking"] is False


@pytest.mark.anyio
async def test_dashscope_qwen_tts_returns_expiring_asset_url_for_private_copy() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "request_id": "dash_tts_1",
                "output": {
                    "finish_reason": "stop",
                    "audio": {
                        "url": "http://dashscope-result.example.com/audio.wav?signature=test",
                        "id": "audio_1",
                        "expires_at": 1772697707,
                    },
                },
                "usage": {"characters": 8},
            },
        )

    response = await make_provider(handler).invoke(
        cap.TTS_SYNTHESIZE,
        TTSSynthesizeRequest(
            purpose="question_speech_generation",
            text="请介绍一个项目",
            voice_profile_id="voice_default_cn",
            format="audio/wav",
        ),
        context(cap.TTS_SYNTHESIZE, "qwen3-tts-flash"),
    )

    assert seen["url"] == (
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
        "multimodal-generation/generation"
    )
    assert seen["payload"] == {
        "model": "qwen3-tts-flash",
        "input": {
            "text": "请介绍一个项目",
            "voice": "Cherry",
            "language_type": "Chinese",
        },
    }
    assert response.audio_uri.startswith("https://dashscope-result.example.com/")
    assert response.content_type == "audio/wav"
    assert response.provider.request_id == "dash_tts_1"


@pytest.mark.anyio
async def test_dashscope_cosyvoice_uses_speech_synthesizer_contract() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "request_id": "dash_tts_2",
                "output": {
                    "finish_reason": "stop",
                    "audio": {"url": "https://dashscope-result.example.com/cosy.wav"},
                },
            },
        )

    response = await make_provider(handler).invoke(
        cap.TTS_SYNTHESIZE,
        TTSSynthesizeRequest(
            purpose="question_speech_generation",
            text="请解释事务隔离级别",
            voice_profile_id="voice_default_cn",
            format="audio/wav",
            speaking_rate=1.25,
        ),
        context(
            cap.TTS_SYNTHESIZE,
            "cosyvoice-v3-flash",
            config={
                "base_url": "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                "sample_rate_hz": 24000,
            },
        ),
    )

    assert seen["url"].endswith("/api/v1/services/audio/tts/SpeechSynthesizer")
    assert seen["payload"]["input"] == {
        "text": "请解释事务隔离级别",
        "voice": "longanyang",
        "format": "wav",
        "sample_rate": 24000,
        "rate": 1.25,
    }
    assert response.audio_uri.endswith("/cosy.wav")


@pytest.mark.anyio
async def test_dashscope_batch_asr_sends_private_audio_as_data_url() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"id": "asr_1", "choices": [{"message": {"content": "欢迎使用实时面试。"}}]})

    response = await make_provider(handler).invoke(
        cap.STT_BATCH,
        BatchSTTRequest(audio_uri="private-media://answer.wav", content_type="audio/wav", audio_bytes=b"RIFF-test"),
        context(
            cap.STT_BATCH,
            "qwen3-asr-flash",
            config={"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "workspace_id": "ws123"},
        ),
    )

    assert seen["url"] == "https://ws123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert seen["payload"]["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:audio/wav;base64,")
    assert response.text == "欢迎使用实时面试。"
    assert response.provider.request_id == "asr_1"


class FakeDashScopeSocket:
    def __init__(self) -> None:
        self.received = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.received.put_nowait(json.dumps({"header": {"event": "task-started", "task_id": "vendor_task"}, "payload": {}}))

    async def send(self, value):
        self.sent.append(value)
        if isinstance(value, bytes):
            self.received.put_nowait(json.dumps({
                "header": {"event": "result-generated", "task_id": "vendor_task"},
                "payload": {"output": {"sentence": {"text": "实时转", "sentence_end": False, "begin_time": 0}}},
            }))
        elif json.loads(value)["header"]["action"] == "finish-task":
            self.received.put_nowait(json.dumps({
                "header": {"event": "result-generated", "task_id": "vendor_task"},
                "payload": {"output": {"sentence": {"text": "实时转写完成。", "sentence_end": True, "begin_time": 0, "end_time": 800}}},
            }))
            self.received.put_nowait(json.dumps({"header": {"event": "task-finished", "task_id": "vendor_task"}, "payload": {}}))

    async def recv(self):
        return await self.received.get()

    async def close(self):
        self.closed = True


class QuietDashScopeSocket(FakeDashScopeSocket):
    """模拟长时间没有 partial 的真实 ASR，确保上行不会逐帧等待下行。"""

    def __init__(self) -> None:
        super().__init__()
        self.recv_calls = 0

    async def send(self, value):
        self.sent.append(value)
        if not isinstance(value, bytes) and json.loads(value)["header"]["action"] == "finish-task":
            self.received.put_nowait(json.dumps({
                "header": {"event": "result-generated", "task_id": "vendor_task"},
                "payload": {"output": {"sentence": {
                    "text": "没有逐帧阻塞。", "sentence_end": True, "begin_time": 0, "end_time": 1200,
                }}},
            }))
            self.received.put_nowait(json.dumps({"header": {"event": "task-finished", "task_id": "vendor_task"}, "payload": {}}))

    async def recv(self):
        self.recv_calls += 1
        return await self.received.get()


class BlockingSendDashScopeSocket(QuietDashScopeSocket):
    """模拟厂商 WebSocket 上行短暂阻塞，验证 LiveKit 收帧不会被连带卡住。"""

    def __init__(self) -> None:
        super().__init__()
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()

    async def send(self, value):
        self.sent.append(value)
        if isinstance(value, bytes):
            self.send_started.set()
            await self.release_send.wait()
            return
        if json.loads(value)["header"]["action"] == "finish-task":
            self.received.put_nowait(json.dumps({
                "header": {"event": "result-generated", "task_id": "vendor_task"},
                "payload": {"output": {"sentence": {
                    "text": "发送恢复后完成。", "sentence_end": True, "begin_time": 0, "end_time": 900,
                }}},
            }))
            self.received.put_nowait(json.dumps({"header": {"event": "task-finished", "task_id": "vendor_task"}, "payload": {}}))


class UncommittedTailDashScopeSocket(FakeDashScopeSocket):
    """The vendor can finish while its last delivered sentence is still partial."""

    def __init__(self) -> None:
        super().__init__()
        self.result_sent = False

    async def send(self, value):
        self.sent.append(value)
        if isinstance(value, bytes) and not self.result_sent:
            self.result_sent = True
            self.received.put_nowait(json.dumps({
                "header": {"event": "result-generated", "task_id": "vendor_task"},
                "payload": {"output": {"sentence": {
                    "text": "前面已经确认。", "sentence_end": True,
                    "begin_time": 0, "end_time": 800,
                }}},
            }))
            self.received.put_nowait(json.dumps({
                "header": {"event": "result-generated", "task_id": "vendor_task"},
                "payload": {"output": {"sentence": {
                    "text": "最后一截也必须保留", "sentence_end": False,
                    "begin_time": 820, "end_time": 1500,
                }}},
            }))
            return
        if not isinstance(value, bytes) and json.loads(value)["header"]["action"] == "finish-task":
            self.received.put_nowait(json.dumps({
                "header": {"event": "task-finished", "task_id": "vendor_task"},
                "payload": {},
            }))


@pytest.mark.anyio
async def test_dashscope_stream_maps_duplex_events_to_one_authoritative_final() -> None:
    socket = FakeDashScopeSocket()

    async def connect(url, **kwargs):
        assert url == "wss://ws123.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
        assert kwargs["additional_headers"]["Authorization"] == "Bearer dashscope-key"
        assert kwargs["proxy"] is None
        return socket

    provider = DashScopeProvider(websocket_connect=connect)
    stream = await provider.open_stream(
        StreamingSTTRequest(
            interview_id="iv_1", turn_id="turn_1",
            audio=StreamingAudioConfig(content_type="audio/pcm", sample_rate_hz=16000, channels=1),
        ),
        context(
            cap.STT_STREAMING,
            "qwen-audio-3.0-asr-flash-streaming",
            config={"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "workspace_id": "ws123"},
        ),
    )
    # Two 50 ms chunks form one default 100 ms vendor packet.
    assert await stream.send_audio(b"\x00\x01" * 800) == []
    assert await stream.send_audio(b"\x00\x02" * 800) == []
    partial = []
    for _ in range(5):
        await asyncio.sleep(0)
        partial.extend(await stream.send_audio(b""))
        if partial:
            break
    finished = await stream.finish()

    assert partial[0].type == "transcript.partial"
    assert [item.type for item in finished].count("transcript.final") == 1
    assert next(item for item in finished if item.type == "transcript.final").text == "实时转写完成。"
    assert socket.closed is True


@pytest.mark.anyio
async def test_dashscope_stream_preserves_uncommitted_tail_in_authoritative_final() -> None:
    socket = UncommittedTailDashScopeSocket()

    async def connect(url, **kwargs):
        return socket

    stream = await DashScopeProvider(websocket_connect=connect).open_stream(
        StreamingSTTRequest(
            interview_id="iv_1",
            turn_id="turn_1",
            audio=StreamingAudioConfig(
                content_type="audio/pcm", sample_rate_hz=16000, channels=1
            ),
        ),
        context(cap.STT_STREAMING, "qwen-audio-3.0-asr-flash-streaming"),
    )
    for _ in range(5):
        assert await stream.send_audio(b"\x00\x01" * 320) == []

    events = await stream.finish()

    final = next(item for item in events if item.type == "transcript.final")
    assert final.text == "前面已经确认。最后一截也必须保留"
    assert [segment.text for segment in final.segments] == [
        "前面已经确认。",
        "最后一截也必须保留",
    ]
    assert [item.type for item in events].count("transcript.final") == 1
    assert socket.closed is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("use_environment_proxy", "expected_proxy"),
    [(False, None), (True, True)],
)
async def test_dashscope_stream_honors_environment_proxy_configuration(
    use_environment_proxy: bool,
    expected_proxy,
) -> None:
    socket = QuietDashScopeSocket()
    seen = {}

    async def connect(url, **kwargs):
        seen.update(kwargs)
        return socket

    stream = await DashScopeProvider(websocket_connect=connect).open_stream(
        StreamingSTTRequest(
            interview_id="iv_1",
            turn_id="turn_1",
            audio=StreamingAudioConfig(
                content_type="audio/pcm", sample_rate_hz=16000, channels=1
            ),
        ),
        context(
            cap.STT_STREAMING,
            "qwen-audio-3.0-asr-flash-streaming",
            config={
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "use_environment_proxy": use_environment_proxy,
            },
        ),
    )

    assert seen["proxy"] is expected_proxy
    assert seen["additional_headers"]["Authorization"] == "Bearer dashscope-key"
    await stream.abort()


@pytest.mark.anyio
async def test_dashscope_stream_keeps_legacy_websocket_header_compatibility() -> None:
    socket = QuietDashScopeSocket()
    seen = {}

    async def connect(url, *, extra_headers):
        seen["url"] = url
        seen["headers"] = extra_headers
        return socket

    stream = await DashScopeProvider(websocket_connect=connect).open_stream(
        StreamingSTTRequest(
            interview_id="iv_1",
            turn_id="turn_1",
            audio=StreamingAudioConfig(
                content_type="audio/pcm", sample_rate_hz=16000, channels=1
            ),
        ),
        context(cap.STT_STREAMING, "qwen-audio-3.0-asr-flash-streaming"),
    )

    assert seen["url"].startswith("wss://")
    assert seen["headers"]["Authorization"] == "Bearer dashscope-key"
    await stream.abort()


@pytest.mark.anyio
async def test_dashscope_stream_supports_pre_proxy_additional_headers_signature() -> None:
    socket = QuietDashScopeSocket()
    seen = {}

    async def connect(url, *, additional_headers, open_timeout, max_size):
        seen["url"] = url
        seen["headers"] = additional_headers
        seen["open_timeout"] = open_timeout
        seen["max_size"] = max_size
        return socket

    stream = await DashScopeProvider(websocket_connect=connect).open_stream(
        StreamingSTTRequest(
            interview_id="iv_1",
            turn_id="turn_1",
            audio=StreamingAudioConfig(
                content_type="audio/pcm", sample_rate_hz=16000, channels=1
            ),
        ),
        context(cap.STT_STREAMING, "qwen-audio-3.0-asr-flash-streaming"),
    )

    assert seen["url"].startswith("wss://")
    assert seen["headers"]["Authorization"] == "Bearer dashscope-key"
    assert seen["open_timeout"] == 5
    assert seen["max_size"] == 4 * 1024 * 1024
    await stream.abort()


@pytest.mark.anyio
async def test_dashscope_stream_does_not_retry_internal_type_error_as_signature_fallback() -> None:
    attempts = 0

    async def connect(url, **kwargs):
        nonlocal attempts
        attempts += 1
        raise TypeError("handshake parser failed internally")

    with pytest.raises(ProviderError) as captured:
        await DashScopeProvider(websocket_connect=connect).open_stream(
            StreamingSTTRequest(
                interview_id="iv_1",
                turn_id="turn_1",
                audio=StreamingAudioConfig(
                    content_type="audio/pcm", sample_rate_hz=16000, channels=1
                ),
            ),
            context(cap.STT_STREAMING, "qwen-audio-3.0-asr-flash-streaming"),
        )

    assert attempts == 1
    assert captured.value.code == "provider_stream_open_failed"


@pytest.mark.anyio
async def test_dashscope_stream_batches_pcm_and_flushes_remainder_on_finish() -> None:
    socket = QuietDashScopeSocket()

    async def connect(url, **kwargs):
        return socket

    stream = await DashScopeProvider(websocket_connect=connect).open_stream(
        StreamingSTTRequest(
            interview_id="iv_1",
            turn_id="turn_1",
            audio=StreamingAudioConfig(
                content_type="audio/pcm", sample_rate_hz=16000, channels=1
            ),
        ),
        context(cap.STT_STREAMING, "qwen-audio-3.0-asr-flash-streaming"),
    )

    frames = [bytes([index]) * 640 for index in range(1, 7)]
    for frame in frames:
        assert await stream.send_audio(frame) == []
    finished = await stream.finish()

    binary_packets = [value for value in socket.sent if isinstance(value, bytes)]
    assert binary_packets == [b"".join(frames[:5]), frames[5]]
    assert [len(value) for value in binary_packets] == [3200, 640]
    assert next(item for item in finished if item.type == "transcript.final").text == "没有逐帧阻塞。"
    assert socket.closed is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("configured_packet_ms", "expected_packet_bytes"),
    [(40, 1280), (1, 640), (500, 6400), ("invalid", 3200)],
)
async def test_dashscope_stream_pcm_packet_configuration_is_bounded(
    configured_packet_ms,
    expected_packet_bytes: int,
) -> None:
    socket = QuietDashScopeSocket()

    async def connect(url, **kwargs):
        return socket

    stream = await DashScopeProvider(websocket_connect=connect).open_stream(
        StreamingSTTRequest(
            interview_id="iv_1",
            turn_id="turn_1",
            audio=StreamingAudioConfig(
                content_type="audio/pcm", sample_rate_hz=16000, channels=1
            ),
        ),
        context(
            cap.STT_STREAMING,
            "qwen-audio-3.0-asr-flash-streaming",
            config={
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "stream_audio_packet_ms": configured_packet_ms,
            },
        ),
    )

    assert stream._audio_send_target_bytes == expected_packet_bytes
    await stream.abort()


@pytest.mark.anyio
async def test_dashscope_stream_does_not_wait_for_vendor_receive_on_every_audio_frame() -> None:
    socket = QuietDashScopeSocket()

    async def connect(url, **kwargs):
        return socket

    provider = DashScopeProvider(websocket_connect=connect)
    stream = await provider.open_stream(
        StreamingSTTRequest(
            interview_id="iv_1", turn_id="turn_1",
            audio=StreamingAudioConfig(content_type="audio/pcm", sample_rate_hz=16000, channels=1),
        ),
        context(cap.STT_STREAMING, "qwen-audio-3.0-asr-flash-streaming"),
    )

    for _ in range(80):
        assert await stream.send_audio(b"\x00\x00" * 320) == []

    # 一次握手接收 + 一个后台阻塞接收；不能退化为 80 次按帧 recv/timeout。
    assert socket.recv_calls <= 3
    finished = await stream.finish()
    assert next(item for item in finished if item.type == "transcript.final").text == "没有逐帧阻塞。"
    assert socket.closed is True


@pytest.mark.anyio
async def test_dashscope_stream_queues_audio_while_vendor_send_is_temporarily_blocked() -> None:
    socket = BlockingSendDashScopeSocket()

    async def connect(url, **kwargs):
        return socket

    stream = await DashScopeProvider(websocket_connect=connect).open_stream(
        StreamingSTTRequest(
            interview_id="iv_1", turn_id="turn_1",
            audio=StreamingAudioConfig(content_type="audio/pcm", sample_rate_hz=16000, channels=1),
        ),
        context(cap.STT_STREAMING, "qwen-audio-3.0-asr-flash-streaming"),
    )

    # send_audio 只进入有界发送队列，即使底层 socket.send 正在等待也必须立即返回。
    for _ in range(5):
        assert await asyncio.wait_for(
            stream.send_audio(b"\x00\x00" * 320), timeout=0.05
        ) == []
    await asyncio.wait_for(socket.send_started.wait(), timeout=0.05)
    # 125 x 20 ms = 2.5 s. This exceeded the old default two-second budget;
    # the five-second bounded default must absorb it without dropping audio.
    for _ in range(120):
        assert await stream.send_audio(b"\x00\x00" * 320) == []
    assert stream._max_pending_audio_bytes == 160_000

    finishing = asyncio.create_task(stream.finish())
    await asyncio.sleep(0)
    assert finishing.done() is False
    socket.release_send.set()
    finished = await asyncio.wait_for(finishing, timeout=1)
    assert next(item for item in finished if item.type == "transcript.final").text == "发送恢复后完成。"


@pytest.mark.anyio
async def test_dashscope_stream_abort_discards_buffered_and_queued_pcm() -> None:
    socket = BlockingSendDashScopeSocket()

    async def connect(url, **kwargs):
        return socket

    stream = await DashScopeProvider(websocket_connect=connect).open_stream(
        StreamingSTTRequest(
            interview_id="iv_1",
            turn_id="turn_1",
            audio=StreamingAudioConfig(
                content_type="audio/pcm", sample_rate_hz=16000, channels=1
            ),
        ),
        context(cap.STT_STREAMING, "qwen-audio-3.0-asr-flash-streaming"),
    )

    # One 100 ms packet is blocked in socket.send and one 20 ms frame remains
    # in the coalescing buffer. Abort must cancel both without waiting on I/O.
    for _ in range(6):
        assert await stream.send_audio(b"\x00\x00" * 320) == []
    await asyncio.wait_for(socket.send_started.wait(), timeout=0.05)

    await asyncio.wait_for(stream.abort(), timeout=0.1)

    assert stream._pending_audio_bytes == 0
    assert stream._audio_send_buffer == bytearray()
    assert stream._sender_queue.empty()
    assert stream._sender_drained.is_set()
    assert socket.closed is True


@pytest.mark.anyio
async def test_dashscope_stream_pcm_buffer_counts_toward_backpressure_budget() -> None:
    socket = BlockingSendDashScopeSocket()

    async def connect(url, **kwargs):
        return socket

    stream = await DashScopeProvider(websocket_connect=connect).open_stream(
        StreamingSTTRequest(
            interview_id="iv_1",
            turn_id="turn_1",
            audio=StreamingAudioConfig(
                content_type="audio/pcm", sample_rate_hz=16000, channels=1
            ),
        ),
        context(
            cap.STT_STREAMING,
            "qwen-audio-3.0-asr-flash-streaming",
            config={
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "stream_send_backpressure_seconds": 0.25,
            },
        ),
    )

    # The 8,000-byte budget includes both complete queued packets and the
    # not-yet-complete coalescing buffer.
    for _ in range(12):
        assert await stream.send_audio(b"\x00\x00" * 320) == []
    with pytest.raises(ProviderError) as captured:
        await stream.send_audio(b"\x00\x00" * 320)

    assert captured.value.code == "provider_backpressure_exceeded"
    await stream.abort()


class FakeQwenRealtimeSocket:
    def __init__(self) -> None:
        self.received = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.received.put_nowait(json.dumps({"type": "session.created", "event_id": "evt_1"}))

    async def send(self, value):
        payload = json.loads(value)
        self.sent.append(payload)
        if payload["type"] == "session.update":
            self.received.put_nowait(json.dumps({"type": "session.updated", "event_id": "evt_2"}))
        elif payload["type"] == "response.create":
            self.received.put_nowait(json.dumps({"type": "response.audio_transcript.done", "transcript": "请补充说明恢复点。"}))
            self.received.put_nowait(json.dumps({"type": "response.audio.delta", "delta": base64.b64encode(b"\x00\x00" * 20).decode("ascii")}))
            self.received.put_nowait(json.dumps({"type": "response.audio.done"}))
            self.received.put_nowait(json.dumps({"type": "response.done", "event_id": "evt_3"}))

    async def recv(self):
        return await self.received.get()

    async def close(self):
        self.closed = True


@pytest.mark.anyio
async def test_dashscope_qwen_realtime_maps_s2s_audio_events() -> None:
    socket = FakeQwenRealtimeSocket()

    async def connect(url, **kwargs):
        assert url == "wss://ws123.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model=qwen3.5-omni-flash-realtime"
        assert kwargs["additional_headers"]["Authorization"] == "Bearer dashscope-key"
        return socket

    provider = DashScopeProvider(websocket_connect=connect)
    stream = await provider.open_dialogue(
        RealtimeSpeechDialogueRequest(
            interview_id="iv_1",
            turn_id="turn_1",
            input_audio=StreamingAudioConfig(
                content_type="audio/pcm", sample_rate_hz=16000, channels=1
            ),
            session_instructions="只播报受控追问",
        ),
        context(
            cap.SPEECH_DIALOGUE_REALTIME,
            "qwen3.5-omni-flash-realtime",
            config={
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "workspace_id": "ws123",
            },
        ),
    )
    await stream.send_audio(b"\x00\x00" * 160)
    events = await stream.commit(
        RealtimeSpeechResponseCommand(
            spoken_text="请补充说明恢复点。",
            response_instructions="只逐字朗读：请补充说明恢复点。",
        )
    )

    session_updates = [item for item in socket.sent if item["type"] == "session.update"]
    session = session_updates[0]
    assert session["session"]["turn_detection"] is None
    assert session["session"]["model"] == "qwen3.5-omni-flash-realtime"
    assert session["session"]["audio"] == {
        "input": {"format": {"type": "pcm", "sample_rate": 16000}},
        "output": {"format": {"type": "pcm", "sample_rate": 24000}},
    }
    assert session["session"]["voice"] == "Tina"
    assert session["session"]["input_audio_transcription"]["model"] == (
        "qwen3-asr-flash-realtime"
    )
    assert session_updates[1]["session"] == {
        "instructions": "只逐字朗读：请补充说明恢复点。"
    }
    response_create = next(item for item in socket.sent if item["type"] == "response.create")
    assert "instructions" not in response_create["response"]
    assert [item.type for item in events] == [
        "output.transcript.final",
        "output.audio.delta",
        "output.audio.done",
        "dialogue.closed",
    ]
    assert socket.closed is True
