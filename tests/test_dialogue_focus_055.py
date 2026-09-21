"""Synthetic multi-round context: no private interview text or media replay."""
import json

import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.validation import StructuredResponseValidationError
from app.services.dialogue_focus import question_for_repeat
from app.services.interviewer_supervisor.context import conversation_memory
from app.services.interviews import InterviewService
from app.repositories.memory import InMemoryStore
from test_prepared_turn_decision import Gateway, _service


def test_repeat_uses_current_child_and_ignores_confirmation_and_repeats():
    turn = {"id": "child", "question_spoken_text": "缓存如何失效？", "conversation_acts": [
        {"act_id": "q", "act_type": "followup", "text": "缓存如何失效？"},
        {"act_id": "r", "act_type": "repeat", "text": "好的，我再说一遍。缓存如何失效？"},
        {"act_id": "c", "act_type": "supplement_check", "text": "还有补充吗？"},
    ]}
    assert question_for_repeat(turn) == "缓存如何失效？"


def test_persisted_focus_survives_refresh_and_legacy_untracked_question_can_be_repeated():
    turn = {"id": "root", "question_spoken_text": "请介绍项目。", "conversation_acts": [
        {"act_id": "q", "act_type": "question", "text": "请介绍项目。"},
        {"act_id": "detail", "turn_id": "root", "act_type": "conversation_acknowledgement",
         "prompt_version": "conversation_reception.v2", "text": "缓存如何失效？"},
        {"act_id": "c", "act_type": "supplement_check", "text": "还有补充吗？"},
    ]}
    assert question_for_repeat(json.loads(json.dumps(turn))) == "缓存如何失效？"
    turn["dialogue_focus"] = {"act_id": "clarified"}
    turn["conversation_acts"].append({"act_id": "clarified", "turn_id": "root",
        "act_type": "conversation_acknowledgement", "text": "这里是在问更新后缓存的处理方式。"})
    assert question_for_repeat(json.loads(json.dumps(turn))) == "这里是在问更新后缓存的处理方式。"
    # Focus from another turn is never eligible.
    turn["conversation_acts"][-1]["turn_id"] = "other"
    assert question_for_repeat(turn) == "缓存如何失效？"


def test_reused_clarification_focus_wins_over_list_insertion_order():
    turn = {"id": "root", "question_spoken_text": "主问题", "dialogue_focus": {"act_id": "a"},
            "conversation_acts": [
                {"act_id": "a", "turn_id": "root", "act_type": "clarification", "text": "澄清A"},
                {"act_id": "b", "turn_id": "root", "act_type": "clarification", "text": "澄清B"},
            ]}
    assert question_for_repeat(turn) == "澄清A"


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["interview_dialogue", "continue", "wait", "presence"])
async def test_social_reply_cannot_smuggle_an_untracked_technical_question(kind):
    gateway = Gateway({"kind": kind, "confidence": .96, "evidence_id": "E1",
                       "reply_text": "接下来能说说你的缓存失效方案吗？"})
    with pytest.raises(StructuredResponseValidationError, match="Reception") as err:
        await _service(gateway).classify_reception("这一部分讲完了。", "org_default")
    assert err.value.reason_code == "reception_cannot_ask_question"
    assert gateway.requests[0].metadata["prompt_version"] == "conversation_reception.v3"


def test_reception_routes_completion_and_repeat_to_server_without_a_new_probe():
    prompt = prompt_contract("conversation_reception", {"text": "刚才的追问再说一遍。"})
    system = prompt.messages[0].content
    assert "没有考察追问权限" in system and "没有补充、下一题时用other" in system
    assert "最近提出的问题" in system


def test_memory_keeps_recent_interviewer_words_and_candidate_tail():
    session = {"turns": [{"id": "root", "question_id": "question", "question_spoken_text": "主问题",
        "utterances": [{"utterance_id": "u", "text": "早" * 3000 + "这一部分已经讲完。", "created_at": "03"}],
        "conversation_acts": [{"act_id": "a", "text": "请澄清刚才的术语。", "created_at": "02"}],
    }], "agent_events": [{"type": "conversation.act.selected", "turn_id": "root", "session_sequence": 1,
        "occurred_at": "02", "payload": {"act_id": "a", "text": "请澄清刚才的术语。", "approved": True}}]}
    dialogue = conversation_memory(session)["recent_dialogue"]
    assert dialogue[-1]["text"].endswith("这一部分已经讲完。")
    assert len(dialogue[-1]["text"]) == 1600
    assert dialogue[-2]["act_id"] == "a"


def test_candidate_rest_shows_parent_and_separate_answers_but_never_future_or_rubric():
    service = InterviewService(InMemoryStore())
    with service.persistence.transaction("org_default") as transaction:
        session = transaction.interview_sessions.add({
            "id": "iv_055", "organization_id": "org_default", "status": "in_progress",
            "current_turn_id": "child", "candidate": {"name": "合成候选人"},
            "created_at": "2026-09-17T00:00:00Z", "updated_at": "2026-09-17T00:00:00Z",
            "turns": [
                {"id": "root", "order": 1, "status": "evaluating", "question_spoken_text": "主问题",
                 "question_snapshot": {"standard_answer": "SECRET"}},
                {"id": "child", "order": 2, "status": "asking", "is_followup": True,
                 "parent_turn_id": "root", "root_turn_id": "root", "followup_depth": 1,
                 "question_spoken_text": "子问题"},
                {"id": "future", "order": 3, "status": "pending", "question_spoken_text": "FUTURE"}],
            "answers": [{"id": "a", "turn_id": "root", "final_transcript": "已提交主回答",
                         "audio_uri": "PRIVATE_AUDIO", "evaluation_status": "pending"}],
        })
    result = service.get_candidate_interview(session["id"], service._candidate_session_token(session))
    assert result["turns"][0]["question_spoken_text"] == "主问题"
    assert result["turns"][1]["parent_turn_id"] == "root"
    assert result["answers"][0]["final_transcript"] == "已提交主回答"
    assert not any(secret in json.dumps(result) for secret in ["SECRET", "FUTURE", "PRIVATE_AUDIO"])
