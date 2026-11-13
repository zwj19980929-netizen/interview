"""A selected explanation must never replace the active parent/child question."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.domain.interview_agent import ClientSignal
from app.services.dialogue_focus import question_for_repeat, clarification_for_repeat, text_for_repeat
from test_expression_stale_fence import _harness, _mutate


def _turn(child=False):
    return {
        "id": "child" if child else "root", "is_followup": child,
        "root_turn_id": "root", "parent_turn_id": "root" if child else None,
        "question_spoken_text": "请求重复到达时如何处理？" if child else "介绍一个项目。",
        "dialogue_focus": {"act_id": "explanation"},
        "conversation_acts": [{"act_id": "explanation", "turn_id": "child" if child else "root",
            "act_type": "conversation_acknowledgement", "text": "幂等指多次同样的操作产生相同的效果。"}],
    }


@pytest.mark.parametrize("child", [False, True])
def test_formal_question_and_explanation_remain_distinct_after_roundtrip(child):
    turn = json.loads(json.dumps(_turn(child)))
    assert question_for_repeat(turn) == turn["question_spoken_text"]
    assert text_for_repeat(turn) == turn["question_spoken_text"]
    assert text_for_repeat(turn, target="last_clarification") == turn["conversation_acts"][0]["text"]
    assert text_for_repeat(turn, target="unknown") == turn["question_spoken_text"]


def test_cross_turn_stale_focus_cannot_repeat_another_turns_explanation():
    turn = _turn(True)
    turn["conversation_acts"][0]["turn_id"] = "root"
    assert clarification_for_repeat(turn) == ""
    assert text_for_repeat(turn, target="last_clarification") == turn["question_spoken_text"]


@pytest.mark.parametrize("child", [False, True])
def test_actual_repeat_button_uses_formal_question_after_multiple_explanations(child):
    async def scenario():
        runtime, channel, _, _ = _harness()
        turn = _turn(child)
        for index in range(8):
            turn["conversation_acts"].append({"act_id": f"later_{index}", "turn_id": turn["id"],
                "act_type": "clarification", "text": f"请澄清之前的术语{index}。"})
        turn["dialogue_focus"] = {"act_id": "later_7"}
        def seed(session):
            session["turns"] = [turn]
            session["current_turn_id"] = turn["id"]
        _mutate(runtime, channel, seed)
        channel._select_act = AsyncMock()
        await channel._repeat_current(ClientSignal(type="question.repeat", turn_id=turn["id"],
            idempotency_key="synthetic-repeat"))
        assert channel._select_act.await_args.kwargs["text"] == turn["question_spoken_text"]
        assert channel._select_act.await_args.kwargs["turn_id"] == turn["id"]
        assert not runtime.interviews.get_interview(channel.interview_id)["answers"]
    asyncio.run(scenario())


def test_full_understanding_memory_retains_active_turn_before_future_plan():
    from app.services.interviewer_supervisor.context import conversation_memory
    active = {"id": "active", "question_spoken_text": "当前题", "utterances": [
        {"utterance_id": "current", "text": "刚才解释的术语再讲一下。", "created_at": "02"}]}
    future = [{"id": f"future{i}", "question_spoken_text": "未提问", "utterances": [
        {"utterance_id": f"future_u{i}", "text": "FUTURE_NOT_CONTEXT", "created_at": "03"}]}
        for i in range(10)]
    memory = conversation_memory({"current_turn_id": "active", "turns": [active, *future]})
    assert any(item.get("utterance_id") == "current" for item in memory["recent_dialogue"])
    assert "FUTURE_NOT_CONTEXT" not in json.dumps(memory)
