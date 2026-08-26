import json

import httpx
import pytest

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ChatJSONRequest, ChatMessage, ChatTextRequest, TTSSynthesizeRequest
from app.providers.deepseek.provider import DeepSeekProvider
from app.providers.zhipuai.provider import ZhipuAIProvider


def make_provider(provider_type, handler):
    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=transport, **kwargs)

    return provider_type(client_factory=client_factory)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("provider_type", "base_url", "model", "expected_provider_id"),
    [
        (DeepSeekProvider, "https://api.deepseek.com", "deepseek-chat", "deepseek"),
        (ZhipuAIProvider, "https://open.bigmodel.cn/api/paas/v4", "glm-5.2", "zhipuai"),
    ],
)
async def test_vendor_chat_json_reuses_compatible_runtime_with_json_object(
    provider_type,
    base_url,
    model,
    expected_provider_id,
) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "vendor_request_1",
                "choices": [{"message": {"content": json.dumps({"score": 88})}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
            },
        )

    response = await make_provider(provider_type, handler).chat_json(
        ChatJSONRequest(
            purpose="answer_evaluation",
            messages=[ChatMessage(role="user", content="评价答案")],
            json_schema={
                "type": "object",
                "required": ["score"],
                "properties": {"score": {"type": "integer"}},
            },
        ),
        config={"base_url": base_url},
        credentials={"api_key": "vendor-key"},
        model=model,
        timeout_s=5,
    )

    assert seen["url"] == "%s/chat/completions" % base_url
    assert seen["authorization"] == "Bearer vendor-key"
    assert seen["payload"]["response_format"] == {"type": "json_object"}
    assert seen["payload"]["messages"][0]["role"] == "system"
    assert "JSON Schema" in seen["payload"]["messages"][0]["content"]
    assert 'Example JSON output: {"score":0}' in seen["payload"]["messages"][0]["content"]
    assert seen["payload"]["messages"][1]["content"] == "评价答案"
    assert response.data == {"score": 88}
    assert response.provider.provider_id == expected_provider_id
    assert response.provider.model == model
    assert response.usage.total_tokens == 10


@pytest.mark.anyio
async def test_deepseek_validates_api_key_with_model_list_without_generation() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"object": "list", "data": [{"id": "deepseek-v4-pro"}]})

    result = await make_provider(DeepSeekProvider, handler).validate_credentials(
        {"base_url": "https://api.deepseek.com"}, {"api_key": "deepseek-key"}, timeout_s=5
    )

    assert seen == {
        "method": "GET",
        "url": "https://api.deepseek.com/models",
        "authorization": "Bearer deepseek-key",
    }
    assert result["status"] == "valid"


@pytest.mark.anyio
async def test_zhipu_validates_api_key_with_minimal_provider_owned_probe() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200, json={"id": "credential_probe", "choices": [{"message": {"content": "pong"}}]}
        )

    result = await make_provider(ZhipuAIProvider, handler).validate_credentials(
        {"base_url": "https://open.bigmodel.cn/api/paas/v4"}, {"api_key": "zhipu-key"}, timeout_s=5
    )

    assert seen["payload"]["model"] == "glm-4.7-flash"
    assert seen["payload"]["max_tokens"] == 1
    assert result["status"] == "valid"


@pytest.mark.anyio
async def test_zhipu_glm_tts_uses_official_speech_contract() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            content=b"zhipu-wave-audio",
            headers={"content-type": "audio/wav", "x-request-id": "zhipu_tts_1"},
        )

    response = await make_provider(ZhipuAIProvider, handler).synthesize_speech(
        TTSSynthesizeRequest(
            text="欢迎参加面试",
            voice_profile_id="voice_default_cn",
            format="audio/wav",
            speaking_rate=1.25,
        ),
        config={"base_url": "https://open.bigmodel.cn/api/paas/v4"},
        credentials={"api_key": "zhipu-key"},
        model="glm-tts",
        timeout_s=5,
    )

    assert seen["url"] == "https://open.bigmodel.cn/api/paas/v4/audio/speech"
    assert seen["authorization"] == "Bearer zhipu-key"
    assert seen["payload"] == {
        "model": "glm-tts",
        "input": "欢迎参加面试",
        "voice": "tongtong",
        "response_format": "wav",
        "speed": 1.25,
    }
    assert response.provider.provider_id == "zhipuai"
    assert response.provider.model == "glm-tts"
    assert response.content_type == "audio/wav"
    assert response.audio_uri.startswith("data:audio/wav;base64,")


@pytest.mark.anyio
async def test_zhipu_glm_tts_rejects_wrong_model_and_oversized_input() -> None:
    provider = ZhipuAIProvider()
    request = TTSSynthesizeRequest(text="连接测试")
    with pytest.raises(ProviderError) as wrong_model:
        await provider.synthesize_speech(
            request,
            config={"base_url": "https://open.bigmodel.cn/api/paas/v4"},
            credentials={"api_key": "zhipu-key"},
            model="glm-5.2",
            timeout_s=5,
        )
    assert wrong_model.value.code == "provider_bad_request"

    with pytest.raises(ProviderError) as oversized:
        await provider.synthesize_speech(
            TTSSynthesizeRequest(text="测" * 1025),
            config={"base_url": "https://open.bigmodel.cn/api/paas/v4"},
            credentials={"api_key": "zhipu-key"},
            model="glm-tts",
            timeout_s=5,
        )
    assert oversized.value.code == "provider_bad_request"


@pytest.mark.anyio
async def test_vendor_transport_initialization_error_is_structured_and_proxy_is_opt_in() -> None:
    seen = {}

    def client_factory(**kwargs):
        seen.update(kwargs)
        raise ImportError("SOCKS proxy support is not installed")

    provider = ZhipuAIProvider(client_factory=client_factory)
    with pytest.raises(ProviderError) as error:
        await provider.chat_text(
            ChatTextRequest(
                purpose="provider_test",
                messages=[ChatMessage(role="user", content="ping")],
            ),
            config={"base_url": "https://open.bigmodel.cn/api/paas/v4"},
            credentials={"api_key": "zhipu-key"},
            model="glm-5.2",
            timeout_s=5,
        )

    assert seen["trust_env"] is False
    assert error.value.code == "provider_transport_unavailable"
    assert error.value.retryable is False
