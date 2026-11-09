"""A provider outage may select approved content, never bypass commit authority."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.core.errors import ApiError
from app.model_gateway.errors import ProviderError
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.interview_agent import InterviewAgentRuntime
from app.services.interview_skills import InterviewSkillService
from app.services.interviews import InterviewService
from test_adaptive_interview import select, submit
from test_interview_skills import approved, snapshot
from test_interviewer_supervisor import channel_for, source_session
from test_planning_runtime_fences import replace_controller, waiting_channel


class PendingOutage:
    """Synchronize real supervisor calls without sleeping or using a provider."""

    def __init__(self, *, expected_calls=1, code="provider_timeout"):
        self.calls = []
        self.expected_calls = expected_calls
        self.code = code
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, capability, request):
        self.calls.append(request)
        if len(self.calls) == self.expected_calls:
            self.started.set()
        await self.release.wait()
        raise ProviderError(self.code, "Synthetic private upstream diagnostic.", retryable=True)


@pytest.mark.anyio
@pytest.mark.parametrize("code", [
    "provider_timeout", "provider_server_error", "provider_rate_limited", "provider_circuit_open",
])
async def test_runtime_outage_selects_one_approved_question_without_manual_retry(monkeypatch, code):
    store, runtime, channel, _, grant = waiting_channel(monkeypatch)
    gateway = PendingOutage(code=code)
    runtime.interviews.supervisor.gateway = gateway
    channel._select_turn = AsyncMock()
    channel._schedule_planning("provider_outage")
    task = channel._planning_task
    try:
        await asyncio.wait_for(gateway.started.wait(), 1)
        # Repeated scheduling while the first request is pending cannot spend
        # more model calls or create a second presentation.
        channel._schedule_planning("duplicate_provider_outage")
        assert channel._planning_task is task
        gateway.release.set()
        await asyncio.wait_for(task, 2)
        current = runtime.interviews.get_interview(grant.interview_id)
        assert len(gateway.calls) == 1
        assert len(current["turns"]) == len(current["adaptive_decision_receipts"]) == 1
        assert current["turns"][0]["question_id"] in {
            question["question_id"]
            for question in current["plan_snapshot"]["assessment_contract"]["candidate_questions"]
        }
        assert current["agent_runtime"].get("planning_problem") is None
        assert current["agent_runtime"]["last_planning"]["selection_strategy"] == "approved_catalog_fallback.v1"
        assert current["agent_runtime"]["last_planning"]["fallback_reason"] == code
        assert current["agent_runtime"]["last_planning"]["model_calls"] == 1
        assert current["adaptive_decision_receipts"][0]["selection_strategy"] == "approved_catalog_fallback.v1"
        assert current["adaptive_decision_receipts"][0]["fallback_reason"] == code
        assert not current["answers"] and not current.get("candidate_input_completed_at")
        channel._select_turn.assert_awaited_once()
        assert channel._select_turn.await_args.args[0]["id"] == current["current_turn_id"]
    finally:
        gateway.release.set()
        await channel.close("test_done")


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_concurrent_fallback_proposals_commit_only_one_question(tmp_path, backend):
    store = SQLiteStore(str(tmp_path / "fallback.sqlite3")) if backend == "sqlite" else InMemoryStore()
    session = source_session(store)
    service = InterviewService(store)
    gateway = PendingOutage(expected_calls=2)
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
        assert sum(event["type"] == "question.selected" for event in current["lifecycle_events"]) == 1
        assert current["decision_revision"] == 1 and len(gateway.calls) == 2
        assert current["agent_runtime"]["last_planning"]["selection_strategy"] == "approved_catalog_fallback.v1"
        # Re-entering after the committed selection is a no-op, including at
        # the model boundary: retries cannot select a second active question.
        repeated = await service.advance_adaptive_interview(session["id"])
        assert repeated["current_turn_id"] == current["current_turn_id"]
        assert len(repeated["turns"]) == 1 and len(gateway.calls) == 2
    finally:
        gateway.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
async def test_timeout_after_real_coverage_completion_ends_through_lifecycle():
    store = InMemoryStore()
    session = source_session()
    session = submit(select(session, "q1").session).session
    session = submit(select(session, "q2").session).session
    with persistence_for(store).transaction("org_default") as transaction:
        session = transaction.interview_sessions.add(session)
    service = InterviewService(store)
    gateway = PendingOutage()
    gateway.release.set()
    service.supervisor.gateway = gateway
    result = await service.advance_adaptive_interview(session["id"])
    assert len(gateway.calls) == 1
    assert result["candidate_input_completion_reason"] == "evidence_sufficient"
    assert result["candidate_input_completed_at"] and result["current_turn_id"] is None
    assert result["assessment_coverage"]["sufficient"] is True
    assert len(result["turns"]) == len(result["answers"]) == 2
    assert result["answers"] == session["answers"]
    assert result["plan_snapshot"]["assessment_contract"] == session["plan_snapshot"]["assessment_contract"]
    receipt = result["adaptive_decision_receipts"][-1]
    assert receipt["kind"] == "end_candidate_input" and receipt["reason"] == "evidence_sufficient"
    assert receipt["selection_strategy"] == "approved_catalog_fallback.v1"
    assert receipt["fallback_reason"] == "provider_timeout"
    assert result["agent_runtime"]["last_planning"]["selection_strategy"] == "approved_catalog_fallback.v1"
    assert result["agent_runtime"]["last_planning"]["fallback_reason"] == "provider_timeout"


@pytest.mark.anyio
@pytest.mark.parametrize("replacement", ["controller", "owner", "pause_resume"])
async def test_provider_outage_cannot_commit_or_publish_after_authority_replacement(monkeypatch, replacement):
    store, runtime, channel, coordinator, grant = waiting_channel(monkeypatch)
    gateway = PendingOutage()
    runtime.interviews.supervisor.gateway = gateway
    channel._select_turn = AsyncMock()
    channel._schedule_planning("stale_provider_outage")
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
        else:
            runtime.interviews.pause_interview(grant.interview_id)
            runtime.interviews.resume_interview(grant.interview_id)
        expected = deepcopy(runtime.interviews.get_interview(grant.interview_id))
        gateway.release.set()
        await asyncio.wait_for(task, 2)
        current = runtime.interviews.get_interview(grant.interview_id)
        assert current == expected
        assert len(gateway.calls) == 1 and not current["turns"]
        assert not current.get("adaptive_decision_receipts")
        assert not current["agent_runtime"].get("planning_problem")
        channel._select_turn.assert_not_awaited()
    finally:
        gateway.release.set()
        await channel.close("test_done")


@pytest.mark.anyio
async def test_skill_revoked_during_provider_outage_prevents_fallback_commit_and_speech():
    store = InMemoryStore()
    skills = InterviewSkillService(store)
    skill = approved(skills, "org_default")
    session = source_session()
    session["plan_snapshot"]["enterprise_skill_snapshot"] = snapshot(skills, skill, "org_default")
    with persistence_for(store).transaction("org_default") as transaction:
        session = transaction.interview_sessions.add(session)
    runtime = InterviewAgentRuntime(store)
    channel = channel_for(runtime, session)
    gateway = PendingOutage()
    runtime.interviews.supervisor.gateway = gateway
    channel._select_turn = AsyncMock()
    channel._schedule_planning("revoked_provider_outage")
    task = channel._planning_task
    try:
        await asyncio.wait_for(gateway.started.wait(), 1)
        skills.revoke(skill["id"], expected_version=skill["version"], actor_id="reviewer",
            reason="Synthetic authorization withdrawal.", organization_id="org_default")
        expected = deepcopy(runtime.interviews.get_interview(session["id"]))
        assert expected["status"] == "paused"
        gateway.release.set()
        await asyncio.wait_for(task, 2)
        current = runtime.interviews.get_interview(session["id"])
        assert current == expected
        assert len(gateway.calls) == 1 and not current["turns"]
        assert not current.get("adaptive_decision_receipts")
        channel._select_turn.assert_not_awaited()
        with pytest.raises(ApiError):
            runtime._approve_conversation_act(session["id"], act_type="question", text="已失效的问题",
                turn_id=None, evidence_quotes=[], approved_by="interview_agent_runtime",
                organization_id="org_default")
    finally:
        gateway.release.set()
        await channel.close("test_done")
