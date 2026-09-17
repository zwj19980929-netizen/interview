"""The initial semantic proposal is revocable and never a fabricated handshake."""
import asyncio
from types import SimpleNamespace

import pytest

from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint, _final, _until
from test_prepared_turn_decision import Gateway, _input, _service
from test_semantic_turn_policy import semantic_response


class SemanticCapture(_Capture):
    def __init__(self, *, thinking=False):
        super().__init__()
        self.current_final = _final("这題没有实际回答。让我想一下。" if thinking else "这题我不会。下一题吧。")
        self.thinking = thinking
        self.entered = asyncio.Event()
        self.release = None
        self.failure = False
        self.flags = []

    async def prepare_decision(self, final, *, semantic_first=False, completion_confirmed=False):
        self.flags.append((semantic_first, completion_confirmed))
        self.prepares.append(final)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.failure:
            raise ValueError("synthetic invalid response")
        utterance, turn, interview = _input()
        utterance = utterance.model_copy(update={"text": final.text})
        interview["_semantic_first"] = True
        data = semantic_response(content="none", intent="thinking" if self.thinking else "decline_topic", technical=False)
        understanding, followup = await _service(Gateway(data)).prepare_decision(utterance, turn, interview)
        return SimpleNamespace(text=final.text, final=final.model_copy(deep=True), understanding=understanding, followup=followup)


def setup(*, thinking=False):
    capture = SemanticCapture(thinking=thinking)
    endpoint, clock, notices, commits = _endpoint(capture=capture)
    spoken = []
    async def speak(kind, guard):
        guard()
        spoken.append(kind)
        return True
    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint.speech_started()
    endpoint.start()
    clock.value = 5
    return endpoint, clock, notices, commits, spoken


def test_clear_decline_submits_first_final_without_confirmed_flag_or_second_inference():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await _until(lambda: commits)
        assert len(commits) == 1 and not spoken
        assert endpoint.capture.flags == [(True, False)]
        assert not endpoint.confirmation.confirmed
        assert endpoint.capture.snapshots == [False]
        assert commits[0].understanding.completion_basis.evidence_quotes == ["下一题吧。"]
        await endpoint.close()
    asyncio.run(scenario())


def test_thinking_acknowledges_once_then_waits_without_supplement_check():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup(thinking=True)
        await _until(lambda: spoken)
        endpoint.confirmation.floor_returned(endpoint)
        for _ in range(3):
            endpoint.speech_started()
            clock.value += 30
            await asyncio.sleep(.06)
        assert not commits and spoken == ["reception_wait"]
        assert len(endpoint.capture.flags) == 1
        assert endpoint.capture.is_open
        await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("interrupt", ["voice", "transcript", "close"])
def test_new_input_or_close_revokes_first_semantic_preparation(interrupt):
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        endpoint.capture.release = asyncio.Event()
        await asyncio.wait_for(endpoint.capture.entered.wait(), 1)
        if interrupt == "voice":
            endpoint.speech_started()
        elif interrupt == "transcript":
            endpoint.observe_transcript("这题我不会。不对，我想起来了。")
        else:
            await endpoint.close()
        endpoint.capture.release.set()
        await asyncio.sleep(.1)
        assert not commits and not spoken and not endpoint.confirmation.confirmed
        if interrupt != "close":
            assert endpoint.capture.is_open and endpoint.capture.resumes > 0
        await endpoint.close()
    asyncio.run(scenario())


def test_semantic_failures_use_one_shared_budget_and_noise_does_not_reset_it():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        endpoint.capture.failure = True
        for attempt in range(3):
            clock.value += 5
            await _until(lambda: endpoint._prepare_failures == attempt + 1)
            if endpoint.confirmation.speaking:
                endpoint.confirmation.floor_returned(endpoint)
        assert len(endpoint.capture.prepares) == 3
        for _ in range(3):
            endpoint.speech_started()
            clock.value += 10
            await asyncio.sleep(.06)
        assert len(endpoint.capture.prepares) == 3
        assert "understanding_retry_exhausted" in notices
        assert not commits and spoken == ["reception_unavailable"] and endpoint.capture.is_open
        await endpoint.close()
    asyncio.run(scenario())
