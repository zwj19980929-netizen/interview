"""A delivered supplement exchange classifies new replies before full analysis."""
import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from app.core.answer_processing_budget import consume_answer_model_call
from app.core.errors import ApiError
from app.model_gateway.schemas import TranscriptSegment
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint, _final
from test_stable_preview_preparation import _preview


def append(final, words):
    end = final.segments[-1].end_ms
    return final.model_copy(deep=True, update={"text": final.text + words,
        "segments": [*final.segments, TranscriptSegment(text=words, confidence=.9,
            start_ms=end + 100, end_ms=end + 1100)]})


class Capture(_Capture):
    supports_stable_preview = True

    def __init__(self):
        super().__init__()
        self.current_final = _final("我用请求标识避免重复创建。")
        self.context = 0
        self.fast = "other"
        self.order = []
        self.flags = []
        self.receipt_attempts = []
        self.deadlines = []
        self.endpoint = None

    async def transcript_preview(self):
        return _preview(self.current_final)

    async def classify_reception(self, text, **kwargs):
        self.order.append("reception")
        return {"kind": self.fast, "confidence": .95, "evidence_quote": text}

    async def classify_supplement_reply(self, text, **kwargs):
        self.order.append("supplement")
        self.deadlines.append(self.endpoint._interaction_deadline)
        return {"intent": "finish", "confidence": .95, "evidence_quote": text}

    async def prepare_decision(self, final, **kwargs):
        assert consume_answer_model_call()
        self.order.append("confirmed" if kwargs.get("completion_confirmed") else "semantic")
        self.flags.append(kwargs)
        self.prepares.append(final.model_copy(deep=True))
        self.deadlines.append(self.endpoint._interaction_deadline)
        return SimpleNamespace(text=final.text, final=final.model_copy(deep=True),
            context=self.context, flags=kwargs,
            understanding=SimpleNamespace(problem=None, intent="answer", turn_intent="finish_answer",
                                          suggested_action="accept"))

    def assert_preparation_current(self, prepared, final, **kwargs):
        if (prepared.context != self.context or prepared.flags != kwargs
                or self.endpoint._identity(prepared.final) != self.endpoint._identity(final)):
            raise ApiError("TURN_DECISION_STALE", "Synthetic evidence changed.", status_code=409)

    def confirm_prepared_decision(self, prepared, base, boundary, final, **kwargs):
        self.receipt_attempts.append((prepared.context, self.context))
        # A new question explanation invalidates this old technical source.
        assert prepared.context != self.context
        return None


async def setup_exchange():
    capture = Capture()
    endpoint, clock, notices, commits = _endpoint(capture=capture)
    capture.endpoint = endpoint
    spoken = []

    async def speak(kind, guard, **kwargs):
        guard()
        spoken.append(kind)
        if kind == "reception_question_clarification":
            capture.context += 1
        return {"act_id": "synthetic_" + kind}

    confirmation = endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint._proposal_revision = endpoint.revision
    await confirmation._listen(endpoint)
    assert spoken == ["check"] and not commits
    await confirmation.complete_playback(endpoint, reason="agent_finished")
    assert confirmation._supplement_question_delivered

    capture.current_final = append(capture.current_final, "请解释一下题目。")
    capture.fast = "question_clarification"
    endpoint._proposal_revision = endpoint.revision
    await confirmation._receive_request(endpoint, capture.current_final, reconcile_complete=True)
    await confirmation.complete_playback(endpoint, reason="agent_finished")
    assert confirmation.phase == "listening" and capture.context == 1
    assert confirmation._supplement_question_delivered and not commits
    capture.fast = "other"
    capture.order.clear()
    capture.flags.clear()
    capture.prepares.clear()
    capture.deadlines.clear()
    return endpoint, capture, clock, notices, commits, spoken


@pytest.mark.anyio
@pytest.mark.parametrize("reply", ["没有补充了。", "还会保存处理状态以支持重试。没有补充了。"])
async def test_explanation_then_new_finish_uses_one_confirmed_analysis_without_renewing_budget(reply):
    endpoint, capture, _, _, commits, _ = await setup_exchange()
    try:
        capture.current_final = append(capture.current_final, reply)
        endpoint._proposal_revision = endpoint.revision
        await endpoint.confirmation._listen(endpoint)
        assert capture.order == ["supplement", "confirmed"]
        assert capture.flags == [{"completion_confirmed": True, "semantic_first": True}]
        assert capture.receipt_attempts == [(0, 1)]
        assert len(commits) == 1 and commits[0].text == capture.current_final.text
        assert len(capture.prepares) == 1
        assert len(set(capture.deadlines)) == 1
        # Compare the absolute deadline; subtracting floating-point instants
        # can round a correctly bounded 20-second interval above or below 20.
        assert endpoint._interaction_deadline == endpoint._interaction_started_at + 20
        key = hashlib.sha256(capture.current_final.text.strip().encode()).hexdigest()
        budget = endpoint._model_budgets[key]
        assert (budget.used, budget.limit) == (1, 2)
        assert budget.consume() and not budget.consume(), "Classification cannot refund or extend model allowance"
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_listening_preview_of_supplement_reply_does_not_start_context_free_preparation():
    endpoint, capture, clock, _, commits, _ = await setup_exchange()
    try:
        capture.current_final = append(capture.current_final, "还会检查状态。没有补充了。")
        endpoint._last_voice = clock.value
        clock.value += 6
        await endpoint.confirmation.step(endpoint, quiet=6)
        assert capture.order == ["supplement", "confirmed"]
        assert len(capture.prepares) == 1 and not endpoint.preparation.has_preparation
        assert len(commits) == 1
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_early_reception_check_does_not_use_preview_as_a_confirmation():
    endpoint, capture, clock, _, commits, _ = await setup_exchange()
    try:
        capture.current_final = append(capture.current_final, "没有补充了。")
        endpoint._last_voice = clock.value
        clock.value += 4
        await endpoint.confirmation.step(endpoint, quiet=4)
        await asyncio.sleep(0)
        assert capture.order == []
        assert not capture.prepares and not commits and not endpoint.confirmation.confirmed
    finally:
        await endpoint.close()
