"""Control speech uses its own delivered handshake, not answer analysis.

The endpoint and spoken policy are real. The capture stands in for the already
validated reception seam and the existing owner-fenced evidence transaction;
gateway source validation and durable exit writes have separate integration
coverage. All speech and identities here are synthetic.
"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.core.answer_processing_budget import AnswerProcessingBudget
from app.core.errors import ApiError
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint, _final, _until
from test_stable_preview_preparation import _preview


TECHNICAL = "我会用消息队列调度任务，并给重复请求设置幂等键。"
REQUEST = TECHNICAL + "我想退出面试了，不好意思。"
CONFIRM = "是的，我确定退出面试。"


class ControlCapture(_Capture):
    supports_stable_preview = True

    def __init__(self, text=REQUEST):
        super().__init__()
        self.current_final = _final(text)
        self.kind = "stop_interview"
        self.reception_confidence = .98
        self.receptions = []
        self.reception_impl = None
        self.exit_reply = "confirm"
        self.exit_reply_confidence = .98
        self.exit_replies = []
        self.exit_reply_impl = None
        self.exit_prepares = []
        self.owner_current = True
        self.allow_answer_analysis = False
        self.after_exit_prepare = None

    def assert_owner(self):
        if not self.owner_current:
            raise ApiError("EVIDENCE_OWNER_FENCED", "Synthetic owner was revoked.", status_code=409)

    async def transcript_snapshot(self, *, resume=True):
        self.assert_owner()
        return await super().transcript_snapshot(resume=resume)

    async def transcript_preview(self):
        self.assert_owner()
        return _preview(self.current_final)

    async def classify_reception(self, text, **context):
        self.assert_owner()
        self.receptions.append((text, deepcopy(context)))
        if self.reception_impl:
            return await self.reception_impl(text)
        return {"kind": self.kind, "confidence": self.reception_confidence,
                "evidence_quote": text, "reply_text": ""}

    async def classify_exit_reply(self, reply):
        self.assert_owner()
        self.exit_replies.append(reply)
        if self.exit_reply_impl:
            return await self.exit_reply_impl(reply)
        return {"intent": self.exit_reply, "confidence": self.exit_reply_confidence,
                "evidence_quote": reply}

    def prepare_exit_decision(self, final, *, exit_confirmation):
        self.assert_owner()
        prepared = SimpleNamespace(text=final.text, final=final.model_copy(deep=True),
                                   exit_confirmation=deepcopy(exit_confirmation))
        self.exit_prepares.append(prepared)
        if self.after_exit_prepare:
            self.after_exit_prepare()
        return prepared

    async def prepare_decision(self, final, **context):
        self.prepares.append((final.model_copy(deep=True), deepcopy(context)))
        assert self.allow_answer_analysis, "An exit control must not require complete answer analysis"
        return SimpleNamespace(text=final.text, final=final.model_copy(deep=True),
            understanding=SimpleNamespace(problem=None, completion_basis=None,
                intent="answer", turn_intent="answering", suggested_action="continue_listening"))


def setup(*, text=REQUEST, commit_impl=None):
    capture = ControlCapture(text)

    async def fenced_commit(prepared, guard):
        guard()
        capture.assert_owner()
        if commit_impl:
            await commit_impl(prepared, guard)

    endpoint, clock, notices, commits = _endpoint(capture=capture, commit_impl=fenced_commit)
    spoken = []

    async def speak(kind, guard, **kwargs):
        guard()
        capture.assert_owner()
        spoken.append((kind, deepcopy(kwargs)))
        return {"act_id": "control_question_%s" % len(spoken)} if kind.startswith("exit_") else True

    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak,
        interview_id="iv_control_086", turn_id="turn_control_086", capture_id="capture_control_086")
    endpoint._proposal_revision = endpoint.revision
    return endpoint, clock, notices, commits, spoken


def spoken_kinds(spoken):
    return [kind for kind, _ in spoken]


def exhaust_answer_analysis(endpoint, text):
    endpoint._use_preparation_budget(text)
    allowance = AnswerProcessingBudget(limit=2, used=2, defer_corrections=True)
    endpoint._model_budgets[endpoint._preparation_budget_key] = allowance
    endpoint._preparation_failures_by_evidence[endpoint._preparation_budget_key] = 2
    endpoint._prepare_failures = 2
    endpoint._preparation_terminal = True
    assert endpoint.preparation_blocked
    return allowance


async def ask(endpoint):
    endpoint._proposal_revision = endpoint.revision
    await endpoint.confirmation._listen(endpoint)


async def deliver(endpoint):
    await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
    assert endpoint.confirmation._exit_question_delivered


async def reply(endpoint, words=CONFIRM, *, confidence=.95):
    endpoint.capture.current_final = _final(endpoint.confirmation._exit_request_final.text + words,
                                            confidence=confidence)
    endpoint.speech_started()
    endpoint._proposal_revision = endpoint.revision
    await endpoint.confirmation.step(endpoint, 1)


def test_exhausted_analysis_does_not_block_a_new_exit_handshake_or_confirmed_exit():
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        try:
            request_allowance = exhaust_answer_analysis(endpoint, REQUEST)
            await ask(endpoint)
            assert spoken_kinds(spoken) == ["exit_check"]
            assert not commits and not endpoint.capture.prepares and endpoint.capture.is_open
            assert not endpoint.confirmation.confirmed
            await deliver(endpoint)
            confirmed_allowance = exhaust_answer_analysis(endpoint, REQUEST + CONFIRM)
            await reply(endpoint)
            assert len(commits) == 1 and len(endpoint.capture.exit_prepares) == 1
            assert not hasattr(commits[0], "understanding")
            assert commits[0].text == REQUEST + CONFIRM
            assert commits[0].exit_confirmation["request_text"] == REQUEST
            assert commits[0].exit_confirmation["reply_text"] == CONFIRM
            assert commits[0].exit_confirmation["question_act_id"] == "control_question_1"
            assert not endpoint.capture.prepares
            assert request_allowance.used == confirmed_allowance.used == 2
            assert not endpoint.capture.is_open
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_preview_only_caches_exit_request_until_the_authoritative_final():
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        try:
            preview = await endpoint.capture.transcript_preview()
            assert await endpoint.confirmation._receive_request(endpoint, preview, prepare_only=True)
            assert not spoken and not commits and not endpoint.capture.exit_prepares
            assert not endpoint.confirmation._exit_waiting and not endpoint.confirmation.confirmed
            await ask(endpoint)
            assert spoken_kinds(spoken) == ["exit_check"]
            assert len(endpoint.capture.receptions) == 1, "A matching final may reuse the source-bound classification"
            assert not endpoint.capture.prepares
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("answer_analysis_exhausted", [False, True])
def test_live_endpoint_routes_a_clear_exit_at_the_fast_reception_pause(answer_analysis_exhausted):
    async def scenario():
        endpoint, clock, _, commits, spoken = setup()
        try:
            endpoint.speech_started(source="audio")
            if answer_analysis_exhausted:
                exhaust_answer_analysis(endpoint, REQUEST)
            clock.value = 1.1
            endpoint.start()
            await _until(lambda: spoken)
            assert spoken_kinds(spoken) == ["exit_check"]
            assert not commits and not endpoint.capture.prepares
            assert len(endpoint.capture.receptions) == 1
            assert endpoint.confirmation._exit_waiting
            assert not endpoint.confirmation._exit_question_delivered
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_new_input_revokes_an_inflight_reception_without_starting_exit():
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        entered = asyncio.Event()
        try:
            async def delayed(text):
                entered.set()
                await asyncio.Event().wait()
            endpoint.capture.reception_impl = delayed
            task = asyncio.create_task(ask(endpoint))
            await entered.wait()
            endpoint.capture.current_final = _final("我改变主意了，继续回答。")
            endpoint.speech_started(source="transcript")
            with pytest.raises(ApiError) as exc:
                await task
            assert exc.value.code == "TURN_DECISION_STALE"
            assert not spoken and not commits and not endpoint.confirmation._exit_waiting
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_control_polling_cannot_replenish_exhausted_technical_analysis():
    async def scenario():
        endpoint, clock, _, commits, spoken = setup(text=TECHNICAL)
        try:
            endpoint.capture.kind = "other"
            endpoint.speech_started(source="audio")
            allowance = exhaust_answer_analysis(endpoint, TECHNICAL)
            clock.value = 1.1
            endpoint.start()
            await _until(lambda: endpoint.capture.receptions)
            await asyncio.sleep(.25)
            assert len(endpoint.capture.receptions) == 1, "The same cached control classification must not poll the model"
            assert allowance.used == 2 and endpoint.preparation_blocked
            assert not endpoint.capture.prepares and not endpoint.capture.exit_prepares
            assert not commits and not spoken
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_preview_exit_cannot_override_a_revised_authoritative_request():
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        try:
            assert await endpoint.confirmation._receive_request(endpoint,
                await endpoint.capture.transcript_preview(), prepare_only=True)
            endpoint.capture.current_final = _final("刚才说退出，不对，我先想一想。")
            endpoint.capture.kind = "wait"
            endpoint.speech_started(source="transcript")
            await ask(endpoint)
            assert spoken_kinds(spoken) == ["reception_wait"]
            assert len(endpoint.capture.receptions) == 2
            assert not endpoint.confirmation._exit_waiting and not commits
            assert not endpoint.capture.exit_prepares
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_repeated_cumulative_request_or_noise_does_not_repeat_exit_question():
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        try:
            await ask(endpoint)
            assert spoken_kinds(spoken) == ["exit_check"]
            await deliver(endpoint)
            for _ in range(2):
                endpoint.speech_started()
                endpoint._proposal_revision = endpoint.revision
                await endpoint.confirmation.step(endpoint, 1)
            assert spoken_kinds(spoken) == ["exit_check"]
            assert not commits and not endpoint.capture.exit_replies
            assert not endpoint.capture.prepares
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["barge_in", "confirmation_playback_timeout", "stale_output"])
def test_an_undelivered_exit_question_cannot_authorize_an_early_yes(reason):
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        try:
            await ask(endpoint)
            assert spoken_kinds(spoken) == ["exit_check"]
            await endpoint.confirmation.complete_playback(endpoint, reason=reason)
            await reply(endpoint)
            assert not commits and not endpoint.capture.exit_replies
            assert spoken_kinds(spoken) == ["exit_check", "exit_clarify"]
            await deliver(endpoint)
            endpoint._proposal_revision = endpoint.revision
            await endpoint.confirmation._exit_reply(endpoint, endpoint.capture.current_final)
            assert not commits and not endpoint.capture.exit_prepares
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_confirmation_spoken_during_the_question_is_not_a_new_reply():
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        try:
            await ask(endpoint)
            assert spoken_kinds(spoken) == ["exit_check"]
            endpoint.capture.current_final = _final(REQUEST + CONFIRM)
            endpoint.speech_started()
            await deliver(endpoint)
            endpoint.speech_started()
            endpoint._proposal_revision = endpoint.revision
            await endpoint.confirmation.step(endpoint, 1)
            assert not commits and not endpoint.capture.exit_replies
            await reply(endpoint, "现在确定退出。")
            assert len(commits) == 1
            assert endpoint.capture.exit_replies == ["现在确定退出。"]
            assert commits[0].exit_confirmation["reply_text"] == "现在确定退出。"
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["new_words", "confidence", "segments"])
def test_final_cut_change_revokes_confirmed_control_without_answer_analysis(failure):
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        try:
            await ask(endpoint)
            assert spoken_kinds(spoken) == ["exit_check"]
            await deliver(endpoint)
            async def revise(reply_words):
                current = endpoint.capture.current_final
                if failure == "new_words":
                    endpoint.capture.current_final = _final(current.text + "等等，我还是继续面试。")
                elif failure == "confidence":
                    endpoint.capture.current_final = current.model_copy(update={"confidence": .6})
                else:
                    segments = [item.model_copy(update={"end_ms": 2000}) for item in current.segments]
                    endpoint.capture.current_final = current.model_copy(update={"segments": segments})
                return {"intent": "confirm", "confidence": .98, "evidence_quote": reply_words}
            endpoint.capture.exit_reply_impl = revise
            await reply(endpoint)
            assert not commits and not endpoint.capture.exit_prepares
            assert not endpoint.capture.prepares and endpoint.capture.is_open
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("when", ["final_cut", "commit"])
def test_owner_revocation_prevents_exit_even_after_a_valid_new_confirmation(when):
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        try:
            await ask(endpoint)
            assert spoken_kinds(spoken) == ["exit_check"]
            await deliver(endpoint)
            if when == "final_cut":
                async def revoke(reply_words):
                    endpoint.capture.owner_current = False
                    return {"intent": "confirm", "confidence": .98, "evidence_quote": reply_words}
                endpoint.capture.exit_reply_impl = revoke
            else:
                endpoint.capture.after_exit_prepare = lambda: setattr(endpoint.capture, "owner_current", False)
            try:
                await reply(endpoint)
            except ApiError as exc:
                assert exc.code == "EVIDENCE_OWNER_FENCED"
            assert not commits and not endpoint.capture.prepares
            if when == "final_cut":
                assert not endpoint.capture.exit_prepares
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(("stt_confidence", "model_confidence"), [(.4, .98), (.95, .4)])
def test_low_confidence_request_cannot_start_exit_handshake(stt_confidence, model_confidence):
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        try:
            endpoint.capture.current_final = _final(REQUEST, confidence=stt_confidence)
            endpoint.capture.reception_confidence = model_confidence
            assert not await endpoint.confirmation._receive_request(endpoint, endpoint.capture.current_final)
            assert not spoken and not commits and not endpoint.confirmation._exit_waiting
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(("words", "kind"), [
    ("稍等，我再想想。", "wait"),
    ("请再读一下刚才的问题。", "repeat"),
    ("这个问题里说的幂等是什么意思？", "question_clarification"),
])
def test_existing_dialogue_reception_keeps_its_own_route(words, kind):
    async def scenario():
        endpoint, _, _, commits, spoken = setup(text=words)
        try:
            endpoint.capture.kind = kind
            await ask(endpoint)
            assert spoken_kinds(spoken) == ["reception_" + kind]
            assert not endpoint.confirmation._exit_waiting and not commits
            assert not endpoint.capture.exit_prepares and not endpoint.capture.prepares
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_withdrawal_of_exit_keeps_technical_speech_and_resumes_answer_review():
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        try:
            await ask(endpoint)
            assert spoken_kinds(spoken) == ["exit_check"]
            await deliver(endpoint)
            endpoint.capture.exit_reply = "continue"
            endpoint.capture.allow_answer_analysis = True
            words = "不了，我继续。补充一下，数据库里也要设置唯一约束。"
            await reply(endpoint, words)
            assert not commits and not endpoint.capture.exit_prepares
            assert not endpoint.confirmation._exit_waiting and endpoint.capture.is_open
            assert endpoint.capture.current_final.text == REQUEST + words
            assert len(endpoint.capture.prepares) == 1
            assert endpoint.capture.prepares[0][0].text == REQUEST + words
            assert endpoint.capture.prepares[0][1]["exit_confirmation"]["decision"] == "continue"
            assert spoken_kinds(spoken)[-1] == "check", "Substantive withdrawal speech keeps the supplement gate"
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_two_exit_classifier_failures_stop_snapshot_polling_until_new_input():
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        try:
            await ask(endpoint)
            assert spoken_kinds(spoken) == ["exit_check"]
            await deliver(endpoint)

            async def unavailable(reply_words):
                raise RuntimeError("Synthetic control classifier unavailable")

            endpoint.capture.exit_reply_impl = unavailable
            endpoint.capture.current_final = _final(REQUEST + CONFIRM)
            endpoint.speech_started()
            for _ in range(2):
                endpoint._proposal_revision = endpoint.revision
                try:
                    await endpoint.confirmation.step(endpoint, 1)
                except RuntimeError:
                    pass
            assert len(endpoint.capture.exit_replies) == 2
            snapshots = list(endpoint.capture.snapshots)
            for _ in range(5):
                endpoint._proposal_revision = endpoint.revision
                await endpoint.confirmation.step(endpoint, 1)
            assert endpoint.capture.snapshots == snapshots, "An exhausted control reply must not churn STT cuts"
            assert len(endpoint.capture.exit_replies) == 2 and not commits
            endpoint.capture.exit_reply_impl = None
            endpoint.capture.current_final = _final(REQUEST + CONFIRM + "现在确定退出。")
            endpoint.speech_started()
            endpoint._proposal_revision = endpoint.revision
            await endpoint.confirmation.step(endpoint, 1)
            assert len(endpoint.capture.exit_replies) == 3
            assert len(commits) == 1 and not endpoint.capture.prepares
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_blocked_ordinary_speech_does_not_cut_again_on_microphone_noise():
    async def scenario():
        endpoint, clock, _, commits, spoken = setup(text=TECHNICAL)
        endpoint.capture.kind = "other"
        try:
            exhaust_answer_analysis(endpoint, TECHNICAL)
            endpoint.speech_started(source="audio")
            clock.value = 2
            endpoint.start()
            await _until(lambda: endpoint.capture.receptions)
            cuts = len(endpoint.capture.snapshots)
            for _ in range(3):
                endpoint.speech_started(source="audio")
                clock.value += 3
                await asyncio.sleep(.12)
            assert len(endpoint.capture.snapshots) == cuts
            assert len(endpoint.capture.receptions) == 1
            assert not endpoint.capture.prepares and not commits and not spoken
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_blocked_partial_can_become_stable_without_new_words_or_audio_revision():
    async def scenario():
        endpoint, clock, _, commits, spoken = setup()
        stable = False
        previews = []
        async def preview():
            previews.append(stable)
            return _preview(endpoint.capture.current_final, has_unstable_tail=not stable)
        endpoint.capture.transcript_preview = preview
        try:
            exhaust_answer_analysis(endpoint, REQUEST)
            endpoint.speech_started(source="audio")
            clock.value = 2
            endpoint.start()
            await _until(lambda: previews)
            revision = endpoint.revision
            assert not endpoint.capture.receptions and not endpoint.capture.snapshots
            stable = True
            await _until(lambda: spoken)
            assert endpoint.revision == revision
            assert spoken_kinds(spoken) == ["exit_check"]
            assert len(endpoint.capture.receptions) == 1 and not endpoint.capture.prepares and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_exit_classifier_budget_cannot_be_renewed_by_metadata_only_revision():
    async def scenario():
        endpoint, _, _, commits, _ = setup()
        try:
            await ask(endpoint)
            await deliver(endpoint)
            async def unavailable(reply_words):
                raise RuntimeError("Synthetic control unavailable")
            endpoint.capture.exit_reply_impl = unavailable
            endpoint.capture.current_final = _final(REQUEST + CONFIRM)
            endpoint.speech_started()
            for _ in range(2):
                endpoint._proposal_revision = endpoint.revision
                try:
                    await endpoint.confirmation.step(endpoint, 1)
                except RuntimeError:
                    pass
            assert len(endpoint.capture.exit_replies) == 2
            endpoint.capture.current_final = endpoint.capture.current_final.model_copy(update={"confidence": .92})
            endpoint.speech_started(source="audio")
            endpoint._proposal_revision = endpoint.revision
            await endpoint.confirmation.step(endpoint, 1)
            assert len(endpoint.capture.exit_replies) == 2 and not commits and not endpoint.capture.prepares
            endpoint.confirmation.retry_pending_prompt(endpoint)
            endpoint.capture.exit_reply_impl = None
            await endpoint.confirmation.step(endpoint, 1)
            assert len(endpoint.capture.exit_replies) == 3 and len(commits) == 1
        finally:
            await endpoint.close()
    asyncio.run(scenario())
