"""Real understanding/endpoint/gateway cancellation, without external calls."""
import asyncio
import json
import logging
import time

import pytest

from app.core.answer_processing_budget import AnswerProcessingBudget, answer_processing_budget
from app.core.model_diagnostics import model_call_scope
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.services.answer_endpoint import InteractionBudgetExpired
from app.services.conversation_understanding import ConversationUnderstandingService
from test_answer_endpoint import _endpoint
from test_model_invocation import configured_store, route
from test_prepared_turn_decision import Gateway, _data, _input, _service


@pytest.fixture
def diagnostics():
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            try:
                records.append(json.loads(record.getMessage()))
            except ValueError:
                pass

    logger = logging.getLogger("interviewer.speech_evidence")
    previous = logger.level
    handler = Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


@pytest.mark.anyio
async def test_real_endpoint_deadline_correlates_cancelled_gateway_and_retains_cancellation(diagnostics):
    store = configured_store()
    started, cancelled = asyncio.Event(), asyncio.Event()

    class HangingAdapter:
        provider_id = "openai_compatible"

        async def invoke(self, *_args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    class BoundGateway(ModelGateway):
        async def invoke(self, capability, request, **kwargs):
            selected = route(retry_count=2, fallback=True)
            selected["purpose"] = request.purpose
            return await super().invoke(capability, request, route=selected)

    gateway = BoundGateway(store, provider_clients={"openai_compatible": HangingAdapter()})
    service = ConversationUnderstandingService(store, gateway=gateway)
    utterance, turn, interview = _input()
    endpoint, _, _, commits = _endpoint()
    endpoint._interaction_deadline = time.monotonic() + .15
    with model_call_scope(interview_id=interview["id"], turn_id=turn["id"], stage="answer_preparation"):
        with pytest.raises(InteractionBudgetExpired):
            await endpoint._infer(service.prepare_decision(utterance, turn, interview), timeout=1)

    assert started.is_set() and cancelled.is_set()
    assert not commits
    assert len(store.model_invocations) == 1
    audit = store.model_invocations[0]
    assert audit["status"] == "cancelled"
    assert audit["interview_id"] == interview["id"] and audit["turn_id"] == turn["id"]
    assert audit["cancel_reason"] == "interaction_timeout"
    assert any(r.get("diagnostic_scope_id") == audit["diagnostic_scope_id"] for r in diagnostics)
    assert not store.model_circuit_states
    serialized = json.dumps(diagnostics, ensure_ascii=False) + json.dumps(store.model_invocations, ensure_ascii=False)
    assert utterance.text not in serialized and "never-logged" not in serialized


@pytest.mark.anyio
async def test_provider_failure_diagnostics_keep_reason_and_no_sensitive_body(diagnostics):
    utterance, turn, interview = _input()
    service = _service(Gateway(error=ProviderError(
        "provider_rate_limited", "PRIVATE_PROVIDER_RESPONSE_WITH_CREDENTIAL", retryable=True,
    )))
    understanding, followup = await service.prepare_decision(utterance, turn, interview)
    assert understanding.problem.reason_code == "provider_rate_limited"
    assert not followup["selected"]
    failures = [r for r in diagnostics if r.get("event", "").endswith("understanding_failed")]
    assert len(failures) == 1
    assert failures[0]["failure_reason"] == "provider_rate_limited"
    assert failures[0]["turn_id"] == turn["id"]
    assert "PRIVATE_PROVIDER_RESPONSE_WITH_CREDENTIAL" not in json.dumps(diagnostics)


@pytest.mark.anyio
async def test_contract_rejection_and_budget_exhaustion_are_distinct_without_extra_calls(diagnostics):
    utterance, turn, interview = _input()
    invalid = _data()
    invalid["understanding"]["claims"][0]["evidence_id"] = "PRIVATE_INVALID_REFERENCE"
    gateway = Gateway(invalid)
    service = _service(gateway)
    budget = AnswerProcessingBudget(limit=1)
    with answer_processing_budget(budget):
        understanding, followup = await service.prepare_decision(utterance, turn, interview)
        assert understanding.problem.reason_code == "wire_schema_invalid"
        assert understanding.problem.source == "schema_or_content"
        assert not followup["selected"]
        # The completed call retains its actual rejection. A later attempt
        # against the spent allowance is distinct and cannot invoke a model.
        blocked, blocked_followup = await service.prepare_decision(utterance, turn, interview)
    assert len(gateway.requests) == 1
    assert budget.used == 1
    assert blocked.problem.reason_code == "call_budget_exhausted"
    assert not blocked_followup["selected"]
    assert any(r.get("event", "").endswith("understanding_rejected") for r in diagnostics)
    failures = [r for r in diagnostics if r.get("event", "").endswith("understanding_failed")]
    assert [r["failure_reason"] for r in failures] == ["wire_schema_invalid", "call_budget_exhausted"]
    assert "PRIVATE_INVALID_REFERENCE" not in json.dumps(diagnostics)


@pytest.mark.anyio
async def test_instrumented_waits_keep_original_model_budget_and_result(diagnostics):
    utterance, turn, interview = _input()
    gateway = Gateway(_data())
    understanding, followup = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and followup["selected"]
    assert len(gateway.requests) == 1
    budget = gateway.requests[0].execution_budget
    assert budget.timeout_s == 18 and budget.max_provider_retries == 0 and budget.max_provider_attempts == 1
    starts = [r for r in diagnostics if r.get("event", "").endswith("operation_started")]
    ends = [r for r in diagnostics if r.get("event", "").endswith("operation_finished")]
    assert starts and len(starts) == len(ends)
    assert {r["diagnostic_scope_id"] for r in starts} == {r["diagnostic_scope_id"] for r in ends}
