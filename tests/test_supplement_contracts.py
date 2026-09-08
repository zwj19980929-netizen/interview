import asyncio
import json

import httpx
import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.validation import StructuredResponseValidationError, validate_structured_response
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import ChatJSONRequest, ChatJSONResponse, ProviderMeta, Usage
from app.repositories.memory import InMemoryStore
from app.services.conversation_understanding import ConversationUnderstandingService
from test_dashscope_provider import make_provider, context


@pytest.mark.parametrize("change", [
    {"intent": "yes"}, {"confidence": 2}, {"evidence_quote": " "},
    {"unknown": "field"}, {"intent": None},
])
def test_confirmation_reply_has_strict_nonempty_bounded_contract(change):
    contract = prompt_contract("supplement_reply", {"reply": "没有补充"})
    assert contract.version == "supplement_reply.v2"
    with pytest.raises(StructuredResponseValidationError):
        validate_structured_response({"intent": "finish", "confidence": 0.9, "evidence_quote": "没有补充", **change}, contract.response_schema)


def test_confirmation_understanding_has_traceable_version_and_ignores_old_control_intents():
    context = {"transcript": "我还没有说完。有补充。用探针核验状态。没有补充了。", "capability_points": ["探针"], "completion_confirmed": True}
    contract = prompt_contract("interview_turn_understanding", context)
    assert contract.version == "interview_turn_understanding.v7"
    assert "不作为能力主张或评分证据" in "".join(m.content for m in contract.messages)
    assert prompt_contract("interview_turn_decision", context).version == "interview_turn_decision.v6"


def test_reply_evidence_cannot_be_invented_even_after_valid_json():
    class Gateway:
        async def invoke(self, capability, request):
            return ChatJSONResponse(data={"intent": "finish", "confidence": 0.99, "evidence_quote": "没有补充"},
                usage=Usage(), provider=ProviderMeta(provider_id="fake", model="fake", request_id="test", latency_ms=0))
    async def scenario():
        service = ConversationUnderstandingService(InMemoryStore(), gateway=Gateway())
        with pytest.raises(ValueError, match="evidence_not_verbatim"):
            await service.classify_supplement_reply("我还有补充", "org_default")
    asyncio.run(scenario())


@pytest.mark.parametrize("version", ["supplement_reply.v1", "supplement_reply.v2"])
def test_supplement_versions_remain_readable_in_mock_and_invocation_audit(version):
    from app.providers.mock.provider import MockProvider

    async def scenario():
        contract = prompt_contract("supplement_reply", {"reply": "我说的内容和字幕不一样。"})
        request = ChatJSONRequest(purpose="interview_turn_understanding", messages=contract.messages,
            json_schema=contract.response_schema, metadata={"prompt_version": version})
        response = await MockProvider().invoke("llm.chat_json", request, context("llm.chat_json", "mock-json"))
        validate_structured_response(response.data, contract.response_schema)
        assert response.data["intent"] == "unclear"
        store = InMemoryStore()
        gateway = ModelGateway(store)
        gateway._log_invocation(invocation_id="synthetic-version", organization_id="org_default",
            capability="llm.chat_json", purpose=request.purpose, route={"id": "synthetic-route"},
            provider_connection_id="synthetic-provider", model_configuration_id="synthetic-model",
            provider_id="mock", model="mock-json", status="success", latency_ms=1,
            attempt=1, fallback_index=0, request_hash="synthetic", response=response,
            prompt_version=version)
        with gateway.persistence.transaction("org_default") as transaction:
            audit = transaction.model_invocations.list()
        assert audit[-1]["prompt_version"] == version

    asyncio.run(scenario())


def test_transcription_dispute_uses_versioned_contract_without_relaxing_evidence_validation():
    reply = "我没有补充，不过字幕里的策略不是我的发言，那个内容识别错了。"
    seen = []

    class Gateway:
        async def invoke(self, capability, request):
            seen.append(request)
            return ChatJSONResponse(data={"intent": "unclear", "confidence": .98,
                "evidence_quote": "字幕里的策略不是我的发言"}, usage=Usage(),
                provider=ProviderMeta(provider_id="synthetic", model="synthetic", request_id="synthetic", latency_ms=0))

    result = asyncio.run(ConversationUnderstandingService(InMemoryStore(), gateway=Gateway()).classify_supplement_reply(reply, "org_default"))
    assert result["intent"] == "unclear" and result["evidence_quote"] in reply
    assert seen[0].metadata["prompt_version"] == "supplement_reply.v2"
    assert set(seen[0].json_schema["required"]) == {"intent", "confidence", "evidence_quote"}
    assert seen[0].json_schema["additionalProperties"] is False
    system = "".join(message.content for message in seen[0].messages if message.role == "system")
    assert "优先unclear" in system and "假设或引用用户投诉" in system and "消除了争议" in system


@pytest.mark.parametrize("invalid", [False, True])
def test_native_400_uses_one_json_object_attempt_and_keeps_full_schema_validation(invalid, caplog):
    seen = []
    contract = prompt_contract("supplement_reply", {"reply": "没有补充"})
    def handler(req):
        payload = json.loads(req.content)
        seen.append(payload)
        if len(seen) == 1:
            return httpx.Response(400, json={"error": {"message": "invalid schema sensitive-prompt-secret"}})
        data = {"intent": "finish", "confidence": 0.95, "evidence_quote": "没有补充"}
        if invalid:
            data["unsafe_extra"] = "not allowed"
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(data)}}]})
    async def scenario():
        request = ChatJSONRequest(purpose="interview_turn_understanding", messages=contract.messages,
            json_schema=contract.response_schema)
        ctx = context("llm.chat_json", "qwen3.7-plus").model_copy(update={"purpose": request.purpose})
        response = await make_provider(handler).invoke("llm.chat_json", request, ctx)
        assert [p["response_format"]["type"] for p in seen] == ["json_schema", "json_object"]
        assert "JSON Schema" in "".join(m["content"] for m in seen[1]["messages"])
        if invalid:
            with pytest.raises(ProviderError) as rejected:
                ModelGateway(InMemoryStore())._validate_response("llm.chat_json", request, response)
            assert rejected.value.code == "provider_schema_invalid"
        else:
            ModelGateway(InMemoryStore())._validate_response("llm.chat_json", request, response)
        assert "sensitive-prompt-secret" not in caplog.text
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403, 422, 429, 500])
def test_other_http_errors_do_not_trigger_native_schema_fallback(status):
    calls = []
    def handler(req):
        calls.append(1)
        return httpx.Response(status, json={"error": {"message": "schema rejected secret"}})
    async def scenario():
        contract = prompt_contract("supplement_reply", {"reply": "没有"})
        request = ChatJSONRequest(purpose="interview_turn_understanding", messages=contract.messages, json_schema=contract.response_schema)
        ctx = context("llm.chat_json", "qwen3.7-plus").model_copy(update={"purpose": request.purpose})
        with pytest.raises(ProviderError):
            await make_provider(handler).invoke("llm.chat_json", request, ctx)
        assert len(calls) == 1
    asyncio.run(scenario())
