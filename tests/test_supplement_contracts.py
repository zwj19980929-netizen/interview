import asyncio
import json
from copy import deepcopy

import httpx
import pytest

from app.core.prompt.contracts import prompt_contract, supplement_reply_canonical_schema
from app.core.prompt.validation import StructuredResponseValidationError, validate_structured_response
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import ChatJSONRequest, ChatJSONResponse, ProviderMeta, Usage
from app.repositories.memory import InMemoryStore
from app.services.conversation_understanding import ConversationUnderstandingService
from test_dashscope_provider import make_provider, context


@pytest.mark.parametrize("kind", ["check", "continue", "clarify"])
def test_new_supplement_approval_uses_central_version_and_keeps_frozen_history(kind):
    from app.core.prompt.contracts import SUPPLEMENT_SPEECH, SUPPLEMENT_SPEECH_VERSION
    from app.services.interview_agent import InterviewAgentRuntime
    from test_interview_agent_contracts import _runtime_session

    store = InMemoryStore()
    session = _runtime_session(store)
    runtime = InterviewAgentRuntime(store)
    arguments = {"act_type": "supplement_" + kind, "text": SUPPLEMENT_SPEECH[kind],
        "turn_id": "turn_1", "evidence_quotes": [], "approved_by": "synthetic",
        "organization_id": "org_default"}
    current = runtime._approve_conversation_act(session["id"], **arguments)
    assert current.prompt_version == SUPPLEMENT_SPEECH_VERSION == "supplement_confirmation.v2"
    assert runtime._approve_conversation_act(session["id"], **arguments).act_id == current.act_id

    legacy_arguments = {**arguments, "text": "历史补充话语。", "prompt_version": "supplement_confirmation.v1"}
    legacy = runtime._approve_conversation_act(session["id"], **legacy_arguments)
    replay = runtime._approve_conversation_act(session["id"], **{**arguments, "text": legacy.text})
    assert replay.act_id == legacy.act_id and replay.prompt_version == "supplement_confirmation.v1"
    saved = runtime.interviews.get_interview(session["id"])["turns"][0]["conversation_acts"]
    assert [item["prompt_version"] for item in saved] == [SUPPLEMENT_SPEECH_VERSION, "supplement_confirmation.v1"]


@pytest.mark.parametrize("change", [
    {"intent": "yes"}, {"confidence": 2}, {"evidence_id": " "},
    {"unknown": "field"}, {"intent": None}, {"confidence": True}, {"evidence_id": "E999"},
])
def test_confirmation_reply_has_strict_nonempty_bounded_contract(change):
    contract = prompt_contract("supplement_reply", {"reply": "没有补充"})
    assert contract.version == "supplement_reply.v11"
    with pytest.raises(StructuredResponseValidationError):
        validate_structured_response({"intent": "finish", "confidence": 0.9, "evidence_id": "E1", "reply_text": "", "answer_effect": "unchanged", **change}, contract.response_schema)


def test_confirmation_understanding_has_traceable_version_and_ignores_old_control_intents():
    context = {"transcript": "我还没有说完。有补充。用探针核验状态。没有补充了。", "capability_points": ["探针"], "completion_confirmed": True}
    contract = prompt_contract("interview_turn_understanding", context)
    assert contract.version == "interview_turn_understanding.v107"
    assert "不作为能力主张或评分证据" in "".join(m.content for m in contract.messages)
    assert prompt_contract("interview_turn_decision", context).version == "interview_turn_decision.v138"


def test_reply_evidence_cannot_be_invented_even_after_valid_json():
    class Gateway:
        async def invoke(self, capability, request):
            return ChatJSONResponse(data={"intent": "finish", "confidence": 0.99, "evidence_id": "E999", "reply_text": "", "answer_effect": "unchanged"},
                usage=Usage(), provider=ProviderMeta(provider_id="fake", model="fake", request_id="test", latency_ms=0))
    async def scenario():
        service = ConversationUnderstandingService(InMemoryStore(), gateway=Gateway())
        with pytest.raises(StructuredResponseValidationError):
            await service.classify_supplement_reply("我还有补充", "org_default")
    asyncio.run(scenario())


@pytest.mark.parametrize("version", ["supplement_reply.v1", "supplement_reply.v2", "supplement_reply.v3", "supplement_reply.v4", "supplement_reply.v5", "supplement_reply.v6", "supplement_reply.v7", "supplement_reply.v8", "supplement_reply.v9", "supplement_reply.v10", "supplement_reply.v11", "supplement_reply.v12"])
def test_supplement_versions_remain_readable_in_mock_and_invocation_audit(version):
    from app.providers.mock.provider import MockProvider

    async def scenario():
        contract = prompt_contract("supplement_reply", {"reply": "我说的内容和字幕不一样。"})
        schema = deepcopy(supplement_reply_canonical_schema() if version in {
            "supplement_reply.v1", "supplement_reply.v2"} else contract.response_schema)
        if version not in {"supplement_reply.v7", "supplement_reply.v8", "supplement_reply.v9", "supplement_reply.v10", "supplement_reply.v11", "supplement_reply.v12"}:
            schema["required"].remove("answer_effect")
            schema["properties"].pop("answer_effect")
            schema["properties"]["intent"]["enum"].remove("confirmation_unchanged")
        if version not in {"supplement_reply.v6", "supplement_reply.v7", "supplement_reply.v8", "supplement_reply.v9", "supplement_reply.v10", "supplement_reply.v11", "supplement_reply.v12"}:
            # Historical contracts never required generated conversation text.
            schema["required"].remove("reply_text")
            for key in ("reply_text", "repeat_target"):
                schema["properties"].pop(key)
        request = ChatJSONRequest(purpose="interview_turn_understanding", messages=contract.messages,
            json_schema=schema, metadata={"prompt_version": version})
        response = await MockProvider().invoke("llm.chat_json", request, context("llm.chat_json", "mock-json"))
        validate_structured_response(response.data, schema)
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
                "evidence_id": "E1", "reply_text": "你可以说一下需要更正的内容。", "answer_effect": "uncertain"}, usage=Usage(),
                provider=ProviderMeta(provider_id="synthetic", model="synthetic", request_id="synthetic", latency_ms=0))

    result = asyncio.run(ConversationUnderstandingService(InMemoryStore(), gateway=Gateway()).classify_supplement_reply(reply, "org_default"))
    assert result["intent"] == "unclear" and result["evidence_quote"] in reply
    assert seen[0].metadata["prompt_version"] == "supplement_reply.v11"
    assert set(seen[0].json_schema["required"]) == {"intent", "answer_effect", "confidence", "evidence_id", "reply_text"}
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
        data = {"intent": "finish", "confidence": 0.95, "evidence_id": "E1", "reply_text": "", "answer_effect": "unchanged"}
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


def test_long_reply_restores_only_referenced_verbatim_evidence_with_bounded_inference():
    from app.core.prompt.understanding_references import understanding_references
    reply = "先说明任务的实现方式。" * 100 + "没有需要补充的了。"
    refs = understanding_references(reply, [])
    last_id = list(refs["evidence"])[-1]
    class Gateway:
        async def invoke(self, capability, request):
            assert request.max_output_tokens == 350
            assert request.execution_budget.max_provider_retries == 0
            assert request.execution_budget.timeout_s == 8
            assert "evidence_quote" not in request.json_schema["properties"]
            return ChatJSONResponse(data={"intent": "finish", "confidence": .98, "evidence_id": last_id, "reply_text": "", "answer_effect": "updated"},
                usage=Usage(), provider=ProviderMeta(provider_id="synthetic", model="synthetic", request_id="synthetic", latency_ms=0))
    result = asyncio.run(ConversationUnderstandingService(InMemoryStore(), gateway=Gateway()).classify_supplement_reply(reply, "org_default"))
    assert result["evidence_quote"] == refs["evidence"][last_id]
    assert result["evidence_quote"] in reply and len(result["evidence_quote"]) <= 240
    assert set(result) == {"intent", "answer_effect", "confidence", "evidence_quote", "reply_text", "repeat_target"}


def test_schema_diagnostics_identify_rule_without_including_rejected_value():
    contract = prompt_contract("supplement_reply", {"reply": "没有了"})
    request = ChatJSONRequest(purpose="interview_turn_understanding", messages=contract.messages, json_schema=contract.response_schema)
    response = ChatJSONResponse(data={"intent": "finish", "confidence": "private-response-value", "evidence_id": "E1", "reply_text": "", "answer_effect": "unchanged"},
        usage=Usage(), provider=ProviderMeta(provider_id="synthetic", model="synthetic", request_id="synthetic", latency_ms=0))
    with pytest.raises(ProviderError) as result:
        ModelGateway(InMemoryStore())._validate_response("llm.chat_json", request, response)
    assert result.value.details == {"schema_reason": "type", "schema_path": "$.confidence"}
    assert "private-response-value" not in str(result.value) + str(result.value.details)


def test_followup_speaks_validated_question_without_repeating_hesitant_evidence():
    quote = "嗯，我想一下，比如这个工作流，就是我刚才说的那个。"
    question = "你提到工作流扩展，具体如何保存和读取任务状态？"
    result = ConversationUnderstandingService(InMemoryStore())._approved_followup_selection(
        {"id": "turn_synthetic", "followup_depth": 0},
        {"selected": True, "evidence_quote": quote, "question_text": question, "target_capability_points": ["状态管理"]},
        {"policy": {"max_probe_chars": 300}, "root_turn_id": "turn_synthetic", "missing_targets": ["状态管理"]},
        provider={"provider_id": "synthetic"}, prompt_version="interview_turn_decision.v8",
    )
    assert result["question_text"] == question
    assert result["conversation_act"]["text"] == question
    assert result["evidence_quotes"] == [quote]
    assert result["target_key_points"] == ["状态管理"]
