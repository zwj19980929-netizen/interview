"""Dialogue context comes from selected events, not approval insertion order."""
from copy import deepcopy

from app.services.interviewer_supervisor.context import conversation_memory


def act(identifier, text, turn_id="root"):
    return {"act_id": identifier, "turn_id": turn_id, "text": text, "created_at": "00"}


def event(sequence, item, *, occurred_at=None, turn_id="root", **payload):
    return {"type": "conversation.act.selected", "session_sequence": sequence,
            "occurred_at": occurred_at or str(sequence).zfill(2), "turn_id": turn_id,
            "payload": {"act_id": item["act_id"], "text": item["text"], "approved": True, **payload}}


def session_with(*acts, events=(), utterances=()):
    return {"turns": [{"id": "root", "question_spoken_text": "主问题",
        "conversation_acts": list(acts), "utterances": list(utterances)}], "agent_events": list(events)}


def test_reused_act_keeps_actual_a_b_a_sequence_and_unselected_approval_is_absent():
    a, b, never = act("a", "澄清A"), act("b", "澄清B"), act("never", "从未选中")
    # Store order and approval timestamps do not establish speaking order.
    session = session_with(a, b, never, events=[event(3, a), event(1, a), event(2, b)])
    original = deepcopy(session)
    history = conversation_memory(session)["recent_dialogue"]
    assert [item["text"] for item in history] == ["澄清A", "澄清B", "澄清A"]
    assert [item["created_at"] for item in history] == ["01", "02", "03"]
    assert session == original


def test_selected_events_and_candidate_tail_merge_by_exchange_time():
    a, b = act("a", "澄清A"), act("b", "澄清B")
    session = session_with(a, b, events=[event(1, a), event(3, b), event(5, a)], utterances=[
        {"utterance_id": "u1", "text": "第一次回复", "created_at": "02"},
        {"utterance_id": "u2", "text": "早" * 3000 + "这一部分已经讲完。", "created_at": "04"},
    ])
    history = conversation_memory(session)["recent_dialogue"]
    assert [item["speaker"] for item in history] == ["interviewer", "candidate", "interviewer", "candidate", "interviewer"]
    assert history[3]["text"].endswith("这一部分已经讲完。") and len(history[3]["text"]) == 1600
    assert history[-1]["act_id"] == "a"


def test_only_four_latest_selected_events_and_two_latest_utterances_are_used():
    a = act("a", "请继续")
    session = session_with(a, events=[event(index, a) for index in range(1, 10)], utterances=[
        {"utterance_id": str(index), "text": str(index), "created_at": str(index).zfill(2)}
        for index in range(1, 5)
    ])
    history = conversation_memory(session)["recent_dialogue"]
    assert [item["created_at"] for item in history if item["speaker"] == "interviewer"] == ["06", "07", "08", "09"]
    assert [item["utterance_id"] for item in history if item["speaker"] == "candidate"] == ["3", "4"]


def test_missing_history_preserves_only_root_question_and_real_candidate():
    session = session_with(act("never", "审批但未说"), utterances=[
        {"utterance_id": "u", "text": "真实回复", "created_at": "03"}])
    history = conversation_memory(session)["recent_dialogue"]
    assert [item["text"] for item in history] == ["主问题", "真实回复"]
    assert all(item.get("act_id") != "never" for item in history)


def test_cross_turn_unapproved_unknown_and_text_mismatched_events_are_excluded():
    a, other = act("a", "当前话语"), act("other", "另一题的私有话语", turn_id="other")
    session = session_with(a, other, events=[
        event(1, a, turn_id="other"), event(2, a, approved=False), event(3, act("unknown", "未知")),
        event(4, a, text="不是批准的文字"), event(5, other),
    ])
    assert conversation_memory(session)["recent_dialogue"] == []


def test_selection_sequence_wins_when_wall_clock_moves_backwards():
    a, b = act("a", "澄清A"), act("b", "澄清B")
    session = session_with(a, b, events=[event(3, a, occurred_at="01"),
        event(2, b, occurred_at="02"), event(1, a, occurred_at="03")])
    assert [item["act_id"] for item in conversation_memory(session)["recent_dialogue"]] == ["a", "b", "a"]


def test_reusable_projection_exposes_only_dialogue_fields_and_supports_smaller_limits():
    from app.services.dialogue_history import recent_turn_dialogue
    a, b = act("a", "澄清A"), act("b", "澄清B")
    a["target_capability_points"] = ["DO_NOT_EXPOSE"]
    session = session_with(a, b, events=[event(1, a), event(2, b), event(3, a)], utterances=[
        {"utterance_id": "u1", "text": "早先回复", "created_at": "00", "audio_uri": "PRIVATE"},
        {"utterance_id": "u2", "text": "新的回复已讲完", "created_at": "04", "audio_uri": "PRIVATE"}])
    result = recent_turn_dialogue(session, session["turns"][0], max_acts=2, max_utterances=1, candidate_limit=3)
    assert [item.get("act_id") for item in result[:2]] == ["b", "a"]
    assert result[-1]["text"] == "已讲完"
    assert all(set(item) <= {"speaker", "turn_id", "text", "created_at", "act_id", "utterance_id"} for item in result)
    assert "DO_NOT_EXPOSE" not in str(result) and "PRIVATE" not in str(result)
