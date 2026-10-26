"""HTTP transport compatibility cannot exceed a trusted single-attempt cap."""
import json

import httpx
import pytest

from app.core.answer_processing_budget import AnswerProcessingBudget, answer_processing_budget
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import ChatJSONRequest, ChatMessage, InvocationExecutionBudget
from app.services.conversation_understanding import ConversationUnderstandingService
from test_dashscope_provider import context, make_provider
from test_model_invocation import configured_store, route
from test_prepared_turn_decision import _input


@pytest.mark.anyio
@pytest.mark.parametrize("budget,expected_requests", [
    (None, 2),
    (InvocationExecutionBudget(timeout_s=8, max_provider_retries=0), 2),
    (InvocationExecutionBudget(timeout_s=18, max_provider_attempts=1), 1),
    (InvocationExecutionBudget(timeout_s=18, max_provider_retries=3, max_provider_attempts=1), 1),
])
async def test_schema_transport_fallback_respects_explicit_single_attempt_only(budget, expected_requests):
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx.Response(400, json={"error": {"message": "unsupported schema"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    request = ChatJSONRequest(
        purpose="interview_turn_understanding",
        messages=[ChatMessage(role="user", content="synthetic input")],
        json_schema={"type": "object", "required": ["ok"], "additionalProperties": False,
                     "properties": {"ok": {"type": "boolean"}}},
        execution_budget=budget,
    )
    ctx = context(cap.LLM_CHAT_JSON, "qwen3.7-plus").model_copy(update={"purpose": request.purpose})
    if expected_requests == 1:
        with pytest.raises(ProviderError) as rejected:
            await make_provider(handler).invoke(cap.LLM_CHAT_JSON, request, ctx)
        assert rejected.value.code == "provider_bad_request"
    else:
        result = await make_provider(handler).invoke(cap.LLM_CHAT_JSON, request, ctx)
        assert result.data == {"ok": True}
    assert len(payloads) == expected_requests
    assert [payload["response_format"]["type"] for payload in payloads] == ["json_schema", "json_object"][:expected_requests]
    assert all("execution_budget" not in payload for payload in payloads)


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 503])
async def test_real_gateway_and_understanding_budget_count_each_dashscope_http_attempt(status):
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(status, json={"error": {"message": "synthetic rejection"}})

    store = configured_store()
    store.provider_connections["provider_conn_primary"].update(
        provider_id="dashscope",
        connection_config={"base_url": "https://dashscope.example.test/compatible-mode/v1"},
    )
    store.model_configurations["model_cfg_primary"].update(
        provider_id="dashscope", provider_model_id="qwen3.7-plus",
    )
    invocation_route = route(retry_count=3, fallback=False)
    invocation_route["purpose"] = "interview_turn_understanding"
    store.model_routes[invocation_route["id"]] = invocation_route
    gateway = ModelGateway(store, provider_clients={"dashscope": make_provider(handler)})
    service = ConversationUnderstandingService(store, gateway=gateway)
    utterance, turn, interview = _input()
    budget = AnswerProcessingBudget(limit=2)

    with answer_processing_budget(budget):
        first, _ = await service.prepare_decision(utterance, turn, interview)
        second, _ = await service.prepare_decision(utterance, turn, interview)
        exhausted, _ = await service.prepare_decision(utterance, turn, interview)

    assert first.problem is not None and second.problem is not None
    assert exhausted.problem.reason_code == "call_budget_exhausted"
    assert budget.used == len(payloads) == len(store.model_invocations) == 2
    assert all(item["status"] == "failed" for item in store.model_invocations)
    assert all(item["attempt"] == 1 and item["fallback_index"] == 0 for item in store.model_invocations)
    assert [payload["response_format"]["type"] for payload in payloads] == ["json_schema", "json_schema"]
