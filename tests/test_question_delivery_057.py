"""Synthetic question delivery: current context and recoverable transports."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from test_expression_stale_fence import _harness, _mutate
from app.core.prompt.interviewer_supervisor import TRANSITIONS
from app.domain.interview_agent import Replayability
from app.persistence.provider import persistence_for
from app.repositories.sqlite import SQLiteStore
from app.services import interview_agent
from app.services.interview_agent import AgentChannel, InterviewAgentRuntime


@pytest.mark.parametrize("transition_key", TRANSITIONS)
@pytest.mark.parametrize("history", ["warmup_only", "unsubmitted_previous", "future_answer", "current_answer"])
def test_question_without_a_previous_submitted_answer_has_no_transition(transition_key, history):
    async def scenario():
        runtime, channel, _, _ = _harness()

        def seed(session):
            current = session["turns"][0]
            session["agent_runtime"].update(
                next_transition_key=transition_key,
                calibration_transcript="这是不参与正式问答的合成试音。",
            )
            if history == "unsubmitted_previous":
                session["turns"].insert(0, {
                    "id": "unsubmitted", "current_understanding": {"intent": "answer_declined"},
                })
            elif history == "future_answer":
                session["turns"].append({
                    "id": "future", "current_understanding": {"intent": "answer_declined"},
                })
                session["answers"] = [{"id": "a_future", "turn_id": "future"}]
            elif history == "current_answer":
                session["answers"] = [{"id": "a_current", "turn_id": current["id"]}]

        _mutate(runtime, channel, seed)
        channel._select_act = AsyncMock()
        session = runtime.interviews.get_interview(channel.interview_id)
        turn = next(item for item in session["turns"] if item["id"] == session["current_turn_id"])
        await channel._select_turn(turn, None)
        assert channel._select_act.await_args.kwargs["text"] == turn["question_spoken_text"]
        assert runtime.interviews.get_interview(channel.interview_id) == session

    asyncio.run(scenario())


def test_acknowledgement_after_a_submitted_answer_is_preserved():
    async def scenario():
        runtime, channel, _, _ = _harness()

        def seed(session):
            session["turns"].insert(0, {
                "id": "recent", "current_understanding": {"intent": "answer"},
            })
            session["answers"] = [{"id": "a_recent", "turn_id": "recent"}]
            session["agent_runtime"]["next_transition_key"] = "acknowledge"

        _mutate(runtime, channel, seed)
        channel._select_act = AsyncMock()
        turn = runtime.interviews.get_interview(channel.interview_id)["turns"][1]
        await channel._select_turn(turn, None)
        assert channel._select_act.await_args.kwargs["text"] == "谢谢你的说明。" + turn["question_spoken_text"]

    asyncio.run(scenario())


@pytest.mark.parametrize("act_type", ["followup", "repeat"])
def test_non_question_delivery_keeps_its_approved_text(act_type):
    async def scenario():
        runtime, channel, _, _ = _harness()
        _mutate(runtime, channel, lambda session: session["agent_runtime"].update(next_transition_key="acknowledge"))
        channel._select_act = AsyncMock()
        turn = runtime.interviews.get_interview(channel.interview_id)["turns"][0]
        await channel._select_turn(turn, None, act_type=act_type)
        assert channel._select_act.await_args.kwargs["act_type"] == act_type
        assert channel._select_act.await_args.kwargs["text"] == turn["question_spoken_text"]

    asyncio.run(scenario())


@pytest.mark.parametrize("recent_understanding", [
    {"intent": "answer", "turn_intent": "finish_answer"},
    {},
])
def test_transition_cannot_apologize_for_an_earlier_decline(recent_understanding):
    async def scenario():
        runtime, channel, _, _ = _harness()
        def seed(session):
            current = session["turns"][0]
            session["turns"] = [
                {"id": "old", "current_understanding": {"intent": "answer_declined"}},
                {"id": "recent", "current_understanding": recent_understanding},
                current,
                {"id": "future", "current_understanding": {"intent": "answer_declined"}},
            ]
            session["answers"] = [{"id": "a1", "turn_id": "old"}, {"id": "a2", "turn_id": "recent"}]
            session["agent_runtime"]["next_transition_key"] = "declined"
        _mutate(runtime, channel, seed)
        channel._select_act = AsyncMock()
        turn = runtime.interviews.get_interview(channel.interview_id)["turns"][2]
        await channel._select_turn(turn, None)
        text = channel._select_act.await_args.kwargs["text"]
        assert "没关系" not in text
        assert text.endswith(turn["question_spoken_text"])
    asyncio.run(scenario())


def test_transition_acknowledges_only_the_immediately_preceding_submitted_decline():
    async def scenario():
        runtime, channel, _, _ = _harness()
        def seed(session):
            session["turns"].insert(0, {"id": "recent", "current_understanding": {"intent": "answer_declined"}})
            session["answers"] = [{"id": "a1", "turn_id": "recent"}]
            session["agent_runtime"]["next_transition_key"] = "continue"
        _mutate(runtime, channel, seed)
        channel._select_act = AsyncMock()
        await channel._select_turn(runtime.interviews.get_interview(channel.interview_id)["turns"][1], None)
        assert channel._select_act.await_args.kwargs["text"].startswith("没关系，我们换个方向。")
    asyncio.run(scenario())


def test_local_only_worker_snapshot_cannot_make_a_gap_in_live_api_delivery(tmp_path, monkeypatch):
    async def scenario():
        seed_runtime, seed_channel, _, _ = _harness()
        path = str(tmp_path / "isolated_processes.sqlite3")
        api_store, worker_store = SQLiteStore(path), SQLiteStore(path)
        with persistence_for(api_store).transaction(seed_channel.organization_id) as tx:
            tx.interview_sessions.add(seed_runtime.interviews.get_interview(seed_channel.interview_id))
        api_runtime, worker_runtime = InterviewAgentRuntime(api_store), InterviewAgentRuntime(worker_store)
        api_channel = AgentChannel(api_runtime, seed_channel.opened)
        api_hub, worker_hub = interview_agent._AgentChannelHub(), interview_agent._AgentChannelHub()
        local_bus = SimpleNamespace(enabled=False)
        monkeypatch.setattr(interview_agent, "realtime_event_bus", lambda: local_bus)
        api_hub._bus = worker_hub._bus = local_bus
        api_hub.register(api_channel)
        monkeypatch.setattr(interview_agent, "_AGENT_CHANNEL_HUB", api_hub)
        await api_channel._emit("floor.changed", {"owner": "agent", "reason": "approved_conversation_act"},
                                replayability=Replayability.REPLAYABLE)
        # Isolate the process-local hubs but retain a real shared database.
        with monkeypatch.context() as worker_process:
            worker_process.setattr(interview_agent, "_AGENT_CHANNEL_HUB", worker_hub)
            await worker_runtime.publish_snapshot(api_channel.interview_id, api_channel.organization_id)
        await api_channel._emit("avatar.performance.producer_finished", {
            "performance_id": "performance_test", "output_id": "output_test", "total_samples": 480,
            "sample_rate_hz": 24_000,
        }, replayability=Replayability.TRANSIENT)
        events = list(api_channel._queue._queue)
        assert [event.session_sequence for event in events] == [1, 2]
        assert api_runtime.interviews.get_interview(api_channel.interview_id)["agent_runtime"]["last_sequence"] == 2
    asyncio.run(scenario())


def test_worker_snapshot_still_publishes_when_cross_instance_bus_is_enabled(monkeypatch):
    async def scenario():
        runtime, channel, _, _ = _harness()
        hub = interview_agent._AgentChannelHub()
        bus = SimpleNamespace(enabled=True, publish=AsyncMock())
        hub._bus = bus
        monkeypatch.setattr(interview_agent, "_AGENT_CHANNEL_HUB", hub)
        monkeypatch.setattr(interview_agent, "realtime_event_bus", lambda: bus)
        await runtime.publish_snapshot(channel.interview_id, channel.organization_id)
        event = bus.publish.await_args.args[1]["event"]
        assert event["type"] == "session.snapshot" and event["payload"] == {}
        assert event["session_sequence"] == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("unavailable_peer", ["foreign_tenant", "closed", "terminating"])
def test_unreachable_or_other_tenant_peer_does_not_allocate_local_worker_sequence(monkeypatch, unavailable_peer):
    async def scenario():
        runtime, channel, _, _ = _harness()
        hub = interview_agent._AgentChannelHub()
        bus = SimpleNamespace(enabled=False)
        hub._bus = bus
        monkeypatch.setattr(interview_agent, "_AGENT_CHANNEL_HUB", hub)
        monkeypatch.setattr(interview_agent, "realtime_event_bus", lambda: bus)
        if unavailable_peer == "foreign_tenant":
            channel.organization_id = "another_organization"
        elif unavailable_peer == "closed":
            channel._closed = True
        else:
            channel._terminating = True
        hub.register(channel)
        await runtime.publish_snapshot(channel.interview_id, "org_default")
        assert runtime.interviews.get_interview(channel.interview_id)["agent_runtime"]["last_sequence"] == 0
        assert channel._queue.empty()
    asyncio.run(scenario())


def test_opening_a_peer_does_not_hide_its_fresh_snapshot_sequence_from_existing_peers(monkeypatch):
    async def scenario():
        runtime, opening, _, _ = _harness()
        hub = interview_agent._AgentChannelHub()
        hub._bus = SimpleNamespace(enabled=False)
        monkeypatch.setattr(interview_agent, "_AGENT_CHANNEL_HUB", hub)
        existing = AgentChannel(runtime, opening.opened.model_copy(update={"connection_id": "existing"}))
        monitor_principal = replace(opening.principal, roles=frozenset({"interviewer"}))
        monitor = AgentChannel(runtime, opening.opened.model_copy(update={"connection_id": "monitor", "principal": monitor_principal}))
        foreign = AgentChannel(runtime, opening.opened.model_copy(update={"connection_id": "foreign",
            "principal": replace(opening.principal, organization_id="another_organization")}))
        for peer in [existing, monitor, foreign]:
            hub.register(peer)
        await opening._queue_fresh_snapshot()
        hub.register(opening)
        await opening._emit("floor.changed", {"owner": "candidate", "reason": "agent_finished"},
                            replayability=Replayability.REPLAYABLE)
        for peer in [opening, existing, monitor]:
            events = list(peer._queue._queue)
            assert [event.session_sequence for event in events] == [1, 2]
            assert [event.type for event in events] == ["session.snapshot", "floor.changed"]
            assert ("candidate" in events[0].payload) is (peer is monitor)
        assert foreign._queue.empty()
    asyncio.run(scenario())
