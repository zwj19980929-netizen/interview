"""Ordinary answers wait for a delivered supplement question and fresh reply."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.core.interaction_budget import remaining_interaction_seconds
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint, _final


BODY = "我会封装异步客户端，统一鉴权和超时处理。我的回答结束了。"
FINISH = "没有需要补充的了。"


class Capture(_Capture):
    def __init__(self, *, intent="answer", turn_intent="finish_answer"):
        super().__init__()
        self.current_final = _final(BODY)
        self.intent, self.turn_intent = intent, turn_intent
        self.replies = []
        self.flags = []

    async def prepare_decision(self, final, **kwargs):
        self.prepares.append(final)
        self.flags.append(kwargs)
        action = "continue_listening" if self.turn_intent == "answering" and not kwargs.get("completion_confirmed") else "next"
        return SimpleNamespace(text=final.text, understanding=SimpleNamespace(
            problem=None, intent=self.intent, turn_intent=self.turn_intent,
            suggested_action=action, completion_basis=None), final=final)

    async def classify_supplement_reply(self, reply, **kwargs):
        self.replies.append(reply)
        return {"intent": "finish" if reply.endswith(FINISH) else "supplement",
                "confidence": .98, "evidence_quote": reply,
                "answer_effect": "unchanged" if reply.endswith(FINISH) else "updated"}


def setup(**kwargs):
    capture = Capture(**kwargs)
    endpoint, clock, notices, commits = _endpoint(capture=capture)
    spoken = []

    async def speak(kind, guard, **kw):
        guard()
        spoken.append(kind)
        return {"act_id": "synthetic_" + str(len(spoken))}

    confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint.confirmation = confirmation
    endpoint._proposal_revision = endpoint.revision
    return endpoint, confirmation, spoken, commits


async def answer(endpoint, confirmation):
    endpoint._proposal_revision = endpoint.revision
    await confirmation._semantic_turn(endpoint, endpoint.capture.current_final)


async def reply(endpoint, confirmation, text):
    endpoint.capture.current_final = _final(text)
    endpoint.speech_started(source="transcript")
    endpoint._proposal_revision = endpoint.revision
    await confirmation._reply(endpoint, endpoint.capture.current_final)


@pytest.mark.anyio
@pytest.mark.parametrize("intent,turn_intent", [
    ("answer", "finish_answer"), ("off_topic", "finish_answer"), ("answer", "answering"),
])
async def test_ordinary_answer_asks_once_then_accepts_only_a_fresh_delivered_reply(intent, turn_intent):
    endpoint, confirmation, spoken, commits = setup(intent=intent, turn_intent=turn_intent)
    try:
        await answer(endpoint, confirmation)
        assert spoken == ["check"] and not commits and endpoint.capture.is_open
        assert not confirmation.confirmed
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        await reply(endpoint, confirmation, BODY + FINISH)
        assert len(commits) == 1 and commits[0].text == BODY + FINISH
        assert endpoint.capture.replies == [FINISH]
        assert spoken == ["check"]
        assert endpoint.capture.flags[-1]["completion_confirmed"] is True
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("early", [False, True])
async def test_old_or_during_playback_finish_cannot_become_a_new_confirmation(early):
    endpoint, confirmation, spoken, commits = setup()
    try:
        if not early:
            endpoint.capture.current_final = _final(BODY + FINISH)
        await answer(endpoint, confirmation)
        endpoint.capture.current_final = _final(BODY + FINISH)
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        await reply(endpoint, confirmation, BODY + FINISH)
        assert not commits and not endpoint.capture.replies and spoken == ["check"]
        await reply(endpoint, confirmation, BODY + FINISH + FINISH)
        assert len(commits) == 1 and endpoint.capture.replies == [FINISH]
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["barge_in", "failed", "confirmation_playback_timeout"])
async def test_interrupted_question_does_not_authorize_a_finish(reason):
    endpoint, confirmation, spoken, commits = setup()
    try:
        await answer(endpoint, confirmation)
        await confirmation.complete_playback(endpoint, reason=reason)
        await reply(endpoint, confirmation, BODY + FINISH)
        assert spoken == ["check", "check"]
        assert not commits and not endpoint.capture.replies and not confirmation.confirmed
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        await reply(endpoint, confirmation, BODY + FINISH + FINISH)
        assert len(commits) == 1
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_continued_details_stay_in_the_same_capture_without_another_check():
    endpoint, confirmation, spoken, commits = setup()
    original_capture = endpoint.capture
    try:
        await answer(endpoint, confirmation)
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        prefix = BODY + "我再补充一下鉴权。"
        await reply(endpoint, confirmation, prefix)
        assert spoken == ["check", "continue"] and not commits
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        endpoint.capture.current_final = _final(prefix + "密钥保存在环境变量中。" + FINISH)
        endpoint.speech_started(source="transcript")
        await answer(endpoint, confirmation)
        assert len(commits) == 1 and commits[0].text == endpoint.capture.current_final.text
        assert endpoint.capture is original_capture
        assert spoken == ["check", "continue"]
        assert endpoint.capture.replies[-1] == "密钥保存在环境变量中。" + FINISH
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_explicit_finish_button_keeps_its_existing_authority():
    endpoint, confirmation, spoken, commits = setup()
    try:
        endpoint.request_finish()
        endpoint._proposal_revision = endpoint.revision
        prepared = await endpoint.capture.prepare_decision(endpoint.capture.current_final)
        await endpoint._propose(final_snapshot=endpoint.capture.current_final,
                                prepared_decision=prepared, recognition_resumed=True)
        assert len(commits) == 1 and not spoken
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("intent,turn_intent", [
    ("answer_declined", "decline_topic"), ("answer", "finish_topic"),
    ("answer_unresolved", "finish_answer"),
])
async def test_distinct_topic_and_unresolved_intents_do_not_acquire_a_second_check(intent, turn_intent):
    endpoint, confirmation, spoken, commits = setup(intent=intent, turn_intent=turn_intent)
    try:
        prepared = await endpoint.capture.prepare_decision(endpoint.capture.current_final)
        assert not confirmation.needs_supplement_confirmation(prepared.understanding)
        assert not await confirmation.gate_answer_completion(endpoint, prepared, endpoint.capture.current_final)
        assert not spoken and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_final_cut_that_changes_a_decline_into_an_answer_must_still_ask():
    endpoint, confirmation, spoken, commits = setup()
    try:
        old = _final("这题不会。")
        prepared = SimpleNamespace(understanding=SimpleNamespace(
            problem=None, intent="answer_declined", turn_intent="decline_topic", suggested_action="next"))
        await endpoint._propose(final_snapshot=old, prepared_decision=prepared, recognition_resumed=True)
        assert spoken == ["check"] and not commits
        assert endpoint.capture.prepares[-1].text == BODY
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_expired_idle_supplement_reminder_has_one_independent_failure_budget():
    endpoint, confirmation, spoken, commits = setup()
    calls = []
    try:
        await answer(endpoint, confirmation)
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        endpoint._interaction_deadline = time.monotonic() - 1

        async def failing_reminder(kind, guard, **kwargs):
            guard()
            calls.append((kind, remaining_interaction_seconds()))
            raise asyncio.TimeoutError()

        confirmation.speak = failing_reminder
        await confirmation.step(endpoint, 16)
        await confirmation.step(endpoint, 60)
        assert len(calls) == 1 and calls[0][0] == "clarify"
        assert 0 < calls[0][1] <= 12
        assert endpoint._prepare_failures == 0 and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_supplement_reminder_propagates_ownership_loss():
    endpoint, confirmation, spoken, commits = setup()
    try:
        await answer(endpoint, confirmation)
        await confirmation.complete_playback(endpoint, reason="agent_finished")

        async def ownership_lost(kind, guard, **kwargs):
            raise ApiError("EVIDENCE_OWNER_FENCED", "Synthetic owner changed.", status_code=409)

        confirmation.speak = ownership_lost
        with pytest.raises(ApiError, match="Synthetic owner changed"):
            await confirmation.step(endpoint, 16)
        assert endpoint._prepare_failures == 0 and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_uncertain_reply_does_not_spend_full_analysis_or_grant_completion():
    endpoint, confirmation, spoken, commits = setup()
    try:
        await answer(endpoint, confirmation)
        await confirmation.complete_playback(endpoint, reason="agent_finished")

        async def uncertain(words, **kwargs):
            endpoint.capture.replies.append(words)
            return {"intent": "unclear", "confidence": .9, "evidence_quote": words, "answer_effect": "uncertain",
                    "reply_text": "我想先确认一下，你刚才是希望我怎么配合？", "repeat_target": None}

        endpoint.capture.classify_supplement_reply = uncertain
        await reply(endpoint, confirmation, BODY + "我再说一句相关的内容。")
        assert endpoint.capture.replies == ["我再说一句相关的内容。"]
        assert len(endpoint.capture.prepares) == 1
        assert endpoint._prepare_failures == 0 and not confirmation.confirmed
        assert spoken == ["check", "clarify"] and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("exit_reply", ["是的，确定结束。", "不了，我继续。"])
async def test_supplement_exit_request_routes_real_audio_to_separate_exit_confirmation(tmp_path, monkeypatch, exit_reply):
    from copy import deepcopy
    from app.core.prompt.contracts import SUPPLEMENT_SPEECH
    from app.core.prompt.candidate_exit import EXIT_CONFIRMATION_SPEECH
    from app.core.prompt.understanding_references import understanding_references
    from app.providers.mock.provider import MockProvider
    from test_automatic_turn_integration import _automatic_session, _current, _feed, _CONTINUATION
    from test_declined_answer_integration import (_semantic_provider, _add_next_question,
        _playable_speech, _answer_without_confirmation, _stop_current_playback, _TECHNICAL)
    from test_evidence_owner_recovery_integration import _wait_until
    from test_spoken_supplement_integration import _audio_aligned_transcripts
    from test_exit_evidence_086 import _assert_retained_exit

    _semantic_provider(monkeypatch, technical=True)
    original = MockProvider.invoke
    requests = []
    body, exit_request = _TECHNICAL, "我不想面了。"

    async def semantics(provider, capability, request, context):
        response = await original(provider, capability, request, context)
        if capability != "llm.chat_json":
            return response
        version = request.metadata.get("prompt_version", "")
        data = deepcopy(response.data)
        if version in {"supplement_reply.v5", "supplement_reply.v6", "supplement_reply.v7", "supplement_reply.v8", "supplement_reply.v9", "supplement_reply.v10"}:
            requests.append("supplement")
            data.update(intent="stop_interview", confidence=.95, reply_text="")
            if version in {"supplement_reply.v7", "supplement_reply.v8", "supplement_reply.v9", "supplement_reply.v10"}:
                data["answer_effect"] = "unchanged"
        elif version.startswith(("interview_turn_understanding.", "interview_turn_decision.")):
            requests.append("understanding")
            text = request.metadata["transcript"]
            understanding = data.get("understanding", data)
            if exit_request in text:
                refs = understanding_references(text, request.metadata["capability_points"])
                continuing = text.endswith("不了，我继续。")
                understanding.update(intent="not_finished" if continuing else "answer",
                    turn_intent="thinking" if continuing else "stop_interview",
                    suggested_action="continue_listening" if continuing else "next",
                    followup_allowed=False,
                    completion_basis=None if continuing else {
                        "source": "explicit_server_intent", "evidence_ids": list(refs["evidence"])[-1:]})
        return response.model_copy(update={"data": data})

    monkeypatch.setattr(MockProvider, "invoke", semantics)
    _audio_aligned_transcripts(monkeypatch, [body, exit_request, exit_reply])
    async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
        _add_next_question(store)
        spoken = _playable_speech(runtime)
        endpoint, clock = await _answer_without_confirmation(managed)
        original_capture = managed._capture_id
        await _wait_until(lambda: endpoint.confirmation.speaking)
        assert spoken == [SUPPLEMENT_SPEECH["check"]] and not _current(runtime)["answers"]
        await _stop_current_playback(runtime, channel)
        await _wait_until(lambda: endpoint.confirmation._supplement_question_delivered)
        await _feed(managed._ingress, _CONTINUATION, 5)
        clock.value += 1
        await _wait_until(lambda: endpoint.confirmation.speaking and endpoint.confirmation._exit_waiting)
        assert spoken[-1] == EXIT_CONFIRMATION_SPEECH
        assert not _current(runtime)["answers"] and not _current(runtime).get("candidate_input_completed_at")
        assert requests.count("supplement") == 1
        await _stop_current_playback(runtime, channel)
        await _wait_until(lambda: endpoint.confirmation._exit_question_delivered)
        await _feed(managed._ingress, _CONTINUATION, 5)
        clock.value += 1
        if exit_reply == "是的，确定结束。":
            await _wait_until(lambda: bool(_current(runtime).get("candidate_input_completed_at")), timeout=5)
            await _wait_until(lambda: all(command["status"] == "completed" for command in store.evidence_commands.values()))
            _assert_retained_exit(store, runtime, body + exit_request + exit_reply,
                voice_frames=5, continuation_frames=10)
            assert requests.count("understanding") == 1, "An exit confirmation must not repeat technical analysis"
        else:
            await _wait_until(lambda: not endpoint.confirmation._exit_waiting)
            assert not _current(runtime)["answers"]
            assert _current(runtime)["status"] == "in_progress" and managed._capture_id == original_capture
            assert not endpoint.confirmation.confirmed
