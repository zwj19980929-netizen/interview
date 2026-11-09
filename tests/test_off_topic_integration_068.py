"""Synthetic audio exercises the real gateway, Evidence owner and commit path."""
import asyncio
import json

import pytest

from app.core.prompt.understanding_references import understanding_references
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.providers.mock.provider import MockProvider
from app.repositories.memory import InMemoryStore
from app.services.evaluation import EvaluationService
from test_automatic_turn_integration import _automatic_session, _current, _assert_one_automatic_answer
from test_declined_answer_integration import (
    _add_next_question, _answer_without_confirmation, _answer_and_confirm_exit,
    _playable_speech, _NEXT_TURN_ID,
)
from test_evidence_owner_recovery_integration import _wait_until
from test_declined_answer_evaluation import session_for, question, persist
from test_off_topic_semantics_068 import BODY, FINISH, TEXT, off_topic_response
from test_exit_evidence_086 import _assert_retained_exit


SUPPLEMENT_REPLY = "没有补充了。"


def _audio_aligned_speech(monkeypatch, phrases):
    from app.providers.mock.provider import MockSTTStream
    original_send = MockSTTStream.send_audio
    pending, assigned = iter(phrases), []

    async def send_audio(stream, chunk):
        if any(chunk) and not getattr(stream, "_synthetic_speech_assigned", False):
            stream._synthetic_speech_assigned = True
            text = next(pending, "")
            assigned.append(text)
            stream.request = stream.request.model_copy(update={"metadata": {
                **stream.request.metadata, "development_transcript": text}})
        return await original_send(stream, chunk)

    monkeypatch.setattr(MockSTTStream, "send_audio", send_audio)
    return assigned


def _provider(monkeypatch, *, technical=False, relevant=False, stop=False, inconsistent_label=False):
    original = MockProvider.invoke
    calls = []

    async def invoke(provider, capability, request, context):
        if capability != "llm.chat_json" or request.purpose != "interview_turn_understanding":
            return await original(provider, capability, request, context)
        version = request.metadata.get("prompt_version", "")
        if version.startswith("conversation_reception."):
            data = {"kind": "other", "confidence": .98, "evidence_id": "E1", "reply_text": ""}
        elif version == "candidate_exit_reply.v1":
            reply = json.loads(request.messages[-1].content)["new_reply"]
            data = {"intent": "confirm", "confidence": .98,
                    "evidence_quote": list(understanding_references(reply, [])["evidence"].values())[-1]}
            calls.append(("exit", reply))
        elif version in {"supplement_reply.v5", "supplement_reply.v6"}:
            reply = json.loads(request.messages[-1].content)["reply"]
            if reply != SUPPLEMENT_REPLY:
                return await original(provider, capability, request, context)
            data = {"intent": "finish", "confidence": .98, "evidence_id": "E1"}
            if version == "supplement_reply.v6":
                data["reply_text"] = ""
            calls.append(("supplement", reply))
        elif version.startswith(("interview_turn_understanding.", "interview_turn_decision.")):
            transcript = request.metadata["transcript"]
            envelope = off_topic_response(transcript, request.metadata["capability_points"],
                intent="stop_interview" if stop else "finish_answer", technical=technical, relevant=relevant)
            if inconsistent_label:
                envelope["understanding"].update(intent="off_topic", followup_allowed=False)
            output_schema = request.json_schema["properties"].get("understanding", request.json_schema)
            if "company_question" in output_schema["properties"]:
                envelope["understanding"]["company_question"] = None
            if "anyOf" in output_schema["properties"]["clarification_target"]:
                assert envelope["understanding"]["ambiguities"] == []
                assert envelope["understanding"]["clarification_target"] is None
                envelope["understanding"]["clarification_target"] = {"resolution": "resolved"}
            data = envelope if "understanding" in request.json_schema["properties"] else envelope["understanding"]
            calls.append(("understanding", transcript))
        else:
            return await original(provider, capability, request, context)
        return ChatJSONResponse(data=data, usage=Usage(), provider=ProviderMeta(
            provider_id="mock", model="synthetic_off_topic", request_id="synthetic_off_topic", latency_ms=0))

    monkeypatch.setattr(MockProvider, "invoke", invoke)
    return calls


@pytest.mark.parametrize("technical,relevant", [(False, False), (True, False), (True, True)])
def test_clear_irrelevance_or_mixed_answer_commits_original_audio_once_and_only_advances_question(
        tmp_path, monkeypatch, technical, relevant):
    from app.core.prompt.contracts import SUPPLEMENT_SPEECH
    from test_automatic_turn_integration import _feed, _CONTINUATION
    from test_declined_answer_integration import _stop_current_playback

    calls = _provider(monkeypatch, technical=technical, relevant=relevant, inconsistent_label=relevant)
    body = (("我使用持久检查点恢复任务。" if relevant else "我曾使用图形引擎绘制三维场景。") if technical else "") + TEXT
    assigned = _audio_aligned_speech(monkeypatch, [body, SUPPLEMENT_REPLY])

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            spoken = _playable_speech(runtime)
            endpoint, clock = await _answer_without_confirmation(managed)
            await _wait_until(lambda: endpoint.confirmation.speaking)
            assert not _current(runtime)["answers"] and spoken == [SUPPLEMENT_SPEECH["check"]]
            await _stop_current_playback(runtime, channel)
            await _wait_until(lambda: endpoint.confirmation._supplement_question_delivered)
            assert not _current(runtime)["answers"]
            await _feed(managed._ingress, _CONTINUATION, 5)
            clock.value += 1
            await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            await _wait_until(lambda: _current(runtime)["current_turn_id"] == _NEXT_TURN_ID)
            _assert_one_automatic_answer(store, runtime, body + SUPPLEMENT_REPLY, voice_frames=5, continuation_frames=5)
            session = _current(runtime)
            result = session["turns"][0]["current_understanding"]
            assert session["answers"][0]["raw_transcript"] == body + SUPPLEMENT_REPLY
            assert result["intent"] == ("answer" if relevant else "off_topic")
            assert bool(result["claims"]) is technical
            assert result["ambiguities"] == [] and result["clarification_target"] is None
            assert result["followup_allowed"] is relevant
            assert result["completion_basis"]["evidence_quotes"] == [SUPPLEMENT_REPLY]
            assert not session.get("candidate_input_completed_at") and not session.get("candidate_exit")
            assert not any(t.get("is_followup") for t in session["turns"])
            assert len([call for call in calls if call[0] == "understanding"]) == 2
            assert [call for call in calls if call[0] == "supplement"] == [("supplement", SUPPLEMENT_REPLY)]
            assert spoken.count(SUPPLEMENT_SPEECH["check"]) == 1 and assigned == [body, SUPPLEMENT_REPLY]
            assert not any(event["payload"]["act_type"] in {"answer_clarify", "reception_off_topic"}
                for event in session["agent_events"] if event["type"] == "conversation.act.selected")

    asyncio.run(scenario())


def test_off_topic_withdrawal_requires_a_delivered_separate_question_and_fresh_audio_confirmation(tmp_path, monkeypatch):
    calls = _provider(monkeypatch, stop=True)
    body = BODY + "我不想继续面试了。"

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [body, "", "", "确定结束面试。", ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            _playable_speech(runtime)
            # Shared helper checks no answer/exit before the question is fully
            # delivered, then feeds a distinct, authoritative audio reply.
            await _answer_and_confirm_exit(runtime, channel, managed)
            await _wait_until(lambda: bool(_current(runtime).get("candidate_input_completed_at")), timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            session = _current(runtime)
            _assert_retained_exit(store, runtime, body + "确定结束面试。", voice_frames=5, continuation_frames=5)
            assert session["current_turn_id"] is None
            assert session["candidate_input_completion_reason"] == "candidate_requested"
            assert [call for call in calls if call[0] == "exit"] == [("exit", "确定结束面试。")]
            assert not any(event["turn_id"] == _NEXT_TURN_ID for event in session["agent_events"]
                if event["type"] == "conversation.act.selected")

    asyncio.run(scenario())


@pytest.mark.anyio
async def test_delivered_old_mistaken_clarification_does_not_repeat_after_fresh_clear_completion(tmp_path, monkeypatch):
    from app.core.prompt.contracts import clarification_speech, SUPPLEMENT_SPEECH
    from test_automatic_turn_integration import _feed, _CONTINUATION
    from test_declined_answer_integration import _stop_current_playback
    _provider(monkeypatch)
    original_invoke = MockProvider.invoke
    assigned = _audio_aligned_speech(monkeypatch, [BODY, FINISH, SUPPLEMENT_REPLY])

    async def invoke(provider, capability, request, context):
        response = await original_invoke(provider, capability, request, context)
        if (capability == "llm.chat_json" and request.purpose == "interview_turn_understanding"
                and request.metadata.get("transcript") == BODY):
            # Reproduce an already-delivered bad old decision. The next real
            # speech must be allowed to correct that understanding rather
            # than promoting the past question into permanent uncertainty.
            data = response.data.get("understanding", response.data)
            data.update(intent="clarification_request", turn_intent="clarify", suggested_action="clarify",
                confidence=.6, completion_basis=None, ambiguities=["桌游的含义需要核实。"],
                clarification_target={"evidence_id": "E1", "focus_quote": "桌游"})
        return response

    monkeypatch.setattr(MockProvider, "invoke", invoke)
    async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
        _add_next_question(store)
        _playable_speech(runtime)
        endpoint, clock = await _answer_without_confirmation(managed)
        await _wait_until(lambda: endpoint.confirmation.speaking and bool(endpoint.confirmation._content_question_act_id))
        assert not _current(runtime)["answers"]
        await _wait_until(lambda: managed.chain._stt.stream._stream is not None)
        await _stop_current_playback(runtime, channel)
        await _wait_until(lambda: endpoint.confirmation._content_question_delivered)
        await _feed(managed._ingress, _CONTINUATION, 5)
        clock.value += 5
        await _wait_until(lambda: endpoint.confirmation.speaking and endpoint.confirmation._supplement_delivery_pending)
        assert not _current(runtime)["answers"]
        await _stop_current_playback(runtime, channel)
        await _wait_until(lambda: endpoint.confirmation._supplement_question_delivered)
        await _feed(managed._ingress, _CONTINUATION, 5)
        clock.value += 1
        await _wait_until(lambda: len(_current(runtime)["answers"]) == 1)
        await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
        await _wait_until(lambda: _current(runtime)["current_turn_id"] == _NEXT_TURN_ID)
        _assert_one_automatic_answer(store, runtime, TEXT + SUPPLEMENT_REPLY, voice_frames=5, continuation_frames=10)
        result = _current(runtime)["turns"][0]["current_understanding"]
        assert result["intent"] == "off_topic" and result["ambiguities"] == [] and result["clarification_target"] is None
        assert result["completion_basis"]["evidence_quotes"] == [SUPPLEMENT_REPLY]
        acts = [event["payload"] for event in _current(runtime)["agent_events"] if event["type"] == "conversation.act.selected"]
        assert len([act for act in acts if act.get("text") == clarification_speech("桌游")]) == 1
        assert len([act for act in acts if act.get("text") == SUPPLEMENT_SPEECH["check"]]) == 1
        assert assigned == [BODY, FINISH, SUPPLEMENT_REPLY]


class _OffTopicScore:
    def __init__(self): self.requests = []

    async def invoke(self, capability, request):
        self.requests.append(request)
        return ChatJSONResponse(data={
            "score": 5, "confidence": .95, "dimension_scores": {"semantic_correctness": 5},
            "covered_key_points": [],
            "missing_key_points": [{"key_point_id": "kp_idempotency", "reason": "未讨论幂等处理。"},
                                   {"key_point_id": "kp_outbox", "reason": "未讨论事务投递。"}],
            "incorrect_claims": [], "evidence": [BODY], "review_flags": [],
            "summary": "回答内容清楚，但未涉及题目要求的可靠任务处理方案。", "suggested_followup": None,
        }, usage=Usage(), provider=ProviderMeta(provider_id="synthetic", model="synthetic_relevance_score",
            request_id="synthetic_relevance_score", latency_ms=0))


@pytest.mark.anyio
async def test_clear_irrelevance_is_scored_from_full_original_text_not_decline_or_unresolved_policy():
    session = session_for(["off_topic"])
    turn, answer = session["turns"][0], session["answers"][0]
    turn["utterances"][0]["text"] = answer["final_transcript"] = TEXT
    understanding = turn["current_understanding"]
    understanding.update(answer_summary="讲述了与任务处理无关的桌游经历。", claims=[],
        evidence_quotes=[BODY, FINISH], covered_capability_points=[], missing_capability_points=["幂等键", "Outbox"],
        answer_content="none", turn_intent="finish_answer", followup_allowed=False,
        completion_basis={"source": "explicit_server_intent", "evidence_quotes": [FINISH]})
    store, gateway = InMemoryStore(), _OffTopicScore()
    persistence = persist(store, session)

    result = await EvaluationService(store, gateway=gateway, persistence=persistence).evaluate_answer(answer, question(0))

    assert len(gateway.requests) == 1 and gateway.requests[0].metadata["answer_text"] == TEXT
    assert result["score"] == 5 and result["model_info"]["model"] == "synthetic_relevance_score"
    assert "scoring_rule_version" not in result["model_info"]
    assert "answer_content_unresolved" not in result["review_flags"] and "transcription_ambiguity" not in result["review_flags"]
    assert not result.get("recognition_warning") and result["covered_key_points"] == []
    assert answer["final_transcript"] == TEXT and turn["utterances"][0]["text"] == TEXT
