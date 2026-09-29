"""Selected explanations cannot become new candidate questions again."""
from copy import deepcopy
import hashlib

import pytest

from app.services.conversation_input_context import conversation_input_context


def _session(requests):
    turn = {"id": "turn_current", "conversation_acts": [], "reception_exchanges": []}
    session = {"id": "synthetic", "current_turn_id": turn["id"], "turns": [turn], "agent_events": []}
    for index, (request, kind, reply) in enumerate(requests, 1):
        act_id, event_id = "act_%s" % index, "selected_%s" % index
        turn["conversation_acts"].append({"turn_id": turn["id"], "act_id": act_id, "text": reply})
        session["agent_events"].append({"event_id": event_id, "type": "conversation.act.selected",
            "turn_id": turn["id"], "session_sequence": index,
            "payload": {"approved": True, "act_id": act_id, "text": reply}})
        turn["reception_exchanges"].append({"exchange_id": "exchange_%s" % index,
            "act_id": act_id, "selected_event_id": event_id, "session_sequence": index,
            "request_text": request, "request_sha256": hashlib.sha256(request.encode()).hexdigest(),
            "source_stream_id": "stream_%s" % index, "kind": kind})
    return session, turn


def test_explanation_repeat_wait_and_finish_are_separate_from_the_complete_answer():
    requests = [("FluxDB 是什么？", "question_clarification", "FluxDB 是本题使用的虚构数据库。"),
                ("请重读一下题目。", "repeat", "你怎样封装数据库客户端？"),
                ("让我想一下。", "wait", "好的，你慢慢想。")]
    session, turn = _session(requests)
    text = "".join(request[0] for request in requests) + "我会把凭据放入配置。我讲完了。"
    before = deepcopy(session)
    result = conversation_input_context(session, turn, text)
    assert result["current_speech"] == "我会把凭据放入配置。我讲完了。"
    assert text[result["current_speech_start"]:] == result["current_speech"]
    assert [row["kind"] for row in result["responded_exchanges"]] == ["question_clarification", "repeat", "wait"]
    for row in result["responded_exchanges"]:
        assert text[row["request_start"]:row["request_end"]] == row["request_text"]
    assert session == before


@pytest.mark.parametrize("kind", ["wait", "continue"])
def test_no_new_speech_keeps_the_acknowledged_request_available_for_full_review(kind):
    request = "我先想一下。想好了，我已经讲完了。"
    session, turn = _session([(request, kind, "好的，我在听。")])
    result = conversation_input_context(session, turn, request)
    assert result["current_speech"] == ""
    assert result["responded_exchanges"][-1]["request_text"] == request
    assert result["current_speech_start"] == len(request)
    assert "completion_basis" not in result and "answer_content" not in result


def test_technical_content_before_or_inside_an_acknowledged_request_is_not_edited():
    request = "我用队列重试，能让我继续想一下吗？"
    session, turn = _session([(request, "wait", "可以，你慢慢想。")])
    text = "我用幂等键避免重复。" + request + "\n我讲完了。"
    result = conversation_input_context(session, turn, text)
    assert result["current_speech"] == "我讲完了。"
    assert result["responded_exchanges"][0]["request_text"] == request
    assert text.startswith("我用幂等键避免重复。")


@pytest.mark.parametrize("change", [
    "unselected", "other_turn", "wrong_act", "wrong_event_text", "not_approved", "wrong_sequence",
    "wrong_hash", "truncated", "missing_stream", "duplicate_event", "duplicate_act", "changed_transcript",
    "unicode_hash", "bytes_hash", "null_hash", "object_hash", "malformed_act", "malformed_payload",
])
def test_unverifiable_or_ambiguous_sources_do_not_consume_speech(change):
    request = "请解释一下 FluxDB。"
    session, turn = _session([(request, "question_clarification", "它是本题使用的数据库。")])
    event, act, receipt = session["agent_events"][0], turn["conversation_acts"][0], turn["reception_exchanges"][0]
    text = request + "我不会这题。"
    if change == "unselected": session["agent_events"].clear()
    elif change == "other_turn": event["turn_id"] = "other"
    elif change == "wrong_act": receipt["act_id"] = "missing"
    elif change == "wrong_event_text": event["payload"]["text"] = "另一个回应"
    elif change == "not_approved": event["payload"]["approved"] = False
    elif change == "wrong_sequence": event["session_sequence"] = 77
    elif change == "wrong_hash": receipt["request_sha256"] = "0" * 64
    elif change == "truncated": receipt["request_text"] = request[-5:]
    elif change == "missing_stream": receipt.pop("source_stream_id")
    elif change == "duplicate_event": session["agent_events"].append(deepcopy(event))
    elif change == "duplicate_act": turn["conversation_acts"].append(deepcopy(act))
    elif change == "changed_transcript": text = text.replace("FluxDB", "FlexDB")
    elif change == "unicode_hash": receipt["request_sha256"] = "字" * 64
    elif change == "bytes_hash": receipt["request_sha256"] = receipt["request_sha256"].encode()
    elif change == "null_hash": receipt["request_sha256"] = None
    elif change == "object_hash": receipt["request_sha256"] = {"hash": receipt["request_sha256"]}
    elif change == "malformed_act": receipt["act_id"] = {"invalid": True}
    elif change == "malformed_payload": event["payload"] = "invalid"
    result = conversation_input_context(session, turn, text)
    assert result["current_speech"] == text and result["current_speech_start"] == 0
    assert result["responded_exchanges"] == []


def test_a_new_identical_question_is_not_consumed_using_an_old_receipt():
    request = "FluxDB 是什么？"
    session, turn = _session([(request, "question_clarification", "它是数据库。")])
    text = request + "刚才没听清。" + request
    result = conversation_input_context(session, turn, text)
    assert result["current_speech"] == text and result["responded_exchanges"] == []


def test_legacy_receipts_without_kind_still_supply_exact_conversation_context():
    request = "请重读一下题目。"
    session, turn = _session([(request, "repeat", "你怎样封装客户端？")])
    turn["reception_exchanges"][0].pop("kind")
    result = conversation_input_context(session, turn, request + "这题不会。")
    assert result["current_speech"] == "这题不会。"
    assert "kind" not in result["responded_exchanges"][0]


def test_receipts_are_ordered_by_selection_and_never_reach_other_turns():
    session, turn = _session([("什么是 FluxDB？", "question_clarification", "它是数据库。"),
                             ("我想一下。", "wait", "好的。")])
    turn["reception_exchanges"].reverse()
    text = "什么是 FluxDB？我想一下。我讲完了。"
    assert conversation_input_context(session, turn, text)["current_speech"] == "我讲完了。"
    session["current_turn_id"] = "another_turn"
    assert conversation_input_context(session, turn, text)["current_speech"] == text


def test_projection_is_bounded_and_has_no_turn_rubrics_or_private_session_fields():
    requests = [("请解释概念%s。" % i, "question_clarification", "概念%s的基本含义。" % i) for i in range(9)]
    session, turn = _session(requests)
    session["private_token"] = "secret"
    turn["question_snapshot"] = {"standard_answer": "private answer"}
    text = "".join(request[0] for request in requests) + "我不会这题。"
    result = conversation_input_context(session, turn, text)
    assert len(result["responded_exchanges"]) == 6
    assert result["current_speech"] == "我不会这题。"
    assert "private_token" not in str(result) and "private answer" not in str(result)


def test_prepared_fingerprint_tracks_receipt_acts_outside_the_short_dialogue_window():
    from app.services.interviews import InterviewService
    requests = [("请解释概念%s。" % i, "question_clarification", "概念%s的基本含义。" % i) for i in range(6)]
    session, turn = _session(requests)
    before = InterviewService._prepared_context_fingerprint(session, turn)
    turn["conversation_acts"][0]["text"] = "第一条选中解释已经变更。"
    assert InterviewService._prepared_context_fingerprint(session, turn) != before
    # Approval-only acts never participated in the source-linked input context.
    unchanged = InterviewService._prepared_context_fingerprint(session, turn)
    turn["conversation_acts"].append({"act_id": "unselected", "turn_id": turn["id"], "text": "未选用内容"})
    assert InterviewService._prepared_context_fingerprint(session, turn) == unchanged


@pytest.mark.anyio
async def test_nonprepared_authoritative_understanding_gets_input_context_without_new_permissions(monkeypatch):
    from test_prepared_decision_binding import _setup
    service, final = _setup()
    request = "幂等是什么意思？"
    synthetic, receipt_turn = _session([(request, "question_clarification", "相同操作重复执行保持结果一致。")])
    with service.persistence.transaction("org_default") as transaction:
        stored = transaction.interview_sessions.get("iv_binding")
        turn = stored["turns"][0]
        for act in receipt_turn["conversation_acts"]:
            act["turn_id"] = turn["id"]
        for event in synthetic["agent_events"]:
            event["turn_id"] = turn["id"]
        turn.update(conversation_acts=receipt_turn["conversation_acts"], reception_exchanges=receipt_turn["reception_exchanges"])
        stored["agent_events"] = synthetic["agent_events"]
        transaction.interview_sessions.update(stored, expected_version=stored["version"])
    captured = []
    class SyntheticInspectionFinished(Exception):
        pass
    async def understand(utterance, turn, session):
        captured.append((utterance, turn, session))
        raise SyntheticInspectionFinished()
    monkeypatch.setattr(service.conversation, "understand", understand)
    before = service.get_interview("iv_binding")
    with pytest.raises(SyntheticInspectionFinished):
        await service._accept_authoritative_transcript("iv_binding", {
            "turn_id": "turn_binding", "final_transcript": request + final.text,
            "audio_uri": "private-file://synthetic-recording", "transcript_source": "server_batch",
            "stt_provider": {"provider_id": "synthetic"},
        })
    utterance, _, context_session = captured[0]
    assert utterance.text == request + final.text and utterance.authoritative and utterance.is_final
    assert context_session["_interviewer_context"]["input_context"]["current_speech"] == final.text
    assert "_semantic_first" not in context_session and "_answer_completion_confirmed" not in context_session
    assert service.get_interview("iv_binding") == before
