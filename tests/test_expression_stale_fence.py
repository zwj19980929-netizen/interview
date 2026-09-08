"""Held synthesis must not publish or mutate a superseded conversation act."""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.auth import Principal
from app.core.errors import ApiError
from app.core.time import utc_now
from app.domain.interview_agent import ClientCapabilities, OpenAgentSession
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.evidence_coordination import EvidenceOwnershipCoordinator
from app.services.interview_agent import AgentChannel, InterviewAgentRuntime


def _harness():
    store = InMemoryStore()
    persistence = persistence_for(store)
    now = utc_now()
    with persistence.transaction("org_default") as transaction:
        transaction.interview_sessions.add({
            "id": "interview_expression_guard",
            "organization_id": "org_default",
            "candidate_id": "candidate_synthetic",
            "status": "in_progress",
            "phase": "position_bank",
            "current_turn_id": "turn_1",
            "turns": [{
                "id": "turn_1", "status": "asking", "order": 1,
                "question_spoken_text": "请说明测试方案。",
                "is_followup": False,
            }],
            "answers": [], "lifecycle_events": [], "agent_events": [],
            "settings": {"record_audio": False, "record_video": False},
            "agent_runtime": {
                "floor": "candidate", "calibration_status": "completed",
                "last_sequence": 0, "active_performance_id": None,
            },
            "created_at": now, "updated_at": now,
        })
    runtime = InterviewAgentRuntime(store)
    channel = AgentChannel(runtime, OpenAgentSession(
        interview_id="interview_expression_guard",
        principal=Principal(actor_id="candidate:candidate_synthetic", organization_id="org_default",
                            roles=frozenset({"candidate"}), authenticated=True),
        connection_id="connection_expression_guard",
        capabilities=ClientCapabilities(webrtc=True, audio_worklet=True, webgl=True,
                                        camera=True, microphone=True, speaker=True, avatar_fps=60),
    ))
    coordinator = EvidenceOwnershipCoordinator(store)
    grant = coordinator.attach_control(
        interview_id=channel.interview_id, organization_id=channel.organization_id,
        connection_id=channel.opened.connection_id, local_instance_id="instance_expression_old",
    )
    channel._evidence_session = SimpleNamespace(ownership=grant, _stopped=False)
    return runtime, channel, coordinator, grant


def _expression():
    return {"audio_uri": "private-file://synthetic-expression", "duration_ms": 1000,
            "visemes": [], "delivery": "cascade"}


async def _select(channel):
    return await channel._select_act(
        act_type="question", text="请说明测试方案。", turn_id="turn_1",
        causation_id="test-expression", evidence_refs=[], gesture="look_at_candidate",
    )


def _mutate(runtime, channel, mutation):
    with runtime.persistence.transaction(channel.organization_id) as transaction:
        session = transaction.interview_sessions.get(channel.interview_id)
        mutation(session)
        transaction.interview_sessions.update(session, expected_version=session["version"])


@pytest.mark.parametrize("provider_fails", [False, True])
@pytest.mark.parametrize("superseded_by", ["pause", "new_turn", "candidate_floor", "owner_lost"])
def test_held_expression_cannot_publish_or_pause_superseding_state(provider_fails, superseded_by):
    async def scenario():
        runtime, channel, coordinator, grant = _harness()
        started, release = asyncio.Event(), asyncio.Event()

        async def held(*_args):
            started.set()
            await release.wait()
            if provider_fails:
                raise ApiError("TTS_UNAVAILABLE", "Synthetic provider failure.", status_code=503)
            return _expression()

        runtime._expression_audio = held
        task = asyncio.create_task(_select(channel))
        await asyncio.wait_for(started.wait(), 1)
        if superseded_by == "owner_lost":
            # Leave the local stopped flag false: the database fence, not a
            # best-effort cancellation notification, must reject this result.
            assert coordinator.release(grant)
            coordinator.attach_control(
                interview_id=channel.interview_id, organization_id=channel.organization_id,
                connection_id="connection_expression_new", local_instance_id="instance_expression_new",
            )
        else:
            def mutation(session):
                if superseded_by == "pause":
                    session["status"] = "paused"
                    session["agent_runtime"]["floor"] = "none"
                elif superseded_by == "new_turn":
                    session["current_turn_id"] = "turn_2"
                    session["turns"].append({"id": "turn_2", "status": "asking"})
                else:
                    session["agent_runtime"]["floor"] = "candidate"
                session["agent_runtime"]["active_performance_id"] = "newer_performance"
            _mutate(runtime, channel, mutation)
        before = runtime.interviews.get_interview(channel.interview_id)
        release.set()
        assert await asyncio.wait_for(task, 1) is None
        after = runtime.interviews.get_interview(channel.interview_id)
        assert after["status"] == before["status"]
        assert after["current_turn_id"] == before["current_turn_id"]
        assert after["agent_runtime"] == before["agent_runtime"]
        assert after["agent_events"] == before["agent_events"]
        assert not any(item["type"] == "avatar.performance.started" for item in after["agent_events"])

    asyncio.run(scenario())


@pytest.mark.parametrize("provider_fails", [False, True])
def test_reselected_same_approved_act_supersedes_old_synthesis(provider_fails):
    async def scenario():
        runtime, channel, _coordinator, _grant = _harness()
        started, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def held_first(*_args):
            nonlocal count
            count += 1
            if count == 1:
                started.set()
                await release.wait()
                if provider_fails:
                    raise ApiError("TTS_UNAVAILABLE", "Synthetic old failure.", status_code=503)
            return _expression()

        runtime._expression_audio = held_first
        old = asyncio.create_task(_select(channel))
        await asyncio.wait_for(started.wait(), 1)
        current = await _select(channel)
        assert current is not None
        selected = runtime.interviews.get_interview(channel.interview_id)
        acts = [item for item in selected["agent_events"] if item["type"] == "conversation.act.selected"]
        assert len(acts) == 2
        assert acts[0]["payload"]["act_id"] == acts[1]["payload"]["act_id"]
        release.set()
        assert await asyncio.wait_for(old, 1) is None
        after = runtime.interviews.get_interview(channel.interview_id)
        assert after["status"] == "in_progress"
        assert after["agent_runtime"]["active_performance_id"] == current.performance_id
        started_events = [item for item in after["agent_events"] if item["type"] == "avatar.performance.started"]
        assert len(started_events) == 1
        assert started_events[0]["payload"]["performance_id"] == current.performance_id
        assert not any(item["type"] == "problem" for item in after["agent_events"])

    asyncio.run(scenario())


def test_expression_error_publication_cannot_pause_a_new_turn():
    async def scenario():
        runtime, channel, _coordinator, _grant = _harness()

        async def failed(*_args):
            raise ApiError("TTS_UNAVAILABLE", "Synthetic provider failure.", status_code=503)

        async def problem_yields_to_new_turn(*_args, **_kwargs):
            def mutation(session):
                session["current_turn_id"] = "turn_2"
                session["turns"].append({"id": "turn_2", "status": "asking"})
                session["agent_runtime"]["active_performance_id"] = "newer_performance"
            _mutate(runtime, channel, mutation)

        runtime._expression_audio = failed
        channel._problem = problem_yields_to_new_turn
        assert await _select(channel) is None
        session = runtime.interviews.get_interview(channel.interview_id)
        assert session["status"] == "in_progress"
        assert session["current_turn_id"] == "turn_2"
        assert session["agent_runtime"]["active_performance_id"] == "newer_performance"

    asyncio.run(scenario())


def test_already_fenced_expression_cannot_select_an_act_or_change_floor():
    async def scenario():
        runtime, channel, coordinator, grant = _harness()
        assert coordinator.release(grant)
        before = runtime.interviews.get_interview(channel.interview_id)
        assert await _select(channel) is None
        after = runtime.interviews.get_interview(channel.interview_id)
        assert after == before

    asyncio.run(scenario())
