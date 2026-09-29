"""A prior stop request or normal answer completion is never exit consent."""
from copy import deepcopy
from dataclasses import replace

import pytest

from app.core.errors import ApiError
from app.core.prompt.candidate_exit import EXIT_CONFIRMATION_SPEECH, EXIT_CONFIRMATION_SPEECH_VERSION
from app.services.candidate_exit_confirmation import validated_exit_context, text_hash
from test_prepared_decision_binding import _setup, _prepared


def exchange(decision="confirm"):
    request = "我会用幂等键处理重复请求。我不想继续面试了。"
    reply = "确认结束。" if decision == "confirm" else "不结束，我继续。"
    text = request + reply
    act = {"act_id": "ask_exit", "turn_id": "turn", "text": EXIT_CONFIRMATION_SPEECH,
           "prompt_version": EXIT_CONFIRMATION_SPEECH_VERSION}
    turn = {"id": "turn", "conversation_acts": [act]}
    session = {"id": "interview", "current_turn_id": "turn", "agent_events": [
        {"type": "conversation.act.selected", "turn_id": "turn",
         "payload": {"act_id": "ask_exit", "approved": True, "text": EXIT_CONFIRMATION_SPEECH}}]}
    context = {"version": EXIT_CONFIRMATION_SPEECH_VERSION, "interview_id": "interview", "turn_id": "turn",
               "capture_id": "capture", "question_act_id": "ask_exit", "question": EXIT_CONFIRMATION_SPEECH,
               "request_text": request, "reply_text": reply, "decision": decision, "transcript_sha256": text_hash(text)}
    return session, turn, text, context


@pytest.mark.parametrize("mutation", ["session", "turn", "act", "unselected", "unapproved", "ordinary_question",
                                     "changed_prefix", "changed_reply", "late_retraction", "extra_field", "bad_hash"])
def test_exchange_cannot_borrow_another_source_or_ignore_new_words(mutation):
    session, turn, text, context = exchange()
    if mutation == "session": context["interview_id"] = "other"
    elif mutation == "turn": context["turn_id"] = "other"
    elif mutation == "act": context["question_act_id"] = "other"
    elif mutation == "unselected": session["agent_events"].clear()
    elif mutation == "unapproved": session["agent_events"][0]["payload"]["approved"] = False
    elif mutation == "ordinary_question": turn["conversation_acts"][0]["prompt_version"] = "supplement_confirmation.v2"
    elif mutation == "changed_prefix": text = text.replace("幂等键", "重试")
    elif mutation == "changed_reply": text = text.replace("确认结束", "不确认结束")
    elif mutation == "late_retraction": text += "不对，我继续。"
    elif mutation == "extra_field": context["force"] = True
    elif mutation == "bad_hash": context["transcript_sha256"] = "字" * 64
    with pytest.raises(ApiError) as error:
        validated_exit_context(session, turn, text, context)
    assert error.value.code == "EXIT_CONFIRMATION_REQUIRED"


def test_withdrawal_cancellation_remains_history_when_candidate_answers_or_requests_exit_again():
    session, turn, text, context = exchange("continue")
    before = deepcopy(context)
    for tail in ["", "我会记录重试次数。", "我改变主意，还是结束面试吧。"]:
        assert validated_exit_context(session, turn, text + tail, context) == before
    assert context == before


@pytest.mark.anyio
async def test_ordinary_answer_confirmation_cannot_authorize_interview_exit():
    service, final = _setup()
    prepared = await _prepared(service, final)
    prepared = replace(prepared, completion_confirmed=True,
                       understanding=prepared.understanding.model_copy(update={"turn_intent": "stop_interview"}))
    before = service.get_interview("iv_binding")
    with pytest.raises(ApiError) as error:
        service.assert_prepared_streaming_decision(prepared, final, "org_default")
    assert error.value.code == "EXIT_CONFIRMATION_REQUIRED"
    assert service.get_interview("iv_binding") == before


def test_old_committed_stop_without_separate_confirmation_cannot_end_interview():
    service, _ = _setup()
    with service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        session["turns"][0]["current_understanding"] = {"turn_intent": "stop_interview"}
        session["answers"] = [{"id": "answer", "turn_id": "turn_binding", "final_transcript": "我不想面了。"}]
        tx.interview_sessions.update(session, expected_version=session["version"])
    before = service.get_interview("iv_binding")
    assert service.finish_on_candidate_intent("iv_binding") == before
    assert service.get_interview("iv_binding") == before


def test_exit_wait_snapshot_is_current_question_only_and_carries_no_permission():
    from app.services.interview_agent import InterviewAgentRuntime

    session = {"status": "in_progress", "current_turn_id": "turn", "agent_runtime": {
        "supplement_confirmation": {"status": "awaiting_exit_reply", "turn_id": "turn",
                                     "capture_id": "capture", "confirmed": True}}}
    assert InterviewAgentRuntime._supplement_projection(session) == {
        "status": "awaiting_exit_reply", "turn_id": "turn", "capture_id": "capture"}
    session["current_turn_id"] = "other"
    assert InterviewAgentRuntime._supplement_projection(session) is None
