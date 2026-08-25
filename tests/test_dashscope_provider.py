import json

import httpx
import pytest

from app.model_gateway import capabilities as cap
from app.model_gateway.schemas import (
    ChatJSONRequest,
    ChatMessage,
    ProviderContext,
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
        provider_config_id="mpc_dashscope",
        capability=capability,
        purpose="provider_test",
        model=model,
        timeout_s=5,
        attempt=1,
        fallback_index=0,
        config=config or {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
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
