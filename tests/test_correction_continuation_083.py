"""A real speculative rejection must spend the second call on its correction.

Only model I/O and audio cuts are synthetic. The endpoint, capture/owner fences,
SQLite session context, prompt construction and semantic validation are real.
"""

import asyncio
from copy import deepcopy
import time

import pytest

from app.core.errors import ApiError
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.services.answer_endpoint import InteractionBudgetExpired
from test_ambiguous_completion_072 import _case
from test_answer_declined_understanding import no_followup
from test_answer_endpoint import _Capture, _endpoint, _until
from test_preparation_fences_081 import _chain
from test_stable_preview_preparation import _preview


_REASON = "completion_has_unresolved_ambiguity"
_CORRECTION = "上一轮结果未通过合同校验"


def _is_correction(request):
    messages = "\n".join(message.content for message in request.messages)
    return _CORRECTION in messages and _REASON in messages


class _CorrectionGateway:
    """A second unchanged original prompt is deliberately still invalid."""

    def __init__(self, bad, good, *, first_valid=False, second="corrected"):
        self.bad, self.good = bad, good
        self.first_valid, self.second = first_valid, second
        self.requests, self.cancelled = [], []
        self.started = [asyncio.Event() for _ in range(4)]
        self.gates = {}
        self.started_at, self.finished_at = [], []

    async def invoke(self, capability, request):
        index = len(self.requests)
        self.requests.append(request)
        self.started_at.append(time.monotonic())
        self.started[index].set()
        try:
            if index in self.gates:
                await self.gates[index].wait()
            if index == 1 and self.second == "provider_error":
                raise ProviderError("provider_rate_limited", "PRIVATE_UPSTREAM_083", retryable=True)
            good = self.first_valid or (_is_correction(request) and self.second == "corrected")
            data = deepcopy(self.good if good else self.bad)
            if "understanding" in request.json_schema.get("properties", {}):
                data = {"understanding": data, "followup": no_followup()}
            return ChatJSONResponse(data=data, usage=Usage(), provider=ProviderMeta(
                provider_id="synthetic_083", model="correction_continuation_test",
                request_id="synthetic_083_%s" % index, latency_ms=1,
            ))
        except asyncio.CancelledError:
            self.cancelled.append(index)
            raise
        finally:
            self.finished_at.append(time.monotonic())


class _CaptureFacade(_Capture):
    """Fake microphone cuts delegate all preparation/binding to production."""

    def __init__(self, harness):
        super().__init__()
        self.harness = harness
        self.current_final = harness.final

    async def prepare_decision(self, final, **kwargs):
        self.prepares.append(final.model_copy(deep=True))
        return await self.harness.chain.prepare_decision(final, **kwargs)

    def assert_preparation_current(self, prepared, final, **kwargs):
        self.harness.chain.assert_preparation_current(prepared, final, **kwargs)


def _setup(tmp_path, monkeypatch, *, composite=False, first_valid=False, second="corrected"):
    harness = _chain(tmp_path, monkeypatch)
    text, _, bad, _, source_turn, _ = _case(composite=composite)
    bad["clarification_target"] = None
    good = deepcopy(bad)
    good.update(ambiguities=[], clarification_target=None)
    harness.final = harness.final.model_copy(update={
        "text": text,
        "segments": [harness.final.segments[0].model_copy(update={"text": text})],
    })
    service = harness.stream.interviews
    with service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        turn = session["turns"][0]
        turn.update(allow_followup=composite,
                    question_spoken_text=source_turn["question_spoken_text"],
                    question_snapshot=deepcopy(source_turn["question_snapshot"]))
        tx.interview_sessions.update(session, expected_version=session["version"])
    gateway = _CorrectionGateway(bad, good, first_valid=first_valid, second=second)
    service.conversation.gateway = gateway
    capture = _CaptureFacade(harness)
    endpoint, clock, notices, commits = _endpoint(capture=capture)
    harness.gateway, harness.capture = gateway, capture
    harness.endpoint, harness.clock = endpoint, clock
    harness.notices, harness.commits = notices, commits
    return harness


async def _start_preview(harness, *, completed=True):
    endpoint = harness.endpoint
    endpoint.speech_started(source="audio")
    harness.clock.value += 4
    endpoint.prepare_stable_preview(_preview(harness.final))
    await asyncio.wait_for(harness.gateway.started[0].wait(), 1)
    if completed:
        await _until(lambda: endpoint.preparation._task.done())
    assert len(harness.gateway.requests) == 1


async def _final_prepare(harness):
    harness.endpoint.cover_transcript(harness.final)
    harness.endpoint._proposal_revision = harness.endpoint.revision
    return await harness.endpoint._prepare_transcript(harness.final, semantic_first=True)


def _assert_limits(harness):
    assert sum(budget.used for budget in harness.endpoint._model_budgets.values()) == 2
    assert all(request.execution_budget.timeout_s == 18 for request in harness.gateway.requests)
    assert all(request.execution_budget.max_provider_attempts == 1 for request in harness.gateway.requests)
    assert all(request.execution_budget.max_provider_retries == 0 for request in harness.gateway.requests)
    assert harness.endpoint._interaction_deadline <= harness.endpoint._interaction_started_at + 40
    assert not harness.commits
    assert harness.stream.interviews.get_interview("iv_binding")["answers"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [False, True])
async def test_real_preview_rejection_continues_with_corrected_contract_on_second_call(tmp_path, monkeypatch, composite):
    harness = _setup(tmp_path, monkeypatch, composite=composite)
    try:
        await _start_preview(harness)
        prepared = await _final_prepare(harness)
        assert [_is_correction(request) for request in harness.gateway.requests] == [False, True]
        assert prepared.understanding.problem is None
        assert prepared.understanding.completion_basis is not None
        assert not prepared.completion_confirmed
        assert harness.endpoint._prepare_failures == 1
        _assert_limits(harness)
        # Reusing analysis is not authority to submit an unconfirmed answer.
        with pytest.raises(ApiError):
            harness.stream.interviews.assert_prepared_streaming_decision(prepared, harness.final, "org_default")
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [False, True])
async def test_valid_first_preview_is_reused_without_a_correction_or_second_call(tmp_path, monkeypatch, composite):
    harness = _setup(tmp_path, monkeypatch, composite=composite, first_valid=True)
    try:
        await _start_preview(harness)
        prepared = await _final_prepare(harness)
        assert prepared.understanding.problem is None
        assert len(harness.gateway.requests) == 1
        assert not _is_correction(harness.gateway.requests[0])
        assert harness.endpoint._prepare_failures == 0
        assert not harness.commits
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("second", ["invalid", "provider_error"])
async def test_failed_corrected_second_call_never_buys_a_third_call(tmp_path, monkeypatch, second, caplog):
    harness = _setup(tmp_path, monkeypatch, second=second)
    try:
        await _start_preview(harness)
        prepared = await _final_prepare(harness)
        assert prepared.understanding.problem is not None
        assert [_is_correction(request) for request in harness.gateway.requests] == [False, True]
        assert harness.endpoint._prepare_failures == 2
        for _ in range(2):
            with pytest.raises(ApiError) as error:
                await _final_prepare(harness)
            assert error.value.code == "UNDERSTANDING_RETRY_EXHAUSTED"
        assert len(harness.gateway.requests) == 2
        _assert_limits(harness)
        assert "PRIVATE_UPSTREAM_083" not in caplog.text
        assert harness.final.text not in caplog.text
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
async def test_correction_starts_immediately_without_sleeping_for_retry_backoff(tmp_path, monkeypatch):
    harness = _setup(tmp_path, monkeypatch)
    harness.gateway.gates[0] = asyncio.Event()
    harness.gateway.gates[1] = asyncio.Event()
    waiter = None
    try:
        await _start_preview(harness, completed=False)
        waiter = asyncio.create_task(_final_prepare(harness))
        await _until(lambda: harness.endpoint._inference_task is not None)
        harness.gateway.gates[0].set()
        await asyncio.wait_for(harness.gateway.started[1].wait(), .5)
        # The usual one-second retry backoff is still in the future: the
        # continuation starts without that delay or a recovery speech round.
        assert harness.endpoint._retry_at > harness.clock.value
        assert harness.gateway.started_at[1] - harness.gateway.finished_at[0] < .5
        assert _is_correction(harness.gateway.requests[1])
        harness.gateway.gates[1].set()
        assert (await waiter).understanding.problem is None
        _assert_limits(harness)
    finally:
        for gate in harness.gateway.gates.values():
            gate.set()
        await harness.endpoint.close()
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.anyio
async def test_expired_interaction_does_not_start_a_correction_request(tmp_path, monkeypatch):
    harness = _setup(tmp_path, monkeypatch)
    try:
        await _start_preview(harness)
        harness.endpoint.cover_transcript(harness.final)
        harness.endpoint._proposal_revision = harness.endpoint.revision
        harness.endpoint._interaction_started_at = time.monotonic() - 41
        harness.endpoint._interaction_deadline = time.monotonic() - 1
        with pytest.raises((ApiError, InteractionBudgetExpired)):
            await _final_prepare(harness)
        assert len(harness.gateway.requests) == 1
        assert not harness.commits
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("interaction_already_started", [False, True])
async def test_completed_preview_correction_outlives_its_wait_deadline_within_legal_final_budget(
    tmp_path, monkeypatch, interaction_already_started,
):
    harness = _setup(tmp_path, monkeypatch)
    real_monotonic, advance = time.monotonic, [0.0]
    try:
        # Advance the monotonic clock only after the preview and its callbacks
        # have completed. No provider work is allowed to outrun its own bound.
        with monkeypatch.context() as patch:
            patch.setattr(time, "monotonic", lambda: real_monotonic() + advance[0])
            if interaction_already_started:
                harness.endpoint.cover_transcript(harness.final)
            await _start_preview(harness)
            await asyncio.sleep(0)
            advance[0] = 21.0
            prepared = await _final_prepare(harness)
            assert prepared.understanding.problem is None
            assert [_is_correction(request) for request in harness.gateway.requests] == [False, True]
            _assert_limits(harness)
            await harness.endpoint.close()
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
async def test_audio_cancels_waiter_but_retains_correction_for_unchanged_final(tmp_path, monkeypatch):
    harness = _setup(tmp_path, monkeypatch)
    harness.gateway.gates[0] = asyncio.Event()
    waiter = None
    try:
        await _start_preview(harness, completed=False)
        waiter = asyncio.create_task(_final_prepare(harness))
        await _until(lambda: harness.endpoint._inference_task is not None)
        harness.endpoint.speech_started(source="audio")
        with pytest.raises(ApiError):
            await waiter
        assert harness.gateway.cancelled == []
        harness.gateway.gates[0].set()
        await _until(lambda: harness.endpoint.preparation._task.done())
        prepared = await _final_prepare(harness)
        assert prepared.understanding.problem is None
        assert [_is_correction(request) for request in harness.gateway.requests] == [False, True]
        _assert_limits(harness)
    finally:
        harness.gateway.gates[0].set()
        await harness.endpoint.close()
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.anyio
async def test_new_server_words_do_not_inherit_an_old_correction(tmp_path, monkeypatch):
    harness = _setup(tmp_path, monkeypatch)
    harness.gateway.gates[1] = asyncio.Event()
    waiter = None
    try:
        await _start_preview(harness)
        text = harness.final.text + "新的补充采用幂等键。"
        harness.endpoint.observe_transcript(text)
        harness.final = harness.final.model_copy(update={
            "text": text,
            "segments": [harness.final.segments[0].model_copy(update={"text": text})],
        })
        waiter = asyncio.create_task(_final_prepare(harness))
        await asyncio.wait_for(harness.gateway.started[1].wait(), 1)
        assert not _is_correction(harness.gateway.requests[1])
        assert not harness.commits
    finally:
        await harness.endpoint.close()
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.anyio
async def test_returning_to_old_text_after_new_input_cannot_resurrect_old_correction(tmp_path, monkeypatch):
    harness = _setup(tmp_path, monkeypatch)
    harness.gateway.gates[1] = asyncio.Event()
    waiter = None
    try:
        await _start_preview(harness)
        harness.endpoint.observe_transcript(harness.final.text + "一个新的服务端假设。")
        harness.endpoint.observe_transcript(harness.final.text)
        waiter = asyncio.create_task(_final_prepare(harness))
        await asyncio.wait_for(harness.gateway.started[1].wait(), 1)
        assert not _is_correction(harness.gateway.requests[1])
        assert sum(budget.used for budget in harness.endpoint._model_budgets.values()) == 2
        assert not harness.commits
    finally:
        await harness.endpoint.close()
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["confidence", "segments", "provider", "context"])
async def test_identical_words_with_changed_complete_evidence_or_context_do_not_reuse_correction(
    tmp_path, monkeypatch, change,
):
    harness = _setup(tmp_path, monkeypatch)
    harness.gateway.gates[1] = asyncio.Event()
    waiter = None
    try:
        await _start_preview(harness)
        if change == "confidence":
            harness.final = harness.final.model_copy(update={"confidence": .91})
        elif change == "segments":
            harness.final = harness.final.model_copy(update={
                "segments": [harness.final.segments[0].model_copy(update={"end_ms": 1400})],
            })
        elif change == "provider":
            harness.final = harness.final.model_copy(update={
                "provider": harness.final.provider.model_copy(update={"request_id": "new_server_evidence_083"}),
            })
        else:
            # The standard answer is a frozen context field intentionally not
            # sent in these prompts. Request equality alone cannot fence it.
            with harness.stream.interviews.persistence.transaction("org_default") as tx:
                session = tx.interview_sessions.get("iv_binding")
                session["turns"][0]["question_snapshot"]["standard_answer"] = "另一份冻结标准。"
                tx.interview_sessions.update(session, expected_version=session["version"])
        waiter = asyncio.create_task(_final_prepare(harness))
        await asyncio.wait_for(harness.gateway.started[1].wait(), 1)
        assert not _is_correction(harness.gateway.requests[1])
        assert not harness.commits
    finally:
        await harness.endpoint.close()
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.anyio
async def test_owner_loss_after_preview_refuses_even_a_correction_request(tmp_path, monkeypatch):
    harness = _setup(tmp_path, monkeypatch)
    try:
        await _start_preview(harness)
        assert harness.coordinator.release(harness.owner)
        with pytest.raises(ApiError) as error:
            await _final_prepare(harness)
        assert error.value.code == "EVIDENCE_OWNER_FENCED"
        assert len(harness.gateway.requests) == 1
        assert not harness.commits
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
async def test_new_input_cancels_an_inflight_correction_and_cannot_commit_it(tmp_path, monkeypatch):
    harness = _setup(tmp_path, monkeypatch)
    harness.gateway.gates[1] = asyncio.Event()
    waiter = None
    try:
        await _start_preview(harness)
        waiter = asyncio.create_task(_final_prepare(harness))
        await asyncio.wait_for(harness.gateway.started[1].wait(), 1)
        assert _is_correction(harness.gateway.requests[1])
        harness.endpoint.observe_transcript(harness.final.text + "新的补充。")
        with pytest.raises(ApiError):
            await waiter
        assert harness.gateway.cancelled == []
        harness.endpoint.cover_transcript(harness.final.model_copy(update={"text": harness.final.text + "新的补充。"}))
        await _until(lambda: harness.gateway.cancelled == [1])
        assert len(harness.gateway.requests) == 2
        assert not harness.commits
    finally:
        await harness.endpoint.close()
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)
