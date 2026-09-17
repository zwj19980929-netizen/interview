"""An explicit spoken end can close a nontechnical answer through Evidence.

Only synthetic provider responses and room transport are controlled. The actual
gateway validation, owner, command journal, recording and answer transaction run.
"""

import asyncio
import io
import json
import wave
from array import array
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.core.prompt.contracts import SUPPLEMENT_SPEECH
from app.core.prompt.understanding_references import understanding_references
from app.adapters.private_media import read_managed_audio
from app.domain.interview_agent import ClientSignal
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.persistence.provider import persistence_for
from app.providers.mock.provider import MockProvider, MockSTTStream
from app.services.evidence_coordination import EvidenceOwnershipCoordinator

from test_answer_endpoint import _Clock
from test_automatic_turn_integration import (
    _automatic_session, _current, _feed, _assert_one_automatic_answer,
    _VOICE, _CONTINUATION, _SILENCE,
)
from test_evidence_owner_recovery_integration import (
    INTERVIEW_ID, ORGANIZATION_ID, TURN_ID, _wait_until,
)
from test_spoken_supplement_integration import (
    _synthetic_tts, _speaking, _finish_playback,
)


_NEXT_TURN_ID = "turn_declined_next"
_POINTS = ["持久检查点", "幂等提交"]
_TECHNICAL = "我使用持久检查点和幂等提交恢复任务。"


def _add_next_question(store):
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        session = transaction.interview_sessions.get(INTERVIEW_ID)
        first = session["turns"][0]
        first["allow_followup"] = True
        first["question_snapshot"]["key_points"] = list(_POINTS)
        second = deepcopy(first)
        second.update(id=_NEXT_TURN_ID, root_turn_id=_NEXT_TURN_ID, order=2,
                      status="pending", allow_followup=False,
                      question_spoken_text="请说明如何验证恢复结果。")
        session["turns"].append(second)
        session["turn_ids"].append(_NEXT_TURN_ID)
        session["settings"]["avatar_mode"] = "local"
        session["followup_policy"].update(max_total=2, max_per_root=1)
        session["scheduled_end_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        transaction.interview_sessions.update(session, expected_version=session["version"])


def _semantic_provider(monkeypatch, *, technical=False, probe=False, explicit_finish=False, stop_interview=False):
    """Return referenced intent evidence, never turn controls into claims."""
    original = MockProvider.invoke
    calls = []

    async def invoke(provider, capability, request, context):
        if capability != "llm.chat_json" or request.purpose != "interview_turn_understanding":
            return await original(provider, capability, request, context)
        if request.metadata.get("prompt_version") == "conversation_reception.v2":
            return await original(provider, capability, request, context)
        if request.metadata.get("prompt_version") == "supplement_reply.v4":
            reply = json.loads(request.messages[-1].content)["reply"]
            intent = "continue" if "再想想" in reply else "finish"
            data = {"intent": intent, "confidence": .98, "evidence_id": "E1"}
            calls.append(("supplement", reply, intent))
        else:
            transcript = request.metadata["transcript"]
            refs = understanding_references(transcript, request.metadata["capability_points"])
            evidence_ids = list(refs["evidence"])
            points = list(refs["capabilities"])
            claims = []
            technical_response = technical and (not probe or request.metadata["turn_id"] == TURN_ID)
            select_probe = technical_response and probe
            if technical_response:
                evidence_id = next(key for key, value in refs["evidence"].items() if value == _TECHNICAL)
                evidence_ids = [evidence_id]
                claims = [{"claim": _TECHNICAL, "evidence_id": evidence_id}]
            data = {"clarification_target": None,
                "intent": "answer" if technical_response else "answer_declined",
                "answer_summary": _TECHNICAL if technical_response else "候选人明确表示本题不再作答。",
                "claims": claims, "evidence_ids": evidence_ids,
                "covered_point_ids": (points[:-1] if select_probe else points) if technical_response else [],
                "missing_point_ids": points[-1:] if select_probe else ([] if technical_response else points),
                "ambiguities": [], "contradictions": [], "confidence": .96,
                "suggested_action": "next",
            }
            if request.metadata["prompt_version"] in {"interview_turn_understanding.v16", "interview_turn_decision.v15", "interview_turn_understanding.v17", "interview_turn_decision.v16", "interview_turn_understanding.v18", "interview_turn_understanding.v19", "interview_turn_decision.v17", "interview_turn_decision.v18"}:
                thinking = transcript.startswith("我得想一下")
                ending = (explicit_finish or stop_interview or not technical_response) and not thinking
                data.update(answer_content="partial" if technical_response else "none",
                            turn_intent="thinking" if thinking else "stop_interview" if stop_interview else ("finish_topic" if technical_response else "decline_topic") if ending else "answering",
                            followup_allowed=technical_response and not ending,
                            completion_basis=({"source": "explicit_server_intent", "evidence_ids": list(refs["evidence"])[-1:]} if ending else None))
                if thinking:
                    data.update(intent="not_finished", answer_summary="候选人正在思考。", suggested_action="continue_listening")
            calls.append(("understanding", transcript, request.metadata["prompt_version"]))
            if "understanding" in request.json_schema["properties"]:
                data = {"understanding": data, "followup": {
                    "selected": select_probe,
                    "question_text": "请说明恢复前如何校验任务所有权？" if select_probe else "",
                    "evidence_id": evidence_ids[0] if select_probe else "",
                    "target_point_ids": points[-1:] if select_probe else [],
                    "rationale": "核验尚未说明的所有权检查。" if select_probe else "", "difficulty": "mid",
                    "sensitive_attribute_inference": False, "leaks_answer": False,
                }}
        return ChatJSONResponse(data=data, usage=Usage(), provider=ProviderMeta(
            provider_id="mock", model="synthetic_intent", request_id="synthetic_intent", latency_ms=0,
        ))

    monkeypatch.setattr(MockProvider, "invoke", invoke)
    return calls


def _playable_speech(runtime):
    original_question_delivery = runtime.avatar.speak
    spoken = _synthetic_tts(runtime)
    # Exercise next-question delivery too; its local text-only fixture falls
    # back to the same synthetic, privately materialized server TTS.
    runtime.avatar.speak = original_question_delivery
    return spoken


async def _stop_current_playback(runtime, channel):
    await _wait_until(lambda: bool(_current(runtime)["agent_runtime"].get("active_performance_id")))
    current = _current(runtime)
    performance_id = current["agent_runtime"]["active_performance_id"]
    await channel.send(ClientSignal(type="avatar.performance.stopped", turn_id=current["current_turn_id"],
        idempotency_key="stopped_" + performance_id, payload={"performance_id": performance_id}))
    await _wait_until(lambda: not _current(runtime)["agent_runtime"].get("active_performance_id"))


async def _answer_and_confirm(runtime, channel, managed):
    endpoint = managed._answer_endpoint
    clock = _Clock()
    endpoint.clock = clock
    await _feed(managed._ingress, _VOICE, 5)
    clock.value = 5
    await _speaking(endpoint, runtime)
    answer_count = len(_current(runtime)["answers"])
    await _stop_current_playback(runtime, channel)
    await _wait_until(lambda: not endpoint.confirmation.speaking)
    assert len(_current(runtime)["answers"]) == answer_count
    await _feed(managed._ingress, _CONTINUATION, 5)
    clock.value += 1
    return endpoint, clock


async def _answer_without_confirmation(managed):
    endpoint = managed._answer_endpoint
    clock = _Clock()
    endpoint.clock = clock
    await _feed(managed._ingress, _VOICE, 5)
    clock.value = 5
    return endpoint, clock


@pytest.mark.parametrize("body", ["我不知道，这题先到这里吧。", "我现在没有补充了。"])
def test_first_final_decline_preserves_original_audio_and_advances_once(tmp_path, monkeypatch, body):
    calls = _semantic_provider(monkeypatch)

    async def scenario():
        reply = "没有补充了。"
        async with _automatic_session(tmp_path, monkeypatch, [body, reply, ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            spoken = _playable_speech(runtime)
            await _answer_without_confirmation(managed)
            await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            await _wait_until(lambda: any(e["type"] == "conversation.act.selected" and e["turn_id"] == _NEXT_TURN_ID
                                         for e in _current(runtime)["agent_events"]))
            _assert_one_automatic_answer(store, runtime, body, voice_frames=5, continuation_frames=0)
            current = _current(runtime)
            assert current["current_turn_id"] == _NEXT_TURN_ID
            assert current["answers"][0]["raw_transcript"] == body
            understanding = current["turns"][0]["current_understanding"]
            assert understanding["intent"] == "answer_declined"
            assert understanding["suggested_action"] == "next"
            assert understanding["claims"] == understanding["covered_capability_points"] == []
            assert understanding["missing_capability_points"] == _POINTS
            assert all(quote in body + reply for quote in understanding["evidence_quotes"])
            assert understanding["evidence_quotes"]
            assert not any(t.get("is_followup") for t in current["turns"])
            acts = [e["payload"]["act_type"] for e in current["agent_events"] if e["type"] == "conversation.act.selected"]
            assert "supplement_check" not in acts and "question" in acts
            assert SUPPLEMENT_SPEECH["check"] not in spoken
            assert not [c for c in calls if c[0] == "supplement"]
            assert [c[2] for c in calls if c[0] == "understanding"] == ["interview_turn_decision.v17"]
            assert understanding["completion_basis"]["source"] == "explicit_server_intent"

            # Replay the committed journal command and an additional stale
            # capture hint after advancing. Neither can answer the next turn.
            seal = next(c for c in store.evidence_commands.values() if c["command_type"] == "evidence.seal")
            duplicate = ClientSignal(type="evidence.finish", turn_id=TURN_ID,
                idempotency_key=seal["payload"]["proposal_id"], payload=seal["payload"])
            command_ids = set(store.evidence_commands)
            await channel.send(duplicate)
            await channel.send(duplicate)
            assert set(store.evidence_commands) == command_ids
            await channel.send(ClientSignal(type="evidence.finish", turn_id=TURN_ID,
                idempotency_key="stale_declined_capture", payload={
                    "endpoint": "explicit", "capture_id": seal["payload"]["capture_id"],
                }))
            assert len(_current(runtime)["answers"]) == 1
            assert _current(runtime)["current_turn_id"] == _NEXT_TURN_ID
            assert not _current(runtime)["turns"][1].get("utterances")
    asyncio.run(scenario())


def test_partial_technical_answer_plus_finish_preserves_evidence_and_declines_proposed_probe(tmp_path, monkeypatch):
    calls = _semantic_provider(monkeypatch, technical=True, probe=True, explicit_finish=True)

    async def scenario():
        body = _TECHNICAL + "其他细节不会了，下一题吧。"
        async with _automatic_session(tmp_path, monkeypatch, [body, ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            spoken = _playable_speech(runtime)
            await _answer_without_confirmation(managed)
            await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            _assert_one_automatic_answer(store, runtime, body, voice_frames=5, continuation_frames=0)
            session = _current(runtime)
            understanding = session["turns"][0]["current_understanding"]
            assert understanding["intent"] == "answer" and understanding["answer_content"] == "partial"
            assert understanding["turn_intent"] == "finish_topic" and not understanding["followup_allowed"]
            assert [claim["evidence_quote"] for claim in understanding["claims"]] == [_TECHNICAL]
            assert understanding["completion_basis"]["evidence_quotes"] == ["其他细节不会了，下一题吧。"]
            assert not any(turn.get("is_followup") for turn in session["turns"])
            assert session["current_turn_id"] == _NEXT_TURN_ID
            assert SUPPLEMENT_SPEECH["check"] not in spoken
            assert not [call for call in calls if call[0] == "supplement"]
            assert len(calls) == 1
    asyncio.run(scenario())


def test_explicit_end_of_interview_saves_current_evidence_and_never_delivers_next_question(tmp_path, monkeypatch):
    _semantic_provider(monkeypatch, technical=True, stop_interview=True)

    async def scenario():
        body = _TECHNICAL + "我不想继续面试了，请结束整场面试。"
        async with _automatic_session(tmp_path, monkeypatch, [body, ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            _playable_speech(runtime)
            await _answer_without_confirmation(managed)
            await _wait_until(lambda: bool(_current(runtime).get("candidate_input_completed_at")), timeout=5)
            session = _current(runtime)
            assert len(session["answers"]) == 1
            assert session["answers"][0]["raw_transcript"] == body
            assert session["turns"][0]["current_understanding"]["turn_intent"] == "stop_interview"
            assert session["current_turn_id"] is None
            assert not [event for event in session["agent_events"] if event["type"] == "conversation.act.selected"
                        and event["turn_id"] == _NEXT_TURN_ID]
            assert not any(turn.get("is_followup") for turn in session["turns"])
    asyncio.run(scenario())


def test_end_interview_in_supplement_reply_preserves_answer_and_stops_whole_interview(tmp_path, monkeypatch):
    _semantic_provider(monkeypatch, technical=True)
    stop = "我今天不想继续面试了，请结束整场面试。"
    original = MockProvider.invoke

    async def semantic_reply(provider, capability, request, context):
        response = await original(provider, capability, request, context)
        if (request.purpose == "interview_turn_understanding" and request.metadata.get("transcript", "").endswith(stop)
                and request.metadata.get("prompt_version") in {"interview_turn_understanding.v17", "interview_turn_decision.v16", "interview_turn_understanding.v19", "interview_turn_decision.v18"}):
            data = response.data.get("understanding", response.data)
            refs = understanding_references(request.metadata["transcript"], request.metadata["capability_points"])
            data.update(answer_content="partial", turn_intent="stop_interview", followup_allowed=False,
                completion_basis={"source": "explicit_server_intent", "evidence_ids": list(refs["evidence"])[-1:]},
                suggested_action="next")
        return response
    monkeypatch.setattr(MockProvider, "invoke", semantic_reply)

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_TECHNICAL, stop, ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            _playable_speech(runtime)
            await _answer_and_confirm(runtime, channel, managed)
            await _wait_until(lambda: bool(_current(runtime).get("candidate_input_completed_at")), timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            session = _current(runtime)
            _assert_one_automatic_answer(store, runtime, _TECHNICAL + stop, voice_frames=5, continuation_frames=5)
            assert session["turns"][0]["current_understanding"]["prompt_version"] == "interview_turn_decision.v18"
            assert session["turns"][0]["current_understanding"]["turn_intent"] == "stop_interview"
            assert session.get("candidate_input_completion_reason") == "candidate_requested"
            assert session["turns"][0]["current_understanding"]["claims"][0]["claim"] == _TECHNICAL
            assert not [event for event in session["agent_events"] if event["type"] == "conversation.act.selected"
                        and event["turn_id"] == _NEXT_TURN_ID]
    asyncio.run(scenario())


def test_technical_body_is_still_an_answer_after_spoken_no(tmp_path, monkeypatch):
    _semantic_provider(monkeypatch, technical=True)

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_TECHNICAL, "没有补充了。", ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            _playable_speech(runtime)
            await _answer_and_confirm(runtime, channel, managed)
            await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            _assert_one_automatic_answer(store, runtime, _TECHNICAL + "没有补充了。", voice_frames=5, continuation_frames=5)
            understanding = _current(runtime)["turns"][0]["current_understanding"]
            assert understanding["intent"] == "answer"
            assert [claim["claim"] for claim in understanding["claims"]] == [_TECHNICAL]
            assert understanding["evidence_quotes"] == [_TECHNICAL]
            assert _current(runtime)["current_turn_id"] == _NEXT_TURN_ID
    asyncio.run(scenario())


def test_thinking_without_finish_confirmation_never_advances(tmp_path, monkeypatch):
    calls = _semantic_provider(monkeypatch)

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, ["我得想一下。", ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            _playable_speech(runtime)
            endpoint, clock = await _answer_without_confirmation(managed)
            await _wait_until(lambda: len(calls) == 1)
            await _finish_playback(channel, runtime, managed)
            await _feed(managed._ingress, _SILENCE, 5)
            clock.value += 30
            await asyncio.sleep(.15)
            assert _current(runtime)["answers"] == []
            assert _current(runtime)["current_turn_id"] == TURN_ID
            assert not endpoint.confirmation.confirmed
            acts = [e["payload"] for e in _current(runtime)["agent_events"] if e["type"] == "conversation.act.selected"]
            assert len(acts) == 1 and acts[0]["act_type"] == "conversation_acknowledgement"
            assert len(calls) == 1, "Thinking does not loop inference or ask for supplements"
            assert not [c for c in store.evidence_commands.values() if c["command_type"] == "evidence.seal"]
    asyncio.run(scenario())


def test_declining_a_followup_advances_without_counting_it_as_an_extra_main_question(tmp_path, monkeypatch):
    _semantic_provider(monkeypatch, technical=True, probe=True)

    async def scenario():
        declined = "这部分我不了解，这题先到这里吧。"
        reply = "没有补充了。"
        transcripts = [_TECHNICAL, reply, declined, reply, ""]
        pending, assigned = iter(transcripts), []
        original_send = MockSTTStream.send_audio

        async def audio_aligned_send(stream, chunk):
            # A new recognition connection is not a new spoken utterance.
            # The final revalidation may open and close an empty connection;
            # assign this script only when its first voiced PCM really arrives.
            if any(chunk) and not getattr(stream, "_synthetic_speech_assigned", False):
                stream._synthetic_speech_assigned = True
                text = next(pending, "")
                assigned.append(text)
                stream.request = stream.request.model_copy(update={"metadata": {
                    **stream.request.metadata, "development_transcript": text,
                }})
            return await original_send(stream, chunk)

        monkeypatch.setattr(MockSTTStream, "send_audio", audio_aligned_send)
        async with _automatic_session(tmp_path, monkeypatch, transcripts, spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
                session = transaction.interview_sessions.get(INTERVIEW_ID)
                session["turns"][0]["question_snapshot"]["key_points"].append("所有权校验")
                transaction.interview_sessions.update(session, expected_version=session["version"])
            _playable_speech(runtime)
            await _answer_and_confirm(runtime, channel, managed)
            await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            _assert_one_automatic_answer(store, runtime, _TECHNICAL + reply, voice_frames=5, continuation_frames=5)
            assert assigned == [_TECHNICAL, reply], "An empty final cut must not consume the follow-up speech"
            current = _current(runtime)
            followup = next(t for t in current["turns"] if t.get("is_followup"))
            assert current["current_turn_id"] == followup["id"]
            snapshot = runtime._snapshot(current, channel.principal)
            assert (snapshot["completed_answers"], snapshot["total_primary_questions"]) == (1, 2)
            await _stop_current_playback(runtime, channel)
            await channel.send(ClientSignal(type="evidence.stream.open", turn_id=followup["id"],
                idempotency_key="open_followup_declined", payload={
                    "content_type": "audio/pcm", "sample_rate_hz": 16000,
                    "channels": 1, "language": "zh-CN",
                }))
            await _answer_without_confirmation(managed)
            await _wait_until(lambda: len(_current(runtime)["answers"]) == 2, timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            current = _current(runtime)
            assert current["current_turn_id"] == _NEXT_TURN_ID
            assert len([t for t in current["turns"] if t.get("is_followup")]) == 1
            assert not [e for e in current["agent_events"] if e["type"] == "conversation.act.selected"
                        and e["payload"]["act_type"] in {"clarification", "supplement_clarify"}]
            snapshot = runtime._snapshot(current, channel.principal)
            assert (snapshot["completed_answers"], snapshot["total_primary_questions"]) == (1, 2)
            answer = next(a for a in current["answers"] if a["turn_id"] == followup["id"])
            assert answer["raw_transcript"] == answer["final_transcript"] == declined
            assert assigned == [_TECHNICAL, reply, declined]
            assert answer["media_evidence"]["complete"] is True
            updated_followup = next(t for t in current["turns"] if t["id"] == followup["id"])
            assert updated_followup["current_understanding"]["intent"] == "answer_declined"
            recording = read_managed_audio(persistence_for(store), ORGANIZATION_ID, answer["audio_uri"])
            with wave.open(io.BytesIO(recording), "rb") as wav:
                samples = array("h", wav.readframes(wav.getnframes()))
            assert samples.count(8192) == 5 * 320 and samples.count(16384) == 0
    asyncio.run(scenario())


def test_stale_owner_cannot_commit_a_prepared_declined_answer(tmp_path, monkeypatch):
    _semantic_provider(monkeypatch)

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, ["我不知道，这题先到这里吧。", "没有补充了。", ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            _playable_speech(runtime)
            prepared, release = asyncio.Event(), asyncio.Event()
            original = managed.chain.prepare_decision

            async def delayed(final, **kwargs):
                decision = await original(final, **kwargs)
                assert decision.understanding.intent == "answer_declined"
                prepared.set()
                await release.wait()
                return decision

            managed.chain.prepare_decision = delayed
            try:
                endpoint, _clock = await _answer_without_confirmation(managed)
                await asyncio.wait_for(prepared.wait(), timeout=5)
                old_owner = managed.ownership
                coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
                assert coordinator.release(old_owner)
                successor = coordinator.claim_owner(interview_id=INTERVIEW_ID,
                    organization_id=ORGANIZATION_ID, local_instance_id="successor_declined_test")
                assert successor.ownership_epoch > old_owner.ownership_epoch
                release.set()
                await _wait_until(lambda: endpoint._closed, timeout=3)
                assert _current(runtime)["answers"] == []
                assert _current(runtime)["current_turn_id"] == TURN_ID
                assert not _current(runtime)["turns"][1].get("utterances")
            finally:
                release.set()
    asyncio.run(scenario())
