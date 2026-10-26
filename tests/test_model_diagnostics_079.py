import asyncio
import json

import pytest

from app.core import model_diagnostics as diagnostics
from app.core.model_diagnostics import diagnosed_wait_for, model_call_scope
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import CircuitBreaker, ModelGateway
from app.persistence.provider import persistence_for
from app.repositories.sqlite import SQLiteStore
from tests.test_model_invocation import configured_store, request, response, route, ScriptedAdapter


class WaitingAdapter:
    provider_id = "openai_compatible"

    def __init__(self):
        self.started = asyncio.Event()
        self.calls = 0
        self.cancelled = 0

    async def invoke(self, capability, request, context):
        self.calls += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


@pytest.fixture
def events(monkeypatch):
    items = []
    monkeypatch.setattr(diagnostics._LOG, "info", lambda value: items.append(json.loads(value)))
    return items


def store_for(backend, tmp_path):
    memory = configured_store()
    if backend == "memory":
        return memory
    store = SQLiteStore(str(tmp_path / "invocations.sqlite3"))
    for name in ("provider_connections", "model_configurations"):
        getattr(store, name).update(getattr(memory, name))
        store.save_many(name, list(getattr(memory, name).values()))
    store.save_provider_secret("provider_conn_primary", {"api_key": "never-logged"})
    return store


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_external_cancel_is_correlated_durable_and_does_not_retry_or_open_circuit(backend, tmp_path, events):
    store = store_for(backend, tmp_path)
    adapter, circuits = WaitingAdapter(), CircuitBreaker()
    gateway = ModelGateway(store, provider_clients={"openai_compatible": adapter}, circuit_breaker=circuits)
    payload = request()
    payload.metadata.update(interview_id="iv_request", turn_id="turn_request", transcript="private transcript")
    with model_call_scope(interview_id="iv_scope", turn_id="turn_scope", capture_id="capture_scope",
                          stage="answer_preparation") as owner:
        task = asyncio.create_task(gateway.invoke(cap.LLM_CHAT_JSON, payload,
                                                 route=route(retry_count=3, fallback=True)))
        await asyncio.wait_for(adapter.started.wait(), timeout=1)
        start = next(item for item in events if item["event"] == "model.attempt_started")
        assert start["provider_invoked"] is True
        assert store.model_invocations == []  # No duplicate cost row for started.
        owner.cancel("input_changed")
        owner.cancel("capture_closed")  # Cleanup cannot overwrite the first cause.
        task.cancel("secret cancellation message")
        with pytest.raises(asyncio.CancelledError):
            await task
    assert adapter.calls == adapter.cancelled == 1
    assert circuits._states == {}
    if backend == "sqlite":
        store = SQLiteStore(str(tmp_path / "invocations.sqlite3"))
    with persistence_for(store).transaction("org_default", read_only=True) as transaction:
        rows = transaction.model_invocations.list()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "cancelled"
    assert row["error_code"] == "provider_cancelled"
    assert row["cancel_reason"] == "input_changed"
    assert row["interview_id"] == "iv_scope" and row["turn_id"] == "turn_scope"
    assert row["parent_scope_id"] == owner.scope_id
    assert row["diagnostic_scope_id"] == start["diagnostic_scope_id"]
    assert row["provider_invoked"] is True
    assert row["input_tokens"] is row["output_tokens"] is row["total_tokens"] is row["estimated_cost_usd"] is None
    serialized = json.dumps([rows, events])
    for private in ("private transcript", "sensitive candidate answer", "never-logged", "secret cancellation message"):
        assert private not in serialized


@pytest.mark.anyio
async def test_outer_wait_deadline_and_gateway_cancel_have_linked_distinct_facts(events):
    store = configured_store()
    adapter, circuits = WaitingAdapter(), CircuitBreaker()
    gateway = ModelGateway(store, provider_clients={"openai_compatible": adapter}, circuit_breaker=circuits)
    with pytest.raises(asyncio.TimeoutError):
        await diagnosed_wait_for(gateway.invoke(cap.LLM_CHAT_JSON, request(), route=route()), timeout=.01,
                                 stage="turn_decision", timeout_reason="decision_timeout",
                                 interview_id="iv_timeout", turn_id="turn_timeout")
    row = store.model_invocations[0]
    terminal = next(item for item in events if item["event"] == "model.operation_finished")
    assert terminal["outcome"] == "timeout" and terminal["cancel_reason"] == "decision_timeout"
    assert row["status"] == "cancelled" and row["cancel_reason"] == "caller_cancelled"
    assert row["parent_scope_id"] == terminal["diagnostic_scope_id"]
    assert terminal["requested_timeout_ms"] == terminal["effective_timeout_ms"] == 10
    assert row["effective_timeout_ms"] == 1000
    assert adapter.calls == adapter.cancelled == 1 and circuits._states == {}


@pytest.mark.anyio
async def test_child_timeout_is_not_misattributed_to_wait_deadline(events):
    async def child():
        raise asyncio.TimeoutError("private child timeout")
    with pytest.raises(asyncio.TimeoutError):
        await diagnosed_wait_for(child(), timeout=10, stage="followup", timeout_reason="followup_timeout")
    terminal = events[-1]
    assert terminal["cancel_reason"] == "child_timeout"
    assert "private child timeout" not in json.dumps(events)


@pytest.mark.anyio
async def test_gateway_own_timeout_retains_provider_error_and_breaker_behavior(events):
    store = configured_store()
    adapter, circuits = WaitingAdapter(), CircuitBreaker()
    gateway = ModelGateway(store, provider_clients={"openai_compatible": adapter}, circuit_breaker=circuits)
    with pytest.raises(ProviderError) as failure:
        await gateway.invoke(cap.LLM_CHAT_JSON, request(), route=route(timeout_s=.01, fallback=False))
    assert failure.value.code == "provider_timeout"
    assert len(store.model_invocations) == 1 and store.model_invocations[0]["status"] == "failed"
    assert adapter.calls == 1 and next(iter(circuits._states.values())).failures == 1
    assert events[-1]["error_code"] == "provider_timeout"


@pytest.mark.anyio
async def test_concurrent_scopes_and_detached_preparation_keep_cancellation_isolated(events):
    store, adapter = configured_store(), WaitingAdapter()
    gateway = ModelGateway(store, provider_clients={"openai_compatible": adapter})
    async def call(interview_id, cancel_reason):
        with model_call_scope(interview_id=interview_id, turn_id="turn_" + interview_id) as owner:
            task = asyncio.create_task(gateway.invoke(cap.LLM_CHAT_JSON, request(), route=route()))
            await asyncio.sleep(.005)
            owner.cancel(cancel_reason)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    await asyncio.gather(call("iv_a", "input_changed"), call("iv_b", "session_closed"))
    rows = {row["interview_id"]: row for row in store.model_invocations}
    assert rows["iv_a"]["cancel_reason"] == "input_changed"
    assert rows["iv_b"]["cancel_reason"] == "session_closed"
    assert rows["iv_a"]["diagnostic_scope_id"] != rows["iv_b"]["diagnostic_scope_id"]
    with model_call_scope() as waiter:
        with model_call_scope(inherit=False) as independent:
            waiter.cancel("waiter_cancelled")
            assert independent.cancel_reason == "caller_cancelled"
            assert independent.parent_scope_id is None
    assert diagnostics.current_model_call_scope() is None


@pytest.mark.anyio
async def test_diagnostic_and_audit_sink_failures_do_not_replace_cancel_or_provider_failure(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("secret sink details")
    monkeypatch.setattr(diagnostics._LOG, "info", broken)
    store, adapter = configured_store(), WaitingAdapter()
    gateway = ModelGateway(store, provider_clients={"openai_compatible": adapter})
    monkeypatch.setattr(gateway, "_log_invocation", broken)
    task = asyncio.create_task(gateway.invoke(cap.LLM_CHAT_JSON, request(), route=route()))
    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    provider_failure = ProviderError("provider_auth_failed", "private provider error", retryable=False)
    gateway = ModelGateway(store, provider_clients={"openai_compatible": ScriptedAdapter([provider_failure])})
    monkeypatch.setattr(gateway, "_log_invocation", broken)
    with pytest.raises(ProviderError) as caught:
        await gateway.invoke(cap.LLM_CHAT_JSON, request(), route=route())
    assert caught.value is provider_failure


@pytest.mark.anyio
async def test_success_and_preflight_failure_have_safe_attempt_events_without_extra_rows(events):
    store = configured_store()
    adapter = ScriptedAdapter([response()])
    gateway = ModelGateway(store, provider_clients={"openai_compatible": adapter})
    await gateway.invoke(cap.LLM_CHAT_JSON, request(), route=route())
    assert [item["outcome"] for item in events] == ["started", "success"]
    assert len(store.model_invocations) == 1
    invalid_route = route(fallback=False)
    invalid_route["primary"]["model_configuration_id"] = "missing"
    with pytest.raises(ProviderError):
        await gateway.invoke(cap.LLM_CHAT_JSON, request(), route=invalid_route)
    assert events[-1]["provider_invoked"] is False
    assert store.model_invocations[-1]["provider_invoked"] is False
    assert adapter.calls == 1


def test_diagnostic_allowlist_drops_sensitive_payload_and_untrusted_categories(events):
    diagnostics.record_model_diagnostic("understanding_rejected", interview_id="iv_safe", turn_id="turn_safe",
        failure_reason="private rejected answer", validation_stage="private rejected answer",
        schema_path="$.understanding.claims[37]", attempt=1, transcript="private transcript",
        messages=[{"content": "private prompt"}], response={"body": "private output"}, api_key="never-logged",
        exception="private exception", provider_request_id="private request with spaces")
    assert events == [{"event": "model.understanding_rejected", "at": events[0]["at"],
                       "interview_id": "iv_safe", "turn_id": "turn_safe", "failure_reason": "unknown",
                       "validation_stage": "unknown", "schema_path": "$.understanding.claims[]", "attempt": 1}]


@pytest.mark.anyio
@pytest.mark.parametrize("sink", ["audit", "circuit"])
async def test_successful_provider_has_one_terminal_when_post_call_audit_fails(monkeypatch, events, sink):
    store, adapter = configured_store(), ScriptedAdapter([response()])
    circuits = CircuitBreaker()
    gateway = ModelGateway(store, provider_clients={"openai_compatible": adapter}, circuit_breaker=circuits)
    failure = OSError("private audit path")
    def broken(*args, **kwargs):
        raise failure
    monkeypatch.setattr(gateway if sink == "audit" else circuits,
                        "_log_invocation" if sink == "audit" else "record_success", broken)
    with pytest.raises(OSError) as caught:
        await gateway.invoke(cap.LLM_CHAT_JSON, request(), route=route(retry_count=3))
    assert caught.value is failure
    assert adapter.calls == 1
    assert [item["event"] for item in events] == ["model.attempt_started", "model.attempt_finished", "model.audit_failed"]
    assert events[1]["outcome"] == "success"
    assert "private audit path" not in json.dumps(events)


@pytest.mark.anyio
async def test_unexpected_adapter_failure_preserves_error_and_has_durable_safe_failure(events):
    failure = RuntimeError("private candidate response body")
    adapter, store, circuits = ScriptedAdapter([failure]), configured_store(), CircuitBreaker()
    gateway = ModelGateway(store, provider_clients={"openai_compatible": adapter}, circuit_breaker=circuits)
    with pytest.raises(RuntimeError) as caught:
        await gateway.invoke(cap.LLM_CHAT_JSON, request(), route=route(retry_count=3))
    assert caught.value is failure
    assert adapter.calls == 1 and circuits._states == {}
    assert len(store.model_invocations) == 1
    assert store.model_invocations[0]["error_code"] == "provider_unexpected_error"
    assert events[-1]["outcome"] == "failed"
    assert "private candidate response body" not in json.dumps([events, store.model_invocations])


@pytest.mark.anyio
async def test_observation_does_not_clip_deadline_or_interfere_with_successful_result(events):
    from app.core.interaction_budget import interaction_deadline
    from time import monotonic
    with interaction_deadline(monotonic() - 1):
        result = await diagnosed_wait_for(asyncio.sleep(0, result="unchanged"), timeout=1,
                                          stage="followup", timeout_reason="followup_timeout")
    assert result == "unchanged"
    assert events[-1]["outcome"] == "success"
    assert events[0]["interaction_remaining_ms"] == 0
    assert events[0]["effective_timeout_ms"] == 1000
