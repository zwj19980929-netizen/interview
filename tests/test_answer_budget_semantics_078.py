"""The paid-call cap and ordinary conversation veto share production seams."""
import asyncio
from copy import deepcopy

import pytest

from app.core.answer_processing_budget import (
    AnswerProcessingBudget, answer_processing_budget, consume_answer_model_call,
)
from app.core.prompt.semantic_turn import SemanticContractError, validate_conversation_clarification
from app.model_gateway.errors import ProviderError
from test_answer_declined_understanding import no_followup
from test_prepared_turn_decision import Gateway, _data, _input, _service
from test_semantic_turn_policy import semantic_response


@pytest.mark.anyio
async def test_parallel_and_nested_tasks_cannot_multiply_one_answer_allowance():
    budget = AnswerProcessingBudget()
    async def call():
        with answer_processing_budget(AnswerProcessingBudget()):
            return consume_answer_model_call()
    with answer_processing_budget(budget):
        results = await asyncio.gather(*(call() for _ in range(12)))
    assert sum(results) == budget.used == 2
    assert consume_answer_model_call(), "Unrelated work must not inherit a closed context"


@pytest.mark.anyio
async def test_format_correction_and_later_recovery_share_actual_call_count():
    invalid = _data()
    invalid["understanding"]["claims"][0]["evidence_id"] = "E999"
    gateway = Gateway(invalid, _data())
    service = _service(gateway)
    utterance, turn, interview = _input()
    budget = AnswerProcessingBudget()
    with answer_processing_budget(budget):
        first, _ = await service.prepare_decision(utterance, turn, interview)
        second, probe = await service.prepare_decision(utterance, turn, interview)
    assert first.problem is None
    assert budget.used == len(gateway.requests) == 2
    assert second.problem.reason_code == "call_budget_exhausted"
    assert second.problem.retryable is False and not probe["selected"]
    assert all(r.execution_budget.timeout_s == 18 for r in gateway.requests)
    assert all(r.execution_budget.max_provider_attempts == 1 for r in gateway.requests)
    assert all(r.execution_budget.max_provider_retries == 0 for r in gateway.requests)


@pytest.mark.anyio
@pytest.mark.parametrize("code,retryable", [
    ("provider_timeout", True), ("provider_rate_limited", True),
    ("provider_auth_failed", False), ("provider_billing_required", False),
    ("provider_circuit_open", True),
])
async def test_safe_failure_retains_actionable_cause_without_provider_text(code, retryable):
    gateway = Gateway(error=ProviderError(code, "private upstream payload", retryable=retryable))
    utterance, turn, interview = _input()
    result, _ = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert result.problem.reason_code == code and result.problem.retryable is retryable
    assert "private upstream payload" not in str(result.model_dump())
    assert len(gateway.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("text,intent", [
    ("回答啦。", "answering"), ("我开始回答了。", "answering"),
    ("我现在可以开始回答吗？", "answering"), ("我先想一下。", "thinking"),
    ("请稍等。", "thinking"),
])
async def test_mistaken_control_clarification_is_corrected_without_spelling_prompt(text, intent):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    interview["_semantic_first"] = True
    good = semantic_response(content="none", intent=intent, technical=False)
    good["followup"] = no_followup()
    good["understanding"].update(answer_summary="候选人在进行流程交流。", followup_allowed=False)
    bad = deepcopy(good)
    bad["understanding"].update(intent="clarification_request", turn_intent="clarify",
        confidence=.6, suggested_action="clarify", ambiguities=["流程用语不明"],
        clarification_target={"evidence_id": "E1", "focus_quote": text.strip("。？")})
    gateway = Gateway(bad, good)
    with answer_processing_budget(AnswerProcessingBudget()):
        understanding, probe = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None
    assert understanding.clarification_target is None and understanding.ambiguities == []
    assert understanding.turn_intent == intent and understanding.completion_basis is None
    assert understanding.claims == [] and not probe["selected"]
    assert len(gateway.requests) == 2
    correction = "".join(m.content for m in gateway.requests[1].messages)
    assert "clarification_targets_conversation_control" in correction
    assert "不能要求候选人解释或拼写" in correction


@pytest.mark.parametrize("text", [
    "任务状态会设置成回答啦。", "他说‘回答啦’是什么意思？", "我不是说开始回答。",
    "刚才字幕里的开始回答不是我说的。", "我把请求放进卡拉布。",
])
def test_veto_does_not_hide_technical_ambiguity_quotes_negation_or_recognition_disputes(text):
    validate_conversation_clarification({"clarification_target": {"evidence_quote": text, "focus_quote": "回答"}})


def test_control_veto_only_rejects_it_never_fabricates_authority():
    data = {"clarification_target": {"evidence_quote": "回答啦。", "focus_quote": "回答啦"}}
    before = deepcopy(data)
    with pytest.raises(SemanticContractError, match="clarification_targets_conversation_control"):
        validate_conversation_clarification(data)
    assert data == before
