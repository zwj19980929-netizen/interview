import asyncio
import time
import pytest

from app.core.interaction_budget import interaction_deadline, remaining_interaction_seconds
from test_answer_endpoint import _endpoint, _final


@pytest.mark.anyio
async def test_nested_budget_cannot_extend_and_child_inherits_deadline():
    assert remaining_interaction_seconds() is None
    deadline = time.monotonic() + .2
    async def inspect():
        return remaining_interaction_seconds()
    with interaction_deadline(deadline):
        with interaction_deadline(time.monotonic() + 20):
            assert 0 < await asyncio.create_task(inspect()) <= .2
    assert remaining_interaction_seconds() is None


@pytest.mark.anyio
async def test_model_and_speech_share_budget_and_timeout_cancels_late_work():
    endpoint, _, _, commits = _endpoint()
    endpoint.cover_transcript(_final())
    endpoint._interaction_deadline = time.monotonic() + .05
    observed, cancelled = [], []
    async def model():
        observed.append(remaining_interaction_seconds())
        await asyncio.sleep(.025)
    async def speech():
        observed.append(remaining_interaction_seconds())
        try:
            await asyncio.sleep(1)
            commits.append("late")
        finally:
            cancelled.append(True)
    try:
        await endpoint._infer(model(), timeout=45)
        with pytest.raises(asyncio.TimeoutError):
            await endpoint._infer(speech(), timeout=45)
        assert 0 < observed[1] < observed[0] <= .05
        assert cancelled and not commits and endpoint.capture.is_open
    finally:
        await endpoint.close()


def test_noise_and_metadata_do_not_renew_but_new_final_and_explicit_retry_do():
    endpoint, _, _, _ = _endpoint()
    first = _final()
    endpoint.cover_transcript(first)
    deadline = endpoint._interaction_deadline
    endpoint.speech_started(source="audio")
    endpoint.cover_transcript(first.model_copy(update={"confidence": .1}))
    assert endpoint._interaction_deadline == deadline
    endpoint.cover_transcript(_final("这是新的回复。"))
    assert endpoint._interaction_deadline > deadline
    endpoint.request_finish()
    assert endpoint._interaction_deadline is None


@pytest.mark.anyio
async def test_expired_budget_does_not_start_speech_side_effect():
    endpoint, _, _, _ = _endpoint()
    endpoint._interaction_deadline = time.monotonic() - 1
    calls = []
    async def speech():
        calls.append("selected")
    with pytest.raises(asyncio.TimeoutError):
        await endpoint._infer(speech(), timeout=45)
    assert not calls
    await endpoint.close()


@pytest.mark.anyio
async def test_expression_timeout_requires_new_words_or_explicit_retry():
    endpoint, _, notices, commits = _endpoint()
    endpoint.cover_transcript(_final())
    await endpoint.expression_preparation_failed()
    assert endpoint.preparation_blocked and notices[-1] == "understanding_retry_exhausted"
    endpoint.speech_started(source="audio")
    endpoint.cover_transcript(_final(confidence=.8))
    assert endpoint.preparation_blocked and not commits and endpoint.capture.is_open
    endpoint.cover_transcript(_final("新的请求。"))
    assert not endpoint.preparation_blocked
    await endpoint.expression_preparation_failed()
    assert endpoint.preparation_blocked
    endpoint.continue_speaking()
    assert not endpoint.preparation_blocked
    await endpoint.close()


@pytest.mark.anyio
async def test_new_stable_request_after_long_wait_gets_fresh_budget_before_final_cut():
    from test_reception_cursor_062 import setup
    endpoint, _, spoken, commits, _ = setup()
    try:
        endpoint.cover_transcript(_final("我想一下。"))
        endpoint._interaction_deadline = time.monotonic() - 1
        preview = await endpoint.capture.transcript_preview()
        assert await endpoint.confirmation._receive_request(endpoint, preview, prepare_only=True)
        deadline = endpoint._interaction_deadline
        assert deadline > time.monotonic()
        assert endpoint.capture.requests and not spoken and not commits
        # Identical stable words, confidence changes and unstable hypotheses
        # cannot buy another full preparation period.
        await endpoint.confirmation._receive_request(endpoint, preview, prepare_only=True)
        assert endpoint._interaction_deadline == deadline
        changed = preview.model_copy(update={"text": "另一个暂定词", "has_unstable_tail": True})
        assert not await endpoint.confirmation._receive_request(endpoint, changed, prepare_only=True)
        assert endpoint._interaction_deadline == deadline
    finally:
        await endpoint.close()
