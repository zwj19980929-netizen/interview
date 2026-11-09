"""Server-only conversational context remains bound to the current capture."""
import asyncio
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.model_gateway.schemas import StableTranscriptPreview
from test_answer_endpoint import _endpoint, _final
from test_answer_reuse_078 import _append, _delivered
from test_preparation_fences_081 import _chain


@pytest.mark.anyio
async def test_confirmed_stable_preview_keeps_semantic_understanding_mode():
    endpoint, _, _, _ = _endpoint()
    endpoint.confirmation = SimpleNamespace(confirmed=True, semantic_first=True)
    received = []
    async def prepare(value, **kwargs):
        received.append(kwargs)
        return endpoint.capture.prepared(value)
    endpoint.capture.prepare_decision = prepare
    final = _final()
    preview = StableTranscriptPreview(stream_id=final.stream_id, revision=2, text=final.text,
        language=final.language, segments=final.segments, provider=final.provider,
        confidence=final.confidence, has_unstable_tail=False)
    endpoint._proposal_revision = endpoint.revision
    await endpoint._prepare_transcript(preview)
    assert received == [{"completion_confirmed": True, "semantic_first": True}]
    await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("invalidated", [None, "capture", "revoked", "owner"])
async def test_confirmation_context_is_fenced_before_and_after_inference(tmp_path, monkeypatch, invalidated):
    harness = _chain(tmp_path, monkeypatch)
    chain = harness.chain
    _delivered(harness.stream.interviews)
    final = _append(harness.final, "对。")
    harness.stream.stream = SimpleNamespace(_final=final)
    entered, release = asyncio.Event(), asyncio.Event()
    calls, guards = [], []
    original_guard = harness.stream.commit_guard
    def guard():
        guards.append(True)
        original_guard()
    async def classify(reply, org, **kwargs):
        calls.append((reply, org, kwargs))
        entered.set()
        await asyncio.wait_for(release.wait(), 1)
        return {"intent": "unclear", "confidence": .4}
    harness.stream.commit_guard = guard
    monkeypatch.setattr(harness.stream.interviews.conversation, "classify_supplement_reply", classify)
    task = asyncio.create_task(chain.classify_supplement_reply("对。", prior_confirmation=True,
        preceding_text="我已经说完了。", question="还有需要补充的吗？",
        final=final, boundary_final=harness.final))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if invalidated == "capture": chain._stt = None
        elif invalidated == "revoked": chain.revoke_audio_input()
        elif invalidated == "owner": assert harness.coordinator.release(harness.owner)
        release.set()
        if invalidated:
            with pytest.raises(ApiError) as error: await asyncio.wait_for(task, 1)
            assert error.value.code == ("EVIDENCE_OWNER_FENCED" if invalidated == "owner" else "TURN_DECISION_STALE")
        else:
            assert (await asyncio.wait_for(task, 1))["intent"] == "unclear"
            assert len(guards) == 2
        assert len(calls) == 1
        reply, organization, context = calls[0]
        assert (reply, organization) == ("对。", "org_default")
        assert context["prior_confirmation"] is True
        assert context["preceding_text"] == "我已经说完了。"
        assert context["conversation_context"]["stage"] == "awaiting_reply"
        assert context["conversation_context"]["reply_target"] == "supplement_question"
        assert context["question"] == context["conversation_context"]["last_delivered_utterance"]
        assert not harness.stream.interviews.get_interview("iv_binding")["answers"]
    finally:
        release.set()
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)
