import asyncio
import base64
import gzip
import json
import struct

import httpx
import pytest

from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    BatchSTTRequest,
    ChatJSONRequest,
    ChatMessage,
    ProviderContext,
    RealtimeSpeechDialogueRequest,
    RealtimeSpeechResponseCommand,
    StreamingAudioConfig,
    StreamingSTTRequest,
    TextEmbeddingRequest,
    TTSSynthesizeRequest,
)
from app.providers.volcengine.protocol import (
    decode_server_frame,
    encode_audio_only_request,
    encode_full_client_request,
)
from app.providers.volcengine.provider import VolcengineProvider


def make_provider(handler):
    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=transport, **kwargs)

    return VolcengineProvider(client_factory=client_factory)


def context(capability: str, model: str, *, config=None, credentials=None) -> ProviderContext:
    if capability == cap.TTS_SYNTHESIZE:
        model_type = "tts"
    elif capability == cap.SPEECH_DIALOGUE_REALTIME:
        model_type = "realtime_speech"
    elif capability.startswith("stt."):
        model_type = "stt"
    elif capability == cap.EMBEDDING_TEXT:
        model_type = "embedding"
    else:
        model_type = "llm"
    return ProviderContext(
        organization_id="org_default",
        invocation_id="invocation_test",
        route_id="route_test",
        provider_connection_id="provider_conn_volcengine",
        model_configuration_id="model_cfg_volcengine",
        model_type=model_type,
        capability=capability,
        purpose="provider_test",
        model=model,
        timeout_s=1,
        attempt=1,
        fallback_index=0,
        connection_config=config
        or {"base_url": "https://ark.cn-beijing.volces.com/api/v3"},
        credentials=credentials
        or {"ark_api_key": "ark-secret", "speech_api_key": "speech-secret"},
    )


def server_asr_frame(payload, *, sequence=1, last=False) -> bytes:
    compressed = gzip.compress(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    flags = 0x3 if last else 0x1
    return (
        bytes([0x11, (0x9 << 4) | flags, 0x11, 0])
        + struct.pack(">i", sequence)
        + struct.pack(">I", len(compressed))
        + compressed
    )


def server_asr_error_frame(message: str, *, code=55000031) -> bytes:
    payload = message.encode("utf-8")
    return (
        bytes([0x11, 0xF0, 0x00, 0])
        + struct.pack(">I", code)
        + struct.pack(">I", len(payload))
        + payload
    )


def decode_client_json_frame(raw: bytes):
    assert raw[:4] == bytes([0x11, 0x10, 0x11, 0])
    size = struct.unpack(">I", raw[4:8])[0]
    return json.loads(gzip.decompress(raw[8 : 8 + size]).decode("utf-8"))


@pytest.mark.anyio
async def test_volcengine_ark_chat_uses_only_ark_credential() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "ark_req_1",
                "choices": [{"message": {"content": json.dumps({"score": 91})}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
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
        context(cap.LLM_CHAT_JSON, "doubao-seed-2-1-pro-260628"),
    )

    assert seen["url"] == "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    assert seen["authorization"] == "Bearer ark-secret"
    assert "speech-secret" not in json.dumps(seen, ensure_ascii=False)
    assert seen["payload"]["response_format"]["type"] == "json_schema"
    assert response.data == {"score": 91}
    assert response.provider.provider_id == "volcengine"


@pytest.mark.anyio
async def test_volcengine_ark_embedding_reuses_the_gateway_contract() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "ark_embedding_1",
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ],
            },
        )

    response = await make_provider(handler).invoke(
        cap.EMBEDDING_TEXT,
        TextEmbeddingRequest(purpose="question_retrieval", texts=["Celery", "幂等键"]),
        context(cap.EMBEDDING_TEXT, "doubao-embedding-text-240715"),
    )

    assert seen == {
        "url": "https://ark.cn-beijing.volces.com/api/v3/embeddings",
        "authorization": "Bearer ark-secret",
        "payload": {
            "model": "doubao-embedding-text-240715",
            "input": ["Celery", "幂等键"],
        },
    }
    assert response.vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert response.dimensions == 2
    assert response.provider.provider_id == "volcengine"


@pytest.mark.anyio
async def test_volcengine_batch_asr_sends_private_audio_with_speech_headers() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            headers={"X-Api-Status-Code": "20000000", "X-Tt-Logid": "speech_req_1"},
            json={
                "result": {
                    "text": "我使用幂等键避免重复消费。",
                    "utterances": [
                        {
                            "text": "我使用幂等键避免重复消费。",
                            "start_time": 20,
                            "end_time": 920,
                            "words": [{"text": "幂等键", "confidence": 0.96}],
                        }
                    ],
                }
            },
        )

    audio = b"private-candidate-audio"
    response = await make_provider(handler).invoke(
        cap.STT_BATCH,
        BatchSTTRequest(
            audio_uri="private-media://answer.wav",
            content_type="audio/wav",
            audio_bytes=audio,
        ),
        context(cap.STT_BATCH, "doubao-seed-asr-2.0"),
    )

    assert seen["url"] == (
        "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
    )
    assert seen["headers"]["x-api-key"] == "speech-secret"
    assert seen["headers"]["x-api-resource-id"] == "volc.bigasr.auc_turbo"
    assert "ark-secret" not in json.dumps(seen, ensure_ascii=False)
    assert base64.b64decode(seen["payload"]["audio"]["data"]) == audio
    assert seen["payload"]["request"]["show_utterances"] is True
    assert response.text == "我使用幂等键避免重复消费。"
    assert response.segments[0].confidence == pytest.approx(0.96)
    assert response.provider.request_id == "speech_req_1"


@pytest.mark.anyio
async def test_volcengine_seed_tts_collects_streamed_json_audio_chunks() -> None:
    seen = {}
    first = b"\x01\x02" * 400
    second = b"\x03\x04" * 400

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        chunks = (
            json.dumps({"code": 20000000, "data": base64.b64encode(first).decode("ascii")})
            + "\n"
            + json.dumps({"code": 20000000, "data": base64.b64encode(second).decode("ascii")})
        ).encode("utf-8")
        return httpx.Response(200, headers={"X-Tt-Logid": "tts_req_1"}, content=chunks)

    response = await make_provider(handler).invoke(
        cap.TTS_SYNTHESIZE,
        TTSSynthesizeRequest(
            text="请补充说明幂等键的设计。",
            voice_profile_id="voice_default_cn",
            format="audio/pcm",
            speaking_rate=1.1,
        ),
        context(
            cap.TTS_SYNTHESIZE,
            "doubao-seed-tts-2.0",
            config={
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "tts_resource_id": "seed-tts-2.0",
                "default_voice": "zh_female_vv_uranus_bigtts",
                "sample_rate_hz": 16_000,
            },
        ),
    )

    assert seen["url"] == "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
    assert seen["headers"]["x-api-key"] == "speech-secret"
    assert seen["headers"]["x-api-resource-id"] == "seed-tts-2.0"
    assert "ark-secret" not in json.dumps(seen, ensure_ascii=False)
    assert seen["payload"] == {
        "req_params": {
            "text": "请补充说明幂等键的设计。",
            "speaker": "zh_female_vv_uranus_bigtts",
            "audio_params": {
                "format": "pcm",
                "sample_rate": 16_000,
                "speech_rate": 10,
                "enable_subtitle": True,
            },
        }
    }
    assert base64.b64decode(response.audio_uri.split(",", 1)[1]) == first + second
    assert response.content_type == "audio/pcm"
    assert response.duration_ms == 50
    assert response.provider.request_id == "tts_req_1"


def test_volcengine_asr_binary_protocol_round_trip_and_final_marker() -> None:
    request_payload = {
        "user": {"uid": "interviewer-server"},
        "audio": {"format": "pcm", "rate": 16000},
        "request": {"show_utterances": True},
    }
    encoded = encode_full_client_request(request_payload)
    assert decode_client_json_frame(encoded) == request_payload

    audio = encode_audio_only_request(b"\x00\x01", last=True)
    assert audio[:4] == bytes([0x11, 0x22, 0x01, 0])
    size = struct.unpack(">I", audio[4:8])[0]
    assert gzip.decompress(audio[8 : 8 + size]) == b"\x00\x01"

    final = decode_server_frame(
        server_asr_frame({"result": {"text": "最终转写"}}, sequence=-2, last=True)
    )
    assert final.sequence == -2
    assert final.is_last is True
    assert final.payload["result"]["text"] == "最终转写"

    with pytest.raises(ProviderError, match="服务器繁忙") as exc_info:
        decode_server_frame(server_asr_error_frame("服务器繁忙"))
    assert exc_info.value.code == "provider_server_error"
    assert exc_info.value.retryable is True


class FakeVolcengineASRSocket:
    def __init__(self) -> None:
        self.received = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.received.put_nowait(server_asr_frame({"result": {}}, sequence=1))

    async def send(self, value):
        self.sent.append(value)
        if value[1] == 0x20:
            self.received.put_nowait(
                server_asr_frame({"result": {"text": "实时转", "utterances": []}}, sequence=2)
            )
        elif value[1] == 0x22:
            self.received.put_nowait(
                server_asr_frame(
                    {
                        "result": {
                            "text": "实时转写完成。",
                            "utterances": [
                                {"text": "实时转写完成。", "start_time": 0, "end_time": 800}
                            ],
                        }
                    },
                    sequence=-3,
                    last=True,
                )
            )

    async def recv(self):
        return await self.received.get()

    async def close(self):
        self.closed = True


@pytest.mark.anyio
async def test_volcengine_streaming_asr_maps_one_authoritative_final() -> None:
    socket = FakeVolcengineASRSocket()

    async def connect(url, **kwargs):
        assert url == "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
        assert kwargs["additional_headers"] == {
            "X-Api-Key": "speech-secret",
            "X-Api-Resource-Id": "volc.seedasr.sauc.duration",
            "X-Api-Request-Id": kwargs["additional_headers"]["X-Api-Request-Id"],
            "X-Api-Connect-Id": kwargs["additional_headers"]["X-Api-Connect-Id"],
        }
        return socket

    provider = VolcengineProvider(websocket_connect=connect)
    stream = await provider.open_stream(
        StreamingSTTRequest(
            interview_id="iv_1",
            turn_id="turn_1",
            audio=StreamingAudioConfig(
                content_type="audio/pcm", sample_rate_hz=16_000, channels=1
            ),
        ),
        context(cap.STT_STREAMING, "doubao-seed-asr-2.0"),
    )
    partial = await stream.send_audio(b"\x00\x01")
    finished = await stream.finish()

    start_payload = decode_client_json_frame(socket.sent[0])
    assert start_payload["request"]["show_utterances"] is True
    assert start_payload["request"]["result_type"] == "full"
    assert "emotion" not in json.dumps(start_payload)
    assert partial[0].type == "transcript.partial"
    assert [item.type for item in finished].count("transcript.final") == 1
    assert next(item for item in finished if item.type == "transcript.final").text == (
        "实时转写完成。"
    )
    assert socket.closed is True


class FakeSeeduplexSocket:
    def __init__(self) -> None:
        self.received = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.received.put_nowait(json.dumps({"type": "session.created", "event_id": "evt_1"}))

    async def send(self, value):
        payload = json.loads(value)
        self.sent.append(payload)
        if payload["type"] == "input_audio_buffer.append":
            self.received.put_nowait(
                json.dumps(
                    {
                        "type": "conversation.item.input_audio_transcription.delta",
                        "delta": "候选人正在回答",
                    }
                )
            )
        elif payload["type"] == "input_audio_buffer.commit":
            self.received.put_nowait(json.dumps({"type": "input_audio_buffer.committed"}))
        elif payload["type"] == "speech_text_buffer.replacement.commit":
            self.received.put_nowait(
                json.dumps(
                    {
                        "type": "response.output_audio.delta",
                        "delta": base64.b64encode(b"\x00\x00" * 20).decode("ascii"),
                    }
                )
            )
            self.received.put_nowait(json.dumps({"type": "response.output_audio.done"}))
            self.received.put_nowait(json.dumps({"type": "response.done", "event_id": "evt_2"}))

    async def recv(self):
        return await self.received.get()

    async def close(self):
        self.closed = True


@pytest.mark.anyio
async def test_volcengine_seeduplex_speaks_only_the_approved_act() -> None:
    socket = FakeSeeduplexSocket()

    async def connect(url, **kwargs):
        assert url == "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"
        assert kwargs["additional_headers"] == {"X-Api-Key": "speech-secret"}
        return socket

    provider = VolcengineProvider(websocket_connect=connect)
    stream = await provider.open_dialogue(
        RealtimeSpeechDialogueRequest(
            interview_id="iv_1",
            turn_id="turn_1",
            input_audio=StreamingAudioConfig(
                content_type="audio/pcm", sample_rate_hz=16_000, channels=1
            ),
            voice="voice_default_cn",
            session_instructions="只播报受控追问，不进行自由问答。",
        ),
        context(
            cap.SPEECH_DIALOGUE_REALTIME,
            "1.2.6.1",
            config={
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "dialogue_default_voice": "zh_female_vv_uranus_bigtts",
            },
        ),
    )
    partial = await stream.send_audio(b"\x00\x00" * 160)
    approved = "你刚才提到幂等键，请说明失效时间如何确定。"
    events = await stream.commit(
        RealtimeSpeechResponseCommand(
            spoken_text=approved,
            response_instructions="只逐字朗读已批准文本。",
        )
    )

    session_create = socket.sent[0]
    assert session_create["type"] == "session.create"
    assert session_create["session"]["model"] == "1.2.6.1"
    assert session_create["session"]["audio"] == {
        "input": {"format": {"type": "pcm", "rate": 16_000}},
        "output": {"format": {"type": "pcm", "rate": 24_000}},
        "voice": "zh_female_vv_uranus_bigtts",
    }
    assert partial[0].type == "input.transcript.partial"
    replacement = next(
        item for item in socket.sent if item["type"] == "speech_text_buffer.replacement.append"
    )
    assert replacement["text"] == approved
    output_finals = [item for item in events if item.type == "output.transcript.final"]
    assert len(output_finals) == 1
    assert output_finals[0].text == approved
    assert any(item.type == "output.audio.delta" for item in events)
    assert events[-1].type == "dialogue.closed"
    assert socket.sent[-1]["type"] == "session.close"
    assert socket.closed is True


@pytest.mark.anyio
async def test_volcengine_speech_capabilities_fail_closed_without_speech_key() -> None:
    provider = VolcengineProvider()

    with pytest.raises(ProviderError, match="Speech API Key") as exc_info:
        await provider.open_stream(
            StreamingSTTRequest(
                interview_id="iv_1",
                turn_id="turn_1",
                audio=StreamingAudioConfig(
                    content_type="audio/pcm", sample_rate_hz=16_000, channels=1
                ),
            ),
            context(
                cap.STT_STREAMING,
                "doubao-seed-asr-2.0",
                credentials={"ark_api_key": "ark-secret"},
            ),
        )

    assert exc_info.value.code == "provider_auth_failed"
