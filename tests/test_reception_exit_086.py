"""Exit requests are conversation proposals, never answer/termination permits."""
import json

import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.conversation_reception import validate_reception_response
from app.core.prompt.validation import StructuredResponseValidationError
from test_prepared_turn_decision import Gateway, _service


def _request(evidence_id="E1", **changes):
    return {"kind": "stop_interview", "confidence": .98,
            "evidence_id": evidence_id, "reply_text": "", **changes}


def _validate(text, evidence_id="E1", **changes):
    contract = prompt_contract("conversation_reception", {"text": text})
    data = _request(evidence_id, **changes)
    validate_reception_response(data, contract.response_schema, current_speech=text)
    return data


def test_exit_request_is_versioned_silent_and_separate_from_confirmation():
    contract = prompt_contract("conversation_reception", {"text": "今天我想先到这里。"})
    assert contract.version == "conversation_reception.v10"
    assert "stop_interview" in contract.response_schema["properties"]["kind"]["enum"]
    system = contract.messages[0].content
    assert "独立退出确认" in system and "不是退出许可" in system
    assert "引用、假设、否认" in system and "最后有效请求" in system
    assert "结束整场以及公司反问" not in system


@pytest.mark.parametrize("text,evidence_id", [
    ("我想退出面试了，不好意思，面试官。", "E1"),
    ("我们结束面试吧。", "E1"),
    ("我不想继续面试了。", "E1"),
    ("我今天就到这里吧，抱歉。", "E1"),
    ("实在不好意思，今天没法继续参加了。", "E1"),
    ("可以结束这次面试了吗？", "E1"),
    ("I don't want to continue this interview.", "E1"),
    ("我会把状态写入数据库。我想退出面试了。", "E2"),
    ("如果任务失败，我会进行重试。我想退出面试了。", "E2"),
    ("如果任务失败就重试，我想退出面试了。", "E1"),
    ("如果任务失败，我会继续处理队列，我想退出面试了。", "E1"),
    ("如果任务失败则重试，我们结束面试吧。", "E1"),
    ("不退出了，我继续面试。还是算了，我现在想结束整场面试。", "E2"),
    ("客户问‘我们结束面试吧。’我现在想结束整场面试。", "E2"),
    ("我们结束面试吧。谢谢。", "E1"),
])
def test_valid_model_request_is_not_blocked_by_technical_contract_or_earlier_conditions(text, evidence_id):
    assert _validate(text, evidence_id) == _request(evidence_id)


@pytest.mark.parametrize("text,evidence_id", [
    ("我不想退出面试。", "E1"),
    ("不要结束面试。", "E1"),
    ("我只是说这一题不会，不是要结束面试。", "E1"),
    ("如果我退出面试会怎么样？", "E1"),
    ("假设我们结束面试。", "E1"),
    ("比如有人说退出面试。", "E1"),
    ("他说我们结束面试吧。", "E1"),
    ("客户说我们结束面试吧。", "E1"),
    ("“我们结束面试吧。”", "E1"),
    ("客户说‘嗯。我们结束面试吧。’", "E2"),
    ("`我们结束面试吧。`", "E1"),
    ("我刚才说想退出面试。", "E1"),
    ("我们结束面试吧。不对，我继续面试。", "E1"),
    ("我们结束面试吧。不退出了。", "E1"),
    ("我们结束面试吧。还是让我再想一想。", "E1"),
    ("我们结束面试吧，不对，我继续面试。", "E1"),
    ("我们结束面试吧。不对，我现在继续面试。", "E1"),
    ("我们结束面试吧。我改变主意了，我继续。", "E1"),
    ("我们结束面试吧。稍等，我还没说完。", "E1"),
    ("如果明天还有事，我们结束面试吧。", "E1"),
    ("如果没有下一题，我想退出面试了。", "E1"),
    ("字幕识别错了，我没说退出面试。", "E1"),
    ("我们结束面试吧。字幕识别错了，我没说。", "E1"),
    ("这题我讲完了，下一题吧。", "E1"),
    ("先暂停一会儿。", "E1"),
    ("If I quit the interview, what happens?", "E1"),
    ("I do not want to quit this interview.", "E1"),
    ("End this interview. No, I want to continue.", "E1"),
])
def test_conflicting_selected_source_cannot_become_a_withdrawal_proposal(text, evidence_id):
    with pytest.raises(StructuredResponseValidationError) as exc:
        _validate(text, evidence_id)
    assert text not in str(exc.value)
    assert exc.value.reason_code in {"exit_request_source_invalid", "exit_request_conflict"}


@pytest.mark.parametrize("changes,reason", [
    ({"reply_text": "确定，我已经结束面试。"}, "reception_silent_kind_has_reply"),
    ({"reply_text": "你确定退出吗？"}, "reception_silent_kind_has_reply"),
    ({"evidence_id": "E99"}, "enum"),
    ({"repeat_target": "current_question"}, "reception_repeat_target_without_repeat"),
    ({"confirmed": True}, "additional_properties"),
])
def test_exit_proposal_rejects_extra_permissions_wrong_sources_and_generated_confirmation(changes, reason):
    with pytest.raises(StructuredResponseValidationError) as exc:
        _validate("我想结束面试。", **changes)
    assert exc.value.reason_code == reason


def test_exit_request_requires_current_source_even_when_wire_schema_has_valid_id():
    contract = prompt_contract("conversation_reception", {"text": "我想退出面试。"})
    with pytest.raises(StructuredResponseValidationError) as exc:
        validate_reception_response(_request(), contract.response_schema)
    assert exc.value.reason_code == "exit_request_source_invalid"


@pytest.mark.anyio
async def test_single_reception_call_preserves_source_and_does_not_return_termination_permission():
    text = "如果任务失败我会重试。我现在希望结束整场面试。"
    gateway = Gateway(_request("E2"))
    decision = await _service(gateway).classify_reception(
        text, "org_default", preceding_text="我还想继续回答。", question="你如何处理任务失败？")
    assert decision == {"kind": "stop_interview", "confidence": .98,
                        "evidence_quote": "我现在希望结束整场面试。", "reply_text": ""}
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request.execution_budget.timeout_s == 4
    assert request.execution_budget.max_provider_retries == 0
    assert request.metadata["prompt_version"] == "conversation_reception.v10"
    payload = json.loads(request.messages[-1].content)
    assert payload["new_speech"] == text
    assert payload["evidence"]["E2"] == decision["evidence_quote"]
