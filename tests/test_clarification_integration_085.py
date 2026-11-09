"""Delivered clarification sources remain bound through real SQLite commits."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.core.prompt.contracts import CLARIFICATION_SPEECH_VERSION, clarification_speech
from app.core.prompt.understanding_references import understanding_references
from app.core.time import utc_now
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, TranscriptSegment, Usage
from app.repositories.sqlite import SQLiteStore
from app.services.content_clarification import text_hash
from app.services.conversation_delivery import record_selection, update_delivery
from app.services.interviews import InterviewService
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _endpoint
from test_answer_reuse_078 import _append, _delivered
from test_preparation_fences_081 import _chain
from test_prepared_decision_binding import _payload
from test_supplement_reply_sqlite_082 import _CaptureFacade


_CLAIM = "我使用请求标识保存处理结果。"
_FOCUS = "青桥"
_REQUEST = _CLAIM + "任务会进入青桥处理"
_RESOLVED = "我指的是持久消息队列。现在我的回答结束了。"
_UNRESOLVED = "那个名称我暂时无法说明。现在我的回答结束了。"
_FINISH = "没有补充了。"
_NEW_CLAIM = "青桥是持久消息队列。"
_NEW_FOCUS = "紫岩"
_NEW_DETAIL = "我还会把消息送到紫岩。"
_ANSWER_END = "现在我的回答结束了。"
_NEW_REPLY = _NEW_CLAIM + _NEW_DETAIL + _ANSWER_END


def _public_context(request):
    message = next(m.content for m in request.messages if m.content.startswith("面试官上下文："))
    return json.loads(message.removeprefix("面试官上下文："))


class _Gateway:
    def __init__(self, harness, *, resolved=False, lose_owner=False, reply_mode=None, old_basis=False):
        self.harness, self.resolved, self.lose_owner = harness, resolved, lose_owner
        self.reply_mode = reply_mode or ("resolved" if resolved else "unresolved")
        self.old_basis = old_basis
        self.requests = []
        self.sources = []

    async def invoke(self, capability, request):
        self.requests.append(request)
        if request.metadata["prompt_version"].startswith("supplement_reply."):
            data = {"intent": "finish", "confidence": .96, "reply_text": "",
                    "evidence_id": request.json_schema["properties"]["evidence_id"]["enum"][-1]}
        else:
            text = request.metadata["transcript"]
            context = _public_context(request).get("content_clarification")
            refs = understanding_references(text, request.metadata["capability_points"],
                **({"clarification_context": context} if context else {}))
            ids = list(refs["evidence"])
            claim = next(key for key, value in refs["evidence"].items() if value == _CLAIM)
            data = {"intent": "answer", "answer_content": "partial", "turn_intent": "clarify",
                "answer_summary": "保留已说明的请求标识做法，任务处理对象待核实。",
                "claims": [{"claim": _CLAIM, "evidence_id": claim}], "evidence_ids": ids,
                # A missing rubric point is a scoring issue, never a reason
                # to invent an additional acoustic ambiguity.
                "covered_point_ids": [], "missing_point_ids": list(refs["capabilities"]),
                "ambiguities": ["青桥所指的处理对象尚不明确。"], "contradictions": [],
                "confidence": .6, "suggested_action": "clarify", "followup_allowed": False,
                "completion_basis": None,
                "clarification_target": {"evidence_id": next(key for key, value in refs["evidence"].items()
                    if _FOCUS in value), "focus_quote": _FOCUS}}
            if context:
                schema = request.json_schema["properties"].get("understanding", request.json_schema)
                branches = schema["properties"]["clarification_target"]["anyOf"]
                source_schema = next(branch for branch in branches
                    if branch["properties"]["resolution"]["enum"] == ["same_focus_unresolved"])
                assert source_schema["properties"]["source_ref"]["enum"] == ["C1"]
                self.sources.append(deepcopy(refs["clarification_sources"]))
                data.update(turn_intent="finish_answer", confidence=.96, suggested_action="next",
                    completion_basis={"source": "explicit_server_intent", "evidence_ids": ids[-1:]})
                if self.reply_mode == "resolved":
                    data.update(intent="answer", ambiguities=[],
                        clarification_target={"resolution": "resolved"}, followup_allowed=True)
                else:
                    data.update(intent="answer_unresolved", clarification_target={
                        "resolution": "same_focus_unresolved", "source_ref": "C1"})
                if self.reply_mode in {"new_focus", "thinking", "stop_interview", "pause", "repeat"}:
                    new_claim_id = next(key for key, value in refs["evidence"].items() if value == _NEW_CLAIM)
                    new_target_id = next(key for key, value in refs["evidence"].items() if value == _NEW_DETAIL)
                    data["claims"].append({"claim": _NEW_CLAIM, "evidence_id": new_claim_id})
                    data.update(intent="answer", answer_summary="保留请求标识和持久消息队列两项做法，新处理对象待核实。",
                        ambiguities=["紫岩所指的处理对象尚不明确。"], clarification_target={
                            "resolution": "new_focus_unresolved", "evidence_id": new_target_id,
                            "focus_quote": _NEW_FOCUS})
                    if self.reply_mode == "thinking":
                        data.update(turn_intent="thinking", completion_basis=None,
                            suggested_action="continue_listening", followup_allowed=False)
                    elif self.reply_mode in {"pause", "repeat"}:
                        data.update(turn_intent=self.reply_mode, completion_basis=None,
                            suggested_action=self.reply_mode, followup_allowed=False)
                    elif self.reply_mode == "stop_interview":
                        data.update(intent="answer_unresolved", turn_intent="stop_interview")
                if self.old_basis:
                    old_id = next(key for key, value in refs["evidence"].items() if value == _ANSWER_END) \
                        if self.old_basis == "duplicate" else claim
                    data["completion_basis"] = {"source": "explicit_server_intent", "evidence_ids": [old_id]}
                if self.lose_owner:
                    assert self.harness.coordinator.release(self.harness.owner)
            if "understanding" in request.json_schema["properties"]:
                data = {"understanding": data, "followup": {"selected": False, "question_text": "",
                    "evidence_id": "", "target_point_ids": [], "rationale": "", "difficulty": "mid",
                    "sensitive_attribute_inference": False, "leaks_answer": False}}
        return ChatJSONResponse(data=deepcopy(data), usage=Usage(), provider=ProviderMeta(
            provider_id="synthetic", model="clarification_085", request_id="synthetic_085", latency_ms=1))


def _select_content_question(service, *, focus=_FOCUS, act_id="clarification_085"):
    with service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        turn = session["turns"][0]
        act = {"act_id": act_id, "turn_id": turn["id"], "act_type": "clarification",
               "text": clarification_speech(focus), "prompt_version": CLARIFICATION_SPEECH_VERSION}
        turn["conversation_acts"].append(act)
        event = {"event_id": "event_" + act_id, "turn_id": turn["id"],
                 "session_sequence": len(session.get("agent_events", [])) + 1,
                 "occurred_at": utc_now(), "type": "conversation.act.selected",
                 "payload": {"act_id": act["act_id"], "text": act["text"], "approved": True}}
        session.setdefault("agent_events", []).append(event)
        record_selection(session, event)
        tx.interview_sessions.update(session, expected_version=session["version"])


async def _acknowledge(endpoint, harness, *, content=False):
    service = harness.stream.interviews
    act_id = endpoint.confirmation._content_question_act_id if content else "supplement_085"
    with service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        assert update_delivery(session, event_id="event_" + act_id, performance_id="performance_" + act_id,
                               status="delivered", updated_at=utc_now())
        tx.interview_sessions.update(session, expected_version=session["version"])
    await endpoint.confirmation.complete_playback(endpoint, reason="agent_finished")
    if content:
        confirmation = endpoint.confirmation
        assert confirmation._content_question_delivered
        with service.persistence.transaction("org_default") as tx:
            session = tx.interview_sessions.get("iv_binding")
            session["turns"][0]["content_clarification"] = {
                "status": "delivered", "capture_id": "capture_085",
                "question_act_id": confirmation._content_question_act_id,
                "request_sha256": text_hash(confirmation._content_request_final.text.strip()),
                "focus_sha256": text_hash(confirmation._content_question_focus)}
            tx.interview_sessions.update(session, expected_version=session["version"])


class _TrackingCapture(_CaptureFacade):
    def __init__(self, harness):
        super().__init__(harness)
        self.results = []

    async def prepare_decision(self, final, **kwargs):
        result = await super().prepare_decision(final, **kwargs)
        self.results.append(result)
        return result


async def _scenario(tmp_path, monkeypatch, *, resolved=False, lose_owner=False, invalidate=None,
                    reply_mode=None, old_basis=False, composite=False):
    harness = _chain(tmp_path, monkeypatch)
    initial_text = _REQUEST + "。" + _ANSWER_END if old_basis == "duplicate" else _REQUEST
    harness.final = harness.final.model_copy(update={"text": initial_text, "segments": [
        TranscriptSegment(text=initial_text, confidence=.95, start_ms=0, end_ms=3000)]})
    service = harness.stream.interviews
    if composite:
        with service.persistence.transaction("org_default") as tx:
            session = tx.interview_sessions.get("iv_binding")
            session["turns"][0]["allow_followup"] = True
            tx.interview_sessions.update(session, expected_version=session["version"])
    capture = _TrackingCapture(harness)
    gateway = service.conversation.gateway = _Gateway(harness, resolved=resolved, lose_owner=lose_owner,
                                                      reply_mode=reply_mode, old_basis=old_basis)
    spoken, committed, spoken_targets, controls = [], [], [], []

    async def commit(prepared, guard):
        guard()
        harness.chain.assert_preparation_current(prepared, capture.current_final,
            semantic_first=True, completion_confirmed=prepared.completion_confirmed,
            content_clarification=prepared.content_clarification)
        result = await service.submit_streaming_answer("iv_binding", _payload(capture.current_final),
            evidence_fence=harness.owner.commit_fence(), prepared_decision=prepared)
        if result["accepted"]:
            committed.append((result, prepared))
        else:
            assert result["conversation_action"] in {"pause", "repeat"}
            controls.append(result)

    endpoint, clock, notices, _ = _endpoint(capture=capture, commit_impl=commit)

    async def speak(kind, guard, **kwargs):
        guard()
        spoken.append(kind)
        if kind == "answer_clarify":
            focus = kwargs["focus_quote"]
            assert focus in {_FOCUS, _NEW_FOCUS}
            act_id = "clarification_085" if not spoken_targets else "clarification_085_new"
            spoken_targets.append(focus)
            _select_content_question(service, focus=focus, act_id=act_id)
            return {"act_id": act_id}
        if kind == "check":
            _delivered(service, status="selected", act_id="supplement_085")
        return True

    confirmation = endpoint.confirmation = SpokenSupplementConfirmation(speak=speak,
        interview_id="iv_binding", turn_id="turn_binding", capture_id="capture_085")
    endpoint.speech_started()
    try:
        await confirmation.step(endpoint, quiet=5)
        assert spoken == ["answer_clarify"] and not committed
        await _acknowledge(endpoint, harness, content=True)
        reply = {"new_focus": _NEW_REPLY,
                 "thinking": _NEW_CLAIM + _NEW_DETAIL + "等一下，我还需要思考。",
                 "pause": _NEW_CLAIM + _NEW_DETAIL + "请先暂停面试。",
                 "repeat": _NEW_CLAIM + _NEW_DETAIL + "请再读一遍当前问题。",
                 "stop_interview": _NEW_CLAIM + _NEW_DETAIL + "我想结束整场面试。"}.get(
                     reply_mode, _RESOLVED if resolved else _UNRESOLVED)
        capture.current_final = _append(harness.final, reply)
        harness.stream.stream = SimpleNamespace(_final=capture.current_final)
        endpoint.observe_transcript(capture.current_final.text)
        clock.value += 5
        if invalidate == "closed_capture":
            harness.chain._stt = None
        elif invalidate:
            with service.persistence.transaction("org_default") as tx:
                session = tx.interview_sessions.get("iv_binding")
                receipt = session["turns"][0]["content_clarification"]
                receipt["status"] = "selected"
                tx.interview_sessions.update(session, expected_version=session["version"])
        failure = None
        try:
            await confirmation.step(endpoint, quiet=5)
            if resolved and not lose_owner and not invalidate:
                assert spoken == ["answer_clarify", "check"] and not committed
                assert not capture.prepares[-1].text.endswith(_FINISH)
                await _acknowledge(endpoint, harness)
                capture.current_final = _append(capture.current_final, _FINISH)
                harness.stream.stream._final = capture.current_final
                endpoint.observe_transcript(capture.current_final.text)
                clock.value += 5
                await confirmation.step(endpoint, quiet=5)
        except ApiError as exc:
            failure = exc.code
        return SimpleNamespace(harness=harness, gateway=gateway, spoken=spoken, committed=committed,
            final=capture.current_final, failure=failure, notices=notices, prepared=capture.results,
            spoken_targets=spoken_targets, phase=confirmation.phase, confirmed=confirmation.confirmed,
            controls=controls)
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("resolved", [False, True])
async def test_delivered_content_reply_preserves_partial_claim_and_commits_exactly_once(tmp_path, monkeypatch, resolved):
    result = await _scenario(tmp_path, monkeypatch, resolved=resolved)
    assert result.failure is None and len(result.committed) == 1
    assert result.spoken == (["answer_clarify", "check"] if resolved else ["answer_clarify"])
    reopened = InterviewService(SQLiteStore(str(tmp_path / "preparation-fences.sqlite3"))).get_interview("iv_binding")
    assert len(reopened["answers"]) == len(reopened["turns"][0]["utterances"]) == 1
    assert reopened["answers"][0]["final_transcript"] == result.final.text
    assert reopened["answers"][0]["transcript_segments"] == _payload(result.final)["segments"]
    understanding = reopened["turns"][0]["current_understanding"]
    assert understanding["intent"] == ("answer" if resolved else "answer_unresolved")
    assert understanding["claims"][0]["evidence_quote"] == _CLAIM
    assert understanding["covered_capability_points"] == []
    assert understanding["missing_capability_points"] == ["幂等键"]
    assert bool(understanding["ambiguities"]) is not resolved
    if not resolved:
        assert understanding["clarification_target"]["focus_quote"] == _FOCUS
        assert understanding["clarification_target"]["evidence_quote"] in _REQUEST
        assert len(result.gateway.requests) == 2  # First question plus one fixed-source reply.
    assert result.gateway.sources
    # Retrying the exact submission cannot create a second durable answer.
    service = result.harness.stream.interviews
    try:
        await service.submit_streaming_answer("iv_binding", _payload(result.final),
            evidence_fence=result.harness.owner.commit_fence(), prepared_decision=result.committed[0][1])
    except ApiError:
        pass
    assert len(service.get_interview("iv_binding")["answers"]) == 1


@pytest.mark.anyio
async def test_owner_lost_during_clarified_preparation_cannot_submit(tmp_path, monkeypatch):
    result = await _scenario(tmp_path, monkeypatch, lose_owner=True)
    assert result.failure == "EVIDENCE_OWNER_FENCED"
    assert not result.committed and not result.harness.stream.interviews.get_interview("iv_binding")["answers"]


@pytest.mark.anyio
@pytest.mark.parametrize("invalidate", ["delivery", "closed_capture"])
async def test_stale_content_delivery_or_capture_cannot_create_fixed_source_authority(tmp_path, monkeypatch, invalidate):
    result = await _scenario(tmp_path, monkeypatch, invalidate=invalidate)
    assert result.failure in {None, "CONTENT_CLARIFICATION_REQUIRED", "TURN_DECISION_STALE", "AGENT_EVIDENCE_NOT_OPEN"}
    assert not result.committed and not result.harness.stream.interviews.get_interview("iv_binding")["answers"]
    assert len(result.gateway.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [False, True])
async def test_new_focus_after_delivered_clarification_preserves_claims_and_only_asks_new_question(
    tmp_path, monkeypatch, composite,
):
    result = await _scenario(tmp_path, monkeypatch, reply_mode="new_focus", composite=composite)
    assert result.failure is None and not result.committed and not result.confirmed
    assert result.spoken == ["answer_clarify", "answer_clarify"]
    assert result.spoken_targets == [_FOCUS, _NEW_FOCUS]
    assert len(result.gateway.requests) == len(result.prepared) == 2
    assert all(request.metadata["prompt_version"].startswith(
        "interview_turn_decision." if composite else "interview_turn_understanding.")
        for request in result.gateway.requests)
    understanding = result.prepared[-1].understanding
    assert understanding.problem is None and understanding.intent == "clarification_request"
    assert understanding.turn_intent == "finish_answer" and understanding.suggested_action == "clarify"
    assert understanding.completion_basis is None and not understanding.followup_allowed
    assert understanding.confidence < .65
    assert [claim.evidence_quote for claim in understanding.claims] == [_CLAIM, _NEW_CLAIM]
    assert understanding.clarification_target.focus_quote == _NEW_FOCUS
    assert understanding.clarification_target.evidence_quote == _NEW_DETAIL
    assert understanding.ambiguities == ["紫岩所指的处理对象尚不明确。"]
    reopened = InterviewService(SQLiteStore(str(tmp_path / "preparation-fences.sqlite3"))).get_interview("iv_binding")
    assert not reopened["answers"] and not reopened["turns"][0]["utterances"]
    acts = reopened["turns"][0]["conversation_acts"]
    assert [act["text"] for act in acts] == [clarification_speech(_FOCUS), clarification_speech(_NEW_FOCUS)]
    assert acts[-1]["act_id"] == "clarification_085_new"


@pytest.mark.anyio
async def test_new_focus_cannot_hide_stale_completion_basis_by_dropping_it(tmp_path, monkeypatch):
    result = await _scenario(tmp_path, monkeypatch, reply_mode="new_focus", old_basis=True)
    assert not result.committed and not result.confirmed
    assert result.spoken_targets == [_FOCUS]
    assert len(result.gateway.requests) == 3  # One first question, then at most two rejected reply calls.
    problem = result.prepared[-1].understanding.problem
    assert problem is not None and problem.source == "schema_or_content"
    assert problem.reason_code == "semantic_current_speech_conflict"
    assert not result.harness.stream.interviews.get_interview("iv_binding")["answers"]


@pytest.mark.anyio
async def test_new_focus_rejects_old_occurrence_even_when_new_finish_has_identical_words(tmp_path, monkeypatch):
    result = await _scenario(tmp_path, monkeypatch, reply_mode="new_focus", old_basis="duplicate")
    assert not result.committed and not result.confirmed and result.spoken_targets == [_FOCUS]
    assert len(result.gateway.requests) == 3
    request = result.gateway.requests[-1]
    context = _public_context(request)["content_clarification"]
    refs = understanding_references(request.metadata["transcript"], request.metadata["capability_points"],
                                    clarification_context=context)
    equal_words = [key for key, value in refs["evidence"].items() if value == _ANSWER_END]
    assert len(equal_words) == 2
    assert refs["offsets"][equal_words[0]] < len(context["request_text"]) <= refs["offsets"][equal_words[1]]
    problem = result.prepared[-1].understanding.problem
    assert problem is not None and problem.source == "schema_or_content"
    assert problem.reason_code == "semantic_current_speech_conflict"
    assert not result.harness.stream.interviews.get_interview("iv_binding")["answers"]


@pytest.mark.anyio
async def test_new_focus_cannot_speak_or_submit_after_owner_is_revoked(tmp_path, monkeypatch):
    result = await _scenario(tmp_path, monkeypatch, reply_mode="new_focus", lose_owner=True)
    assert result.failure == "EVIDENCE_OWNER_FENCED"
    assert not result.committed and result.spoken_targets == [_FOCUS]
    assert len(result.gateway.requests) == 2
    assert not result.harness.stream.interviews.get_interview("iv_binding")["answers"]


@pytest.mark.anyio
@pytest.mark.parametrize("reply_mode,expected_speech,expected_action", [
    ("thinking", "reception_wait", "continue_listening"),
    ("stop_interview", "exit_check", "next"),
])
async def test_new_focus_does_not_override_wait_or_independent_exit_confirmation(
    tmp_path, monkeypatch, reply_mode, expected_speech, expected_action,
):
    result = await _scenario(tmp_path, monkeypatch, reply_mode=reply_mode)
    assert result.failure is None and not result.committed and not result.confirmed
    assert result.spoken == ["answer_clarify", expected_speech]
    assert result.spoken_targets == [_FOCUS]
    assert len(result.gateway.requests) == 2
    understanding = result.prepared[-1].understanding
    assert understanding.problem is None and understanding.turn_intent == reply_mode
    assert understanding.suggested_action == expected_action
    assert [claim.evidence_quote for claim in understanding.claims] == [_CLAIM, _NEW_CLAIM]
    assert not result.harness.stream.interviews.get_interview("iv_binding")["answers"]
    if reply_mode == "thinking":
        assert understanding.completion_basis is None
    else:
        assert understanding.completion_basis.evidence_quotes == ["我想结束整场面试。"]


@pytest.mark.anyio
@pytest.mark.parametrize("reply_mode", ["pause", "repeat"])
async def test_new_focus_keeps_pause_and_repeat_as_non_answer_controls(tmp_path, monkeypatch, reply_mode):
    result = await _scenario(tmp_path, monkeypatch, reply_mode=reply_mode)
    assert result.failure is None and not result.committed and not result.confirmed
    assert result.spoken == ["answer_clarify"] and result.spoken_targets == [_FOCUS]
    assert len(result.gateway.requests) == 2
    assert len(result.controls) == 1 and result.controls[0]["accepted"] is False
    assert result.controls[0]["conversation_action"] == reply_mode
    reopened = InterviewService(SQLiteStore(str(tmp_path / "preparation-fences.sqlite3"))).get_interview("iv_binding")
    assert not reopened["answers"]
    understanding = reopened["turns"][0]["current_understanding"]
    assert understanding["turn_intent"] == understanding["suggested_action"] == reply_mode
    assert understanding["problem"] is None and understanding["completion_basis"] is None
    assert understanding["followup_allowed"] is False
    assert [claim["evidence_quote"] for claim in understanding["claims"]] == [_CLAIM, _NEW_CLAIM]
