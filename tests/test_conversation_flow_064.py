"""Concept dialogue remains separate from final answer and interview intent."""
import asyncio
import json

import pytest

from app.core.prompt.understanding_references import understanding_references
from app.core.prompt.contracts import SUPPLEMENT_SPEECH
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.providers.mock.provider import MockProvider
from test_answer_endpoint import _Clock
from test_automatic_turn_integration import _automatic_session, _current, _feed, _VOICE, _CONTINUATION
from test_declined_answer_integration import _add_next_question, _NEXT_TURN_ID, _playable_speech, _confirm_pending_exit
from test_evidence_owner_recovery_integration import TURN_ID, _wait_until
from test_spoken_supplement_integration import _audio_aligned_transcripts, _finish_playback
from test_exit_evidence_086 import _assert_retained_exit


_QUESTION = "持久检查点是什么意思？"
_EXPLANATION = "持久检查点是已保存的任务进度记录。"
_WAIT = "让我想一下。"
_WAIT_REPLY = "好的，你慢慢想。"
_TECHNICAL = "我使用持久检查点和幂等提交恢复任务。"
_SUPPLEMENT_REPLY = "没有补充了。"


def _dialogue_provider(monkeypatch, *, ending, misread_fast_finish=False):
    original = MockProvider.invoke
    calls, corrections = [], []

    async def invoke(provider, capability, request, context):
        version = str(request.metadata.get("prompt_version") or "")
        if capability != "llm.chat_json" or request.purpose != "interview_turn_understanding":
            return await original(provider, capability, request, context)
        if version == "candidate_exit_reply.v1":
            reply = json.loads(request.messages[-1].content)["new_reply"]
            data = {"intent": "confirm", "confidence": .98, "evidence_quote": reply}
        elif version.startswith("supplement_reply."):
            value = json.loads(request.messages[-1].content)
            assert value["reply"] == _SUPPLEMENT_REPLY
            evidence_id = next(key for key, text in value["evidence"].items() if text == _SUPPLEMENT_REPLY)
            data = {"intent": "finish", "answer_effect": "unchanged", "confidence": .98, "evidence_id": evidence_id,
                    "reply_text": ""}
            calls.append({"stage": "supplement", "current_speech": value["reply"]})
        elif version.startswith("conversation_reception."):
            value = json.loads(request.messages[-1].content)
            current = value["new_speech"]
            kind = ("question_clarification" if current == _QUESTION else "wait" if current == _WAIT
                    or (misread_fast_finish and ending in current) else "other")
            data = {"kind": kind, "confidence": .98, "evidence_id": next(iter(value["evidence"])),
                    "reply_text": _EXPLANATION if kind == "question_clarification" else _WAIT_REPLY if kind == "wait" else ""}
            calls.append({"stage": "reception", "current_speech": current, "kind": kind})
        elif version.startswith(("interview_turn_understanding.", "interview_turn_decision.")):
            transcript = request.metadata["transcript"]
            references = understanding_references(transcript, request.metadata["capability_points"])
            evidence, points = references["evidence"], list(references["capabilities"])
            input_context = next((json.loads(message.content.removeprefix("面试官上下文：")).get("input_context")
                                  for message in request.messages if message.content.startswith("面试官上下文：")), None)
            calls.append({"stage": "understanding", "transcript": transcript, "input_context": input_context})
            finishing = ending in transcript
            thinking = not finishing and _WAIT in transcript
            technical = _TECHNICAL in transcript
            evidence_id = next((key for key, text in evidence.items() if text == _TECHNICAL), None)
            ending_id = (list(evidence)[-1] if transcript.endswith(("确定结束面试。", _SUPPLEMENT_REPLY)) else
                         next((key for key, text in evidence.items() if ending in text), None))
            data = {"intent": "answer" if technical else "answer_declined" if finishing else "not_finished",
                "answer_content": "technical" if technical else "none",
                "turn_intent": ("stop_interview" if ending == "我不想继续面试了。" else
                                "finish_answer" if technical else "decline_topic") if finishing else "thinking",
                "answer_summary": "候选人说明任务恢复方法。" if technical else "候选人明确表达当前交流请求。",
                "claims": [{"claim": _TECHNICAL, "evidence_id": evidence_id}] if technical else [],
                "evidence_ids": [evidence_id] if technical else [ending_id] if finishing else list(evidence)[-1:],
                "covered_point_ids": points if technical else [], "missing_point_ids": [] if technical else points,
                "ambiguities": [], "contradictions": [], "clarification_target": None,
                "confidence": .96, "suggested_action": "next" if finishing else "continue_listening",
                "followup_allowed": technical and finishing,
                "completion_basis": {"source": "explicit_server_intent", "evidence_ids": [ending_id]} if finishing else None,
            }
            # Simulate exactly the prior bug once: the full model overlooks a
            # selected explanation and tries to clarify its old request again.
            wrong_old_focus = thinking and transcript not in corrections
            if wrong_old_focus:
                corrections.append(transcript)
            if not thinking and not finishing or wrong_old_focus:
                first = next(iter(evidence))
                data.update(intent="clarification_request", turn_intent="clarify", confidence=.6,
                            suggested_action="clarify", ambiguities=["需要确认概念疑问。"],
                            clarification_target={"evidence_id": first, "focus_quote": _QUESTION})
            schema = request.json_schema
            understanding_schema = schema["properties"].get("understanding", schema)
            if "company_question" in understanding_schema["properties"]:
                data["company_question"] = None
            if "understanding" in schema["properties"]:
                data = {"understanding": data, "followup": {"selected": False, "question_text": "",
                    "evidence_id": "", "target_point_ids": [], "rationale": "", "difficulty": "mid",
                    "sensitive_attribute_inference": False, "leaks_answer": False}}
        else:
            return await original(provider, capability, request, context)
        return ChatJSONResponse(data=data, usage=Usage(), provider=ProviderMeta(
            provider_id="mock", model="synthetic_dialogue_064", request_id="synthetic_dialogue_064", latency_ms=0))
    monkeypatch.setattr(MockProvider, "invoke", invoke)
    return calls, corrections


@pytest.mark.parametrize(("body", "ending", "expected_intent"), [
    ("这一题我不会。", "这一题我不会。", "decline_topic"),
    (_TECHNICAL + "我已经说完了。", "我已经说完了。", "finish_answer"),
    (_TECHNICAL + "我这边的内容就到这里。", "我这边的内容就到这里。", "finish_answer"),
    ("我不想继续面试了。", "我不想继续面试了。", "stop_interview"),
])
@pytest.mark.parametrize("misread_fast_finish", [False, True])
def test_real_endpoint_explanation_wait_then_current_finish_completes_once(
    tmp_path, monkeypatch, body, ending, expected_intent, misread_fast_finish,
):
    calls, corrections = _dialogue_provider(monkeypatch, ending=ending, misread_fast_finish=misread_fast_finish)

    async def scenario():
        transcripts = ([_QUESTION, _WAIT, body, "", "", "确定结束面试。", ""] if expected_intent == "stop_interview"
                       else [_QUESTION, _WAIT, body, ""])
        if expected_intent == "finish_answer":
            # Assign words only when actual ingress audio arrives. Empty STT
            # cuts at the playback boundary must not consume the fresh reply.
            _audio_aligned_transcripts(monkeypatch, [_QUESTION, _WAIT, body, _SUPPLEMENT_REPLY])
            transcripts = [""]
        async with _automatic_session(tmp_path, monkeypatch, transcripts,
                stable_previews=True, spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            spoken = _playable_speech(runtime)
            endpoint, clock = managed._answer_endpoint, _Clock()
            endpoint.clock = clock

            await _feed(managed._ingress, _VOICE, 5)
            clock.value = 1.1
            await _wait_until(lambda: len(_current(runtime)["turns"][0].get("reception_exchanges", [])) == 1, timeout=5)
            assert spoken == [_EXPLANATION] and not _current(runtime)["answers"]
            await _finish_playback(channel, runtime, managed)

            await _feed(managed._ingress, _CONTINUATION, 5)
            clock.value += 1.1
            await _wait_until(lambda: len(_current(runtime)["turns"][0].get("reception_exchanges", [])) == 2, timeout=5)
            assert spoken == [_EXPLANATION, _WAIT_REPLY]
            await _finish_playback(channel, runtime, managed)
            clock.value += 1.1
            await _wait_until(lambda: endpoint.confirmation._reception_review is None and endpoint._inference_task is None, timeout=5)
            assert corrections and not _current(runtime)["answers"]
            assert spoken == [_EXPLANATION, _WAIT_REPLY], "An answered concept must not become a new unclear answer"

            await _feed(managed._ingress, _CONTINUATION, 5)
            clock.value += 5.1
            acknowledged_finish = misread_fast_finish and ending == "我这边的内容就到这里。"
            if acknowledged_finish:
                await _wait_until(lambda: len(spoken) == 3, timeout=5)
                await _finish_playback(channel, runtime, managed)
                clock.value += 1.1
            if expected_intent == "stop_interview":
                await _confirm_pending_exit(runtime, channel, managed, clock)
            elif expected_intent == "finish_answer":
                await _wait_until(lambda: SUPPLEMENT_SPEECH["check"] in spoken, timeout=5)
                assert not _current(runtime)["answers"]
                assert not endpoint.confirmation.confirmed
                await _finish_playback(channel, runtime, managed)
                assert endpoint.confirmation.phase == "awaiting_reply"
                assert endpoint.confirmation._supplement_question_delivered
                assert not _current(runtime)["answers"]
                await _feed(managed._ingress, _CONTINUATION, 5)
                clock.value += 1.1
            if expected_intent == "stop_interview":
                await _wait_until(lambda: bool(_current(runtime).get("candidate_input_completed_at")), timeout=5)
                await _wait_until(lambda: all(command["status"] == "completed" for command in store.evidence_commands.values()))
            else:
                await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            current = _current(runtime)
            root = next(turn for turn in current["turns"] if turn["id"] == TURN_ID)
            expected_text = _QUESTION + _WAIT + body + (
                "确定结束面试。" if expected_intent == "stop_interview" else
                _SUPPLEMENT_REPLY if expected_intent == "finish_answer" else "")
            if expected_intent == "stop_interview":
                _assert_retained_exit(store, runtime, expected_text, voice_frames=5, continuation_frames=15)
            else:
                assert root["current_understanding"]["turn_intent"] == expected_intent
                assert current["answers"][0]["final_transcript"] == expected_text
                assert current["answers"][0]["media_evidence"]["complete"] is True
            assert not [act for act in root["conversation_acts"] if act.get("act_type") == "clarification"]
            assert spoken.count(_WAIT_REPLY) == (2 if acknowledged_finish else 1), "The old waiting request must not shadow the newest finish"
            seals = [command for command in store.evidence_commands.values() if command["command_type"] == "evidence.seal"]
            assert len(seals) == 1
            if expected_intent == "stop_interview":
                assert current["candidate_input_completion_reason"] == "candidate_requested"
                assert current["current_turn_id"] is None
                assert not [event for event in current["agent_events"]
                            if event["type"] == "conversation.act.selected" and event.get("turn_id") == _NEXT_TURN_ID]
            else:
                assert current["current_turn_id"] == _NEXT_TURN_ID
            if expected_intent == "finish_answer":
                assert spoken.count(SUPPLEMENT_SPEECH["check"]) == 1
                assert [call["current_speech"] for call in calls if call["stage"] == "supplement"] == [_SUPPLEMENT_REPLY]
                assert root["current_understanding"]["claims"][0]["claim"] == _TECHNICAL
                assert root["current_understanding"]["completion_basis"]["evidence_quotes"] == [_SUPPLEMENT_REPLY]
            final_calls = [call for call in calls if call["stage"] == "understanding" and ending in call["transcript"]]
            assert final_calls and all(call["input_context"] for call in final_calls)
            for call in final_calls:
                input_context = call["input_context"]
                if expected_intent == "finish_answer" and call["transcript"].endswith(_SUPPLEMENT_REPLY):
                    # Full understanding retains the unanswered body; a prior
                    # reception acknowledgement instead advances that boundary.
                    assert input_context["current_speech"] == (
                        _SUPPLEMENT_REPLY if acknowledged_finish else body + _SUPPLEMENT_REPLY)
                else:
                    assert input_context["current_speech"] == body or (
                        expected_intent == "stop_interview" and input_context["current_speech"] == "确定结束面试。") or (
                        acknowledged_finish and input_context["current_speech"] == ""
                        and input_context["responded_exchanges"][-1]["request_text"] == body)

    asyncio.run(scenario())
