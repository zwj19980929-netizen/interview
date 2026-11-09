"""A contract correction uses the recovery window, not a nearly spent wait.

All model responses and microphone cuts are synthetic; the endpoint, shared
allowance, evidence owner, prompt validation and temporary SQLite are real.
"""
import asyncio
from copy import deepcopy
import time

import pytest

from app.core.errors import ApiError
from app.core.interaction_budget import remaining_interaction_seconds
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from test_answer_declined_understanding import no_followup
from test_answer_endpoint import _until
from test_correction_continuation_083 import _setup, _final_prepare


_REASON = "unresolved_answer_contract_invalid"


class _Gateway:
    def __init__(self, bad, good, *, delays=(0, 0), first_valid=False, always_invalid=False, provider_error=False):
        self.bad, self.good = deepcopy(bad), deepcopy(good)
        self.bad.update(intent="answer_unresolved", clarification_target=None)
        self.delays = delays
        self.first_valid, self.always_invalid, self.provider_error = first_valid, always_invalid, provider_error
        self.requests, self.remaining, self.started_at, self.finished_at, self.cancelled = [], [], [], [], []
        self.started = [asyncio.Event() for _ in range(4)]
        self.gates = {}
        self.after_first = None

    async def invoke(self, capability, request):
        index = len(self.requests)
        self.requests.append(request)
        self.remaining.append(remaining_interaction_seconds())
        self.started_at.append(time.monotonic())
        self.started[index].set()
        try:
            if self.provider_error:
                raise ProviderError("provider_auth_failed", "Synthetic provider authentication rejected", retryable=False)
            if index in self.gates:
                await self.gates[index].wait()
            await asyncio.sleep(self.delays[min(index, len(self.delays) - 1)])
            corrected = any("上一轮结果未通过合同校验" in message.content and _REASON in message.content
                            for message in request.messages)
            valid = self.first_valid or (corrected and not self.always_invalid)
            data = deepcopy(self.good if valid else self.bad)
            if index == 0 and self.after_first is not None:
                self.after_first()
            if "understanding" in request.json_schema.get("properties", {}):
                data = {"understanding": data, "followup": no_followup()}
            return ChatJSONResponse(data=data, usage=Usage(), provider=ProviderMeta(
                provider_id="synthetic_085", model="bounded_recovery", request_id="synthetic_085_%s" % index,
                latency_ms=1))
        except asyncio.CancelledError:
            self.cancelled.append(index)
            raise
        finally:
            self.finished_at.append(time.monotonic())


def _harness(tmp_path, monkeypatch, *, composite=False, **kwargs):
    harness = _setup(tmp_path, monkeypatch, composite=composite)
    old = harness.gateway
    harness.gateway = _Gateway(old.bad, old.good, **kwargs)
    harness.stream.interviews.conversation.gateway = harness.gateway
    return harness


def _used(harness):
    return sum(budget.used for budget in harness.endpoint._model_budgets.values())


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [False, True])
async def test_first_rejection_immediately_continues_in_original_recovery_window(tmp_path, monkeypatch, composite):
    # 20/40 seconds scaled to .2/.4. The first response arrives at .12;
    # correction needs .13 and must not be cancelled by the old .2 deadline.
    monkeypatch.setattr("app.services.answer_endpoint._INTERACTION_PREPARATION_TIMEOUT", .2)
    monkeypatch.setattr("app.services.answer_endpoint._INTERACTION_RECOVERY_TIMEOUT", .4)
    harness = _harness(tmp_path, monkeypatch, composite=composite, delays=(.12, .13))
    try:
        result = await _final_prepare(harness)
        assert result.understanding.problem is None
        assert len(harness.gateway.requests) == _used(harness) == 2
        assert not harness.gateway.cancelled
        assert harness.gateway.remaining[1] > .18
        assert harness.gateway.started_at[1] - harness.gateway.finished_at[0] < .08
        assert harness.endpoint._interaction_deadline == harness.endpoint._interaction_started_at + .4
        assert harness.endpoint._prepare_failures == 1
        assert all(r.execution_budget.timeout_s == 18 for r in harness.gateway.requests)
        assert all(r.execution_budget.max_provider_attempts == 1 for r in harness.gateway.requests)
        assert all(r.execution_budget.max_provider_retries == 0 for r in harness.gateway.requests)
        assert not harness.commits and not harness.stream.interviews.get_interview("iv_binding")["answers"]
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
async def test_first_success_keeps_one_call_and_original_foreground_deadline(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch, first_valid=True)
    try:
        result = await _final_prepare(harness)
        assert result.understanding.problem is None
        assert len(harness.gateway.requests) == _used(harness) == 1
        assert harness.endpoint._interaction_deadline == harness.endpoint._interaction_started_at + 20
        assert harness.endpoint._prepare_failures == 0
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [False, True])
async def test_two_content_rejections_stop_automatic_calls_but_allow_explicit_retry(tmp_path, monkeypatch, composite):
    harness = _harness(tmp_path, monkeypatch, composite=composite, always_invalid=True)
    try:
        result = await _final_prepare(harness)
        assert result.understanding.problem.source == "schema_or_content"
        assert result.understanding.problem.reason_code == _REASON
        assert len(harness.gateway.requests) == _used(harness) == 2
        assert harness.endpoint.preparation_blocked
        assert harness.endpoint.preparation_recovery_details["retryable"] is True
        assert harness.endpoint.preparation_recovery_details["reason_code"] == "invalid_result"
        assert not harness.endpoint._terminal_provider_failure
        for _ in range(2):
            assert not harness.endpoint.start_recovery_preparation(harness.final)
            with pytest.raises(ApiError) as caught:
                await _final_prepare(harness)
            assert caught.value.code == "UNDERSTANDING_RETRY_EXHAUSTED"
        assert len(harness.gateway.requests) == 2
        assert harness.endpoint.retry_answer_processing()
        assert not harness.endpoint.preparation_blocked and _used(harness) == 0
        assert not harness.commits
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
async def test_second_timeout_exhausts_the_original_window_without_a_third_call(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.answer_endpoint._INTERACTION_PREPARATION_TIMEOUT", .1)
    monkeypatch.setattr("app.services.answer_endpoint._INTERACTION_RECOVERY_TIMEOUT", .3)
    harness = _harness(tmp_path, monkeypatch, delays=(.04, 1))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await _final_prepare(harness)
        await _until(lambda: not harness.endpoint._preparation_tasks)
        assert len(harness.gateway.requests) == _used(harness) == 2
        assert harness.gateway.cancelled == [1]
        assert harness.endpoint.preparation_blocked
        assert harness.endpoint._interaction_deadline == harness.endpoint._interaction_started_at + .3
        assert not harness.endpoint.start_recovery_preparation(harness.final)
        assert not harness.commits
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
async def test_nonretryable_provider_failure_stays_blocked(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch, provider_error=True)
    try:
        result = await _final_prepare(harness)
        assert result.understanding.problem.reason_code == "provider_auth_failed"
        assert harness.endpoint.preparation_recovery_details["retryable"] is False
        assert harness.endpoint._terminal_provider_failure
        assert not harness.endpoint.retry_answer_processing()
        assert not harness.endpoint.start_recovery_preparation(harness.final)
        assert len(harness.gateway.requests) == _used(harness) == 1
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
async def test_cancelled_waiter_does_not_start_correction_until_new_authoritative_review(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    harness.gateway.gates[0] = asyncio.Event()
    task = asyncio.create_task(_final_prepare(harness))
    try:
        await asyncio.wait_for(harness.gateway.started[0].wait(), 1)
        harness.endpoint.speech_started(source="audio")
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], ApiError) and result[0].code == "TURN_DECISION_STALE"
        harness.gateway.gates[0].set()
        await _until(lambda: not harness.endpoint._preparation_tasks)
        assert len(harness.gateway.requests) == _used(harness) == 1
        assert not harness.commits
        prepared = await _final_prepare(harness)
        assert prepared.understanding.problem is None
        assert len(harness.gateway.requests) == _used(harness) == 2
    finally:
        harness.gateway.gates[0].set()
        await asyncio.gather(task, return_exceptions=True)
        await harness.endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["context", "owner", "capture"])
async def test_rejected_result_cannot_continue_after_binding_changes(tmp_path, monkeypatch, change):
    harness = _harness(tmp_path, monkeypatch)
    def invalidate():
        if change == "capture":
            harness.chain._stt = None
        elif change == "owner":
            assert harness.coordinator.release(harness.owner)
        else:
            with harness.stream.interviews.persistence.transaction("org_default") as tx:
                session = tx.interview_sessions.get("iv_binding")
                session["turns"][0]["question_spoken_text"] = "另一道合成问题。"
                tx.interview_sessions.update(session, expected_version=session["version"])
    harness.gateway.after_first = invalidate
    try:
        with pytest.raises(ApiError):
            await _final_prepare(harness)
        assert len(harness.gateway.requests) == _used(harness) == 1
        assert not harness.commits
    finally:
        await harness.endpoint.close()
