"""Synthetic turn handoff and fresh supplementary confirmation evidence."""
from copy import deepcopy
import json

import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.validation import StructuredResponseValidationError
from test_formal_followup_056 import completed_answer
from test_prepared_turn_decision import Gateway, _input, _service


def test_inviting_clarification_hands_the_floor_to_formal_decision():
    context = {"transcript": "我会按请求标识去重。有没有哪些细节您想继续了解？",
               "capability_points": ["请求去重"], "semantic_first": True}
    contract = prompt_contract("interview_turn_decision", context)
    text = "\n".join(message.content for message in contract.messages)
    assert "交还话轮" in text and "邀请核实" in text
    assert "不是要求结束话题" in text and "不为凑数量" in text
    reception = prompt_contract("conversation_reception", {"text": "有没有哪些细节您想继续了解？"})
    reception_text = "\n".join(message.content for message in reception.messages)
    assert "邀请核实" in reception_text and "other" in reception_text
    assert "不要用interview_dialogue" in reception_text


@pytest.mark.anyio
@pytest.mark.parametrize("selection", ["selected", "sufficient", "budget", "rejected"])
async def test_invitation_retains_answer_and_respects_real_followup_decision(selection):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": "我会记录恢复位置。有没有哪些细节您想继续了解？"})
    interview["_semantic_first"] = True
    data = completed_answer(selected=selection != "sufficient")
    if selection == "rejected":
        data["followup"]["question_text"] = "何时记录？怎样恢复？"
    if selection == "budget":
        turn["allow_followup"] = False
        data = data["understanding"]
    service = _service(Gateway(data, *([{"selected": True, "question_text": "何时？怎样？"}]
                                      if selection == "rejected" else [])))
    understanding, decision = await service.prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.turn_intent == "finish_answer"
    assert understanding.followup_allowed and understanding.claims
    assert understanding.completion_basis.evidence_quotes == ["有没有哪些细节您想继续了解？"]
    assert service.can_complete_without_confirmation(understanding, utterance.text)
    assert decision["reason"] == {"selected": "claim_detail_verification", "sufficient": "model_declined",
        "budget": "followup_disabled", "rejected": "followup_repair_rejected"}[selection]


@pytest.mark.anyio
async def test_supplement_classifier_uses_bounded_server_context_but_only_fresh_evidence():
    gateway = Gateway({"intent": "finish", "confidence": .95, "evidence_id": "E1"})
    result = await _service(gateway).classify_supplement_reply("是的。", "org_default",
        prior_confirmation=True, preceding_text="已讲完。" * 400, question="确认完成吗？" * 200)
    request = gateway.requests[0]
    context = json.loads(request.messages[-1].content)
    assert context["prior_confirmation"] is True
    assert len(context["preceding_text"]) <= 1000 and len(context["question"]) <= 600
    assert context["evidence"] == {"E1": "是的。"}
    assert request.metadata["prompt_version"] == "supplement_reply.v5"
    assert result["evidence_quote"] == "是的。"


@pytest.mark.anyio
async def test_previous_confirmation_cannot_be_cited_as_new_finish_authority():
    gateway = Gateway({"intent": "finish", "confidence": .95, "evidence_id": "E2"})
    with pytest.raises(StructuredResponseValidationError):
        await _service(gateway).classify_supplement_reply("对。", "org_default",
            prior_confirmation=True, preceding_text="已经讲完了。")


def test_supplement_context_disambiguates_confirmation_without_renewing_old_permission():
    contract = prompt_contract("supplement_reply", {"reply": "对。", "prior_confirmation": True,
        "preceding_text": "没有要补充的了。", "question": ""})
    text = contract.messages[0].content
    assert "不是当前结束许可" in text and "当前新回复" in text
    assert "确认对象" in text and "无法确定" in text
    no_context = prompt_contract("supplement_reply", {"reply": "对。"})
    data = json.loads(no_context.messages[-1].content)
    assert data["prior_confirmation"] is False and data["preceding_text"] == ""
    assert "缺少上下文" in text


@pytest.mark.parametrize("purpose", ["interview_turn_decision", "controlled_followup"])
def test_followup_selects_one_focus_before_wording_the_final_question(purpose):
    from app.core.prompt.contracts import FOLLOWUP_SINGLE_FOCUS_INSTRUCTION
    contract = prompt_contract(purpose, {"semantic_first": True, "transcript": "我记录请求号。",
        "capability_points": ["幂等处理"]})
    assert contract.messages[-1].role == "system"
    assert contract.messages[-1].content == FOLLOWUP_SINGLE_FOCUS_INSTRUCTION
    assert "rationale只说明这一个焦点" in contract.messages[-1].content
    assert "不给答案选项" in contract.messages[-1].content
    schema = contract.response_schema["properties"].get("followup", contract.response_schema)
    assert "不能罗列多个" in schema["properties"]["rationale"]["description"]
    order = list(schema["properties"])
    assert order == schema["required"]
    assert order.index("rationale") < order.index("question_text")
    assert order.index("evidence_id" if purpose == "interview_turn_decision" else "evidence_quote") < order.index("rationale")
    assert "虽然只有一个问号" in contract.messages[-1].content


@pytest.mark.parametrize("purpose", ["interview_turn_understanding", "interview_turn_decision"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_semantic_schema_forms_current_intent_before_selecting_action(purpose, confirmed):
    contract = prompt_contract(purpose, {"semantic_first": True, "completion_confirmed": confirmed,
        "transcript": "我先记录恢复位置。等一下，我还需要考虑失败情况。", "capability_points": ["恢复"]})
    schema = contract.response_schema["properties"].get("understanding", contract.response_schema)
    order = list(schema["properties"])
    assert order == schema["required"]
    assert order.index("answer_content") < order.index("intent")
    assert order.index("turn_intent") < order.index("suggested_action")
    assert order[-1] == "suggested_action"


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [False, True])
@pytest.mark.parametrize("violation", ["next", "followup"])
async def test_thinking_control_retry_receives_exact_reason_and_keeps_claims(composite, violation, caplog):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "等一下，我还需要考虑失败情况。"})
    interview["_semantic_first"] = True
    corrected = completed_answer(selected=False)
    corrected["understanding"].update(turn_intent="thinking", answer_content="partial",
        completion_basis=None, followup_allowed=False, suggested_action="continue_listening")
    invalid = deepcopy(corrected)
    invalid["understanding"].update({"suggested_action": "next"} if violation == "next" else {"followup_allowed": True})

    class CorrectingGateway(Gateway):
        async def invoke(self, capability, request):
            if self.requests:
                text = "\n".join(message.content for message in request.messages)
                assert "semantic_control_contract_invalid" in text
                assert "本次需纠正语义控制字段" in text
                assert "不能从intent=answer推导为next" in text
            return await super().invoke(capability, request)

    gateway = CorrectingGateway(*(value if composite else value["understanding"] for value in (invalid, corrected)))
    service = _service(gateway)
    with caplog.at_level("WARNING", logger="app.services.conversation_understanding"):
        if composite:
            understanding, followup = await service.prepare_decision(utterance, turn, interview)
            assert not followup["selected"]
        else:
            understanding = await service.understand(utterance, turn, interview)
    assert understanding.problem is None and understanding.claims
    assert understanding.turn_intent == "thinking" and understanding.suggested_action == "continue_listening"
    assert not understanding.followup_allowed and understanding.completion_basis is None
    assert understanding.claims[0].evidence_quote == _input()[0].text
    assert len(gateway.requests) == 2
    assert "reason=semantic_control_contract_invalid" in caplog.text
    assert utterance.text not in caplog.text


@pytest.mark.anyio
async def test_repeated_control_violation_stays_rejected_with_precise_reason():
    utterance, turn, interview = _input()
    interview["_semantic_first"] = True
    data = completed_answer(selected=False)
    data["understanding"].update(turn_intent="thinking", completion_basis=None, followup_allowed=False)
    gateway = Gateway(data, data)
    understanding, followup = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem and understanding.problem.reason_code == "semantic_control_contract_invalid"
    assert understanding.suggested_action == "pause" and not understanding.claims
    assert followup["reason"] == "understanding_unavailable" and not followup["selected"]
    assert len(gateway.requests) == 2
