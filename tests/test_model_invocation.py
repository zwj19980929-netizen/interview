import asyncio

import pytest

from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import CircuitBreaker, ModelGateway
from app.model_gateway.schemas import (
    ChatJSONRequest,
    ChatJSONResponse,
    ChatMessage,
    ProviderMeta,
    Usage,
)
from app.repositories.memory import InMemoryStore


def response(data=None, *, input_tokens=0, output_tokens=0) -> ChatJSONResponse:
    return ChatJSONResponse(
        data=data or {"score": 80, "summary": "ok"},
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
        provider=ProviderMeta(
            provider_id="openai_compatible",
            model="scripted-model",
            request_id="scripted-request",
            latency_ms=0,
        ),
    )


class ScriptedAdapter:
    provider_id = "openai_compatible"

    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def invoke(self, capability, request, context):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class SlowAdapter:
    provider_id = "openai_compatible"

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, capability, request, context):
        self.calls += 1
        await asyncio.sleep(0.05)
        return response()


def configured_store() -> InMemoryStore:
    store = InMemoryStore()
    store.provider_configs["mpc_primary"] = {
        "id": "mpc_primary",
        "organization_id": "org_default",
        "provider_id": "openai_compatible",
        "display_name": "Primary",
        "enabled": True,
        "config": {
            "pricing": {
                "input_per_million_tokens": 1.0,
                "output_per_million_tokens": 2.0,
            }
        },
        "credential_ref": "secret://model-providers/mpc_primary",
        "created_at": "2026-08-24T00:00:00Z",
        "updated_at": "2026-08-24T00:00:00Z",
    }
    store.provider_configs["mpc_fallback"] = {
        "id": "mpc_fallback",
        "organization_id": "org_default",
        "provider_id": "mock",
        "display_name": "Fallback",
        "enabled": True,
        "config": {},
        "credential_ref": "secret://model-providers/mpc_fallback",
        "created_at": "2026-08-24T00:00:00Z",
        "updated_at": "2026-08-24T00:00:00Z",
    }
    store.save_provider_secret("mpc_primary", {"api_key": "never-logged"})
    return store


def request() -> ChatJSONRequest:
    return ChatJSONRequest(
        purpose="answer_evaluation",
        messages=[ChatMessage(role="user", content="sensitive candidate answer")],
        json_schema={
            "type": "object",
            "required": ["score", "summary"],
            "properties": {
                "score": {"type": "integer", "minimum": 0, "maximum": 100},
                "summary": {"type": "string"},
            },
        },
        metadata={"answer_text": "candidate answer", "key_points": []},
    )


def route(*, retry_count=0, timeout_s=1.0, fallback=True, policy=None):
    return {
        "id": "route_fault_matrix",
        "organization_id": "org_default",
        "capability": cap.LLM_CHAT_JSON,
        "purpose": "answer_evaluation",
        "primary": {
            "provider_config_id": "mpc_primary",
            "model": "scripted-model",
            "timeout_s": timeout_s,
        },
        "fallbacks": [
            {
                "provider_config_id": "mpc_fallback",
                "model": "mock-json",
                "timeout_s": 1,
            }
        ] if fallback else [],
        "policy": {"retry_count": retry_count, **(policy or {})},
        "enabled": True,
    }


@pytest.mark.anyio
async def test_invocation_retries_once_and_audits_attempts_cost_and_hash() -> None:
    store = configured_store()
    adapter = ScriptedAdapter(
        [
            ProviderError("provider_timeout", "first attempt timed out", retryable=True),
            response(input_tokens=1_000_000, output_tokens=500_000),
        ]
    )
    gateway = ModelGateway(store, provider_clients={"openai_compatible": adapter})

    result = await gateway.invoke(cap.LLM_CHAT_JSON, request(), route=route(retry_count=1, fallback=False))

    assert result.data["score"] == 80
    assert adapter.calls == 2
    assert [item["status"] for item in store.model_invocations] == ["failed", "success"]
    assert [item["attempt"] for item in store.model_invocations] == [1, 2]
    assert len({item["invocation_id"] for item in store.model_invocations}) == 1
    assert len({item["redacted_request_hash"] for item in store.model_invocations}) == 1
    assert len(store.model_invocations[0]["redacted_request_hash"]) == 64
    assert store.model_invocations[-1]["estimated_cost_usd"] == 2.0
    serialized_logs = str(store.model_invocations)
    assert "sensitive candidate answer" not in serialized_logs
    assert "never-logged" not in serialized_logs


@pytest.mark.anyio
async def test_schema_invalid_primary_falls_back_through_same_pipeline() -> None:
    store = configured_store()
    adapter = ScriptedAdapter([response({"unexpected": True})])
    gateway = ModelGateway(store, provider_clients={"openai_compatible": adapter})

    result = await gateway.invoke(
        cap.LLM_CHAT_JSON,
        request(),
        route=route(policy={"fallback_on": ["schema_invalid"]}),
    )

    assert result.provider.provider_id == "mock"
    assert [item["status"] for item in store.model_invocations] == ["failed", "fallback_success"]
    assert store.model_invocations[0]["error_code"] == "provider_schema_invalid"
    assert store.model_invocations[1]["provider_config_id"] == "mpc_fallback"


@pytest.mark.anyio
async def test_pipeline_timeout_opens_circuit_and_future_call_skips_primary() -> None:
    store = configured_store()
    adapter = SlowAdapter()
    gateway = ModelGateway(
        store,
        provider_clients={"openai_compatible": adapter},
        circuit_breaker=CircuitBreaker(),
    )
    invocation_route = route(
        timeout_s=0.01,
        policy={"circuit_failure_threshold": 1, "circuit_recovery_seconds": 60},
    )

    first = await gateway.invoke(cap.LLM_CHAT_JSON, request(), route=invocation_route)
    second = await gateway.invoke(cap.LLM_CHAT_JSON, request(), route=invocation_route)

    assert first.provider.provider_id == second.provider.provider_id == "mock"
    assert adapter.calls == 1
    assert [item["error_code"] for item in store.model_invocations if item["status"] == "failed"] == [
        "provider_timeout",
        "provider_circuit_open",
    ]


@pytest.mark.anyio
async def test_default_circuit_breaker_is_shared_through_persistence() -> None:
    store = configured_store()
    adapter = SlowAdapter()
    invocation_route = route(
        timeout_s=0.01,
        policy={"circuit_failure_threshold": 1, "circuit_recovery_seconds": 60},
    )
    first_gateway = ModelGateway(store, provider_clients={"openai_compatible": adapter})
    second_gateway = ModelGateway(store, provider_clients={"openai_compatible": adapter})

    await first_gateway.invoke(cap.LLM_CHAT_JSON, request(), route=invocation_route)
    await second_gateway.invoke(cap.LLM_CHAT_JSON, request(), route=invocation_route)

    assert adapter.calls == 1
    assert len(store.model_circuit_states) == 1
    assert any(
        item.get("error_code") == "provider_circuit_open"
        for item in store.model_invocations
    )


@pytest.mark.anyio
async def test_non_retryable_auth_error_does_not_fall_back() -> None:
    store = configured_store()
    adapter = ScriptedAdapter(
        [ProviderError("provider_auth_failed", "credentials rejected", retryable=False)]
    )
    gateway = ModelGateway(store, provider_clients={"openai_compatible": adapter})

    with pytest.raises(ProviderError) as exc_info:
        await gateway.invoke(cap.LLM_CHAT_JSON, request(), route=route())

    assert exc_info.value.code == "provider_auth_failed"
    assert exc_info.value.details["attempts"] == 1
    assert len(store.model_invocations) == 1
    assert store.model_invocations[0]["provider_id"] == "openai_compatible"


@pytest.mark.anyio
async def test_production_requires_an_explicit_model_route(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    store = InMemoryStore()
    gateway = ModelGateway(store)

    with pytest.raises(ProviderError) as exc_info:
        await gateway.invoke(cap.LLM_CHAT_JSON, request())

    assert exc_info.value.code == "provider_route_missing"

    store.model_routes["route_default"] = {
        "id": "route_default",
        "organization_id": "org_default",
        "capability": cap.LLM_CHAT_JSON,
        "purpose": "default",
        "primary": {"provider_config_id": "mpc_mock", "model": "mock-json", "timeout_s": 1},
        "fallbacks": [],
        "policy": {},
        "enabled": True,
    }
    with pytest.raises(ProviderError) as default_exc_info:
        await gateway.invoke(cap.LLM_CHAT_JSON, request())

    assert default_exc_info.value.code == "provider_route_missing"
