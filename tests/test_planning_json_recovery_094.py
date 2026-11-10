"""Recover only classified wire JSON failures within the existing planning budget."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.errors import ApiError
from app.model_gateway.errors import ProviderError
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.interviews import InterviewService
from app.services.interviewer_supervisor.tools import InterviewerTools
from test_interviewer_supervisor import Gateway, decision, source_session, supervisor
from test_planning_runtime_fences import replace_controller, waiting_channel


def parse_failure(kind="invalid_json_object", *, retryable=True, code="provider_schema_invalid"):
    return ProviderError(code, "private vendor response", retryable=retryable,
                         details={"json_failure_kind": kind})


def read_tool():
    return decision(None, action="use_tool", tool_name="questions.read",
                    argument="q2", reason_code="inspect_context")


def expert_tool():
    return decision(None, action="use_tool", tool_name="specialists.consult",
                    reason_code="inspect_context")


def assert_fallback(result, session, count):
    assert result.action == "select_question"
    InterviewerTools(session).require_selection(result.question_id, result.inquiry_unit_id)
    assert result.selection_strategy == "approved_catalog_fallback.v1"
    assert result.fallback_reason == "provider_schema_invalid"
    assert result.model_calls == count
    assert "private" not in repr(result)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["empty_content", "invalid_json_object"])
async def test_classified_parse_failure_retries_once_then_keeps_valid_model_decision(kind):
    session = source_session()
    original = deepcopy(session)
    gateway = Gateway(parse_failure(kind), decision("q2"))
    result = await supervisor(gateway).propose_next(session)
    assert result.question_id == "q2" and result.model_calls == 2
    assert result.selection_strategy is None and result.fallback_reason is None
    assert session == original
    assert len(gateway.calls) == 2
    for request in gateway.calls:
        assert request.execution_budget.max_provider_retries == 0
        assert request.execution_budget.max_provider_attempts == 1
        assert 0 < request.execution_budget.timeout_s <= 15
        assert request.metadata["prompt_version"] == "interviewer_supervisor.v4"
    assert gateway.calls[1].execution_budget.timeout_s <= gateway.calls[0].execution_budget.timeout_s
    serialized = json.dumps([request.model_dump() for request in gateway.calls])
    assert "private vendor response" not in serialized
    assert "specialists.consult" not in gateway.calls[1].json_schema["properties"]["tool_name"]["enum"]


@pytest.mark.anyio
async def test_second_parse_failure_uses_independent_catalog_without_third_attempt():
    session = source_session()
    original = deepcopy(session)
    gateway = Gateway(parse_failure(), parse_failure("empty_content"), decision("q2"))
    result = await supervisor(gateway).propose_next(session)
    assert_fallback(result, session, 2)
    assert len(gateway.calls) == 2 and session == original


@pytest.mark.anyio
async def test_parse_retry_is_shared_with_later_planner_calls():
    session = source_session()
    gateway = Gateway(parse_failure(), read_tool(), parse_failure(), decision("q2"))
    result = await supervisor(gateway).propose_next(session)
    assert_fallback(result, session, 3)
    assert len(gateway.calls) == 3
    assert result.tools_used == ("questions.read",)
    assert gateway.calls[-1].json_schema["properties"]["tool_name"]["enum"] == [None]


@pytest.mark.anyio
async def test_second_planner_call_can_retry_into_final_call():
    gateway = Gateway(read_tool(), parse_failure(), decision("q2"))
    result = await supervisor(gateway).propose_next(source_session())
    assert result.question_id == "q2" and result.model_calls == 3
    assert result.selection_strategy is None
    assert gateway.calls[-1].json_schema["properties"]["tool_name"]["enum"] == [None]
    observations = json.loads(gateway.calls[-1].messages[-1].content)["tool_observations"]
    assert len(observations) == 1 and observations[0]["tool_name"] == "questions.read"


@pytest.mark.anyio
async def test_specialist_parse_failure_reserves_final_planner_slot_instead_of_retrying():
    session = source_session()
    gateway = Gateway(expert_tool(), parse_failure(),
                      {"question_ids": ["q2"], "evidence_gap": "A gap."}, decision("q2"))
    result = await supervisor(gateway).propose_next(session)
    assert_fallback(result, session, 2)
    assert len(gateway.calls) == 2 and result.tools_used == ("specialists.consult",)


@pytest.mark.anyio
async def test_parse_failure_on_final_planner_call_does_not_add_fourth_attempt():
    session = source_session()
    gateway = Gateway(expert_tool(), {"question_ids": ["q2"], "evidence_gap": "A gap."},
                      parse_failure(), decision("q2"))
    result = await supervisor(gateway).propose_next(session)
    assert_fallback(result, session, 3)
    assert len(gateway.calls) == 3


@pytest.mark.anyio
@pytest.mark.parametrize("remaining", [0.0, 0.9])
async def test_parse_failure_with_too_little_time_falls_back_without_new_deadline(monkeypatch, remaining):
    import app.services.interviewer_supervisor.service as module
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.value))

    class LateFailure(Gateway):
        async def invoke(self, capability, request):
            clock.value = 115.0 - remaining
            return await super().invoke(capability, request)

    session = source_session()
    gateway = LateFailure(parse_failure(), decision("q2"))
    result = await supervisor(gateway).propose_next(session)
    assert_fallback(result, session, 1)
    assert len(gateway.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [
    ProviderError("provider_schema_invalid", "private vendor response", retryable=True),
    ProviderError("provider_schema_invalid", "private vendor response", retryable=True,
                  details={"json_failure_kind": "invalid_json_object", "schema_reason": "required", "schema_path": "$"}),
    parse_failure("unknown"), parse_failure([]), parse_failure({}), parse_failure(retryable=False),
    parse_failure(code="provider_auth_failed"), parse_failure(code="provider_billing_required"),
    parse_failure(code="provider_output_truncated"),
    decision(extra="forbidden"), decision("foreign"), {"action": "select_question"},
])
async def test_unclassified_schema_domain_auth_and_nonretryable_errors_stay_fail_closed(failure):
    gateway = Gateway(failure, decision("q2"))
    with pytest.raises(ApiError) as error:
        await supervisor(gateway).propose_next(source_session())
    assert error.value.code == "INTERVIEW_DECISION_UNAVAILABLE"
    assert len(gateway.calls) == 1 and "private" not in str(error.value)


@pytest.mark.anyio
async def test_invalid_schema_after_retry_is_not_catalog_fallback():
    gateway = Gateway(parse_failure(), decision("foreign"), decision("q2"))
    with pytest.raises(ApiError) as error:
        await supervisor(gateway).propose_next(source_session())
    assert error.value.code == "INTERVIEW_DECISION_UNAVAILABLE"
    assert len(gateway.calls) == 2


@pytest.mark.anyio
async def test_cancellation_during_retry_propagates_without_catalog_selection():
    class CancelRetry(Gateway):
        async def invoke(self, capability, request):
            if self.calls:
                self.calls.append(request)
                raise asyncio.CancelledError()
            return await super().invoke(capability, request)

    gateway = CancelRetry(parse_failure())
    with pytest.raises(asyncio.CancelledError):
        await supervisor(gateway).propose_next(source_session())
    assert len(gateway.calls) == 2


@pytest.mark.anyio
async def test_tool_exhaustion_without_parse_error_is_not_success():
    gateway = Gateway(read_tool(), read_tool(), read_tool())
    with pytest.raises(ApiError):
        await supervisor(gateway).propose_next(source_session())
    assert len(gateway.calls) == 3


@pytest.mark.anyio
async def test_no_legal_fallback_question_remains_unavailable(monkeypatch):
    monkeypatch.setattr(InterviewerTools, "fallback_selection", lambda self: None)
    gateway = Gateway(parse_failure(), parse_failure())
    with pytest.raises(ApiError) as error:
        await supervisor(gateway).propose_next(source_session())
    assert error.value.code == "INTERVIEW_DECISION_UNAVAILABLE"
    assert len(gateway.calls) == 2


class PendingParseRetry:
    """Hold the actual retry while authority or another proposal may change."""

    def __init__(self, *, retries=1):
        self.calls = []
        self.retry_count = 0
        self.expected_retries = retries
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, capability, request):
        self.calls.append(request)
        # The second call of an operation no longer advertises specialists.
        if "specialists.consult" not in request.json_schema["properties"]["tool_name"]["enum"]:
            self.retry_count += 1
            if self.retry_count == self.expected_retries:
                self.started.set()
            await self.release.wait()
        raise parse_failure()


@pytest.mark.anyio
async def test_runtime_two_parse_failures_select_one_approved_question_without_manual_retry(monkeypatch):
    _, runtime, channel, _, grant = waiting_channel(monkeypatch)
    gateway = PendingParseRetry()
    runtime.interviews.supervisor.gateway = gateway
    channel._select_turn = AsyncMock()
    channel._schedule_planning("parse_failure")
    task = channel._planning_task
    try:
        await asyncio.wait_for(gateway.started.wait(), 1)
        channel._schedule_planning("duplicate_parse_failure")
        assert channel._planning_task is task
        gateway.release.set()
        await asyncio.wait_for(task, 2)
        current = runtime.interviews.get_interview(grant.interview_id)
        assert len(gateway.calls) == 2
        assert len(current["turns"]) == len(current["adaptive_decision_receipts"]) == 1
        assert not current["answers"] and not current.get("candidate_input_completed_at")
        assert current["agent_runtime"].get("planning_problem") is None
        assert current["agent_runtime"]["last_planning"]["selection_strategy"] == "approved_catalog_fallback.v1"
        assert current["agent_runtime"]["last_planning"]["fallback_reason"] == "provider_schema_invalid"
        assert current["agent_runtime"]["last_planning"]["model_calls"] == 2
        assert current["adaptive_decision_receipts"][0]["fallback_reason"] == "provider_schema_invalid"
        channel._select_turn.assert_awaited_once()
        assert channel._select_turn.await_args.args[0]["id"] == current["current_turn_id"]
    finally:
        gateway.release.set()
        await channel.close("test_done")


@pytest.mark.anyio
@pytest.mark.parametrize("replacement", ["controller", "owner", "pause_resume", "context"])
async def test_authority_change_during_retry_prevents_question_commit_or_speech(monkeypatch, replacement):
    store, runtime, channel, coordinator, grant = waiting_channel(monkeypatch)
    gateway = PendingParseRetry()
    runtime.interviews.supervisor.gateway = gateway
    channel._select_turn = AsyncMock()
    channel._schedule_planning("parse_failure")
    task = channel._planning_task
    try:
        await asyncio.wait_for(gateway.started.wait(), 1)
        if replacement == "controller":
            replace_controller(store, coordinator, grant)
        elif replacement == "owner":
            assert coordinator.release(grant)
            successor = coordinator.claim_owner(interview_id=grant.interview_id,
                organization_id="org_default", local_instance_id="successor")
            assert successor.ownership_epoch > grant.ownership_epoch
        elif replacement == "pause_resume":
            runtime.interviews.pause_interview(grant.interview_id)
            runtime.interviews.resume_interview(grant.interview_id)
        else:
            with persistence_for(store).transaction("org_default") as transaction:
                changed = transaction.interview_sessions.get(grant.interview_id)
                changed["plan_snapshot"]["company_context"] = "Updated approved company context."
                transaction.interview_sessions.update(changed, expected_version=changed["version"])
        expected = deepcopy(runtime.interviews.get_interview(grant.interview_id))
        gateway.release.set()
        await asyncio.wait_for(task, 2)
        current = runtime.interviews.get_interview(grant.interview_id)
        assert current == expected and len(gateway.calls) == 2
        assert not current["turns"] and not current.get("adaptive_decision_receipts")
        assert not current["agent_runtime"].get("planning_problem")
        channel._select_turn.assert_not_awaited()
    finally:
        gateway.release.set()
        await channel.close("test_done")


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_concurrent_parse_recovery_commits_only_one_question(tmp_path, backend):
    store = SQLiteStore(str(tmp_path / "parse_recovery.sqlite3")) if backend == "sqlite" else InMemoryStore()
    session = source_session(store)
    service = InterviewService(store)
    gateway = PendingParseRetry(retries=2)
    service.supervisor.gateway = gateway
    tasks = [asyncio.create_task(service.advance_adaptive_interview(session["id"])) for _ in range(2)]
    try:
        await asyncio.wait_for(gateway.started.wait(), 1)
        gateway.release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, ApiError)]
        assert len(successes) == len(failures) == 1
        assert failures[0].code == "AGENT_DECISION_STALE"
        current = service.get_interview(session["id"])
        assert len(current["turns"]) == len(current["adaptive_decision_receipts"]) == 1
        assert current["decision_revision"] == 1 and len(gateway.calls) == 4
        assert sum(event["type"] == "question.selected" for event in current["lifecycle_events"]) == 1
        repeated = await service.advance_adaptive_interview(session["id"])
        assert repeated["current_turn_id"] == current["current_turn_id"]
        assert len(repeated["turns"]) == 1 and len(gateway.calls) == 4
    finally:
        gateway.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
