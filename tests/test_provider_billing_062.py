"""Synthetic HTTP billing errors remain distinct, safe and non-retrying."""
import json

import httpx
import pytest

from app.core.errors import provider_error_handler
from app.domain.model_route_readiness import safe_probe_code
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import ChatJSONRequest, ChatMessage
from app.repositories.memory import InMemoryStore
from app.services.interviews import InterviewService
from app.services.review import EnterpriseReviewService
from test_interview_processing_recovery import ORG, pending_session
from test_openai_compatible_provider import make_provider
from test_model_route_readiness import fixture as readiness_fixture, refresher, state, stored


PRIVATE = "synthetic-secret-and-candidate-content"
CODE = "provider_billing_required"


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["_post", "_get", "_post_binary"])
@pytest.mark.parametrize("status,code,retryable,api_status", [
    (402, CODE, False, 503),
    (400, "provider_bad_request", False, 400),
    (401, "provider_auth_failed", False, 400),
    (429, "provider_rate_limited", True, 429),
])
async def test_http_errors_keep_safe_distinct_classification(method, status, code, retryable, api_status):
    provider = make_provider(lambda _: httpx.Response(status, json={"error": {"message": PRIVATE}}))
    args = ["https://synthetic.invalid/v1/test"]
    if method != "_get":
        args.append({"input": PRIVATE})
    with pytest.raises(ProviderError) as raised:
        await getattr(provider, method)(*args, api_key=PRIVATE, timeout_s=1)
    error = raised.value
    assert (error.code, error.retryable) == (code, retryable)
    assert PRIVATE not in str(error) + json.dumps(error.details)
    response = await provider_error_handler(None, error)
    assert response.status_code == api_status
    body = json.loads(response.body)
    assert body["error"]["code"] == code
    assert body["error"]["details"]["retryable"] is retryable
    assert PRIVATE not in response.body.decode()
    if status == 402:
        assert error.details == {"http_status": 402}
        assert "计费或额度" in body["error"]["message"]
        assert safe_probe_code(code) == code


@pytest.mark.anyio
async def test_gateway_billing_is_one_attempt_with_402_audit_even_when_retry_configured():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(402, json={"error": {"message": PRIVATE}})
    store = InMemoryStore()
    store.provider_connections["billing_connection"] = {
        "id": "billing_connection", "organization_id": "org_default",
        "provider_id": "openai_compatible", "enabled": True,
        "connection_config": {"base_url": "https://synthetic.invalid/v1"},
        "credential_ref": "secret://provider-connections/billing_connection", "credential_status": "valid",
    }
    store.save_provider_secret("billing_connection", {"api_key": PRIVATE})
    store.model_configurations["billing_model"] = {
        "id": "billing_model", "organization_id": "org_default", "provider_connection_id": "billing_connection",
        "provider_id": "openai_compatible", "provider_model_id": "synthetic", "model_type": "llm",
        "supported_capabilities": [cap.LLM_CHAT_JSON], "enabled": True, "status": "ready",
        "settings": {}, "default_parameters": {},
    }
    store.model_routes["billing_route"] = {
        "id": "billing_route", "organization_id": "org_default", "capability": cap.LLM_CHAT_JSON,
        "purpose": "provider_test", "enabled": True,
        "primary": {"model_configuration_id": "billing_model", "timeout_s": 1},
        "fallbacks": [], "policy": {"retry_count": 3, "retry_backoff_ms": 0},
    }
    gateway = ModelGateway(store, provider_clients={"openai_compatible": make_provider(handler)})
    with pytest.raises(ProviderError) as raised:
        await gateway.invoke(cap.LLM_CHAT_JSON, ChatJSONRequest(
            purpose="provider_test", messages=[ChatMessage(role="user", content=PRIVATE)]))
    assert raised.value.code == CODE and not raised.value.retryable
    assert len(calls) == 1
    assert len(store.model_invocations) == 1
    record = store.model_invocations[0]
    assert (record["error_code"], record["http_status"], record["attempt"]) == (CODE, 402, 1)
    assert PRIVATE not in json.dumps(record)


@pytest.mark.anyio
async def test_failed_evaluation_persists_billing_reason_and_preserves_answer():
    store = InMemoryStore()
    session, persistence = pending_session(store)
    service = InterviewService(store, persistence=persistence)
    service.retry_processing(session["id"], ORG)
    with persistence.transaction(ORG) as tx:
        work = tx.outbox.list()[0]

    async def fail(*_):
        raise ProviderError(CODE, PRIVATE, retryable=False, details={"http_status": 402})
    service.evaluation.evaluate_answer = fail
    with pytest.raises(ProviderError):
        await service.process_outbox_work(work["id"], ORG)
    with persistence.transaction(ORG) as tx:
        failed = tx.outbox.get(work["id"])
        saved = tx.interview_sessions.get(session["id"])
    assert failed["status"] == "dead_letter" and failed["attempt_count"] == 1
    assert failed["last_error_code"] == CODE and failed["error_retryable"] is False
    assert "计费或额度" in failed["last_error"]
    event = next(item for item in saved["lifecycle_events"] if item["type"] == "evaluation.failed")
    assert "计费或额度" in event["payload"]["error"]
    assert PRIVATE not in json.dumps([failed, event])
    assert [a["final_transcript"] for a in saved["answers"]] == [a["final_transcript"] for a in session["answers"]]
    projection = EnterpriseReviewService(store, persistence=persistence).get_review(session["id"], ORG)
    assert projection["processing"]["failed"] == 1
    assert any(turn.get("answer", {}).get("evaluation_failure_code") == CODE for turn in projection["turns"])


@pytest.mark.anyio
async def test_billing_api_message_never_uses_provider_error_text():
    response = await provider_error_handler(None, ProviderError(
        CODE, PRIVATE, retryable=False, details={"http_status": 402}))
    assert response.status_code == 503
    assert PRIVATE not in response.body.decode()
    assert "计费或额度" in json.loads(response.body)["error"]["message"]


@pytest.mark.anyio
async def test_route_health_preserves_billing_code_without_provider_text():
    service, _ = readiness_fixture()
    calls = []
    async def probe(*_):
        calls.append(1)
        raise ProviderError(CODE, PRIVATE, retryable=False, details={"http_status": 402})
    refresher(service, probe)
    await service.refresh_routes(["route_0"])
    await service.refresh_routes(["route_0"])
    assert calls == [1]
    assert state(service)["reason_code"] == CODE
    assert stored(service)["last_health"]["retryable"] is False
    assert PRIVATE not in json.dumps(stored(service))
