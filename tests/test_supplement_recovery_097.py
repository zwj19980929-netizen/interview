"""New authoritative replies retain bounded recovery without buying authority."""
import asyncio

import pytest

from app.core.errors import ApiError
from app.model_gateway.errors import ProviderError
from test_answer_endpoint import _final, _until
from test_semantic_tail_runtime_096 import exchange, malformed


def fresh(endpoint, text):
    final = _final("合成回答结束。" + text)
    endpoint.capture.current_final = final
    endpoint.speech_started(source="audio")
    endpoint._proposal_revision = endpoint.revision
    return final


@pytest.mark.anyio
async def test_new_completion_reply_keeps_its_format_correction_after_old_filler_failed():
    endpoint, confirmation, clock, _, commits, spoken = exchange()
    calls, accepted = [], []

    async def classify(reply, **context):
        calls.append((reply, context))
        if len(calls) < 4:
            raise malformed()
        return {"intent": "finish", "answer_effect": "unchanged", "confidence": .95,
                "evidence_quote": reply, "reply_text": ""}

    async def accept(_endpoint, final, **kwargs):
        _endpoint.assert_current()
        accepted.append((final, kwargs))

    endpoint.capture.classify_supplement_reply = classify
    confirmation._accept_finish = accept
    try:
        first = fresh(endpoint, "Well.")
        await confirmation._reply(endpoint, first)
        await confirmation._reply(endpoint, first)
        assert endpoint.preparation_blocked
        await confirmation.control_step(endpoint, quiet=10)
        assert spoken == ["reception_recovery_clarification"]
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        clock.value += 10
        second = fresh(endpoint, "Well.没有需要补充的了。")
        await confirmation._reply(endpoint, second)
        assert len(calls) == 3 and not endpoint.preparation_blocked
        await confirmation._reply(endpoint, second)
        assert len(calls) == 4 and len(accepted) == 1
        assert calls[1][1]["correction_reason"] == "invalid_json_object"
        assert calls[2][1].get("correction_reason") is None
        assert calls[3][1]["correction_reason"] == "invalid_json_object"
        assert accepted[0][0] is second
        assert endpoint._prepare_failures == 0, "Small-reply errors must not spend full-answer retries"
        assert not commits and endpoint.capture.is_open
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_failed_exchange_has_four_calls_total_even_across_new_words():
    endpoint, confirmation, _, _, commits, _ = exchange()
    calls = []

    async def classify(reply, **context):
        calls.append(reply)
        raise malformed()

    endpoint.capture.classify_supplement_reply = classify
    try:
        for text in ["第一段控制回复。", "第二段控制回复。", "第三段控制回复。", "第四段控制回复。"]:
            final = fresh(endpoint, text)
            await confirmation._reply(endpoint, final)
            await confirmation._reply(endpoint, final)
        assert len(calls) == 4
        assert endpoint.preparation_blocked and endpoint._recovery_retryable
        assert not commits and not confirmation.confirmed
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_new_words_and_metadata_cannot_extend_failed_exchange_deadline():
    endpoint, confirmation, clock, _, commits, _ = exchange()
    calls = []

    async def classify(reply, **context):
        calls.append(reply)
        raise malformed()

    endpoint.capture.classify_supplement_reply = classify
    try:
        first = fresh(endpoint, "第一段控制回复。")
        await confirmation._reply(endpoint, first)
        clock.value += 39
        second = fresh(endpoint, "第二段控制回复。")
        await confirmation._reply(endpoint, second)
        assert len(calls) == 2
        clock.value += 1.01
        second.provider.request_id = "synthetic-source-rotation"
        endpoint.speech_started(source="audio")
        endpoint._proposal_revision = endpoint.revision
        await confirmation._reply(endpoint, second)
        third = fresh(endpoint, "第三段控制回复。")
        await confirmation._reply(endpoint, third)
        assert len(calls) == 2 and endpoint.preparation_blocked
        assert not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_valid_classification_ends_failure_episode_without_erasing_same_words_ledger():
    endpoint, confirmation, clock, _, commits, _ = exchange()
    calls, replies = [], []

    async def classify(reply, **context):
        calls.append(reply)
        if reply.endswith("请稍等。"):
            return {"intent": "wait", "answer_effect": "unchanged", "confidence": .95,
                    "evidence_quote": reply, "reply_text": "好的。"}
        raise malformed()

    async def respond(_endpoint, final, reply, decision, **kwargs):
        _endpoint.assert_current()
        replies.append(reply)

    endpoint.capture.classify_supplement_reply = classify
    confirmation._respond_reception = respond
    try:
        old = fresh(endpoint, "第一段控制回复。")
        await confirmation._reply(endpoint, old)
        await confirmation._reply(endpoint, old)
        success = fresh(endpoint, "请稍等。")
        await confirmation._reply(endpoint, success)
        clock.value += 50
        newer = fresh(endpoint, "第三段控制回复。")
        await confirmation._reply(endpoint, newer)
        await confirmation._reply(endpoint, newer)
        success = fresh(endpoint, "现在请稍等。")
        await confirmation._reply(endpoint, success)
        assert len(calls) == 6 and len(replies) == 2
        endpoint._proposal_revision = endpoint.revision
        await confirmation._reply(endpoint, old)
        assert len(calls) == 6, "A successful reply cannot refund two calls for old words"
        assert not commits and endpoint.capture.is_open
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_two_cancelled_calls_cannot_refund_same_words_but_new_reply_has_two_slots():
    endpoint, confirmation, clock, _, commits, _ = exchange()
    calls, waiting = [], asyncio.Event()

    async def classify(reply, **context):
        calls.append((reply, context))
        if len(calls) <= 2:
            waiting.set()
            await asyncio.Event().wait()
        raise malformed()

    endpoint.capture.classify_supplement_reply = classify
    first = fresh(endpoint, "第一段控制回复。")
    try:
        for index in range(2):
            waiting.clear()
            endpoint._proposal_revision = endpoint.revision
            task = asyncio.create_task(confirmation._reply(endpoint, first))
            await asyncio.wait_for(waiting.wait(), 1)
            endpoint.speech_started(source="audio")
            with pytest.raises(ApiError, match="New input|changed"):
                await task
        endpoint._proposal_revision = endpoint.revision
        await confirmation._reply(endpoint, first)
        assert len(calls) == 2
        second = fresh(endpoint, "第二段控制回复。")
        await confirmation._reply(endpoint, second)
        await confirmation._reply(endpoint, second)
        assert len(calls) == 4 and calls[-1][1]["correction_reason"] == "invalid_json_object"
        assert endpoint.preparation_blocked and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_full_answer_failures_keep_their_original_two_failure_limit():
    endpoint, confirmation, _, _, commits, _ = exchange()
    confirmation.phase = "listening"
    endpoint._use_preparation_budget("合成技术回答。")
    endpoint._begin_interaction("合成技术回答。")
    try:
        endpoint._record_preparation_failure(problem=malformed())
        assert not endpoint.preparation_blocked
        endpoint._record_preparation_failure(problem=malformed())
        assert endpoint.preparation_blocked and endpoint._prepare_failures == 2
        assert not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_last_call_uses_remaining_episode_time_and_ignores_old_technical_deadline():
    endpoint, confirmation, clock, _, commits, _ = exchange()
    calls, cancelled = [], asyncio.Event()

    async def classify(reply, **context):
        calls.append(reply)
        if len(calls) == 1:
            raise malformed()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    endpoint.capture.classify_supplement_reply = classify
    try:
        first = fresh(endpoint, "第一段控制回复。")
        endpoint._interaction_deadline = 0  # Full-answer work cannot govern this smaller call.
        await confirmation._reply(endpoint, first)
        assert len(calls) == 1
        clock.value += 39.99
        second = fresh(endpoint, "第二段控制回复。")
        await asyncio.wait_for(confirmation._reply(endpoint, second), .5)
        assert len(calls) == 2 and cancelled.is_set()
        assert endpoint._recovery_reason == "timeout"
        clock.value += .02
        await confirmation._reply(endpoint, fresh(endpoint, "第三段控制回复。"))
        assert len(calls) == 2 and endpoint.preparation_blocked and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_permanent_provider_failure_is_not_renewed_by_fresh_words():
    endpoint, confirmation, _, _, commits, _ = exchange()
    calls = []

    async def classify(reply, **context):
        calls.append(reply)
        raise ProviderError("provider_auth_failed", "synthetic denial", retryable=False)

    endpoint.capture.classify_supplement_reply = classify
    try:
        await confirmation._reply(endpoint, fresh(endpoint, "第一段控制回复。"))
        await confirmation._reply(endpoint, fresh(endpoint, "第二段控制回复。"))
        assert len(calls) == 1 and not endpoint._recovery_retryable
        assert not endpoint.retry_answer_processing()
        assert endpoint.preparation_blocked and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["continue", "retry"])
async def test_explicit_controls_renew_calls_but_only_continue_revokes_completion_fact(action):
    endpoint, confirmation, _, _, commits, _ = exchange()
    calls, proof = [], object()

    async def classify(reply, **context):
        calls.append(context)
        raise malformed()

    endpoint.capture.classify_supplement_reply = classify
    try:
        final = fresh(endpoint, "第一段控制回复。")
        await confirmation._reply(endpoint, final)
        await confirmation._reply(endpoint, final)
        confirmation._confirmation_proof = proof
        confirmation._confirmed_final = final
        if action == "continue":
            endpoint.continue_speaking()
        else:
            assert endpoint.retry_answer_processing()
        assert (confirmation._confirmation_proof is proof) == (action == "retry")
        assert (confirmation._confirmed_final is final) == (action == "retry")
        endpoint._proposal_revision = endpoint.revision
        await confirmation._reply(endpoint, final)
        await confirmation._reply(endpoint, final)
        assert len(calls) == 4 and calls[2].get("correction_reason") is None
        assert endpoint.preparation_blocked and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["revision", "capture", "epoch", "deadline"])
async def test_stale_or_late_success_cannot_finish_a_failure_episode(change):
    from test_spoken_supplement import Capture

    endpoint, confirmation, clock, _, commits, _ = exchange()
    calls, accepted = [], []

    async def classify(reply, **context):
        calls.append(reply)
        if len(calls) == 1:
            raise malformed()
        if change == "revision":
            endpoint.speech_started(source="audio")
        elif change == "capture":
            endpoint.capture = Capture()
        elif change == "epoch":
            endpoint._reset_supplement_budget()
        else:
            clock.value += 41
        return {"intent": "finish", "answer_effect": "unchanged", "confidence": .95,
                "evidence_quote": reply, "reply_text": ""}

    async def accept(*args, **kwargs):
        accepted.append(kwargs)

    endpoint.capture.classify_supplement_reply = classify
    confirmation._accept_finish = accept
    try:
        final = fresh(endpoint, "第一段控制回复。")
        await confirmation._reply(endpoint, final)
        if change == "deadline":
            await confirmation._reply(endpoint, final)
            assert endpoint.preparation_blocked and endpoint._recovery_reason == "timeout"
        else:
            with pytest.raises(ApiError) as caught:
                await confirmation._reply(endpoint, final)
            assert caught.value.code == "TURN_DECISION_STALE"
        assert len(calls) == 2 and not accepted and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_supplement_success_cannot_clear_exhausted_full_answer_budget():
    endpoint, confirmation, _, _, commits, _ = exchange()
    technical_text = "合成技术回答。"
    endpoint._begin_interaction(technical_text)
    endpoint._use_preparation_budget(technical_text)
    endpoint._record_preparation_failure(problem=malformed())
    endpoint._record_preparation_failure(problem=malformed())
    technical_key = endpoint._preparation_budget_key
    calls = []

    async def classify(reply, **context):
        calls.append(reply)
        return {"intent": "wait", "answer_effect": "unchanged", "confidence": .95,
                "evidence_quote": reply, "reply_text": "好的。"}

    async def respond(*args, **kwargs):
        pass

    endpoint.capture.classify_supplement_reply = classify
    confirmation._respond_reception = respond
    try:
        await confirmation._reply(endpoint, fresh(endpoint, "请稍等。"))
        assert len(calls) == 1
        assert endpoint._prepare_failures == 2 and endpoint._preparation_terminal
        assert endpoint._preparation_failures_by_evidence[technical_key] == 2
        endpoint._use_preparation_budget(technical_text)
        assert endpoint.preparation_blocked and not commits
    finally:
        await endpoint.close()
