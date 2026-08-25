import json

import httpx
import pytest

from app.model_gateway import capabilities as cap
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import ChatJSONRequest, ChatMessage, TextEmbeddingRequest
from app.providers.openai_compatible.provider import OpenAICompatibleProvider
from app.repositories.memory import InMemoryStore


def make_provider(handler):
    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=transport, **kwargs)

    return OpenAICompatibleProvider(client_factory=client_factory)


@pytest.mark.anyio
async def test_openai_compatible_chat_json_parses_structured_response() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_test",
                "choices": [{"message": {"content": json.dumps({"score": 91, "summary": "ok"})}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    provider = make_provider(handler)
    response = await provider.chat_json(
        ChatJSONRequest(
            purpose="answer_evaluation",
            messages=[ChatMessage(role="user", content="ping")],
            json_schema={
                "type": "object",
                "required": ["score", "summary"],
                "properties": {"score": {"type": "integer"}, "summary": {"type": "string"}},
            },
        ),
        config={"base_url": "https://models.example.com/v1"},
        credentials={"api_key": "test-key"},
        model="chat-model",
        timeout_s=5,
    )

    assert seen["authorization"] == "Bearer test-key"
    assert seen["payload"]["model"] == "chat-model"
    assert seen["payload"]["response_format"]["type"] == "json_schema"
    assert response.data == {"score": 91, "summary": "ok"}
    assert response.usage.total_tokens == 15
    assert response.provider.request_id == "chatcmpl_test"


@pytest.mark.anyio
async def test_openai_compatible_embedding_parses_vectors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["model"] == "embedding-model"
        assert payload["input"] == ["hello", "world"]
        return httpx.Response(
            200,
            json={
                "id": "emb_test",
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ],
            },
        )

    provider = make_provider(handler)
    response = await provider.embed_text(
        TextEmbeddingRequest(purpose="question_indexing", texts=["hello", "world"]),
        config={"base_url": "https://models.example.com/v1"},
        credentials={"api_key": "test-key"},
        model="embedding-model",
        timeout_s=5,
    )

    assert response.vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert response.dimensions == 2
    assert response.provider.request_id == "emb_test"


@pytest.mark.anyio
async def test_model_gateway_routes_to_openai_compatible_provider() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl_gateway",
                    "choices": [{"message": {"content": json.dumps({"result": "ok"})}}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
                },
            )
        return httpx.Response(
            200,
            json={"id": "emb_gateway", "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
        )

    store = InMemoryStore()
    store.provider_configs["mpc_openai"] = {
        "id": "mpc_openai",
        "organization_id": "org_default",
        "provider_id": "openai_compatible",
        "display_name": "Test OpenAI Compatible",
        "enabled": True,
        "config": {"base_url": "https://models.example.com/v1"},
        "credential_ref": "secret://model-providers/mpc_openai",
        "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-07-01T00:00:00Z",
    }
    store.save_provider_secret("mpc_openai", {"api_key": "test-key"})
    store.model_routes["route_chat"] = {
        "id": "route_chat",
        "organization_id": "org_default",
        "capability": cap.LLM_CHAT_JSON,
        "purpose": "provider_test",
        "primary": {"provider_config_id": "mpc_openai", "model": "chat-model", "timeout_s": 5},
        "fallbacks": [],
        "policy": {},
        "enabled": True,
    }
    store.model_routes["route_embedding"] = {
        "id": "route_embedding",
        "organization_id": "org_default",
        "capability": cap.EMBEDDING_TEXT,
        "purpose": "question_indexing",
        "primary": {"provider_config_id": "mpc_openai", "model": "embedding-model", "timeout_s": 5},
        "fallbacks": [],
        "policy": {},
        "enabled": True,
    }

    gateway = ModelGateway(store, provider_clients={"openai_compatible": make_provider(handler)})
    chat = await gateway.invoke(
        cap.LLM_CHAT_JSON,
        ChatJSONRequest(
            purpose="provider_test",
            messages=[ChatMessage(role="user", content="ping")],
        )
    )
    embedding = await gateway.invoke(
        cap.EMBEDDING_TEXT,
        TextEmbeddingRequest(purpose="question_indexing", texts=["hello"]),
    )

    assert chat.data == {"result": "ok"}
    assert chat.provider.provider_id == "openai_compatible"
    assert embedding.vectors == [[0.1, 0.2, 0.3]]
    assert len(store.model_invocations) == 2
    assert {item["status"] for item in store.model_invocations} == {"success"}
