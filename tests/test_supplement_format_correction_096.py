"""Format correction stays within one HTTP attempt and never unwraps model data."""
import json

import httpx
import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.validation import StructuredResponseValidationError
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.providers.dashscope.provider import DashScopeProvider
from app.repositories.memory import InMemoryStore
from app.services.conversation_understanding import ConversationUnderstandingService
from app.services.model_admin import ModelAdminService


REPLY = "合成测试：这题我已经说完，没有需要补充的了。"
CONTEXT = {"stage": "awaiting_reply", "reply_target": "supplement_question",
           "current_question": "请说明任务队列的作用。", "root_question": "请说明任务队列的作用。",
           "last_delivered_utterance": "你还有需要补充的吗？"}


def valid(**overrides):
    return {"intent": "finish", "confidence": .96, "evidence_id": "E1", "reply_text": "",
            "answer_effect": "unchanged", **overrides}


def service_with_wire_responses(responses):
    """Real gateway/provider, synthetic RAM config and strict mocked HTTP only."""
    store = InMemoryStore()
    admin = ModelAdminService(store)
    connection = admin.create_provider_connection({"provider_id": "dashscope", "display_name": "synthetic",
        "credentials": {"api_key": "synthetic-private-key"}, "connection_config": {}})
    models = [admin.create_model_configuration({"provider_connection_id": connection["id"],
        "model_type": "llm", "provider_model_id": name, "display_name": "synthetic"})
        for name in ("qwen3.7-plus", "qwen-plus")]
    for model in models:
        store.model_configurations[model["id"]]["status"] = "ready"
    admin.create_route({"capability": "llm.chat_json", "purpose": "interview_turn_understanding",
        "primary": {"model_configuration_id": models[0]["id"], "timeout_s": 30},
        "fallbacks": [{"model_configuration_id": models[1]["id"], "timeout_s": 30}],
        "policy": {"retry_count": 3, "retry_backoff_ms": 0, "circuit_failure_threshold": 0}})
    calls = []
    pending = iter(responses)

    def handler(request):
        calls.append({"payload": json.loads(request.content), "timeout": request.extensions.get("timeout")})
        selected = next(pending)
        if isinstance(selected, int):
            return httpx.Response(selected, json={"error": {"message": "synthetic rejected request"}})
        return httpx.Response(200, json={"id": "synthetic_request", "choices": [
            {"message": {"content": selected}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 31, "total_tokens": 41}})

    adapter = DashScopeProvider(client_factory=lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), **kw))
    gateway = ModelGateway(store, provider_clients={"dashscope": adapter})
    return ConversationUnderstandingService(store, gateway=gateway), calls, store


def assert_single_wire(call):
    payload = call["payload"]
    assert payload["model"] == "qwen3.7-plus" and payload["temperature"] == 0
    assert payload["max_tokens"] == 350 and payload["enable_thinking"] is False
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["response_format"]["json_schema"]["schema"]["type"] == "object"
    assert all(value == 8 for value in call["timeout"].values())


def test_correction_changes_version_and_instruction_not_evidence_or_schema():
    context = {"reply": REPLY, "conversation_context": CONTEXT}
    normal = prompt_contract("supplement_reply", context)
    corrected = prompt_contract("supplement_reply", {**context, "correction_reason": "invalid_json_object"})
    assert normal.version == "supplement_reply.v11" and corrected.version == "supplement_reply.v12"
    assert normal.response_schema == corrected.response_schema
    assert corrected.response_schema["type"] == "object"
    assert "answer_effect" in corrected.response_schema["required"]
    assert set(corrected.response_schema["properties"]["answer_effect"]["enum"]) == {"unchanged", "updated", "uncertain"}
    assert [m.content for m in normal.messages if m.role == "user"] == [m.content for m in corrected.messages if m.role == "user"]
    assert corrected.messages != normal.messages
    assert normal.response_schema["additionalProperties"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("rejected", [[], "PRIVATE_REJECTED_RESPONSE", 7, True, None,
    json.dumps(valid()), [valid()]])
async def test_second_external_attempt_is_changed_single_http_request_without_unwrapping(rejected):
    service, calls, store = service_with_wire_responses([json.dumps(rejected), json.dumps(valid())])
    with pytest.raises(ProviderError) as caught:
        await service.classify_supplement_reply(REPLY, "org_default", conversation_context=CONTEXT)
    assert caught.value.code == "provider_schema_invalid"
    assert caught.value.details["json_failure_kind"] == "invalid_json_object"
    assert caught.value.details["json_decode_reason"] == "non_object"
    assert len(calls) == 1
    result = await service.classify_supplement_reply(REPLY, "org_default", conversation_context=CONTEXT,
        correction_reason="invalid_json_object")
    assert result["intent"] == "finish" and result["answer_effect"] == "unchanged"
    assert result["evidence_quote"] == REPLY and result["reply_text"] == ""
    assert len(calls) == 2
    for call in calls:
        assert_single_wire(call)
    first, second = (item["payload"] for item in calls)
    assert first["response_format"] == second["response_format"]
    assert first["messages"] != second["messages"]
    assert [m for m in first["messages"] if m["role"] == "user"] == [m for m in second["messages"] if m["role"] == "user"]
    assert "PRIVATE_REJECTED_RESPONSE" not in json.dumps(second)
    assert [item["prompt_version"] for item in store.model_invocations] == ["supplement_reply.v11", "supplement_reply.v12"]
    assert all(item["attempt"] == 1 and item["fallback_index"] == 0 for item in store.model_invocations)


@pytest.mark.anyio
async def test_invalid_correction_does_not_hide_a_third_call_or_accept_wrapped_object():
    service, calls, store = service_with_wire_responses([json.dumps([valid()]), json.dumps(json.dumps(valid()))])
    for correction in (None, "invalid_json_object"):
        with pytest.raises(ProviderError) as caught:
            await service.classify_supplement_reply(REPLY, "org_default", conversation_context=CONTEXT,
                **({"correction_reason": correction} if correction else {}))
        assert caught.value.details["json_decode_reason"] == "non_object"
    assert len(calls) == len(store.model_invocations) == 2
    assert all(row["status"] == "failed" and row["attempt"] == 1 for row in store.model_invocations)


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 401, 429, 500])
@pytest.mark.parametrize("correction", [None, "invalid_json_object"])
async def test_format_mode_never_hides_http_retry_or_json_object_downgrade(status, correction):
    service, calls, _ = service_with_wire_responses([status])
    with pytest.raises(ProviderError):
        await service.classify_supplement_reply(REPLY, "org_default", conversation_context=CONTEXT,
            **({"correction_reason": correction} if correction else {}))
    assert len(calls) == 1
    assert_single_wire(calls[0])


@pytest.mark.anyio
@pytest.mark.parametrize("change", [{"evidence_id": "E999"}, {"confidence": True},
    {"confidence": 1.2}, {"answer_effect": "invented"}, {"extra": "rejected"},
    {"reply_text": "这里是未授权的生成发言。"}])
async def test_format_correction_still_enforces_full_schema_and_semantic_rules(change):
    service, calls, _ = service_with_wire_responses([json.dumps(valid(**change))])
    with pytest.raises((ProviderError, StructuredResponseValidationError)):
        await service.classify_supplement_reply(REPLY, "org_default", conversation_context=CONTEXT,
            correction_reason="invalid_json_object")
    assert len(calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("intent,effect,spoken", [
    ("supplement", "updated", ""), ("stop_interview", "unchanged", ""),
    ("unclear", "uncertain", "我想确认一下，你现在希望怎么继续？"),
])
async def test_format_repair_does_not_coerce_control_or_uncertain_intent_to_finish(intent, effect, spoken):
    service, calls, _ = service_with_wire_responses([json.dumps(valid(intent=intent, answer_effect=effect, reply_text=spoken))])
    result = await service.classify_supplement_reply(REPLY, "org_default", conversation_context=CONTEXT,
        correction_reason="invalid_json_object")
    assert result["intent"] == intent and result["answer_effect"] == effect
    assert len(calls) == 1


@pytest.mark.anyio
async def test_format_correction_cannot_accept_unresolved_transcript_dispute_as_unchanged():
    service, calls, _ = service_with_wire_responses([json.dumps(valid(
        intent="transcript_correction", answer_effect="unchanged",
        reply_text="你可以说一下需要更正的内容。"))])
    with pytest.raises(StructuredResponseValidationError):
        await service.classify_supplement_reply(
            "刚才字幕里的工具名称不是我说的。", "org_default", conversation_context=CONTEXT,
            correction_reason="invalid_json_object")
    assert len(calls) == 1
    assert_single_wire(calls[0])
