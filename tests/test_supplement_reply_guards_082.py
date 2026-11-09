"""Earlier supplement routing never manufactures delivery or completion."""
import asyncio

import pytest

from app.core.errors import ApiError
from test_answer_endpoint import _final
from test_stable_preview_preparation import _preview
from test_supplement_confirmation_073 import BODY, FINISH, answer, setup


async def delivered_reply():
    endpoint, confirmation, spoken, commits = setup()
    await answer(endpoint, confirmation)
    await confirmation.complete_playback(endpoint, reason="agent_finished")
    assert confirmation._supplement_question_delivered
    # A later acknowledgement can return this same exchange to listening.
    confirmation.phase = "listening"
    final = _final(BODY + FINISH)
    endpoint.capture.current_final = final
    endpoint.observe_transcript(final.text)
    endpoint._proposal_revision = endpoint.revision
    return endpoint, confirmation, spoken, commits, final


@pytest.mark.anyio
@pytest.mark.parametrize("preview", [False, True])
async def test_routing_eligibility_is_side_effect_free_and_not_completion(preview):
    endpoint, confirmation, spoken, commits, final = await delivered_reply()
    try:
        before = (confirmation.phase, confirmation.boundary, confirmation._boundary_final,
                  confirmation._prepared_answer, len(endpoint.capture.prepares), list(spoken))
        transcript = _preview(final) if preview else final
        assert confirmation.should_classify_supplement_reply(transcript)
        assert not confirmation.confirmed and confirmation._confirmed_final is None
        assert not endpoint.capture.replies and not commits
        assert before == (confirmation.phase, confirmation.boundary, confirmation._boundary_final,
                          confirmation._prepared_answer, len(endpoint.capture.prepares), list(spoken))
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", [
    "undelivered", "missing", "empty_reply", "unstable_preview", "ambiguous_rewrite",
    "missing_boundary", "exit_waiting", "content_clarification", "confirmed", "legacy_mode",
])
async def test_ineligible_exchange_cannot_skip_semantic_review(change):
    endpoint, confirmation, _, commits, final = await delivered_reply()
    try:
        transcript = final
        if change == "undelivered":
            confirmation._supplement_question_delivered = False
        elif change == "missing":
            transcript = None
        elif change == "empty_reply":
            transcript = confirmation._boundary_final.model_copy(deep=True)
        elif change == "unstable_preview":
            transcript = _preview(final, has_unstable_tail=True)
        elif change == "ambiguous_rewrite":
            transcript = _final("A rewritten answer without a trustworthy old boundary.")
            transcript.segments = []
        elif change == "missing_boundary":
            confirmation._boundary_final = None
        elif change == "exit_waiting":
            confirmation._exit_waiting = True
        elif change == "content_clarification":
            confirmation._content_question_focus = "Synthetic clarification focus"
        elif change == "confirmed":
            confirmation.confirmed = True
        else:
            confirmation.semantic_first = False
        assert not confirmation.should_classify_supplement_reply(transcript)
        assert not endpoint.capture.replies and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_low_confidence_reply_keeps_original_clarification_gate():
    endpoint, confirmation, spoken, commits, final = await delivered_reply()
    try:
        final.confidence = .4
        calls = len(endpoint.capture.prepares)
        await confirmation._listen(endpoint)
        assert spoken == ["check", "clarify"]
        assert not endpoint.capture.replies, "Low-confidence STT cannot reach the finish classifier"
        assert len(endpoint.capture.prepares) == calls
        assert not confirmation.confirmed and not commits and endpoint.capture.is_open
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_early_listening_does_not_bypass_allow_answer_review():
    endpoint, confirmation, spoken, commits, _ = await delivered_reply()
    try:
        calls = len(endpoint.capture.prepares)
        await confirmation._listen(endpoint, allow_answer_review=False)
        assert not endpoint.capture.replies and len(endpoint.capture.prepares) == calls
        assert spoken == ["check"] and not confirmation.confirmed and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["audio", "transcript"])
@pytest.mark.parametrize("resistant", [False, True])
async def test_new_input_revokes_even_a_late_finish_classification(source, resistant):
    endpoint, confirmation, _, commits, _ = await delivered_reply()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def classify(reply, **kwargs):
        endpoint.capture.replies.append(reply)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            if not resistant:
                raise
        return {"intent": "finish", "confidence": .99, "evidence_quote": reply}

    endpoint.capture.classify_supplement_reply = classify
    task = asyncio.create_task(confirmation._listen(endpoint))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if source == "audio":
            endpoint.speech_started(source="audio")
        else:
            endpoint.observe_transcript(BODY + FINISH + "A fresh technical correction.")
        with pytest.raises(ApiError) as rejected:
            await asyncio.wait_for(task, 1)
        assert rejected.value.code == "TURN_DECISION_STALE"
        assert cancelled.is_set()
        assert not confirmation.confirmed and confirmation._confirmed_final is None
        assert not commits and endpoint.capture.is_open
        assert len(endpoint.capture.prepares) == 1
    finally:
        await endpoint.close()
        await asyncio.gather(task, return_exceptions=True)
