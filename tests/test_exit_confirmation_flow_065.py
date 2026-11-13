"""A candidate withdrawal needs a new reply after a delivered question."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint, _final


REQUEST = "我用幂等键保证重复请求不会产生副作用。我不想面了。"


class ExitCapture(_Capture):
    def __init__(self):
        super().__init__()
        self.current_final = _final(REQUEST)
        self.exit_decision = "confirm"
        self.exit_confidence = .98
        self.replies = []
        self.contexts = []
        self.classify_impl = None
        self.intent = "stop_interview"

    async def classify_exit_reply(self, reply):
        self.replies.append(reply)
        if self.classify_impl:
            return await self.classify_impl(reply)
        return {"intent": self.exit_decision, "confidence": self.exit_confidence, "evidence_quote": reply}

    async def prepare_decision(self, final, **kwargs):
        self.prepares.append(final.model_copy(deep=True))
        self.contexts.append(deepcopy(kwargs))
        continuing = (kwargs.get("exit_confirmation") or {}).get("decision") == "continue" and final.text.endswith("不了，我继续。")
        return SimpleNamespace(text=final.text, final=final.model_copy(deep=True),
            exit_confirmation=kwargs.get("exit_confirmation"),
            understanding=SimpleNamespace(problem=None, completion_basis=None,
                suggested_action="continue_listening" if continuing else "next",
                turn_intent="answering" if continuing else self.intent))


def setup():
    capture = ExitCapture()
    endpoint, clock, notices, commits = _endpoint(capture=capture)
    spoken = []

    async def speak(kind, guard):
        guard()
        spoken.append(kind)
        return {"act_id": "question_%s" % len(spoken)} if kind.startswith("exit_") else True

    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak,
        interview_id="iv_test", turn_id="turn_test", capture_id="capture_test")
    return endpoint, clock, notices, commits, spoken


async def ask(endpoint):
    endpoint._proposal_revision = endpoint.revision
    await endpoint._propose(final_snapshot=endpoint.capture.current_final, recognition_resumed=True)


async def reply(endpoint, words, *, confidence=.95):
    endpoint.capture.current_final = _final(REQUEST + words, confidence=confidence)
    endpoint.speech_started()
    endpoint._proposal_revision = endpoint.revision
    await endpoint.confirmation.step(endpoint, 1)


def test_first_stop_keeps_capture_and_original_answer_until_a_new_confirmation():
    async def scenario():
        endpoint, clock, _, commits, spoken = setup()
        await ask(endpoint)
        assert spoken == ["exit_check"]
        assert not commits and endpoint.capture.is_open
        assert not endpoint.confirmation.confirmed
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        assert endpoint.confirmation._exit_question_delivered
        await reply(endpoint, "是的，确定结束。")
        assert len(commits) == 1 and commits[0].text == REQUEST + "是的，确定结束。"
        context = commits[0].exit_confirmation
        assert context["request_text"] == REQUEST
        assert context["reply_text"] == "是的，确定结束。"
        assert context["decision"] == "confirm" and context["question_act_id"] == "question_1"
        assert context["capture_id"] == "capture_test"
        assert endpoint.capture.replies == ["是的，确定结束。"]
        await endpoint.close()
    asyncio.run(scenario())


def test_no_confirmation_policy_cannot_commit_a_whole_interview_stop():
    async def scenario():
        capture = ExitCapture()
        endpoint, _, _, commits = _endpoint(capture=capture)
        await ask(endpoint)
        assert not commits and capture.is_open
        await endpoint.close()
    asyncio.run(scenario())


def test_silence_only_reminds_once_and_never_exits():
    async def scenario():
        endpoint, clock, _, commits, spoken = setup()
        await ask(endpoint)
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        clock.value = 16
        await endpoint.confirmation.step(endpoint, 16)
        assert spoken == ["exit_check", "exit_clarify"]
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        clock.value = 600
        await endpoint.confirmation.step(endpoint, 600)
        assert len(spoken) == 2 and not commits and not endpoint.capture.replies
        await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["barge_in", "confirmation_playback_timeout", "candidate_continues", "stale_output"])
def test_interrupted_question_never_borrows_the_early_yes(reason):
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        await ask(endpoint)
        await endpoint.confirmation.complete_playback(endpoint, reason=reason)
        await reply(endpoint, "确定。")
        assert spoken == ["exit_check", "exit_clarify"]
        assert not commits and not endpoint.capture.replies
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        endpoint._proposal_revision = endpoint.revision
        await endpoint.confirmation._exit_reply(endpoint, endpoint.capture.current_final)
        assert not commits, "The earlier yes precedes the reasked question"
        await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(("decision", "confidence"), [("uncertain", .98), ("confirm", .5)])
def test_uncertain_reply_cannot_commit_and_becomes_a_new_question_boundary(decision, confidence):
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        await ask(endpoint)
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        endpoint.capture.exit_decision, endpoint.capture.exit_confidence = decision, confidence
        await reply(endpoint, "嗯。")
        assert not commits and spoken[-1] == "exit_clarify"
        assert endpoint.confirmation._exit_request_final.text == REQUEST + "嗯。"
        await endpoint.close()
    asyncio.run(scenario())


def test_low_stt_confidence_never_reaches_exit_classifier():
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        await ask(endpoint)
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        await reply(endpoint, "确定。", confidence=.4)
        assert not commits and not endpoint.capture.replies and spoken[-1] == "exit_clarify"
        await endpoint.close()
    asyncio.run(scenario())


def test_denial_retains_answer_and_context_then_allows_normal_answer_completion():
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        await ask(endpoint)
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        endpoint.capture.exit_decision = "continue"
        await reply(endpoint, "不了，我继续。")
        assert not commits and endpoint.capture.is_open and not endpoint.confirmation._exit_waiting
        assert spoken[-1] == "reception_continue"
        saved = deepcopy(endpoint.confirmation._exit_context)
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        endpoint.capture.intent = "finish_answer"
        endpoint.capture.current_final = _final(REQUEST + "不了，我继续。幂等键还需要唯一约束。说完了。")
        endpoint.speech_started()
        await ask(endpoint)
        assert len(commits) == 1
        assert commits[0].exit_confirmation == saved, "The withdrawal receipt must not expand to later words"
        assert "幂等键还需要唯一约束" in commits[0].text
        assert spoken.count("exit_check") == 1
        await endpoint.close()
    asyncio.run(scenario())


def test_new_stop_after_denial_requires_a_new_delivered_question():
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        await ask(endpoint)
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        endpoint.capture.exit_decision = "continue"
        await reply(endpoint, "不了，我继续。")
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        endpoint.capture.current_final = _final(REQUEST + "不了，我继续。现在我决定结束面试。")
        endpoint.speech_started()
        await ask(endpoint)
        assert not commits and spoken == ["exit_check", "reception_continue", "exit_check"]
        assert not endpoint.confirmation._exit_question_delivered
        await endpoint.close()
    asyncio.run(scenario())


def test_explicit_answer_finish_button_does_not_confirm_exit():
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        await ask(endpoint)
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        endpoint.request_finish()
        await endpoint.confirmation.step(endpoint, 1)
        assert not commits and not endpoint.confirmation.confirmed and spoken == ["exit_check"]
        await endpoint.close()
    asyncio.run(scenario())


def test_ambiguous_old_transcript_rewrite_requires_new_question():
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        await ask(endpoint)
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        endpoint.capture.current_final = _final("修订后的旧回答。确定结束。")
        endpoint.speech_started()
        endpoint._proposal_revision = endpoint.revision
        await endpoint.confirmation.step(endpoint, 1)
        assert not commits and not endpoint.capture.replies and spoken[-1] == "exit_clarify"
        await endpoint.close()
    asyncio.run(scenario())


def test_new_speech_revokes_an_inflight_confirmation_classification():
    async def scenario():
        endpoint, _, _, commits, _ = setup()
        await ask(endpoint)
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        started = asyncio.Event()
        async def classify(words):
            started.set()
            await asyncio.Event().wait()
        endpoint.capture.classify_impl = classify
        task = asyncio.create_task(reply(endpoint, "确定。"))
        await started.wait()
        endpoint.speech_started()
        with pytest.raises(ApiError) as error:
            await task
        assert error.value.code == "TURN_DECISION_STALE"
        assert not commits and not endpoint.confirmation.confirmed
        await endpoint.close()
    asyncio.run(scenario())


def test_speech_before_playback_completed_and_later_noise_cannot_supply_confirmation():
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        await ask(endpoint)
        endpoint.capture.current_final = _final(REQUEST + "确定结束。")
        endpoint.speech_started()
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        assert endpoint.confirmation._exit_request_final.text == REQUEST + "确定结束。"
        await ask(endpoint)
        assert not commits and not endpoint.capture.replies
        endpoint.speech_started()  # noise with the same complete server final
        await ask(endpoint)
        assert not commits and not endpoint.capture.replies
        endpoint.capture.current_final = _final(REQUEST + "确定结束。是的，现在确定退出。")
        endpoint.speech_started()
        endpoint._proposal_revision = endpoint.revision
        await endpoint.confirmation.step(endpoint, 1)
        assert len(commits) == 1 and endpoint.capture.replies == ["是的，现在确定退出。"]
        assert spoken == ["exit_check"]
        await endpoint.close()
    asyncio.run(scenario())


def test_transport_sequence_changes_do_not_revoke_identical_exit_evidence():
    async def scenario():
        endpoint, _, _, commits, _ = setup()
        await ask(endpoint)
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        source = _final(REQUEST + "确定结束。")
        endpoint.capture.current_final = source.model_copy(update={"sequence": 99, "stream_id": "rotated"})
        endpoint.speech_started()
        endpoint._proposal_revision = endpoint.revision
        await endpoint.confirmation._exit_reply(endpoint, source)
        assert len(commits) == 1
        await endpoint.close()
    asyncio.run(scenario())


def test_withdrawal_reply_keeps_its_substantive_answer_until_supplement_confirmation(tmp_path, monkeypatch):
    from app.core.prompt.contracts import SUPPLEMENT_SPEECH
    from app.core.prompt.understanding_references import understanding_references
    from app.providers.mock.provider import MockProvider
    from test_automatic_turn_integration import _automatic_session, _current, _feed, _CONTINUATION
    from test_declined_answer_integration import (
        _TECHNICAL, _NEXT_TURN_ID, _add_next_question, _semantic_provider,
        _playable_speech, _answer_and_confirm_exit, _stop_current_playback,
    )
    from test_evidence_owner_recovery_integration import _wait_until
    from test_spoken_supplement_integration import _audio_aligned_transcripts, _speaking

    _semantic_provider(monkeypatch, technical=True, stop_interview=True)
    original = MockProvider.invoke
    reply_words = "不了，我继续。补充一点，我会设置唯一约束。我的回答说完了。"
    body = _TECHNICAL + "我不想面了。"
    _audio_aligned_transcripts(monkeypatch, [body, reply_words, "没有补充了。"])

    async def invoke(provider, capability, request, context):
        response = await original(provider, capability, request, context)
        version = str(request.metadata.get("prompt_version") or "")
        if version.startswith(("interview_turn_understanding.", "interview_turn_decision.")):
            server_context = next((json.loads(message.content.removeprefix("面试官上下文："))
                for message in request.messages if message.content.startswith("面试官上下文：")), {})
            if (server_context.get("exit_confirmation") or {}).get("decision") == "continue":
                data = response.data.get("understanding", response.data)
                references = understanding_references(request.metadata["transcript"], request.metadata["capability_points"])
                data.update(turn_intent="finish_answer", suggested_action="next", followup_allowed=True,
                    completion_basis={"source": "explicit_server_intent", "evidence_ids": list(references["evidence"])[-1:]})
        return response
    monkeypatch.setattr(MockProvider, "invoke", invoke)

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""],
                spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            _playable_speech(runtime)
            endpoint, clock = await _answer_and_confirm_exit(runtime, channel, managed)
            await _speaking(endpoint, runtime)
            current = _current(runtime)
            assert not current["answers"] and not current.get("candidate_input_completed_at")
            selected = [event for event in current["agent_events"] if event["type"] == "conversation.act.selected"]
            assert selected[-1]["payload"]["text"] == SUPPLEMENT_SPEECH["check"]
            assert not endpoint.confirmation._exit_waiting
            capture_id = managed._capture_id
            await _stop_current_playback(runtime, channel)
            await _wait_until(lambda: endpoint.confirmation._supplement_question_delivered)
            assert managed._capture_id == capture_id and not _current(runtime)["answers"]
            await _feed(managed._ingress, _CONTINUATION, 5)
            clock.value += 1
            await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            current = _current(runtime)
            assert not current.get("candidate_input_completed_at")
            assert current["current_turn_id"] == _NEXT_TURN_ID
            assert current["answers"][0]["final_transcript"] == body + reply_words + "没有补充了。"
            assert current["turns"][0]["current_understanding"]["turn_intent"] == "finish_answer"
            assert current["turns"][0]["current_understanding"]["completion_basis"]["evidence_quotes"] == ["没有补充了。"]
            assert not current["turns"][0].get("candidate_exit_confirmation")
            assert current["turns"][0]["current_understanding"]["claims"][0]["evidence_quote"] == _TECHNICAL
    asyncio.run(scenario())


def test_exit_waiting_snapshot_and_capture_recovery_keep_the_confirmation_object(tmp_path, monkeypatch):
    from test_automatic_turn_integration import _automatic_session, _current
    from test_declined_answer_integration import (
        _TECHNICAL, _add_next_question, _semantic_provider, _playable_speech,
        _answer_without_confirmation, _stop_current_playback,
    )
    from test_evidence_owner_recovery_integration import TURN_ID, _wait_until

    _semantic_provider(monkeypatch, technical=True, stop_interview=True)
    async def scenario():
        body = _TECHNICAL + "我不想面了。"
        async with _automatic_session(tmp_path, monkeypatch, [body, "", "", ""],
                spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            _playable_speech(runtime)
            endpoint, _ = await _answer_without_confirmation(managed)
            await _wait_until(lambda: endpoint.confirmation.speaking
                and bool(endpoint.confirmation._exit_question_act_id)
                and managed.chain._stt.stream._stream is not None)
            capture_id = managed._capture_id
            await _stop_current_playback(runtime, channel)
            await _wait_until(lambda: (_current(runtime)["agent_runtime"].get("supplement_confirmation") or {}).get("status")
                == "awaiting_exit_reply")
            assert endpoint.confirmation._exit_question_delivered
            await managed._capture_recovery_notice("capture_recovered", capture_id, TURN_ID, channel)
            assert _current(runtime)["agent_runtime"]["floor_reason"] == "exit_awaiting_reply"
            await channel._emit_snapshot(None)
            snapshot = next(event for event in reversed(list(channel._queue._queue)) if event.type == "session.snapshot")
            assert snapshot.payload["supplement_confirmation"] == {
                "status": "awaiting_exit_reply", "turn_id": TURN_ID, "capture_id": capture_id}
            assert not _current(runtime)["answers"] and not _current(runtime).get("candidate_input_completed_at")
            assert managed.chain.is_open
    asyncio.run(scenario())
