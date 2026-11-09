"""Only a delivered, current content question can authorize unresolved completion."""
import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.core.prompt.contracts import CLARIFICATION_SPEECH_VERSION, clarification_speech
from app.core.prompt.understanding_references import understanding_references
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.persistence.provider import persistence_for
from app.providers.mock.provider import MockProvider
from app.services.content_clarification import text_hash, validated_content_clarification
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint, _final
from test_prepared_decision_binding import _setup, _payload
from test_prepared_turn_decision import Gateway
from test_semantic_recovery_066 import FRAGMENT, REPLY, unresolved_response


def clarification_setup():
    capture = _Capture()
    capture.current_final = _final(FRAGMENT)
    endpoint, clock, notices, commits = _endpoint(capture=capture)
    spoken = []

    async def speak(kind, guard, **kwargs):
        guard()
        spoken.append((kind, kwargs))
        return {"act_id": "act_content"}

    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak,
        interview_id="iv_binding", turn_id="turn_binding", capture_id="capture_content")
    endpoint._proposal_revision = endpoint.revision
    understanding = SimpleNamespace(clarification_target=SimpleNamespace(
        evidence_quote=FRAGMENT.split("。")[0] + "。", focus_quote="蓝桥"))
    return endpoint, understanding, commits, spoken


@pytest.mark.anyio
async def test_question_selection_and_old_finish_do_not_replace_delivered_fresh_reply():
    endpoint, understanding, commits, spoken = clarification_setup()
    try:
        await endpoint.confirmation.clarify_answer(endpoint, understanding, endpoint.capture.current_final)
        assert endpoint.confirmation.speaking and spoken[0][0] == "answer_clarify"
        assert endpoint.confirmation.content_clarification_context(_final(FRAGMENT + REPLY)) is None
        # A response that arrives while the question is playing belongs to the
        # delivery boundary. It cannot become a later finishing reply.
        early = FRAGMENT + REPLY
        endpoint.capture.current_final = _final(early)
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        assert endpoint.confirmation._content_question_delivered
        assert endpoint.confirmation.content_clarification_context(_final(early)) is None
        context = endpoint.confirmation.content_clarification_context(_final(early + "不补充了。"))
        assert context["request_text"] == early and context["reply_text"] == "不补充了。"
        assert context["question_act_id"] == "act_content" and context["delivered"] is True
        assert not commits and not endpoint.confirmation.confirmed
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["barge_in", "confirmation_playback_timeout", "candidate_continues", "failed", "stale_output"])
async def test_interrupted_or_failed_content_question_provides_no_completion_authority(reason):
    endpoint, understanding, commits, spoken = clarification_setup()
    try:
        await endpoint.confirmation.clarify_answer(endpoint, understanding, endpoint.capture.current_final)
        await endpoint.confirmation.complete_playback(endpoint, reason=reason)
        assert not endpoint.confirmation._content_question_delivered
        assert endpoint.confirmation.content_clarification_context(_final(FRAGMENT + REPLY)) is None
        assert not commits and endpoint.capture.snapshots == []
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("boundary", ["missing", "partial", "changed_focus"])
async def test_delivery_needs_complete_final_with_the_original_focus(boundary):
    endpoint, understanding, commits, spoken = clarification_setup()
    try:
        await endpoint.confirmation.clarify_answer(endpoint, understanding, endpoint.capture.current_final)
        endpoint.capture.current_final = (None if boundary == "missing" else
            _final(FRAGMENT).model_copy(update={"type": "transcript.partial", "is_final": False}) if boundary == "partial" else
            _final("已经修正的另一段内容。"))
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        assert not endpoint.confirmation._content_question_delivered
        assert endpoint.confirmation.content_clarification_context(_final(FRAGMENT + REPLY)) is None
        assert not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["stt_failed", "capture_closed"])
async def test_failed_or_stale_delivery_boundary_does_not_create_content_receipt(failure):
    endpoint, understanding, commits, spoken = clarification_setup()
    try:
        await endpoint.confirmation.clarify_answer(endpoint, understanding, endpoint.capture.current_final)

        async def broken_snapshot(*, resume=True):
            if failure == "stt_failed":
                raise ApiError("STT_PROVIDER_UNAVAILABLE", "synthetic failure", status_code=503)
            endpoint.capture.is_open = False
            return _final(FRAGMENT)

        endpoint.capture.transcript_snapshot = broken_snapshot
        await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
        assert not endpoint.confirmation._content_question_delivered
        assert endpoint.confirmation.content_clarification_context(_final(FRAGMENT + REPLY)) is None
        assert not commits
    finally:
        await endpoint.close()


def exchange():
    act = {"act_id": "act_content", "turn_id": "turn_binding", "text": clarification_speech("蓝桥"),
           "prompt_version": CLARIFICATION_SPEECH_VERSION}
    turn = {"id": "turn_binding", "conversation_acts": [act], "content_clarification": {
        "status": "delivered", "capture_id": "capture_content", "question_act_id": "act_content",
        "request_sha256": text_hash(FRAGMENT), "focus_sha256": text_hash("蓝桥")}}
    session = {"id": "iv_binding", "current_turn_id": "turn_binding", "agent_events": [{
        "type": "conversation.act.selected", "turn_id": "turn_binding",
        "payload": {"act_id": "act_content", "text": act["text"], "approved": True}}]}
    context = {"version": "content_clarification.v1", "request_text": FRAGMENT, "focus_quote": "蓝桥",
               "reply_text": REPLY, "question_act_id": "act_content", "delivered": True}
    return session, turn, FRAGMENT + REPLY, context


@pytest.mark.parametrize("change", ["receipt_missing", "receipt_not_delivered", "capture_missing", "request_hash",
    "focus_hash", "act_id", "act_turn", "act_version", "act_text", "unselected", "unapproved", "old_prefix",
    "new_tail", "duplicate_act", "extra_field"])
def test_content_proof_is_bound_to_selected_current_question_and_exact_reply(change):
    session, turn, text, context = exchange()
    if change == "receipt_missing": turn.pop("content_clarification")
    elif change == "receipt_not_delivered": turn["content_clarification"]["status"] = "selected"
    elif change == "capture_missing": turn["content_clarification"]["capture_id"] = ""
    elif change == "request_hash": turn["content_clarification"]["request_sha256"] = "0" * 64
    elif change == "focus_hash": turn["content_clarification"]["focus_sha256"] = "0" * 64
    elif change == "act_id": context["question_act_id"] = "another_act"
    elif change == "act_turn": turn["conversation_acts"][0]["turn_id"] = "other_turn"
    elif change == "act_version": turn["conversation_acts"][0]["prompt_version"] = "supplement_confirmation.v2"
    elif change == "act_text": turn["conversation_acts"][0]["text"] = "是否还有补充？"
    elif change == "unselected": session["agent_events"].clear()
    elif change == "unapproved": session["agent_events"][0]["payload"]["approved"] = False
    elif change == "old_prefix": text = text.replace("任务", "旧问题")
    elif change == "new_tail": text += "等一下，我还没说完。"
    elif change == "duplicate_act": turn["conversation_acts"].append(deepcopy(turn["conversation_acts"][0]))
    else: context["completion_confirmed"] = True
    with pytest.raises(ApiError) as error:
        validated_content_clarification(session, turn, text, context)
    assert error.value.code == "CONTENT_CLARIFICATION_REQUIRED"


async def prepare_unresolved():
    service, _ = _setup()
    session, turn, text, context = exchange()
    with service.persistence.transaction("org_default") as tx:
        current = tx.interview_sessions.get("iv_binding")
        current["turns"][0].update(conversation_acts=turn["conversation_acts"],
                                   content_clarification=turn["content_clarification"])
        following = deepcopy(current["turns"][0])
        following.update(id="turn_content_next", root_turn_id="turn_content_next", order=2,
                         status="pending", conversation_acts=[])
        following.pop("content_clarification", None)
        current["turns"].append(following)
        current["turn_ids"].append(following["id"])
        current["agent_events"] = session["agent_events"]
        tx.interview_sessions.update(current, expected_version=current["version"])
    _, _, data = unresolved_response()
    data["missing_point_ids"] = ["P1"]
    data["clarification_target"] = {"resolution": "same_focus_unresolved", "source_ref": "C1"}
    service.conversation.gateway = Gateway(data)
    final = _final(text)
    prepared = await service.prepare_streaming_decision("iv_binding", "turn_binding", final,
        snapshot_ref="private-file://synthetic", semantic_first=True, content_clarification=context)
    assert prepared.understanding.problem is None
    return service, final, prepared


@pytest.mark.anyio
async def test_unresolved_prepared_answer_preserves_raw_words_and_cannot_end_the_interview():
    service, final, prepared = await prepare_unresolved()
    service.assert_prepared_streaming_decision(prepared, final, "org_default")
    result = await service.submit_streaming_answer("iv_binding", _payload(final), prepared_decision=prepared)
    assert result["accepted"] is True
    assert result["answer"]["final_transcript"] == result["answer"]["raw_transcript"] == FRAGMENT + REPLY
    current = service.get_interview("iv_binding")
    assert current["turns"][0]["current_understanding"]["intent"] == "answer_unresolved"
    assert service.finish_on_candidate_intent("iv_binding") == current
    assert not current.get("candidate_input_completed_at") and not current.get("candidate_exit_confirmed_at")


@pytest.mark.anyio
@pytest.mark.parametrize("boundary", ["prepare", "preflight", "commit"])
async def test_removed_delivery_proof_is_rejected_at_every_preparation_and_commit_boundary(boundary):
    service, final, prepared = await prepare_unresolved()
    with service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        session["turns"][0].pop("content_clarification")
        tx.interview_sessions.update(session, expected_version=session["version"])
    before = service.get_interview("iv_binding")
    with pytest.raises(ApiError) as error:
        if boundary == "prepare":
            await service.prepare_streaming_decision("iv_binding", "turn_binding", final,
                snapshot_ref="private-file://synthetic", semantic_first=True,
                content_clarification=prepared.content_clarification)
        elif boundary == "preflight":
            service.assert_prepared_streaming_decision(prepared, final, "org_default")
        else:
            await service.submit_streaming_answer("iv_binding", _payload(final), prepared_decision=prepared)
    assert error.value.code == "CONTENT_CLARIFICATION_REQUIRED"
    assert service.get_interview("iv_binding") == before


@pytest.mark.anyio
async def test_normal_completion_flag_cannot_supply_missing_content_clarification():
    service, final, prepared = await prepare_unresolved()
    prepared = replace(prepared, content_clarification=None, completion_confirmed=True)
    with pytest.raises(ApiError):
        service.assert_prepared_streaming_decision(prepared, final, "org_default")
    assert not service.get_interview("iv_binding")["answers"]


@pytest.mark.anyio
async def test_old_finish_statement_cannot_authorize_unresolved_completion_after_clarification():
    service, final, prepared = await prepare_unresolved()
    old_basis = prepared.understanding.completion_basis.model_copy(update={"evidence_quotes": ["我的介绍完毕。"]})
    prepared = replace(prepared, understanding=prepared.understanding.model_copy(update={"completion_basis": old_basis}))
    with pytest.raises(ApiError) as error:
        service.assert_prepared_streaming_decision(prepared, final, "org_default")
    assert error.value.code == "TURN_DECISION_STALE"
    assert not service.get_interview("iv_binding")["answers"]


@pytest.mark.anyio
@pytest.mark.parametrize("entry", ["preflight", "commit"])
async def test_changed_capture_receipt_invalidates_prepared_content_completion(entry):
    service, final, prepared = await prepare_unresolved()
    with service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        session["turns"][0]["content_clarification"]["capture_id"] = "different_capture"
        tx.interview_sessions.update(session, expected_version=session["version"])
    before = service.get_interview("iv_binding")
    with pytest.raises(ApiError) as error:
        if entry == "preflight":
            service.assert_prepared_streaming_decision(prepared, final, "org_default")
        else:
            await service.submit_streaming_answer("iv_binding", _payload(final), prepared_decision=prepared)
    assert error.value.code == "TURN_DECISION_STALE"
    assert service.get_interview("iv_binding") == before


def synthetic_provider(monkeypatch):
    original = MockProvider.invoke
    calls = []

    async def invoke(provider, capability, request, context):
        if (capability != "llm.chat_json" or request.purpose != "interview_turn_understanding"
                or not request.metadata.get("prompt_version", "").startswith("interview_turn_")):
            return await original(provider, capability, request, context)
        text = request.metadata["transcript"]
        refs = understanding_references(text, request.metadata["capability_points"])
        _, _, data = unresolved_response()
        data["evidence_ids"] = list(refs["evidence"])
        data["missing_point_ids"] = list(refs["capabilities"])
        finished = text.endswith(REPLY)
        data["completion_basis"] = {"source": "explicit_server_intent", "evidence_ids": list(refs["evidence"])[-1:]}
        if finished:
            schema = request.json_schema
            if "understanding" in schema["properties"]:
                schema = schema["properties"]["understanding"]
            assert any(branch["properties"]["resolution"]["enum"] == ["same_focus_unresolved"]
                       for branch in schema["properties"]["clarification_target"]["anyOf"])
            data["clarification_target"] = {"resolution": "same_focus_unresolved", "source_ref": "C1"}
        else:
            data.update(intent="clarification_request", turn_intent="clarify", suggested_action="clarify",
                        confidence=.6, completion_basis=None)
        calls.append((finished, text))
        if "understanding" in request.json_schema["properties"]:
            data = {"understanding": data, "followup": {"selected": False, "question_text": "",
                "evidence_id": "", "target_point_ids": [], "rationale": "", "difficulty": "mid",
                "sensitive_attribute_inference": False, "leaks_answer": False}}
        return ChatJSONResponse(data=data, usage=Usage(), provider=ProviderMeta(provider_id="mock",
            model="synthetic_clarification", request_id="synthetic_clarification", latency_ms=0))

    monkeypatch.setattr(MockProvider, "invoke", invoke)
    return calls


@pytest.mark.anyio
async def test_real_capture_clarifies_then_preserves_unresolved_answer_and_advances_only_this_question(tmp_path, monkeypatch):
    from test_automatic_turn_integration import _automatic_session, _current, _feed, _CONTINUATION
    from test_declined_answer_integration import (_add_next_question, _playable_speech, _answer_without_confirmation,
        _stop_current_playback, _NEXT_TURN_ID)
    from test_evidence_owner_recovery_integration import _wait_until
    from app.providers.mock.provider import MockSTTStream
    calls = synthetic_provider(monkeypatch)
    pending, assigned = iter([FRAGMENT, REPLY]), []
    original_send = MockSTTStream.send_audio

    async def audio_aligned_send(stream, chunk):
        if any(chunk) and not getattr(stream, "_synthetic_speech_assigned", False):
            stream._synthetic_speech_assigned = True
            text = next(pending, "")
            assigned.append(text)
            stream.request = stream.request.model_copy(update={"metadata": {
                **stream.request.metadata, "development_transcript": text,
            }})
        return await original_send(stream, chunk)

    monkeypatch.setattr(MockSTTStream, "send_audio", audio_aligned_send)
    async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
        _add_next_question(store)
        _playable_speech(runtime)
        endpoint, clock = await _answer_without_confirmation(managed)
        await _wait_until(lambda: endpoint.confirmation.speaking and bool(endpoint.confirmation._content_question_act_id), timeout=5)
        assert not _current(runtime)["answers"]
        await _wait_until(lambda: managed.chain._stt.stream._stream is not None)
        await _stop_current_playback(runtime, channel)
        await _wait_until(lambda: endpoint.confirmation._content_question_delivered)
        receipt = _current(runtime)["turns"][0]["content_clarification"]
        assert receipt["request_sha256"] == text_hash(FRAGMENT)
        assert receipt["capture_id"] == managed._capture_id
        await _feed(managed._ingress, _CONTINUATION, 5)
        clock.value += 5
        await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
        await _wait_until(lambda: _current(runtime)["current_turn_id"] == _NEXT_TURN_ID, timeout=5)
        current = _current(runtime)
        assert current["answers"][0]["raw_transcript"] == current["answers"][0]["final_transcript"] == FRAGMENT + REPLY
        assert current["turns"][0]["current_understanding"]["intent"] == "answer_unresolved"
        assert not current.get("candidate_input_completed_at") and not current.get("candidate_exit_confirmed_at")
        assert any(finished for finished, _ in calls)
        assert assigned == [FRAGMENT, REPLY]


@pytest.mark.anyio
async def test_new_capture_clears_persisted_and_process_local_content_authority(tmp_path, monkeypatch):
    from test_automatic_turn_integration import _automatic_session, _current
    from test_evidence_owner_recovery_integration import INTERVIEW_ID, ORGANIZATION_ID, TURN_ID
    async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
        _, turn, _, _ = exchange()
        with persistence_for(store).transaction(ORGANIZATION_ID) as tx:
            session = tx.interview_sessions.get(INTERVIEW_ID)
            session["turns"][0]["content_clarification"] = turn["content_clarification"]
            tx.interview_sessions.update(session, expected_version=session["version"])
        old = managed._answer_endpoint
        old.confirmation._content_question_delivered = True
        old.confirmation._content_request_final = _final(FRAGMENT)
        old.confirmation._content_question_focus = "蓝桥"
        await old.close()
        managed._capture_id = "replacement_synthetic_capture"
        managed._start_answer_endpoint()
        assert managed._answer_endpoint is not old
        assert not _current(runtime)["turns"][0].get("content_clarification")
        assert managed._answer_endpoint.confirmation.content_clarification_context(_final(FRAGMENT + REPLY)) is None
        assert not managed._answer_endpoint.confirmation.confirmed


def test_unresolved_candidate_exit_requires_fresh_confirmation_and_preserves_answer_and_media(tmp_path, monkeypatch):
    from app.core.prompt.candidate_exit import EXIT_CONFIRMATION_SPEECH
    from app.providers.mock.provider import MockSTTStream
    from test_automatic_turn_integration import (
        _automatic_session, _current, _feed, _CONTINUATION,
    )
    from test_exit_evidence_086 import _assert_retained_exit
    from test_declined_answer_integration import (
        _TECHNICAL, _add_next_question, _playable_speech, _answer_without_confirmation,
        _stop_current_playback, _NEXT_TURN_ID,
    )
    from test_evidence_owner_recovery_integration import INTERVIEW_ID, _wait_until

    body = _TECHNICAL + "任务转到蓝桥，蓝桥。我不想面了。"
    reply = "确定结束面试。"
    calls, assigned = [], []
    pending = iter([body, reply])
    original_invoke, original_send = MockProvider.invoke, MockSTTStream.send_audio

    async def invoke(provider, capability, request, context):
        version = request.metadata.get("prompt_version", "")
        if capability != "llm.chat_json" or request.purpose != "interview_turn_understanding":
            return await original_invoke(provider, capability, request, context)
        if version == "candidate_exit_reply.v1":
            fresh_reply = json.loads(request.messages[-1].content)["new_reply"]
            calls.append(("exit_reply", fresh_reply))
            data = {"intent": "confirm", "confidence": .98, "evidence_quote": fresh_reply}
        elif version.startswith("interview_turn_"):
            text = request.metadata["transcript"]
            refs = understanding_references(text, request.metadata["capability_points"])
            technical_id = next(key for key, value in refs["evidence"].items() if value == _TECHNICAL)
            fragment_id = next(key for key, value in refs["evidence"].items() if "蓝桥" in value)
            data = {
                "intent": "answer_unresolved", "answer_content": "partial", "turn_intent": "stop_interview",
                "answer_summary": "候选人已说明检查点与幂等提交，但蓝桥的技术含义待核实，并明确要求退出面试。",
                "claims": [{"claim": _TECHNICAL, "evidence_id": technical_id}],
                "evidence_ids": list(refs["evidence"]),
                "covered_point_ids": list(refs["capabilities"]), "missing_point_ids": [],
                "ambiguities": ["蓝桥的实际含义无法核实。"], "contradictions": [],
                "confidence": .98, "suggested_action": "next", "followup_allowed": False,
                "completion_basis": {"source": "explicit_server_intent", "evidence_ids": list(refs["evidence"])[-1:]},
                "clarification_target": {"evidence_id": fragment_id, "focus_quote": "蓝桥"},
            }
            calls.append(("understanding", text))
            if "understanding" in request.json_schema["properties"]:
                data = {"understanding": data, "followup": {
                    "selected": False, "question_text": "", "evidence_id": "", "target_point_ids": [],
                    "rationale": "", "difficulty": "mid", "sensitive_attribute_inference": False,
                    "leaks_answer": False,
                }}
        else:
            return await original_invoke(provider, capability, request, context)
        return ChatJSONResponse(data=data, usage=Usage(), provider=ProviderMeta(
            provider_id="mock", model="synthetic_unresolved_exit", request_id="synthetic_unresolved_exit", latency_ms=0,
        ))

    async def audio_aligned_send(stream, chunk):
        if any(chunk) and not getattr(stream, "_synthetic_speech_assigned", False):
            stream._synthetic_speech_assigned = True
            text = next(pending, "")
            assigned.append(text)
            stream.request = stream.request.model_copy(update={"metadata": {
                **stream.request.metadata, "development_transcript": text,
            }})
        return await original_send(stream, chunk)

    monkeypatch.setattr(MockProvider, "invoke", invoke)
    monkeypatch.setattr(MockSTTStream, "send_audio", audio_aligned_send)

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            spoken = _playable_speech(runtime)
            ingress = managed._ingress
            stopped_recordings = []
            original_stop = runtime.media_captures.stop_for_interview

            async def stop_recording(interview_id, **kwargs):
                stopped_recordings.append(interview_id)
                return await original_stop(interview_id, **kwargs)

            monkeypatch.setattr(runtime.media_captures, "stop_for_interview", stop_recording)
            endpoint, clock = await _answer_without_confirmation(managed)
            await _wait_until(lambda: endpoint.confirmation.speaking
                and bool(endpoint.confirmation._exit_question_act_id)
                and managed.chain._stt.stream._stream is not None, timeout=5)
            current = _current(runtime)
            selected = [event for event in current["agent_events"] if event["type"] == "conversation.act.selected"]
            assert selected[-1]["payload"]["text"] == EXIT_CONFIRMATION_SPEECH
            assert spoken == [EXIT_CONFIRMATION_SPEECH]
            assert not current["answers"] and not current.get("candidate_input_completed_at")
            assert not current["turns"][0].get("content_clarification")
            assert not endpoint.confirmation._content_question_act_id
            assert not endpoint.confirmation._exit_question_delivered
            assert not stopped_recordings and ingress.connected and not managed._stopped
            assert not any(kind == "exit_reply" for kind, _ in calls)

            await _stop_current_playback(runtime, channel)
            await _wait_until(lambda: endpoint.confirmation._exit_question_delivered)
            assert not _current(runtime)["answers"], "Playing the question cannot replace a new candidate reply"
            await _feed(ingress, _CONTINUATION, 5)
            clock.value += 5
            await _wait_until(lambda: bool(_current(runtime).get("candidate_input_completed_at")), timeout=5)
            await _wait_until(lambda: all(command["status"] == "completed" for command in store.evidence_commands.values()))
            await _wait_until(lambda: bool(_current(runtime)["agent_runtime"].get("completion_closing_performance_id")))
            assert stopped_recordings == [INTERVIEW_ID]
            await _stop_current_playback(runtime, channel)
            await _wait_until(lambda: managed._stopped and not ingress.connected)

            current = _current(runtime)
            retained = _assert_retained_exit(store, runtime, body + reply,
                voice_frames=5, continuation_frames=5)
            # Exit preserves the unresolved words and technical body for human
            # review; it does not manufacture a completed, assessed answer.
            assert _TECHNICAL in retained["raw_transcript"] and "任务转到蓝桥，蓝桥。" in retained["raw_transcript"]
            assert ("understanding", body + reply) not in calls
            assert current["candidate_input_completion_reason"] == "candidate_requested"
            assert current["candidate_exit_confirmed_at"] and channel._evidence_session is None
            assert not any(event["type"] == "conversation.act.selected" and event["turn_id"] == _NEXT_TURN_ID
                for event in current["agent_events"])
            assert len([event for event in current["agent_events"] if event["type"] == "completed"]) == 1
            assert [(kind, text) for kind, text in calls if kind == "exit_reply"] == [("exit_reply", reply)]
            assert assigned == [body, reply]

    asyncio.run(scenario())
