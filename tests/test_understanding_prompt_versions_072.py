"""Versioned ambiguity recovery keeps source schemas and historical reads intact."""
from copy import deepcopy

import pytest
from pydantic import TypeAdapter

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.semantic_turn import semantic_content_correction_instruction
from app.core.prompt.validation import validate_structured_response
from app.domain.interview_agent import TurnUnderstanding
from app.model_gateway import capabilities as cap
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import ChatJSONRequest
from app.repositories.memory import InMemoryStore


BRANCHES = [
    # semantic, confirmed, optional context, exit confirmation, resolved input,
    # old understanding version, old decision version
    (True, False, False, False, False, 74, 105),
    (True, True, False, False, False, 75, 106),
    (True, False, True, False, False, 76, 107),
    (True, True, True, False, False, 77, 108),
    (True, False, False, True, False, 78, 109),
    (True, True, False, True, False, 79, 110),
    (True, False, True, True, False, 80, 111),
    (True, True, True, True, False, 81, 112),
    (False, False, False, False, False, 82, 113),
    (False, True, False, False, False, 83, 114),
    (False, False, False, False, True, 84, 115),
    (False, True, False, False, True, 85, 116),
]


def _context(semantic, confirmed, optional, exit_confirmation, resolved):
    text = "我用幂等键防止重复提交。"
    interviewer = {}
    if optional:
        interviewer.update(skill={}, company_context={"available": False})
    if exit_confirmation:
        interviewer["exit_confirmation"] = {"status": "awaiting_reply"}
    if resolved:
        interviewer["input_context"] = {
            "version": "conversation_input_context.v1",
            "current_speech_start": 0, "current_speech": text,
            "responded_exchanges": [{"request_text": "请再读一遍。", "reply_text": "如何防止重复提交？"}],
        }
    return {
        "transcript": text, "capability_points": ["幂等键", "重试"],
        "semantic_first": semantic, "completion_confirmed": confirmed,
        "interviewer_context": interviewer,
    }


@pytest.mark.anyio
@pytest.mark.parametrize("purpose", ["interview_turn_understanding", "interview_turn_decision"])
@pytest.mark.parametrize("branch", BRANCHES)
async def test_all_affected_branches_are_versioned_validated_and_audited_with_history(purpose, branch):
    semantic, confirmed, optional, exit_confirmation, resolved, old_understanding, old_decision = branch
    context = _context(semantic, confirmed, optional, exit_confirmation, resolved)
    contract = prompt_contract(purpose, context)
    old_number = old_understanding if purpose == "interview_turn_understanding" else old_decision
    old_version = f"{purpose}.v{old_number}"
    assert contract.version == f"{purpose}.v{old_number + 24}"
    messages = "\n".join(message.content for message in contract.messages)
    assert ("semantic_controls.v3" in messages) is semantic
    assert "semantic_controls.v2" not in messages
    schema = contract.response_schema["properties"].get("understanding", contract.response_schema)
    assert ("turn_intent" in schema["properties"]) is semantic
    assert schema["additionalProperties"] is False
    assert schema["properties"]["evidence_ids"]["items"]["enum"] == ["E1"]
    assert schema["properties"]["covered_point_ids"]["items"]["enum"] == ["P1", "P2"]

    versions = TypeAdapter(TurnUnderstanding.model_fields["prompt_version"].annotation)
    store = InMemoryStore()
    gateway = ModelGateway(store)
    results = []
    for version in (old_version, contract.version):
        assert versions.validate_python(version) == version
        response = await gateway.invoke(cap.LLM_CHAT_JSON, ChatJSONRequest(
            organization_id="org_default", purpose="interview_turn_understanding",
            messages=contract.messages, json_schema=contract.response_schema,
            metadata={"prompt_version": version, "transcript": context["transcript"],
                      "capability_points": context["capability_points"]},
        ))
        validate_structured_response(response.data, contract.response_schema)
        results.append(response.data)
        assert store.model_invocations[-1]["prompt_version"] == version
        assert store.model_invocations[-1]["status"] == "success"
        if semantic:
            understanding = response.data.get("understanding", response.data)
            assert understanding["turn_intent"] == "answering"
            assert understanding["completion_basis"] is None
    assert results[0] == results[1], "Registering a new version must preserve the mock wire format"


@pytest.mark.parametrize("purpose", ["interview_turn_understanding", "interview_turn_decision"])
@pytest.mark.parametrize("delivered", [False, True])
@pytest.mark.parametrize("reason,required_guidance", [
    ("completion_has_unresolved_ambiguity", [
        "知识缺失属于missing", "上下文已能唯一确定的音近词", "真实歧义不能为通过校验而删除",
        "保留可靠claims", "不能重复追问同一疑点", "只有播完后的新回复明确坚持结束",
        "不得编造送达记录", "将本题结束改成退出整场",
    ]),
    ("unresolved_answer_contract_invalid", [
        "不是知识缺失、一般低置信度", "保留可靠claims", "completion_basis只能引用播完后的新回复",
        "不能复用澄清前说完", "若疑点已解决则正常answer", "仍须独立退出确认",
    ]),
])
def test_targeted_corrections_preserve_schema_sources_and_require_actual_clarification_delivery(
    purpose, delivered, reason, required_guidance,
):
    context = _context(True, False, True, False, False)
    context["transcript"] = "我会记录序号后放入蓝桥。我说完了。"
    if delivered:
        context["interviewer_context"]["content_clarification"] = {
            "version": "content_clarification.v1", "request_text": "我会记录序号后放入蓝桥。",
            "focus_quote": "蓝桥", "reply_text": "我说完了。",
            "question_act_id": "act_synthetic_clarification", "delivered": True,
        }
    before = deepcopy(context)
    original = prompt_contract(purpose, context)
    corrected = prompt_contract(purpose, {**context, "correction_reason": reason})
    assert context == before
    assert original.version == corrected.version
    assert original.response_schema == corrected.response_schema
    corrections = [message.content for message in corrected.messages
                   if message.content.startswith("上一轮结果未通过合同校验")]
    assert len(corrections) == 1
    correction = corrections[0]
    assert reason in correction and semantic_content_correction_instruction(reason) in correction
    for required in [*required_guidance, "clarification_request+finish_answer+clarify",
                     "confidence低于0.65", "followup_allowed=false", "completion_basis=null"]:
        assert required in correction
    assert context["transcript"] not in correction and "蓝桥" not in correction
    messages = "\n".join(message.content for message in corrected.messages)
    assert ("本轮内容澄清阶段：服务端已提供已送达的content_clarification记录。" in messages) is delivered
