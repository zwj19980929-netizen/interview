"""A later presence check cannot fail the completed answer-understanding turn."""
import asyncio
import time

import pytest

from app.core.errors import ApiError
from app.core.interaction_budget import remaining_interaction_seconds
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _endpoint, _final, _until
from test_runtime_budget_068 import Capture


async def _explained(*, failure=None):
    capture = Capture()
    capture.current_final = _final("合成术语是什么？")
    endpoint, clock, notices, commits = _endpoint(capture=capture)
    calls = []
    async def classify(*args, **kwargs):
        return {"kind": "question_clarification", "confidence": .95, "reply_text": "这是合成术语的中性解释。"}
    async def speak(kind, guard, **kwargs):
        guard()
        calls.append((kind, remaining_interaction_seconds()))
        if kind == "reception_idle_check" and failure is not None:
            raise failure
        return True
    capture.classify_reception = classify
    confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint.confirmation = confirmation
    endpoint.speech_started()
    endpoint._proposal_revision = endpoint.revision
    assert await confirmation._receive_request(endpoint, capture.current_final)
    await confirmation.complete_playback(endpoint, reason="agent_finished")
    assert confirmation.phase == "listening" and not endpoint._prepare_failures
    endpoint._interaction_deadline = time.monotonic() - 1
    clock.value += 16
    return endpoint, clock, notices, commits, calls


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [None, asyncio.TimeoutError(), ValueError("Synthetic optional speech error")])
async def test_idle_after_successful_explanation_has_its_own_budget_and_never_marks_answer_failed(failure):
    endpoint, clock, notices, commits, calls = await _explained(failure=failure)
    old_deadline = endpoint._interaction_deadline
    endpoint.start()
    try:
        await _until(lambda: len(calls) > 1 or endpoint._prepare_failures > 0)
        assert [kind for kind, _ in calls] == ["reception_question_clarification", "reception_idle_check"]
        assert 0 < calls[-1][1] <= 12
        assert not endpoint._prepare_failures and not endpoint.preparation_blocked
        assert endpoint._interaction_deadline == old_deadline, "A reminder cannot renew model preparation"
        assert "understanding_retry_exhausted" not in notices and not commits
        if failure is None:
            await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        for _ in range(3):
            clock.value += 60
            await asyncio.sleep(.11)
        assert len(calls) == 2 and endpoint.capture.is_open and not endpoint._prepare_failures
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_failed_optional_reminder_does_not_prevent_new_authoritative_answer_processing():
    endpoint, _, notices, commits, calls = await _explained(failure=asyncio.TimeoutError())
    try:
        await endpoint.confirmation._idle_check(endpoint, 16)
        assert not endpoint._prepare_failures
        final = _final("我用幂等键避免重复提交。")
        endpoint.capture.current_final = final
        endpoint.observe_transcript(final.text)
        endpoint.cover_transcript(final)
        endpoint._proposal_revision = endpoint.revision
        await endpoint._prepare_transcript(final, semantic_first=True)
        assert len(endpoint.capture.prepares) == 1 and endpoint.capture.budgets[-1] > 19
        assert not commits and "understanding_retry_exhausted" not in notices
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_new_words_do_not_get_mistaken_for_idle_after_explanation():
    endpoint, _, _, _, calls = await _explained()
    try:
        endpoint.capture.current_final = _final("合成术语是什么？我开始回答了。")
        assert not await endpoint.confirmation._idle_check(endpoint, 16)
        assert len(calls) == 1 and not endpoint.confirmation._idle_check_sent
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("code", ["TURN_DECISION_STALE", "EVIDENCE_OWNER_FENCED"])
async def test_stale_owner_during_reminder_still_propagates_and_cannot_retry_old_output(code):
    error = ApiError(code, "Synthetic input changed", status_code=409)
    endpoint, _, _, commits, calls = await _explained(failure=error)
    try:
        with pytest.raises(ApiError) as caught:
            await endpoint.confirmation._idle_check(endpoint, 16)
        assert caught.value is error and not commits
        assert endpoint.confirmation._idle_check_sent
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_revoked_reminder_child_propagates_as_stale_output():
    endpoint, _, _, commits, _ = await _explained(failure=asyncio.CancelledError())
    try:
        with pytest.raises(ApiError) as caught:
            await endpoint.confirmation._idle_check(endpoint, 16)
        assert caught.value.code == "TURN_DECISION_STALE"
        assert not commits and not endpoint._prepare_failures
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_cancelled_reminder_parent_does_not_swallow_task_cancellation():
    endpoint, _, _, commits, _ = await _explained()
    started = asyncio.Event()
    async def pending_speech(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    endpoint.confirmation.speak = pending_speech
    task = asyncio.create_task(endpoint.confirmation._idle_check(endpoint, 16))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not commits and not endpoint._prepare_failures
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await endpoint.close()
