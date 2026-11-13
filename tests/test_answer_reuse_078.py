"""Reuse unchanged answer evidence only after a delivered fresh confirmation."""
from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

from app.core.errors import ApiError
from app.core.prompt.contracts import SUPPLEMENT_SPEECH, SUPPLEMENT_SPEECH_VERSION
from app.core.time import utc_now
from app.model_gateway.schemas import TranscriptSegment
from app.services.conversation_delivery import record_selection, update_delivery
from app.services.prepared_answer_completion import pure_completion_reply
from app.services.interview_evidence import InterviewEvidenceChain
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.interviews import InterviewService
from test_prepared_decision_binding import _payload, _setup
from test_prepared_turn_decision import Gateway
from test_off_topic_semantics_068 import off_topic_response
from test_supplement_commit_gate_073 import _ordinary


def _delivered(service, *, status="delivered", act_id="supplement_078"):
    with service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        turn = session["turns"][0]
        act = {"act_id": act_id, "turn_id": turn["id"], "act_type": "supplement_check",
               "prompt_version": SUPPLEMENT_SPEECH_VERSION, "text": SUPPLEMENT_SPEECH["check"]}
        turn["conversation_acts"].append(act)
        event = {"event_id": "event_" + act_id, "turn_id": turn["id"], "session_sequence": len(turn["conversation_acts"]),
                 "occurred_at": utc_now(), "type": "conversation.act.selected",
                 "payload": {"act_id": act_id, "text": act["text"], "approved": True}}
        session.setdefault("agent_events", []).append(event)
        record_selection(session, event)
        if status != "selected":
            update_delivery(session, event_id=event["event_id"], performance_id="performance_" + act_id,
                            status=status, updated_at=utc_now())
        tx.interview_sessions.update(session, expected_version=session["version"])


def _append(base, reply="我已经说完了。"):
    last = base.segments[-1].end_ms
    return base.model_copy(deep=True, update={"text": base.text + reply,
        "provider": base.provider.model_copy(update={"request_id": "new_capture_segment", "latency_ms": 11}),
        "segments": [*base.segments, TranscriptSegment(text=reply, confidence=.95,
                                                       start_ms=last + 100, end_ms=last + 900)]})


def _confirm(service, prepared, base, final, *, boundary=None, reply="我已经说完了。", delivered=True,
             decision=None):
    return service.confirm_prepared_streaming_decision(prepared, base, boundary or base, final,
        reply_text=reply, question_delivered=delivered,
        decision=decision or {"intent": "finish", "confidence": .95})


@pytest.mark.anyio
async def test_fresh_confirmation_reuses_understanding_without_a_second_model_call(monkeypatch):
    from app.core import speech_diagnostics
    rows = []
    monkeypatch.setattr(speech_diagnostics, "_LOG", SimpleNamespace(info=lambda value: rows.append(json.loads(value))))
    service, base, prepared = await _ordinary()
    calls = len(service.conversation.gateway.requests)
    original = prepared.understanding.model_dump(mode="json")
    _delivered(service)
    final = _append(base)
    completed = _confirm(service, prepared, base, final)
    assert completed is not None and completed.completion_receipt is not None
    assert completed.completion_confirmed and not prepared.completion_confirmed
    assert completed.understanding.understanding_id != prepared.understanding.understanding_id
    assert completed.understanding.completion_basis.evidence_quotes == ["我已经说完了。"]
    assert completed.followup == prepared.followup
    for key in ("answer_summary", "claims", "evidence_quotes", "covered_capability_points", "missing_capability_points"):
        assert completed.understanding.model_dump(mode="json")[key] == original[key]
    service.assert_prepared_streaming_decision(completed, final, "org_default")
    result = await service.submit_streaming_answer("iv_binding", _payload(final), prepared_decision=completed)
    assert result["accepted"] and result["answer"]["final_transcript"] == final.text
    assert result["answer"]["transcript_segments"] == _payload(final)["segments"]
    assert result["answer"]["stt_confidence"] <= min(s.confidence for s in final.segments)
    assert len(service.conversation.gateway.requests) == calls
    receipt = next(row for row in rows if row["event"] == "turn_control.prepared_answer_reused")
    assert receipt["source_understanding_id"] == prepared.understanding.understanding_id
    assert receipt["current_understanding_id"] == completed.understanding.understanding_id
    assert receipt["question_act_id"] == "supplement_078"
    assert base.text not in json.dumps(receipt, ensure_ascii=False)


@pytest.mark.anyio
@pytest.mark.parametrize("status,delivered", [("selected", True), ("interrupted", True), ("failed", True), ("delivered", False)])
async def test_selected_or_interrupted_question_does_not_grant_reuse(status, delivered):
    service, base, prepared = await _ordinary()
    _delivered(service, status=status)
    before = service.get_interview("iv_binding")
    assert _confirm(service, prepared, base, _append(base), delivered=delivered) is None
    assert service.get_interview("iv_binding") == before


@pytest.mark.anyio
@pytest.mark.parametrize("reply", ["我已经说完了。我再补充重试使用指数退避。", "我不想面了。", "没有补充吗？我还没说完。", "不是，我刚才说错了。"])
async def test_even_misclassified_finish_cannot_hide_new_technical_or_control_content(reply):
    service, base, prepared = await _ordinary()
    _delivered(service)
    assert _confirm(service, prepared, base, _append(base, reply), reply=reply) is None


@pytest.mark.anyio
@pytest.mark.parametrize("change", [
    "old_text", "old_confidence", "old_timing", "missing_segments", "provider", "language", "aggregate_raised",
    "reply_low_confidence", "overlapping_reply", "boundary_changed",
])
async def test_final_keeps_exact_old_source_and_quality_or_reuse_misses(change):
    service, base, prepared = await _ordinary()
    _delivered(service)
    final, boundary = _append(base), base
    if change == "old_text":
        final = final.model_copy(update={"text": "不对。" + final.text})
    elif change in {"old_confidence", "old_timing"}:
        field, value = ("confidence", .8) if change == "old_confidence" else ("end_ms", 1900)
        final = final.model_copy(update={"segments": [final.segments[0].model_copy(update={field: value}), final.segments[-1]]})
    elif change == "missing_segments":
        final = final.model_copy(update={"segments": []})
    elif change == "provider":
        final = final.model_copy(update={"provider": final.provider.model_copy(update={"model": "other_model"})})
    elif change == "language":
        final = final.model_copy(update={"language": "en-US"})
    elif change == "aggregate_raised":
        final = final.model_copy(update={"confidence": 1.0})
    elif change in {"reply_low_confidence", "overlapping_reply"}:
        field, value = ("confidence", .4) if change == "reply_low_confidence" else ("start_ms", 100)
        final = final.model_copy(update={"segments": [final.segments[0], final.segments[-1].model_copy(update={field: value})]})
    elif change == "boundary_changed":
        boundary = base.model_copy(update={"confidence": .8})
    assert _confirm(service, prepared, base, final, boundary=boundary) is None
    assert not service.get_interview("iv_binding")["answers"]


@pytest.mark.anyio
@pytest.mark.parametrize("changed", ["policy", "question", "reception", "clarification", "exit", "lifecycle"])
async def test_changed_context_invalidates_prepared_technical_result(changed):
    service, base, prepared = await _ordinary()
    _delivered(service)
    with service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        turn = session["turns"][0]
        if changed == "policy":
            session["followup_policy"] = {"max_depth": 0}
        elif changed == "question":
            turn["question_spoken_text"] = "其他问题"
        elif changed == "reception":
            turn["reception_exchanges"] = [{"act_id": "new_request"}]
        elif changed == "clarification":
            turn["content_clarification"] = {"status": "delivered"}
        elif changed == "exit":
            turn["conversation_acts"].append({"act_id": "exit", "prompt_version": "candidate_exit_confirmation.v1"})
        else:
            session["lifecycle_events"].append({"id": "new_pause", "type": "interview.paused"})
        tx.interview_sessions.update(session, expected_version=session["version"])
    assert _confirm(service, prepared, base, _append(base)) is None


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["new_question", "changed_final", "changed_claim", "changed_followup"])
async def test_composed_result_is_fenced_again_at_commit(change):
    service, base, prepared = await _ordinary()
    _delivered(service)
    final = _append(base)
    completed = _confirm(service, prepared, base, final)
    assert completed is not None
    if change == "new_question":
        _delivered(service, status="selected", act_id="new_check")
    elif change == "changed_final":
        final = _append(final, "还有一点技术细节。")
    elif change == "changed_claim":
        completed = replace(completed, understanding=completed.understanding.model_copy(update={"answer_summary": "凭空修改"}))
    else:
        completed = replace(completed, followup={**completed.followup, "selected": True})
    before = service.get_interview("iv_binding")
    with pytest.raises(ApiError) as caught:
        await service.submit_streaming_answer("iv_binding", _payload(final), prepared_decision=completed)
    assert caught.value.code == "TURN_DECISION_STALE"
    assert service.get_interview("iv_binding") == before


@pytest.mark.parametrize("reply", ["没有补充了。", "我已经说完了。", "好的，没有其他要补充的了。", "就这些，谢谢。", "我回答完毕了。"])
def test_common_pure_completion_replies_are_eligible(reply):
    assert pure_completion_reply(reply)


@pytest.mark.anyio
@pytest.mark.parametrize("storage", ["memory", "sqlite"])
async def test_already_prepared_followup_is_committed_without_regeneration(storage, tmp_path, monkeypatch):
    service, original = _setup()
    sqlite_path = tmp_path / "answer-reuse-078.sqlite3"
    if storage == "sqlite":
        saved = service.get_interview("iv_binding")
        store = SQLiteStore(str(sqlite_path))
        connect = store._connect

        def connection():
            result = connect()
            result.execute("PRAGMA busy_timeout = 30")
            return result

        monkeypatch.setattr(store, "_connect", connection)
        service = InterviewService(store)
        with service.persistence.transaction("org_default") as tx:
            tx.interview_sessions.add(saved)
    with service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        session["turns"][0]["allow_followup"] = True
        session["followup_policy"] = {"max_total": 1, "max_per_root": 1, "max_depth": 1}
        tx.interview_sessions.update(session, expected_version=session["version"])
    text = "我使用请求标识记录处理结果，并在请求到达时检查处理状态。重复请求返回此前的结果，不再重复创建业务对象。标识跟业务记录一起持久保存，并通过唯一约束避免并发创建重复结果。"
    base = original.model_copy(update={"text": text, "segments": [
        TranscriptSegment(text=text, confidence=.95, start_ms=0, end_ms=6000)]})
    wire = off_topic_response(text, ["幂等键"], technical=True, relevant=True)
    wire["followup"].update(selected=True, question_text="你提到使用请求标识，具体如何生成这个标识？",
                            evidence_id="E1", target_point_ids=["P1"], rationale="核验标识生成细节")
    service.conversation.gateway = Gateway(wire)
    prepared = await service.prepare_streaming_decision("iv_binding", "turn_binding", base,
        snapshot_ref="private-file://synthetic_prefix", semantic_first=True)
    assert prepared.understanding.problem is None and prepared.followup["selected"]
    _delivered(service)
    final = _append(base, "没有补充了。")
    composed = _confirm(service, prepared, base, final, reply="没有补充了。")
    assert composed is not None
    result = await service.submit_streaming_answer("iv_binding", _payload(final), prepared_decision=composed)
    assert result["accepted"]
    session = service.get_interview("iv_binding")
    followups = [turn for turn in session["turns"] if turn.get("is_followup")]
    assert len(followups) == 1 and followups[0]["question_spoken_text"] == wire["followup"]["question_text"]
    assert len(service.conversation.gateway.requests) == 1
    if storage == "sqlite":
        reopened = InterviewService(SQLiteStore(str(sqlite_path))).get_interview("iv_binding")
        assert len(reopened["answers"]) == 1
        assert reopened["answers"][0]["transcript_segments"] == _payload(final)["segments"]
        assert reopened["turns"][0]["current_understanding"]["completion_basis"]["evidence_quotes"] == ["没有补充了。"]
        assert reopened["current_turn_id"] == followups[0]["id"]


@pytest.mark.anyio
async def test_reuse_is_bound_to_live_capture_and_owner_before_composing():
    service, base, prepared = await _ordinary()
    _delivered(service)
    chain = InterviewEvidenceChain(InMemoryStore(), "iv_binding")
    checks = []
    revoked = False

    def current():
        checks.append(True)
        if revoked:
            raise ApiError("EVIDENCE_OWNER_LOST", "Owner lost", status_code=409)

    stream = SimpleNamespace(_assert_commit_allowed=current, interviews=service, turn_id="turn_binding")
    chain._stt = stream
    final = _append(base)
    kwargs = {"reply_text": "我已经说完了。", "decision": {"intent": "finish", "confidence": .95},
              "question_delivered": True}
    assert chain.confirm_prepared_decision(prepared, base, base, final, **kwargs) is None
    bound = replace(prepared, capture_binding=stream)
    assert chain.confirm_prepared_decision(bound, base, base, final, **kwargs) is not None
    revoked = True
    with pytest.raises(ApiError, match="Owner lost"):
        chain.confirm_prepared_decision(bound, base, base, final, **kwargs)
    assert len(checks) == 3
    assert not service.get_interview("iv_binding")["answers"]


@pytest.mark.anyio
async def test_old_capture_result_cannot_detach_a_replacement_capture():
    service, base, prepared = await _ordinary()
    chain = InterviewEvidenceChain(InMemoryStore(), "iv_binding")
    stream = SimpleNamespace(turn_id="turn_binding")
    chain._stt = stream
    old = replace(prepared, capture_binding=object())
    with pytest.raises(ApiError) as caught:
        await chain.finish({}, prepared_decision=old)
    assert caught.value.code == "TURN_DECISION_STALE"
    assert chain._stt is stream and not chain._finishing
