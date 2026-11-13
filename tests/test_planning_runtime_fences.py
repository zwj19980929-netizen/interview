"""Planning side effects retain database ownership and session-context fences."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.evidence_coordination import EvidenceOwnershipCoordinator
from app.services.interview_agent import InterviewAgentRuntime
from test_interviewer_supervisor import source_session, channel_for


def waiting_channel(monkeypatch):
    store = InMemoryStore()
    session = source_session(store)
    runtime = InterviewAgentRuntime(store)
    channel = channel_for(runtime, session)
    coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
    grant = coordinator.attach_control(interview_id=session["id"], organization_id="org_default",
        connection_id="connection", local_instance_id="owner")
    monkeypatch.setattr(channel, "_planning_fence", grant.commit_fence)
    return store, runtime, channel, coordinator, grant


def replace_controller(store, coordinator, grant):
    successor = coordinator.attach_control(interview_id=grant.interview_id, organization_id="org_default",
        connection_id="successor", local_instance_id="owner")
    assert successor.control_generation > grant.control_generation
    with persistence_for(store).transaction("org_default") as transaction:
        session = transaction.interview_sessions.get(grant.interview_id)
        session["agent_runtime"].update(floor="candidate", floor_reason="successor_listening")
        transaction.interview_sessions.update(session, expected_version=session["version"])


@pytest.mark.anyio
@pytest.mark.parametrize("replacement", ["controller", "owner"])
async def test_queued_planning_cannot_change_floor_after_controller_replacement(monkeypatch, replacement):
    store, runtime, channel, coordinator, grant = waiting_channel(monkeypatch)
    runtime.interviews.advance_adaptive_interview = AsyncMock()
    channel._schedule_planning("queued")
    task = channel._planning_task
    if replacement == "controller":
        replace_controller(store, coordinator, grant)
    else:
        assert coordinator.release(grant)
        successor = coordinator.claim_owner(interview_id=grant.interview_id,
            organization_id="org_default", local_instance_id="successor_owner")
        assert successor.ownership_epoch > grant.ownership_epoch
    expected = deepcopy(runtime.interviews.get_interview(grant.interview_id))
    await task
    assert runtime.interviews.get_interview(grant.interview_id) == expected
    runtime.interviews.advance_adaptive_interview.assert_not_awaited()
    await channel.close("test_done")


@pytest.mark.anyio
@pytest.mark.parametrize("replacement", ["controller", "owner", "pause_resume"])
async def test_delayed_planning_error_cannot_poison_replacement_context(monkeypatch, replacement):
    store, runtime, channel, coordinator, grant = waiting_channel(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args, **kwargs):
        started.set()
        await release.wait()
        raise RuntimeError("private provider response")
    runtime.interviews.advance_adaptive_interview = delayed
    channel._schedule_planning("delayed")
    task = channel._planning_task
    await asyncio.wait_for(started.wait(), 1)
    if replacement == "controller":
        replace_controller(store, coordinator, grant)
    elif replacement == "owner":
        assert coordinator.release(grant)
        coordinator.claim_owner(interview_id=grant.interview_id,
            organization_id="org_default", local_instance_id="successor_owner")
    else:
        runtime.interviews.pause_interview(grant.interview_id)
        runtime.interviews.resume_interview(grant.interview_id)
    expected = deepcopy(runtime.interviews.get_interview(grant.interview_id))
    release.set()
    await task
    assert runtime.interviews.get_interview(grant.interview_id) == expected
    assert not expected["agent_runtime"].get("planning_problem")
    assert not expected["turns"]
    await channel.close("test_done")


@pytest.mark.anyio
@pytest.mark.parametrize("replacement", ["controller", "owner", "pause_resume"])
async def test_real_planning_commit_rejects_late_proposal_after_authority_change(monkeypatch, replacement):
    from test_interviewer_supervisor import Gateway, decision
    store, runtime, channel, coordinator, grant = waiting_channel(monkeypatch)
    runtime.interviews.supervisor.gateway = Gateway(decision())
    original = runtime.interviews.supervisor.propose_next
    started, release = asyncio.Event(), asyncio.Event()
    async def delayed(source):
        proposal = await original(source)
        started.set()
        await release.wait()
        return proposal
    runtime.interviews.supervisor.propose_next = delayed
    channel._select_turn = AsyncMock()
    channel._schedule_planning("actual_commit")
    task = channel._planning_task
    await asyncio.wait_for(started.wait(), 1)
    if replacement == "controller":
        replace_controller(store, coordinator, grant)
    elif replacement == "owner":
        assert coordinator.release(grant)
        coordinator.claim_owner(interview_id=grant.interview_id,
            organization_id="org_default", local_instance_id="successor_owner")
    else:
        runtime.interviews.pause_interview(grant.interview_id)
        runtime.interviews.resume_interview(grant.interview_id)
    expected = deepcopy(runtime.interviews.get_interview(grant.interview_id))
    release.set()
    await task
    assert runtime.interviews.get_interview(grant.interview_id) == expected
    assert not expected["turns"] and not expected["agent_runtime"].get("planning_problem")
    channel._select_turn.assert_not_awaited()
    await channel.close("test_done")


@pytest.mark.parametrize("finish_reason", ["budget_exhausted", "coverage_complete"])
def test_managed_automatic_answer_naturally_finishes_with_farewell_and_receipt(tmp_path, monkeypatch, finish_reason):
    from app.domain.adaptive_interview import freeze_assessment_contract, frozen_candidate, initialize_adaptive_snapshot
    from app.domain.interview_agent import ClientSignal
    from test_automatic_turn_integration import _automatic_session, _current, _assert_one_automatic_answer
    from test_declined_answer_integration import _semantic_provider, _answer_without_confirmation, _playable_speech
    from test_evidence_owner_recovery_integration import ORGANIZATION_ID, INTERVIEW_ID, _wait_until
    from test_interviewer_supervisor import Gateway, decision

    _semantic_provider(monkeypatch)
    body = "这题我不会，我们聊下一个话题吧。"
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [body, ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
                session = transaction.interview_sessions.get(INTERVIEW_ID)
                turn = session["turns"][0]
                question = {**turn["question_snapshot"], "id": turn["question_id"], "version": 1,
                    "skills": ["recovery"], "key_points": [{"id": "checkpoint", "text": "持久检查点", "weight": 1.0}]}
                candidates = [frozen_candidate(question, competency_ids=["recovery"], source_type="position_bank")]
                if finish_reason == "coverage_complete":
                    candidates.append(frozen_candidate({**question, "id": "unused_question"},
                        competency_ids=["recovery"], source_type="position_bank"))
                contract = freeze_assessment_contract(role={"id": "role", "version": 1},
                    candidates=candidates, dimension_weights={"recovery": 1.0}, question_count=len(candidates), duration_minutes=10,
                    policy={"min_root_questions": 1, "max_root_questions": len(candidates)})
                plan = initialize_adaptive_snapshot({"assessment_contract": contract})
                plan["question_snapshots"] = [{"question_snapshot_id": turn["question_snapshot_id"],
                    "question_id": turn["question_id"], "competency_ids": ["recovery"], "dimension": "recovery", "weight": 1.0}]
                session.update(execution_schema_version=3, plan_snapshot=plan,
                    dialogue_state="asking", decision_revision=1, adaptive_decision_receipts=[])
                turn.update(competency_ids=["recovery"], assessed_rubric_point_ids=["checkpoint"])
                turn["question_snapshot"]["key_points"] = question["key_points"]
                session["settings"]["avatar_mode"] = "local"
                transaction.interview_sessions.update(session, expected_version=session["version"])
            gateway = Gateway(decision(None, action="finish_interview", reason_code=finish_reason, transition_key="none"))
            runtime.interviews.supervisor.gateway = gateway
            spoken = _playable_speech(runtime)
            await _answer_without_confirmation(managed)
            await _wait_until(lambda: bool(_current(runtime)["agent_runtime"].get("completion_closing_performance_id")), timeout=5)
            session = _current(runtime)
            assert session["candidate_input_completion_reason"] == ("evidence_sufficient" if finish_reason == "coverage_complete" else finish_reason)
            assert session["current_turn_id"] is None
            assert len(gateway.calls) == (1 if finish_reason == "coverage_complete" else 0)
            _assert_one_automatic_answer(store, runtime, body, voice_frames=5)
            assert channel._evidence_session is managed and not managed._stopped
            assert any("本次面试已经完成" in text for text in spoken)
            assert not any(event["type"] == "conversation.act.selected" and event["payload"]["act_type"] == "question"
                           for event in session["agent_events"])
            closing_id = session["agent_runtime"]["completion_closing_performance_id"]
            await channel.send(ClientSignal(type="avatar.performance.stopped", idempotency_key="natural_end_receipt",
                payload={"performance_id": closing_id}))
            await _wait_until(lambda: bool(_current(runtime)["agent_runtime"].get("completion_emitted_at")))
            receipts = [event for event in _current(runtime)["agent_events"] if event["type"] == "completed"]
            assert len(receipts) == 1
            assert receipts[0]["payload"]["human_review_required"] is True
            assert receipts[0]["payload"]["submitted_at"] == session["candidate_input_completed_at"]
    asyncio.run(scenario())
