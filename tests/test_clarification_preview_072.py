"""Content clarification replies take the authoritative, proof-aware path."""

import asyncio
from types import SimpleNamespace

import pytest

from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint, _final, _until
from test_stable_preview_preparation import _preview


FRAGMENT = "我用蓝桥来传递任务，其他状态会记录。"
REPLY = "我说的是消息队列，没有补充了。"


class Capture(_Capture):
    def __init__(self):
        super().__init__()
        self.current_final = _final(FRAGMENT)
        self.requests = []
        self.prepare_hook = None

    async def prepare_decision(self, transcript, **kwargs):
        self.requests.append((transcript.model_copy(deep=True), kwargs))
        if self.prepare_hook:
            return await self.prepare_hook(transcript, kwargs)
        return self.prepared(transcript)


def setup():
    capture = Capture()
    endpoint, clock, _, commits = _endpoint(capture=capture)
    clock.value += 3.1  # Speculative analysis starts only after a stable longer pause.

    async def speak(kind, guard, **kwargs):
        guard()
        return {"act_id": "act_content_072"}

    confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint.confirmation = confirmation
    endpoint._proposal_revision = endpoint.revision
    understanding = SimpleNamespace(clarification_target=SimpleNamespace(
        evidence_quote=FRAGMENT, focus_quote="蓝桥"))
    return endpoint, confirmation, understanding, commits


@pytest.mark.anyio
async def test_ordinary_answer_keeps_speculation_without_claiming_a_final():
    endpoint, confirmation, _, commits = setup()
    try:
        assert not confirmation.content_clarification_active
        preview = _preview(endpoint.capture.current_final)
        endpoint.prepare_stable_preview(preview)
        await _until(lambda: len(endpoint.capture.requests) == 1)
        supplied, kwargs = endpoint.capture.requests[0]
        assert not hasattr(supplied, "is_final")
        assert kwargs == {"semantic_first": True}
        assert not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_selecting_content_question_cancels_old_preview_before_speech_returns():
    endpoint, confirmation, understanding, commits = setup()
    cancelled, selected, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked_prepare(transcript, kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def selecting_speech(kind, guard, **kwargs):
        guard()
        selected.set()
        await release.wait()
        return {"act_id": "act_content_072"}

    endpoint.capture.prepare_hook = blocked_prepare
    confirmation.speak = selecting_speech
    task = None
    try:
        endpoint.prepare_stable_preview(_preview(endpoint.capture.current_final))
        await _until(lambda: len(endpoint.capture.requests) == 1)
        task = asyncio.create_task(confirmation.clarify_answer(
            endpoint, understanding, endpoint.capture.current_final))
        await asyncio.wait_for(selected.wait(), 1)
        await asyncio.wait_for(cancelled.wait(), 1)
        assert confirmation.content_clarification_active
        endpoint.prepare_stable_preview(_preview(_final(FRAGMENT + "我再说一句。")))
        assert not endpoint.preparation.matching(_final(FRAGMENT + "我再说一句。"))
        await asyncio.sleep(0)
        assert len(endpoint.capture.requests) == 1
        release.set()
        await task
        assert confirmation.speaking
        endpoint.prepare_stable_preview(_preview(_final(FRAGMENT + REPLY)))
        assert not endpoint.preparation.matching(_final(FRAGMENT + REPLY))
        await asyncio.sleep(0)
        assert len(endpoint.capture.requests) == 1
        assert not commits
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await endpoint.close()


@pytest.mark.anyio
async def test_delivered_reply_skips_preview_and_formal_preparation_keeps_exact_proof():
    endpoint, confirmation, understanding, commits = setup()
    try:
        await confirmation.clarify_answer(endpoint, understanding, endpoint.capture.current_final)
        # Words received during playback become part of the old prefix.
        delivered_prefix = FRAGMENT + "请让我想一下。"
        endpoint.capture.current_final = _final(delivered_prefix)
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        assert confirmation.content_clarification_active
        assert confirmation.content_clarification_context(_final(delivered_prefix)) is None
        final = _final(delivered_prefix + REPLY)
        endpoint.prepare_stable_preview(_preview(final))
        assert not endpoint.preparation.matching(final)
        await asyncio.sleep(0)
        assert endpoint.capture.requests == []
        await endpoint._prepare_transcript(final, semantic_first=True)
        assert len(endpoint.capture.requests) == 1
        supplied, kwargs = endpoint.capture.requests[0]
        assert supplied.is_final and supplied.type == "transcript.final"
        assert kwargs == {"semantic_first": True, "content_clarification": {
            "version": "content_clarification.v1", "request_text": delivered_prefix,
            "focus_quote": "蓝桥", "reply_text": REPLY,
            "question_act_id": "act_content_072", "delivered": True}}
        assert not confirmation.confirmed and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("final_text", [FRAGMENT, "供应商修改了旧的前缀。" + REPLY])
async def test_missing_fresh_suffix_never_gets_fabricated_clarification_proof(final_text):
    endpoint, confirmation, understanding, commits = setup()
    try:
        await confirmation.clarify_answer(endpoint, understanding, endpoint.capture.current_final)
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        final = _final(final_text)
        endpoint.prepare_stable_preview(_preview(final))
        assert not endpoint.preparation.matching(final)
        await asyncio.sleep(0)
        assert endpoint.capture.requests == []
        await endpoint._prepare_transcript(final, semantic_first=True)
        assert endpoint.capture.requests[0][1] == {"semantic_first": True}
        assert confirmation.content_clarification_context(final) is None
        assert not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["barge_in", "confirmation_playback_timeout", "failed"])
async def test_undelivered_question_stays_noncommitting_and_does_not_restart_speculation(reason):
    endpoint, confirmation, understanding, commits = setup()
    try:
        await confirmation.clarify_answer(endpoint, understanding, endpoint.capture.current_final)
        await confirmation.complete_playback(endpoint, reason=reason)
        final = _final(FRAGMENT + REPLY)
        endpoint.prepare_stable_preview(_preview(final))
        assert not endpoint.preparation.matching(final)
        await asyncio.sleep(0)
        assert endpoint.capture.requests == []
        assert confirmation.content_clarification_context(final) is None
        assert not confirmation.confirmed and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_unplayed_output_recovery_restores_ordinary_preview_eligibility():
    endpoint, confirmation, understanding, commits = setup()
    try:
        checkpoint = confirmation.begin_unplayed_speech()
        await confirmation.clarify_answer(endpoint, understanding, endpoint.capture.current_final)
        assert confirmation.content_clarification_active
        assert confirmation.restore_unplayed_speech(checkpoint)
        assert not confirmation.content_clarification_active
        endpoint.prepare_stable_preview(_preview(endpoint.capture.current_final))
        await _until(lambda: len(endpoint.capture.requests) == 1)
        assert endpoint.capture.requests[0][1] == {"semantic_first": True}
        assert not commits
    finally:
        await endpoint.close()
