"""Interrupted conversation must not consume an unspoken candidate request."""
import asyncio
import gc

import pytest

from app.core.errors import ApiError
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _endpoint, _final
from test_conversation_reception import ReceptionCapture
from test_spoken_supplement import ask, setup
from test_answer_endpoint import _until
from app.core.prompt.contracts import SUPPLEMENT_SPEECH


@pytest.mark.anyio
@pytest.mark.parametrize("interruption", ["unavailable", "new_audio", "new_words"])
async def test_unspoken_reception_request_is_retried_with_its_complete_new_speech(interruption):
    capture = ReceptionCapture("other")
    endpoint, _, _, commits = _endpoint(capture=capture)
    entered, release = asyncio.Event(), asyncio.Event()
    selected = []
    attempts = 0

    async def speak(kind, guard):
        nonlocal attempts
        attempts += 1
        guard()
        if attempts == 1:
            entered.set()
            if interruption == "unavailable":
                return False
            await release.wait()
        guard()
        selected.append(kind)
        return True

    confirmation = endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    original = "我会先说明系统边界。"
    pending = "请让我再想一下。"
    endpoint._proposal_revision = endpoint.revision
    try:
        assert not await confirmation._receive_request(endpoint, _final(original))
        capture.kind = "wait"
        task = asyncio.create_task(confirmation._receive_request(endpoint, _final(original + pending)))
        await asyncio.wait_for(entered.wait(), 1)
        if interruption != "unavailable":
            endpoint.speech_started(source="audio")
            release.set()
            with pytest.raises(ApiError) as caught:
                await task
            assert caught.value.code == "TURN_DECISION_STALE"
        else:
            assert await task
        assert not selected and not commits
        assert confirmation._reception_seen_text == original, "Unspoken requests are still pending"

        endpoint._proposal_revision = endpoint.revision
        fresh = pending + ("对。" if interruption == "new_words" else "")
        assert await confirmation._receive_request(endpoint, _final(original + fresh))
        assert selected == ["reception_wait"] and not commits
        assert capture.requests[-1][0] == fresh
        assert capture.requests[-1][1]["preceding_text"] == original
        assert confirmation._reception_seen_text == original + fresh
    finally:
        release.set()
        await endpoint.close()


@pytest.mark.anyio
async def test_cancelled_inference_waiter_consumes_already_failed_owned_child():
    endpoint, _, _, _, _ = setup()
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    parent = None

    async def failed_speech():
        # The owner is cancelled before asyncio.wait can read this result.
        # A done child still belongs to the endpoint and must be consumed.
        loop.call_soon(parent.cancel)
        raise ApiError("TURN_DECISION_STALE", "Synthetic revoked speech.", status_code=409)

    try:
        parent = asyncio.create_task(endpoint._infer(failed_speech(), timeout=1))
        with pytest.raises(asyncio.CancelledError):
            await parent
        parent = None
        await asyncio.sleep(0)
        gc.collect()
        assert errors == []
    finally:
        loop.set_exception_handler(old_handler)
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize(("fresh", "intent", "next_speech"), [
    ("对。", "finish", None),
    ("是的，刚才已经讲完了。", "finish", None),
    ("不对，我还得想想。", "continue", "continue"),
    ("另外重试需要保持同一幂等键。", "supplement", "continue"),
    ("我只是在引用客户说的已经讲完。", "unclear", "clarify"),
    ("字幕仍然不是我说的话。", "unclear", "clarify"),
])
async def test_new_reply_reclassifies_with_prior_confirmed_words_but_never_prior_permission(fresh, intent, next_speech):
    endpoint, clock, _, commits, spoken = setup()
    release = asyncio.Event()
    requests = []
    prior = "这部分没有需要补充的了。"

    async def classify(reply, **context):
        requests.append((reply, context))
        if len(requests) == 1:
            return {"intent": "finish", "confidence": .96, "evidence_quote": reply}
        assert not endpoint.confirmation.confirmed, "New words must revoke the old permission before classification"
        return {"intent": intent, "confidence": .96, "evidence_quote": reply}

    async def slow(final):
        await release.wait()
        return endpoint.capture.prepared(final)

    endpoint.capture.classify_supplement_reply = classify
    endpoint.capture.prepare_impl = slow
    try:
        await ask(endpoint, clock, spoken)
        prior_final = _final("合成回答结束。" + prior)
        endpoint.capture.current_final = prior_final
        endpoint.observe_transcript(prior_final.text)
        clock.value += 1
        await _until(lambda: len(endpoint.capture.prepares) == 1)

        current = _final(prior_final.text + fresh)
        endpoint.capture.current_final = current
        endpoint.observe_transcript(current.text)
        clock.value += 1
        await _until(lambda: len(requests) == 2)
        assert requests == [(prior, {}), (fresh, {"prior_confirmation": True, "preceding_text": prior,
                                               "question": SUPPLEMENT_SPEECH["check"]})]
        if next_speech:
            await _until(lambda: len(spoken) == 2)
            assert spoken == ["check", next_speech] and not commits
            assert endpoint.capture.is_open and not endpoint.confirmation.confirmed
        else:
            await _until(lambda: len(endpoint.capture.prepares) == 2)
            assert not commits and spoken == ["check"]
            release.set()
            await _until(lambda: len(commits) == 1)
            assert commits[0].text == current.text
            assert commits[0].final.segments == current.segments
    finally:
        release.set()
        await endpoint.close()
