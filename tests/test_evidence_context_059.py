"""Server-only conversational context remains bound to the current capture."""
import asyncio
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.model_gateway.schemas import StableTranscriptPreview
from app.repositories.memory import InMemoryStore
from app.services.interview_evidence import InterviewEvidenceChain
from test_answer_endpoint import _endpoint, _final


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
async def test_confirmation_context_is_fenced_before_and_after_inference(invalidated):
    chain = InterviewEvidenceChain(InMemoryStore(), "synthetic_interview")
    entered, release = asyncio.Event(), asyncio.Event()
    calls, guards = [], []
    allowed = True
    def guard():
        guards.append(True)
        if not allowed:
            raise ApiError("TURN_DECISION_STALE", "Owner changed", status_code=409)
    async def classify(reply, org, **kwargs):
        calls.append((reply, org, kwargs))
        entered.set()
        await release.wait()
        return {"intent": "unclear", "confidence": .4}
    chain._stt = SimpleNamespace(_assert_commit_allowed=guard,
        interviews=SimpleNamespace(conversation=SimpleNamespace(classify_supplement_reply=classify)))
    task = asyncio.create_task(chain.classify_supplement_reply("对。", prior_confirmation=True,
        preceding_text="我已经说完了。", question="还有需要补充的吗？"))
    await entered.wait()
    if invalidated == "capture": chain._stt = None
    elif invalidated == "revoked": chain.revoke_audio_input()
    elif invalidated == "owner": allowed = False
    release.set()
    if invalidated:
        with pytest.raises(ApiError) as error: await task
        assert error.value.code == "TURN_DECISION_STALE"
    else:
        assert (await task)["intent"] == "unclear"
        assert len(guards) == 2
    assert calls == [("对。", "org_default", {"prior_confirmation": True,
        "preceding_text": "我已经说完了。", "question": "还有需要补充的吗？"})]
