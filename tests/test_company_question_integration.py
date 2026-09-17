"""Inserted company Q&A through real STT, durable audio, owner and TTS seams."""
import asyncio
from copy import deepcopy
import json

import pytest

from app.core.prompt.understanding_references import understanding_references
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.persistence.provider import persistence_for
from app.providers.mock.provider import MockProvider
from test_automatic_turn_integration import _automatic_session, _current, _feed, _VOICE, _CONTINUATION
from test_spoken_supplement_integration import _synthetic_tts, _finish_playback
from test_evidence_owner_recovery_integration import INTERVIEW_ID, ORGANIZATION_ID, TURN_ID, _wait_until
from test_answer_endpoint import _Clock
from test_company_questions import QUESTION, FACT, TECH, company_understanding


def provider(monkeypatch, *, blocked_reply=None):
    original = MockProvider.invoke
    calls = []
    async def invoke(instance, capability, request, context):
        version = request.metadata.get("prompt_version")
        if version == "conversation_reception.v2":
            return await original(instance, capability, request, context)
        if version == "company_question_reply.v1":
            calls.append(("company", json.loads(request.messages[-1].content)))
            if blocked_reply is not None:
                await blocked_reply.wait()
            data = {"status": "supported", "citations": [{"reference_id": "company:context", "quote": FACT}]}
        elif version == "supplement_reply.v4":
            value = json.loads(request.messages[-1].content)
            calls.append(("supplement", value))
            data = {"intent": "company_question" if QUESTION in value["reply"] else "finish", "confidence": .99, "evidence_id": "E1"}
        elif capability == "llm.chat_json" and request.purpose == "interview_turn_understanding":
            text, points = request.metadata["transcript"], request.metadata["capability_points"]
            calls.append(("understanding", text))
            if QUESTION in text:
                data = company_understanding(text, points, mixed=TECH in text, completed="下一题" in text)
            else:
                refs = understanding_references(text, points)
                eid = next(iter(refs["evidence"]))
                data = {"intent": "answer", "answer_content": "technical", "turn_intent": "answering",
                    "completion_basis": None, "followup_allowed": False, "clarification_target": None,
                    "answer_summary": TECH, "claims": [{"claim": TECH, "evidence_id": eid}],
                    "evidence_ids": [eid], "covered_point_ids": [], "missing_point_ids": list(refs["capabilities"]),
                    "ambiguities": [], "contradictions": [], "confidence": .96, "suggested_action": "accept"}
            if "understanding" in request.json_schema["properties"]:
                data = {"understanding": data, "followup": {"selected": False, "question_text": "",
                    "evidence_id": "", "target_point_ids": [], "rationale": "", "difficulty": "mid",
                    "sensitive_attribute_inference": False, "leaks_answer": False}}
        else:
            if request.purpose == "answer_evaluation":
                calls.append(("evaluation", request.metadata.get("answer_text")))
            return await original(instance, capability, request, context)
        return ChatJSONResponse(data=data, usage=Usage(), provider=ProviderMeta(
            provider_id="mock", model="synthetic_company", request_id="synthetic_company", latency_ms=0))
    monkeypatch.setattr(MockProvider, "invoke", invoke)
    return calls


def configure(store, *, has_company=True):
    with persistence_for(store).transaction(ORGANIZATION_ID) as tx:
        session = tx.interview_sessions.get(INTERVIEW_ID)
        session.setdefault("plan_snapshot", {})["company_context"] = FACT if has_company else None
        session["settings"]["avatar_mode"] = "local"
        session["turns"][0]["question_snapshot"]["source_question_version"] = 1
        session["plan_snapshot"]["question_snapshots"] = [{
            "question_snapshot_id": session["turns"][0]["question_snapshot_id"], "weight": 1, "dimension": "general"}]
        tx.interview_sessions.update(session, expected_version=session["version"])


def audio_aligned_transcripts(monkeypatch, transcripts):
    """Assign synthetic speech when audio arrives, not when an empty stream opens.

    Continuous listening may cut/reopen an empty stream to revalidate a final.
    Such a cut must not consume the next synthetic candidate utterance.
    """
    from app.providers.mock.provider import MockSTTStream
    pending = iter(transcripts)
    original_send = MockSTTStream.send_audio

    async def send(stream, chunk):
        if any(chunk) and not getattr(stream, "_synthetic_speech_assigned", False):
            stream._synthetic_speech_assigned = True
            stream.request = stream.request.model_copy(update={"metadata": {
                **stream.request.metadata, "development_transcript": next(pending, ""),
            }})
        return await original_send(stream, chunk)

    monkeypatch.setattr(MockSTTStream, "send_audio", send)


def acts(runtime):
    return [e for e in _current(runtime)["agent_events"] if e["type"] == "conversation.act.selected"]


@pytest.mark.parametrize("has_company,mixed,supplement", [(True, False, False), (False, False, False),
                                                         (True, True, False), (True, True, True), (False, True, True)])
def test_real_company_reply_keeps_capture_question_and_scoring_unchanged(tmp_path, monkeypatch, has_company, mixed, supplement):
    calls = provider(monkeypatch)
    async def scenario():
        initial = (TECH if mixed and not supplement else "") + QUESTION
        transcripts = [TECH, QUESTION, "下一题吧。", ""] if supplement else [initial, "下一题吧。", ""]
        audio_aligned_transcripts(monkeypatch, transcripts)
        async with _automatic_session(tmp_path, monkeypatch, [], spoken_confirmation=True) as (store, runtime, channel, managed):
            configure(store, has_company=has_company)
            spoken = _synthetic_tts(runtime)
            endpoint = managed._answer_endpoint
            clock = _Clock()
            endpoint.clock = clock
            capture = managed._capture_id
            before = _current(runtime)
            await _feed(managed._ingress, _VOICE, 5)
            clock.value = 5
            if supplement:
                await _wait_until(lambda: any(e["payload"]["act_type"] == "supplement_check" for e in acts(runtime)))
                await _finish_playback(channel, runtime, managed)
                assert endpoint.confirmation.phase == "awaiting_reply"
                await _feed(managed._ingress, _CONTINUATION, 5)
                clock.value += 1
            await _wait_until(lambda: any(e["payload"]["act_type"] == "company_answer" for e in acts(runtime)), timeout=5)
            await _wait_until(lambda: bool(spoken) and any("公司资料" in text or "无法确认" in text for text in spoken))
            current = _current(runtime)
            assert current["current_turn_id"] == TURN_ID and current["turn_ids"] == before["turn_ids"]
            assert current.get("decision_revision") == before.get("decision_revision")
            assert not current["answers"] and not [x for x in calls if x[0] == "evaluation"]
            assert not [item for item in store.evidence_commands.values() if item["command_type"] == "evidence.seal"]
            assert managed._capture_id == capture and managed.chain.is_open
            exchanges = current["turns"][0]["company_question_exchanges"]
            assert len(exchanges) == 1 and exchanges[0]["question_span"]["quote"] == QUESTION
            assert exchanges[0]["media_checkpoint"]["recoverability"] == "sealed_prefix"
            assert len([x for x in calls if x[0] == "company"]) == int(has_company)
            assert (FACT in exchanges[0]["reply"]["text"]) is has_company
            await _finish_playback(channel, runtime, managed)
            assert endpoint.confirmation.phase == "listening"
            # More silence and the same final cannot trigger another reply.
            clock.value += 30
            await asyncio.sleep(.12)
            assert len([e for e in acts(runtime) if e["payload"]["act_type"] == "company_answer"]) == 1
            assert managed._capture_id == capture and not _current(runtime)["answers"]
            await _feed(managed._ingress, _CONTINUATION, 5)
            clock.value += 5
            await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            answer = _current(runtime)["answers"][0]
            assert QUESTION in answer["raw_transcript"] and QUESTION in answer["final_transcript"]
            assert QUESTION not in answer["scoring_transcript"]
            assert answer["media_evidence"]["complete"] is True
            if mixed:
                assert TECH in answer["scoring_transcript"]
                with persistence_for(store).transaction(ORGANIZATION_ID) as tx:
                    pending = [item for item in tx.outbox.list() if item["kind"] == "answer.evaluate"]
                assert pending
                await runtime.interviews.process_outbox_work(pending[0]["id"], ORGANIZATION_ID)
                assert any(x[0] == "evaluation" and TECH in x[1] and QUESTION not in x[1] for x in calls)
                assert [item for item in store.model_invocations if item["purpose"] == "answer_evaluation"][-1]["prompt_version"] == "answer_evaluation.v7"
    asyncio.run(scenario())


@pytest.mark.parametrize("interruption", ["new_audio", "company_changed", "owner_changed", "pause", "close"])
def test_late_company_reply_cannot_outlive_current_audio_context_or_owner(tmp_path, monkeypatch, interruption):
    async def scenario():
        release = asyncio.Event()
        calls = provider(monkeypatch, blocked_reply=release)
        try:
            async with _automatic_session(tmp_path, monkeypatch, [QUESTION, "我还要继续。", ""], spoken_confirmation=True) as (store, runtime, channel, managed):
                configure(store)
                _synthetic_tts(runtime)
                endpoint = managed._answer_endpoint
                clock = _Clock()
                endpoint.clock = clock
                await _feed(managed._ingress, _VOICE, 5)
                clock.value = 5
                await _wait_until(lambda: any(x[0] == "company" for x in calls))
                assert endpoint.confirmation.phase == "preparing_company"
                if interruption == "new_audio":
                    await _feed(managed._ingress, _CONTINUATION, 2)
                elif interruption == "close":
                    await endpoint.close()
                elif interruption == "pause":
                    runtime.interviews.pause_interview(INTERVIEW_ID, reason="synthetic_pause", organization_id=ORGANIZATION_ID)
                else:
                    with persistence_for(store).transaction(ORGANIZATION_ID) as tx:
                        if interruption == "owner_changed":
                            owner = tx.evidence_ownerships.list()[0]
                            owner["ownership_epoch"] += 1
                            owner["owner_instance_id"] = "synthetic_successor"
                            tx.evidence_ownerships.update(owner, expected_version=owner["version"])
                        else:
                            session = tx.interview_sessions.get(INTERVIEW_ID)
                            session["plan_snapshot"]["company_context"] = "新的冻结来源。"
                            tx.interview_sessions.update(session, expected_version=session["version"])
                release.set()
                await asyncio.sleep(.2)
                current = _current(runtime)
                assert not current["answers"]
                if interruption == "company_changed":
                    # A fresh semantic attempt may safely use the new context;
                    # the delayed old source must never become the reply.
                    from app.services.company_questions import source_hash
                    for exchange in current["turns"][0].get("company_question_exchanges", []):
                        assert exchange["reply"]["source_hash"] == source_hash("新的冻结来源。")
                        assert FACT not in exchange["reply"]["text"]
                else:
                    assert not current["turns"][0].get("company_question_exchanges")
                    assert not any(e["payload"]["act_type"] == "company_answer" for e in acts(runtime))
                assert current["current_turn_id"] == TURN_ID
        finally:
            release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["none", "exception"])
def test_prepared_company_reply_survives_expression_failure_and_retries_without_new_fact_model(tmp_path, monkeypatch, failure):
    calls = provider(monkeypatch)
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [QUESTION, "", ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            configure(store)
            _synthetic_tts(runtime)
            original = channel._select_act
            attempts = []
            async def fail_once(**kwargs):
                if kwargs["act_type"] == "company_answer":
                    attempts.append(kwargs)
                    if len(attempts) == 1:
                        if failure == "exception":
                            raise RuntimeError("synthetic expression failure")
                        return None
                return await original(**kwargs)
            monkeypatch.setattr(channel, "_select_act", fail_once)
            endpoint = managed._answer_endpoint
            clock = _Clock()
            endpoint.clock = clock
            await _feed(managed._ingress, _VOICE, 5)
            clock.value = 5
            await _wait_until(lambda: len(attempts) == 1)
            await _wait_until(lambda: endpoint.confirmation.phase == "listening")
            first = _current(runtime)["turns"][0]["company_question_exchanges"][0]
            assert first["delivery_status"] == "pending"
            assert not _current(runtime)["answers"]
            if failure == "exception":
                await _wait_until(lambda: endpoint._prepare_failures == 1 and endpoint._retry_at > clock.value)
            endpoint.continue_speaking()
            clock.value += 5
            try:
                await _wait_until(lambda: len(attempts) == 2, timeout=5)
            except AssertionError:
                raise AssertionError(str({"attempts": len(attempts), "phase": endpoint.confirmation.phase,
                    "failures": endpoint._prepare_failures, "retry_at": endpoint._retry_at,
                    "clock": clock.value, "revision": endpoint.revision, "blocked": endpoint._blocked_revision,
                    "calls": [x[0] for x in calls], "status": _current(runtime)["status"]})) from None
            await _wait_until(lambda: bool(_current(runtime)["agent_runtime"].get("active_performance_id")))
            assert len([x for x in calls if x[0] == "company"]) == 1
            assert len(_current(runtime)["turns"][0]["company_question_exchanges"]) == 1
            await _finish_playback(channel, runtime, managed)
            assert _current(runtime)["turns"][0]["company_question_exchanges"][0]["delivery_status"] == "delivered"
            assert not _current(runtime)["answers"] and managed.chain.is_open
    asyncio.run(scenario())
