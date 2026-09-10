import asyncio

import pytest

from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint, _final, _until


class Capture(_Capture):
    recovery_required = False

    def __init__(self):
        super().__init__()
        self.replies = []
        self.intent = "finish"
        self.confidence = 0.95
        self.classify_impl = None

    async def classify_supplement_reply(self, reply):
        self.replies.append(reply)
        if self.classify_impl:
            return await self.classify_impl(reply)
        return {"intent": self.intent, "confidence": self.confidence, "evidence_quote": reply}

    async def prepare_decision(self, final, *, completion_confirmed=False):
        assert completion_confirmed
        return await super().prepare_decision(final)


def setup():
    capture = Capture()
    endpoint, clock, notices, commits = _endpoint(capture=capture)
    spoken = []

    async def speak(kind, guard):
        guard()
        spoken.append(kind)
        return True

    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    return endpoint, clock, notices, commits, spoken


async def ask(endpoint, clock, spoken):
    endpoint.speech_started()
    endpoint.start()
    clock.value += 4.99
    await asyncio.sleep(0.12)
    assert not spoken
    clock.value += 0.01
    await _until(lambda: spoken)
    assert spoken == ["check"]
    endpoint.confirmation.floor_returned(endpoint)


def test_five_seconds_only_asks_and_no_reply_never_submits():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        endpoint.detector.probability = None
        await ask(endpoint, clock, spoken)
        clock.value += 16
        await _until(lambda: len(spoken) == 2)
        endpoint.confirmation.floor_returned(endpoint)
        clock.value += 600
        await asyncio.sleep(0.15)
        assert spoken == ["check", "clarify"]
        assert not commits and not endpoint.capture.prepares and not endpoint.capture.replies
        assert not endpoint.detector.calls
        await endpoint.close()
    asyncio.run(scenario())


def test_unrecognized_reply_eventually_speaks_clarification_without_submitting():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        endpoint.capture.current_final = None
        endpoint.speech_started()
        clock.value += 1
        await _until(lambda: "transcript_unavailable" in notices)
        clock.value += 5
        await _until(lambda: spoken == ["check", "clarify"])
        endpoint.confirmation.floor_returned(endpoint)
        clock.value += 100
        await asyncio.sleep(0.12)
        assert not commits and not endpoint.capture.replies
        assert spoken == ["check", "clarify"], "Bound reminders; silence never grants completion"
        await endpoint.close()
    asyncio.run(scenario())


def test_spoken_no_finishes_complete_authoritative_answer_once():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        endpoint.capture.current_final = _final("合成回答结束。没有补充了。")
        endpoint.speech_started()
        clock.value += 1
        await _until(lambda: commits)
        assert endpoint.capture.replies == ["没有补充了。"]
        assert endpoint.capture.snapshots == [True, False], "Use the reply final without opening/finishing an empty stream"
        assert len(commits) == 1 and commits[0].text == "合成回答结束。没有补充了。"
        await endpoint.close()
    asyncio.run(scenario())


def test_unrecognized_tail_after_continue_still_asks_but_never_commits_old_prefix():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        endpoint.capture.intent = "continue"
        endpoint.capture.current_final = _final("合成回答结束。我再想想。")
        endpoint.speech_started()
        clock.value += 1
        await _until(lambda: spoken[-1] == "continue")
        endpoint.confirmation.floor_returned(endpoint)
        endpoint.capture.current_final = None
        clock.value += 5
        await _until(lambda: len(spoken) == 3)
        assert spoken[-1] == "check" and not commits
        endpoint.confirmation.floor_returned(endpoint)
        endpoint.speech_started()
        clock.value += 1
        await _until(lambda: "transcript_unavailable" in notices)
        assert not commits, "An old boundary cannot stand in for a complete reply final"
        endpoint.capture.intent = "finish"
        endpoint.capture.current_final = _final("合成回答结束。我再想想。没有需要补充的了。")
        endpoint.speech_started()
        clock.value += 3
        await _until(lambda: commits)
        assert len(commits) == 1 and commits[0].text.endswith("没有需要补充的了。")
        await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("intent", ["continue", "supplement"])
def test_spoken_yes_or_substantive_addition_keeps_same_capture(intent):
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        endpoint.capture.intent = intent
        endpoint.capture.current_final = _final("合成回答结束。有补充，先检查服务状态。")
        endpoint.speech_started()
        clock.value += 1
        await _until(lambda: spoken[-1] == "continue")
        endpoint.confirmation.floor_returned(endpoint)
        assert endpoint.confirmation.phase == "listening"
        assert endpoint.capture.is_open and not commits and not endpoint.capture.prepares
        clock.value += 5
        await _until(lambda: len(spoken) == 3)
        assert spoken == ["check", "continue", "check"]
        assert endpoint.confirmation.boundary == endpoint.capture.current_final.text
        await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("intent,confidence", [("unclear", 0.95), ("finish", 0.5)])
def test_uncertain_reply_asks_again_and_does_not_reinterpret_old_reply(intent, confidence):
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        endpoint.capture.intent, endpoint.capture.confidence = intent, confidence
        endpoint.capture.current_final = _final("合成回答结束。嗯。")
        endpoint.speech_started()
        clock.value += 1
        await _until(lambda: spoken[-1] == "clarify")
        endpoint.confirmation.floor_returned(endpoint)
        clock.value += 1
        await asyncio.sleep(0.12)
        assert endpoint.capture.replies == ["嗯。"] and not commits
        await endpoint.close()
    asyncio.run(scenario())


def test_new_speech_cancels_inflight_finish_intent():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        started = asyncio.Event()
        async def slow(reply):
            started.set()
            await asyncio.Event().wait()
        endpoint.capture.classify_impl = slow
        endpoint.capture.current_final = _final("合成回答结束。没有。")
        endpoint.speech_started()
        clock.value += 1
        await started.wait()
        endpoint.speech_started()
        await _until(lambda: endpoint._inference_task is None)
        assert not endpoint.confirmation.confirmed and not commits
        assert endpoint.capture.is_open
        await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("new_input", ["voice", "server_hypothesis"])
def test_continuation_after_finish_intent_revokes_held_final_during_answer_preparation(new_input):
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        entered = asyncio.Event()
        async def slow(final):
            entered.set()
            await asyncio.Event().wait()
        endpoint.capture.prepare_impl = slow
        endpoint.capture.current_final = _final("合成回答结束。没有了。")
        endpoint.speech_started()
        clock.value += 1
        await asyncio.wait_for(entered.wait(), 1)
        assert endpoint.confirmation.confirmed
        if new_input == "voice":
            endpoint.speech_started()
        else:
            endpoint.observe_transcript("合成回答结束。没有了。不过还要补充一点。")
        await _until(lambda: endpoint._inference_task is None and endpoint.capture.resumes > 0)
        assert not commits and not endpoint.confirmation.confirmed
        assert endpoint.confirmation.phase == "awaiting_reply" and endpoint.capture.is_open
        assert spoken == ["check"], "New input keeps the confirmation context instead of re-asking"
        await endpoint.close()
    asyncio.run(scenario())


def test_revised_prefix_cannot_be_treated_as_a_new_confirmation():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        endpoint.capture.current_final = _final("修订了旧回答。没有补充。")
        endpoint.speech_started()
        clock.value += 1
        await _until(lambda: spoken[-1] == "clarify")
        assert not endpoint.capture.replies and not commits
        await endpoint.close()
    asyncio.run(scenario())


def test_low_confidence_speech_is_not_permission_to_finish():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        endpoint.capture.current_final = _final("合成回答结束。没有。", confidence=0.3)
        endpoint.speech_started()
        clock.value += 1
        await _until(lambda: spoken[-1] == "clarify")
        assert not endpoint.capture.replies and not commits
        await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("recovery", ["new_words", "explicit_retry"])
def test_classification_failures_are_bounded_by_words_not_acoustic_revisions(recovery):
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        async def fail(reply):
            raise ValueError("synthetic invalid classifier output")
        endpoint.capture.classify_impl = fail
        endpoint.capture.current_final = _final("合成回答结束。我补充一个处理方法。")
        for attempt in range(3):
            endpoint.speech_started()
            clock.value += 5
            await _until(lambda: endpoint._prepare_failures == attempt + 1)
        assert len(endpoint.capture.replies) == 3
        assert "understanding_retry_exhausted" in notices
        for _ in range(3):
            endpoint.speech_started()
            clock.value += 10
            await asyncio.sleep(.06)
        assert len(endpoint.capture.replies) == 3
        assert not commits and endpoint.capture.is_open
        endpoint.capture.classify_impl = None
        endpoint.capture.intent = "continue"
        if recovery == "new_words":
            endpoint.capture.current_final = _final("合成回答结束。我补充一个处理方法。还有超时重试。")
            endpoint.observe_transcript(endpoint.capture.current_final.text)
        else:
            endpoint.continue_speaking()
        endpoint.speech_started()
        clock.value += 5
        await _until(lambda: spoken[-1] == "continue")
        assert len(endpoint.capture.replies) == 4
        assert not commits and endpoint.capture.is_open
        await endpoint.close()
    asyncio.run(scenario())


def test_successful_reply_retry_clears_transient_warning():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        async def classify(reply):
            if len(endpoint.capture.replies) == 1:
                raise ValueError("synthetic schema mismatch")
            return {"intent": "continue", "confidence": .98, "evidence_quote": reply}
        endpoint.capture.classify_impl = classify
        endpoint.capture.current_final = _final("合成回答结束。我还想补充。")
        endpoint.speech_started()
        clock.value += 1
        await _until(lambda: "understanding_unavailable" in notices)
        clock.value += 3
        await _until(lambda: spoken[-1] == "continue")
        assert notices.index("understanding_unavailable") < len(notices) - 1
        assert "supplement_awaiting_reply" in notices[notices.index("understanding_unavailable") + 1:]
        assert len(endpoint.capture.replies) == 2 and not commits
        await endpoint.close()
    asyncio.run(scenario())
