"""Answer diagnostics preserve cancellation, evidence and timing policy."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app.core import model_diagnostics, speech_diagnostics
from app.core.errors import ApiError
from app.core.model_diagnostics import current_model_call_scope
from app.services.answer_endpoint import InteractionBudgetExpired
from app.services.speculative_turn_preparation import SpeculativeTurnPreparation
from test_answer_endpoint import _endpoint, _final
from test_stable_preview_preparation import _preview


CONTEXT = {"interview_id": "iv_diagnostic", "turn_id": "turn_diagnostic", "capture_id": "capture_diagnostic"}


@pytest.fixture
def rows(monkeypatch):
    values = []
    logger = SimpleNamespace(info=lambda message: values.append(json.loads(message)))
    monkeypatch.setattr(model_diagnostics, "_LOG", logger)
    monkeypatch.setattr(speech_diagnostics, "_LOG", logger)
    return values


def endpoint_with_context():
    endpoint, clock, notices, commits = _endpoint()
    endpoint._diagnostic_context = dict(CONTEXT)
    endpoint.preparation._diagnostic_context = dict(CONTEXT)
    return endpoint, clock, notices, commits


def events(rows, name):
    return [row for row in rows if row["event"] == "model." + name]


@pytest.mark.anyio
async def test_remaining_budget_is_observed_and_timeout_reaches_child_before_cancellation(rows):
    endpoint, _, _, commits = endpoint_with_context()
    endpoint.cover_transcript(_final())
    endpoint._interaction_deadline = time.monotonic() + .04
    cancelled = []

    async def work():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            scope = current_model_call_scope()
            cancelled.append((scope.scope_id, scope.cancel_reason))
            raise

    with pytest.raises(InteractionBudgetExpired):
        await endpoint._infer(work(), timeout=18)
    started, finished = events(rows, "scope_started")[0], events(rows, "scope_finished")[0]
    assert started["requested_timeout_ms"] == 18000
    assert 0 <= started["effective_timeout_ms"] <= 40
    assert 0 <= started["interaction_remaining_ms"] <= 40
    assert all(started[key] == value for key, value in CONTEXT.items())
    assert finished["outcome"] == "timeout" and finished["cancel_reason"] == "interaction_timeout"
    assert cancelled == [(started["diagnostic_scope_id"], "interaction_timeout")]
    assert finished["diagnostic_scope_id"] == started["diagnostic_scope_id"]
    assert not commits and endpoint.capture.is_open
    await endpoint.close()


@pytest.mark.anyio
async def test_local_timeout_is_distinct_and_does_not_replenish_interaction_budget(rows):
    endpoint, _, _, _ = endpoint_with_context()
    endpoint.cover_transcript(_final())
    deadline = endpoint._interaction_deadline
    with pytest.raises(asyncio.TimeoutError):
        await endpoint._infer(asyncio.Event().wait(), timeout=.015, use_interaction_budget=False)
    assert events(rows, "scope_finished")[-1]["cancel_reason"] == "local_timeout"
    assert endpoint._interaction_deadline == deadline
    await endpoint.close()


@pytest.mark.anyio
async def test_expired_wait_is_visible_without_starting_work(rows):
    endpoint, _, _, _ = endpoint_with_context()
    endpoint._interaction_deadline = time.monotonic() - 1
    calls = []

    async def work():
        calls.append(True)

    with pytest.raises(InteractionBudgetExpired):
        await endpoint._infer(work(), timeout=18)
    assert not calls and endpoint._inference_task is None
    assert events(rows, "scope_started")[0]["effective_timeout_ms"] == 0
    assert events(rows, "scope_finished")[0]["cancel_reason"] == "interaction_timeout"
    await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["audio", "client", "transcript"])
async def test_new_input_cancellation_is_not_a_processing_failure_or_repeated_per_frame(rows, source):
    endpoint, _, _, commits = endpoint_with_context()
    started, cancelled = asyncio.Event(), []

    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(current_model_call_scope().cancel_reason)
            raise

    task = asyncio.create_task(endpoint._infer(work(), timeout=18))
    await started.wait()
    endpoint.speech_started(source=source)
    endpoint.speech_started(source=source)
    with pytest.raises(ApiError) as rejected:
        await task
    assert rejected.value.code == "TURN_DECISION_STALE"
    assert cancelled == ["input_changed"]
    assert events(rows, "scope_finished")[-1]["outcome"] == "cancelled"
    requests = events(rows, "scope_cancel_requested")
    assert len(requests) == 1 and requests[0]["source"] == source
    assert not commits and endpoint._prepare_failures == 0
    await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("closing", [False, True])
async def test_parent_cancellation_preserves_exception_and_marks_real_cause(rows, closing):
    endpoint, _, _, _ = endpoint_with_context()
    started, cancelled = asyncio.Event(), []

    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(current_model_call_scope().cancel_reason)
            raise

    task = asyncio.create_task(endpoint._infer(work(), timeout=18))
    await started.wait()
    if closing:
        endpoint._task = task
        await endpoint.close()
    else:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    reason = "capture_closed" if closing else "caller_cancelled"
    assert cancelled == [reason]
    assert events(rows, "scope_finished")[-1]["cancel_reason"] == reason
    assert endpoint._prepare_failures == 0
    await endpoint.close()


@pytest.mark.anyio
async def test_child_timeout_does_not_claim_the_waiter_deadline_expired(rows):
    endpoint, _, _, _ = endpoint_with_context()

    async def work():
        raise asyncio.TimeoutError()

    with pytest.raises(asyncio.TimeoutError):
        await endpoint._infer(work(), timeout=18)
    assert events(rows, "scope_finished")[-1]["cancel_reason"] == "child_timeout"
    assert not events(rows, "scope_cancel_requested")
    await endpoint.close()


@pytest.mark.anyio
async def test_failed_prepared_value_is_logged_without_changing_result(rows):
    endpoint, _, _, _ = endpoint_with_context()
    result = SimpleNamespace(understanding=SimpleNamespace(problem=SimpleNamespace(
        reason_code="provider_timeout", retryable=True)))

    async def work():
        return result

    assert await endpoint._infer(work(), timeout=18) is result
    finished = events(rows, "scope_finished")[-1]
    assert finished["outcome"] == "failed" and finished["failure_reason"] == "provider_timeout"
    assert endpoint._prepare_failures == 0
    await endpoint.close()


@pytest.mark.anyio
async def test_log_sink_failure_never_replaces_business_success_or_timeout(monkeypatch):
    def unavailable(_):
        raise OSError("private diagnostic sink failure")
    logger = SimpleNamespace(info=unavailable)
    monkeypatch.setattr(model_diagnostics, "_LOG", logger)
    monkeypatch.setattr(speech_diagnostics, "_LOG", logger)
    endpoint, _, _, _ = endpoint_with_context()

    async def work():
        return "unchanged"

    assert await endpoint._infer(work(), timeout=18) == "unchanged"
    with pytest.raises(asyncio.TimeoutError):
        await endpoint._infer(asyncio.Event().wait(), timeout=.01, use_interaction_budget=False)
    speech_diagnostics.record_turn_control("understanding_retry_budget", **CONTEXT,
                                           failures=1, exhausted=False, reason_code="provider_timeout")
    await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["input_changed", "superseded", "capture_closed"])
async def test_speculative_cancellation_has_its_own_scope_and_preserves_cancellation(rows, reason):
    started, cancelled = asyncio.Event(), []

    async def prepare(preview, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(current_model_call_scope().cancel_reason)
            raise

    preparation = SpeculativeTurnPreparation(prepare, lambda value: value.text,
                                             diagnostic_context=CONTEXT)
    preparation.start(_preview(_final()))
    task = preparation._task
    await started.wait()
    if reason == "capture_closed":
        await preparation.close()
    else:
        preparation.cancel(reason=reason, source="transcript" if reason == "input_changed" else None)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == [reason]
    finished = events(rows, "scope_finished")[-1]
    assert finished["operation"] == "speculative_prepare"
    assert finished["cancel_reason"] == reason and finished["outcome"] == "cancelled"
    assert all(finished[key] == value for key, value in CONTEXT.items())
    await preparation.close()


@pytest.mark.anyio
async def test_shielded_waiter_cancellation_does_not_mislabel_continuing_preparation(rows):
    endpoint, _, _, _ = endpoint_with_context()
    endpoint.confirmation = SimpleNamespace(confirmed=True, semantic_first=False)
    endpoint._proposal_revision = endpoint.revision
    started, finish = asyncio.Event(), asyncio.Event()
    underlying_scopes, cancelled = [], []

    async def prepare(final, **kwargs):
        scope = current_model_call_scope()
        underlying_scopes.append(scope)
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            cancelled.append(scope.cancel_reason)
            raise
        return endpoint.capture.prepared(final)

    endpoint.capture.prepare_decision = prepare
    task = asyncio.create_task(endpoint._prepare_transcript(_final()))
    await started.wait()
    endpoint._mark_task_cancellation(endpoint._inference_task, endpoint._inference_scope, "input_changed", source="audio")
    endpoint._inference_task.cancel()
    with pytest.raises(ApiError):
        await task
    assert endpoint._confirmed_preparation is not None and not endpoint._confirmed_preparation.done()
    assert not cancelled and underlying_scopes[0].cancel_reason == "caller_cancelled"
    finish.set()
    await endpoint._confirmed_preparation
    assert underlying_scopes[0].cancel_reason == "caller_cancelled"
    assert events(rows, "scope_finished")[-1]["operation"] == "confirmed_wait"
    assert events(rows, "scope_finished")[-1]["cancel_reason"] == "input_changed"
    await endpoint.close()


@pytest.mark.anyio
async def test_confirmed_failure_callback_keeps_original_scope_after_waiter_has_gone(rows):
    endpoint, _, _, _ = endpoint_with_context()
    endpoint.trace = lambda event, **fields: speech_diagnostics.record_turn_control(event, **CONTEXT, **fields)
    endpoint.confirmation = SimpleNamespace(confirmed=True, semantic_first=False)
    endpoint._proposal_revision = endpoint.revision
    started, finish = asyncio.Event(), asyncio.Event()

    async def prepare(final, **kwargs):
        started.set()
        await finish.wait()
        result = endpoint.capture.prepared(final)
        result.understanding.problem = SimpleNamespace(source="provider", reason_code="provider_timeout", retryable=True)
        return result

    endpoint.capture.prepare_decision = prepare
    waiter = asyncio.create_task(endpoint._prepare_transcript(_final()))
    await started.wait()
    underlying = endpoint._confirmed_preparation
    scope_id = endpoint._confirmed_preparation_scope.scope_id
    endpoint._mark_task_cancellation(endpoint._inference_task, endpoint._inference_scope, "input_changed")
    endpoint._inference_task.cancel()
    with pytest.raises(ApiError):
        await waiter
    assert current_model_call_scope() is None
    finish.set()
    await underlying
    failures = [row for row in rows if row["event"] == "turn_control.understanding_retry_budget"]
    assert len(failures) == 1 and failures[0]["diagnostic_scope_id"] == scope_id
    assert failures[0]["reason_code"] == "provider_timeout"
    await endpoint.close()


@pytest.mark.anyio
async def test_failed_preview_accounting_links_original_work_instead_of_replacement_waiter(rows):
    endpoint, clock, _, _ = endpoint_with_context()
    endpoint.trace = lambda event, **fields: speech_diagnostics.record_turn_control(event, **CONTEXT, **fields)
    calls = []

    async def prepare(final, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise asyncio.TimeoutError()
        return endpoint.capture.prepared(final)

    endpoint.capture.prepare_decision = prepare
    clock.value += 3.1
    endpoint.prepare_stable_preview(_preview(_final()))
    scope_id = endpoint.preparation.diagnostic_scope.scope_id
    with pytest.raises(asyncio.TimeoutError):
        await endpoint.preparation._task
    endpoint.cover_transcript(_final())
    endpoint._proposal_revision = endpoint.revision
    assert (await endpoint._prepare_transcript(_final(), semantic_first=True)).understanding.problem is None
    failures = [row for row in rows if row["event"] == "turn_control.understanding_retry_budget"]
    assert len(failures) == 1 and failures[0]["diagnostic_scope_id"] == scope_id
    assert len(calls) == 2
    await endpoint.close()


def test_retry_cause_and_scope_are_safe_and_untrusted_values_are_dropped(rows):
    speech_diagnostics.record_turn_control("understanding_retry_budget", **CONTEXT,
        failures=1, exhausted=False, revision=4, stage="answer_understanding", reason_code="provider_timeout",
        retryable=True, diagnostic_scope_id="model_scope_synthetic", parent_scope_id="model_scope_parent",
        interaction_remaining_ms=1234.8, response_body="private provider result", prompt="private prompt")
    row = rows[-1]
    assert row["reason_code"] == "provider_timeout" and row["retryable"] is True
    assert row["interaction_remaining_ms"] == 1234 and row["diagnostic_scope_id"] == "model_scope_synthetic"
    speech_diagnostics.record_turn_control("understanding_retry_budget", **CONTEXT,
        reason_code="private provider text", diagnostic_scope_id="private id with spaces", interaction_remaining_ms=float("nan"))
    assert "reason_code" not in rows[-1] and "diagnostic_scope_id" not in rows[-1]
    assert "private" not in json.dumps(rows)
