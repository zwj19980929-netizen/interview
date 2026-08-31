import json
import asyncio
import base64

import httpx
import pytest

from app.model_gateway import capabilities as cap
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


@pytest.mark.anyio
async def test_dashscope_stream_maps_duplex_events_to_one_authoritative_final() -> None:
    socket = FakeDashScopeSocket()

    async def connect(url, **kwargs):
        assert url == "wss://ws123.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
        assert kwargs["additional_headers"]["Authorization"] == "Bearer dashscope-key"
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
    partial = await stream.send_audio(b"\x00\x01")
    finished = await stream.finish()

    assert partial[0].type == "transcript.partial"
    assert [item.type for item in finished].count("transcript.final") == 1
    assert next(item for item in finished if item.type == "transcript.final").text == "实时转写完成。"
    assert socket.closed is True


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

    session = next(item for item in socket.sent if item["type"] == "session.update")
    assert session["session"]["turn_detection"] is None
    assert session["session"]["input_audio_format"] == "pcm16"
    assert [item.type for item in events] == [
        "output.transcript.final",
        "output.audio.delta",
        "output.audio.done",
        "dialogue.closed",
    ]
    assert socket.closed is True
