"""Transient planning failures may select only current approved evidence scope."""
import asyncio
from copy import deepcopy

import pytest

from app.core.errors import ApiError
from app.model_gateway.errors import ProviderError
from app.services.interviewer_supervisor.context import context_fingerprint
from app.services.interviewer_supervisor.tools import InterviewerTools
from test_interviewer_supervisor import Gateway, decision, source_session, supervisor


@pytest.mark.anyio
@pytest.mark.parametrize("code", ["provider_timeout", "provider_server_error", "provider_rate_limited", "provider_circuit_open"])
async def test_transient_failure_uses_approved_selection_without_mutation(code):
    source = source_session()
    before = deepcopy(source)
    gateway = Gateway(ProviderError(code, "sensitive vendor body", retryable=True))
    result = await supervisor(gateway).propose_next(source)
    assert result.action == "select_question"
    InterviewerTools(source).require_selection(result.question_id, result.inquiry_unit_id)
    assert result.context_fingerprint == context_fingerprint(source)
    assert result.selection_strategy == "approved_catalog_fallback.v1"
    assert result.fallback_reason == code and result.model_calls == 1
    assert source == before and len(gateway.calls) == 1
    assert "sensitive" not in repr(result)


@pytest.mark.anyio
async def test_only_current_model_timeout_recovers(monkeypatch):
    import app.services.interviewer_supervisor.service as module
    monkeypatch.setattr(module, "remaining_interaction_seconds", lambda: .01)
    class SlowGateway:
        async def invoke(self, *args):
            await asyncio.Event().wait()
    result = await supervisor(SlowGateway()).propose_next(source_session())
    assert result.fallback_reason == "provider_timeout" and result.model_calls == 1
    monkeypatch.setattr(module, "remaining_interaction_seconds", lambda: 0)
    with pytest.raises(ApiError) as error:
        await supervisor(Gateway()).propose_next(source_session())
    assert error.value.code == "INTERVIEW_DECISION_BUDGET_EXHAUSTED"


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [
    ProviderError("provider_timeout", "private", retryable=False),
    ProviderError("provider_auth_failed", "private", retryable=True),
    ProviderError("provider_billing_required", "private", retryable=True),
    ProviderError("provider_schema_invalid", "private", retryable=True),
    RuntimeError("private"), decision("foreign"), {"action": "select_question"},
])
async def test_non_transient_or_invalid_response_stays_fail_closed(failure):
    with pytest.raises(ApiError) as error:
        await supervisor(Gateway(failure)).propose_next(source_session())
    assert "private" not in str(error.value)


@pytest.mark.anyio
async def test_cancellation_never_selects():
    class CancelGateway:
        async def invoke(self, *args):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await supervisor(CancelGateway()).propose_next(source_session())


@pytest.mark.anyio
async def test_budget_recomputed_after_provider_wait(monkeypatch):
    import app.services.interviewer_supervisor.service as module
    original = module.budget_summary
    calls = []
    def budget(source, now):
        calls.append(now)
        return {**original(source, now), "exhausted": len(calls) > 1, "remaining_seconds": 0 if len(calls) > 1 else 600}
    monkeypatch.setattr(module, "budget_summary", budget)
    result = await supervisor(Gateway(ProviderError("provider_timeout", "private", retryable=True))).propose_next(source_session())
    assert result.action == "finish_interview" and result.reason_code == "budget_exhausted"
    assert result.question_id is None and len(calls) == 2


@pytest.mark.anyio
async def test_no_fitting_question_is_not_a_fabricated_success(monkeypatch):
    import app.services.interviewer_supervisor.service as module
    original = module.budget_summary
    monkeypatch.setattr(module, "budget_summary", lambda source, now: {**original(source, now), "remaining_seconds": 1})
    with pytest.raises(ApiError) as error:
        await supervisor(Gateway(ProviderError("provider_timeout", "private", retryable=True))).propose_next(source_session())
    assert error.value.code == "INTERVIEW_DECISION_UNAVAILABLE"


def test_fallback_is_independent_of_model_catalog_pagination(monkeypatch):
    tools = InterviewerTools(source_session(), remaining_seconds=600)
    monkeypatch.setattr(tools, "catalog_page", lambda *args: {"items": [], "next_cursor": "next"})
    result = tools.fallback_selection()
    assert result is not None
    tools.require_selection(*result)
    limited = InterviewerTools(source_session(), remaining_seconds=1)
    assert limited.fallback_selection() is None


def test_fallback_excludes_asked_units_and_closed_sources():
    source = source_session()
    candidates = source["plan_snapshot"]["assessment_contract"]["candidate_questions"]
    for candidate in candidates:
        candidate["inquiry_units"] = [
            {"id": candidate["question_id"] + suffix, "question_text": "合成问题", "competency_ids": candidate["competency_ids"]}
            for suffix in ("_a", "_b")]
    source["turns"] = [
        {"id": "root1", "question_id": "q1", "inquiry_unit_id": "q1_a", "current_understanding": {}},
        {"id": "root2", "question_id": "q2", "inquiry_unit_id": "q2_a", "current_understanding": {}},
        {"id": "followup", "question_id": "generated", "is_followup": True, "root_turn_id": "root2",
         "current_understanding": {"turn_intent": "decline_topic", "followup_allowed": False}},
    ]
    tools = InterviewerTools(source, remaining_seconds=600)
    assert "q2" not in tools.candidates
    assert tools.candidates["q1"]["inquiry_units"][0]["id"] == "q1_b"
    chosen = tools.fallback_selection()
    assert chosen in {("q1", "q1_b"), ("q3", "q3_a"), ("q3", "q3_b")}
    tools.require_selection(*chosen)
    with pytest.raises(ValueError):
        tools.require_selection("q1", "q3_a")
