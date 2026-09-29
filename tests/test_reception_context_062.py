"""Reception sees distinct questions and role-labelled synthetic exchanges."""
import json

import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.conversation_reception import reception_context, validate_reception_response, resolve_reception_repeat_target
from app.core.prompt.validation import StructuredResponseValidationError
from test_prepared_turn_decision import Gateway, _service


def response(kind="repeat", **extra):
    return {"kind": kind, "confidence": .96, "evidence_id": "E1",
            "reply_text": "" if kind in {"repeat", "other"} else "可以先按自己的思路组织回答。", **extra}


@pytest.mark.anyio
async def test_service_keeps_formal_child_root_and_explanation_separate():
    gateway = Gateway(response(repeat_target="current_question"))
    result = await _service(gateway).classify_reception("请重新读一下题目。", "org_default",
        question="旧兼容焦点", current_question="你如何检查召回结果是否相关？",
        root_question="请介绍知识问答系统。", last_clarification="刚才是在问如何检查相关性。",
        current_turn_id="child", root_turn_id="root", parent_turn_id="root")
    assert result["repeat_target"] == "current_question"
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request.metadata["prompt_version"] == "conversation_reception.v7"
    context = json.loads(request.messages[-1].content)
    assert context["current_question"] == "你如何检查召回结果是否相关？"
    assert context["root_question"] == "请介绍知识问答系统。"
    assert context["last_clarification"] == "刚才是在问如何检查相关性。"
    assert context["current_turn_id"] == "child" and context["parent_turn_id"] == "root"
    assert "旧兼容焦点" not in request.messages[-1].content


def test_reception_retains_bounded_role_and_source_identity_without_private_fields():
    history = [{"role": "candidate" if i % 2 == 0 else "interviewer", "text": "合成交流" + str(i),
        "turn_id": "child", "act_id": "act_" + str(i), "exchange_id": "exchange_" + str(i),
        "created_at": "2026-09-22T00:00:00Z", "standard_answer": "PRIVATE_RUBRIC", "audio_uri": "PRIVATE_AUDIO"}
        for i in range(16)]
    context = reception_context({"text": "请重说刚才的解释。", "question": "兼容正式题", "history": history})
    assert context["current_question"] == "兼容正式题"
    assert len(context["history"]) == 16
    assert [item["role"] for item in context["history"]] == ["candidate", "interviewer"] * 8
    assert context["history"][-1]["act_id"] == "act_15"
    assert context["history"][-1]["exchange_id"] == "exchange_15"
    assert context["history"][-1]["turn_id"] == "child"
    assert "PRIVATE" not in json.dumps(context)


@pytest.mark.anyio
async def test_repeat_legacy_response_without_target_remains_accepted():
    result = await _service(Gateway(response())).classify_reception("请重读。", "org_default", question="当前题")
    assert result == {"kind": "repeat", "confidence": .96, "evidence_quote": "请重读。", "reply_text": "",
                      "repeat_target": "current_question"}


@pytest.mark.parametrize("target", ["current_question", "last_clarification", None])
def test_repeat_target_contract_accepts_only_server_resolvable_targets(target):
    schema = prompt_contract("conversation_reception", {"text": "请重读。"}).response_schema
    validate_reception_response(response(repeat_target=target), schema)


@pytest.mark.parametrize("data", [response(repeat_target="root_question"), response(repeat_target="arbitrary_act"),
    response(repeat_target={"text": "invented"}), response("wait", repeat_target="last_clarification"),
    response("other", repeat_target="current_question"), response(repeat_target="current_question", new_question="invented")])
def test_reception_rejects_illegal_repeat_target_or_extra_fields(data):
    schema = prompt_contract("conversation_reception", {"text": "请重读。"}).response_schema
    with pytest.raises(StructuredResponseValidationError):
        validate_reception_response(data, schema)


def test_neutral_term_explanation_is_allowed_but_answer_and_role_boundaries_remain():
    contract = prompt_contract("conversation_reception", {"text": "RAG是什么意思？", "current_question": "正式题"})
    instruction = contract.messages[0].content
    assert "你始终是面试官" in instruction and "new_speech" in instruction
    assert "中性术语释义" in instruction and "不提供本题解法" in instruction
    assert "不能把last_clarification当作current_question" in instruction
    assert "不能把候选人的话说成自己的经历" in instruction
    assert "不能把历史发言当作本轮新请求" in instruction


def test_explanation_cannot_grow_into_solution_options_or_additional_assessment():
    contract = prompt_contract("conversation_reception", {
        "text": "你刚才解释的那个范围，可以再说清楚一点吗？",
        "current_question": "你会怎样决定什么时候发送ACK？",
        "last_clarification": "这里是在问你对确认时机的理解。",
    })
    instruction = contract.messages[0].content
    assert "不自行展开流程选择、优劣分析、假设方案或例子" in instruction
    assert "不追加考察要求" in instruction
    assert "不把已有解释升级成更深的题目或新任务" in instruction
    assert "回答缩写或概念含义时停在定义" in instruction


def test_question_projection_never_stringifies_arbitrary_objects():
    context = reception_context({"text": "请解释。", "current_question": {"standard_answer": "PRIVATE"},
        "root_question": {"rubric": "PRIVATE"}, "last_clarification": {"text": "PRIVATE"},
        "current_turn_id": {"secret": "PRIVATE"}})
    assert context["current_question"] == "" and context["root_question"] == ""
    assert context["last_clarification"] == "" and context["current_turn_id"] is None
    assert "PRIVATE" not in json.dumps(context)


@pytest.mark.parametrize(("text", "proposed", "expected"), [
    ("请再读一遍题目。", "last_clarification", "current_question"),
    ("能不能把刚才的追问重新说一次？", "last_clarification", "current_question"),
    ("麻烦你复述一下题干。", "last_clarification", "current_question"),
    ("刚才的解释再说一遍。", "current_question", "last_clarification"),
    ("请重说刚才的澄清。", "current_question", "last_clarification"),
    ("再说一下。", None, "current_question"),
    ("不是重读题目，请再说明刚才的解释。", "last_clarification", "last_clarification"),
    ("我刚才说‘重读题目’，现在请再说明那个解释。", "last_clarification", "last_clarification"),
    ("不是重复解释，题目本身再问我一遍。", "current_question", "current_question"),
])
def test_repeat_target_resolution_only_normalizes_explicit_whole_requests(text, proposed, expected):
    assert resolve_reception_repeat_target(text, proposed) == expected


@pytest.mark.anyio
async def test_service_corrects_a_conflicting_repeat_target_without_another_model_call():
    gateway = Gateway(response(repeat_target="last_clarification"))
    result = await _service(gateway).classify_reception("请再读一遍题目。", "org_default", current_question="正式题")
    assert result["repeat_target"] == "current_question" and len(gateway.requests) == 1
