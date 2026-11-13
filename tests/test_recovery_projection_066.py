"""Durable recovery is a scoped display fact, never microphone authorization."""
import asyncio
from copy import deepcopy

import pytest

from app.domain.interview_agent import ClientSignal, FloorOwner
from test_answer_preparation_projection import _snapshot
from test_automatic_turn_integration import _automatic_session, _current
from test_evidence_owner_recovery_integration import (
    INTERVIEW_ID, ORGANIZATION_ID, TURN_ID, _opened, _ticket,
)


@pytest.mark.parametrize("reason,status", [
    ("understanding_unavailable", "retrying"),
    ("understanding_retry_exhausted", "retry_required"),
    ("understanding_result_rejected", "retry_required"),
])
@pytest.mark.parametrize("floor", ["candidate", "agent", "none"])
def test_recovery_survives_lost_delivery_and_reconnect_on_each_floor(tmp_path, monkeypatch, reason, status, floor):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
            endpoint, capture_id, chain = managed._answer_endpoint, managed._capture_id, managed.chain
            runtime._set_floor(INTERVIEW_ID, FloorOwner(floor), "synthetic_recovery_floor", ORGANIZATION_ID)
            original_emit = channel._emit
            async def lost_problem(kind, payload, **kwargs):
                if kind == "problem":
                    raise RuntimeError("synthetic disconnected controller")
                return await original_emit(kind, payload, **kwargs)
            monkeypatch.setattr(channel, "_emit", lost_problem)
            await endpoint._notify(reason)
            expected = {"status": status, "turn_id": TURN_ID, "capture_id": capture_id}
            snapshot = _snapshot(runtime, channel)
            assert snapshot["answer_preparation"] == expected and snapshot["floor"] == floor
            assert _current(runtime)["agent_runtime"]["answer_preparation"] == expected
            command_count = len(store.evidence_commands)
            await channel.close("synthetic_control_drop")
            _ticket(store, "recovery_projection_066")
            replacement = await runtime.open(_opened("recovery_projection_066"))
            try:
                recovered = [event.payload for event in replacement._queue._queue if getattr(event, "type", None) == "session.snapshot"]
                assert recovered and recovered[-1]["answer_preparation"] == expected
                assert recovered[-1]["floor"] == floor
                assert set(recovered[-1]["answer_preparation"]) == {"status", "turn_id", "capture_id"}
                assert len(store.evidence_commands) == command_count
                assert managed.chain is chain and managed._answer_endpoint is endpoint
                assert managed._capture_id == capture_id and chain.is_open
                assert not _current(runtime)["answers"]
            finally:
                await replacement.close("test_complete")
    asyncio.run(scenario())


@pytest.mark.parametrize("reason,status", [
    ("understanding_unavailable", "retrying"), ("understanding_result_rejected", "retry_required"),
])
def test_ordinary_activity_keeps_recovery_until_scoped_success(tmp_path, monkeypatch, reason, status):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (_, runtime, channel, managed):
            endpoint = managed._answer_endpoint
            capture_id = managed._capture_id
            await endpoint._notify(reason)
            expected = {"status": status, "turn_id": TURN_ID, "capture_id": capture_id}
            for activity in ("answer_listening", "answer_preparing", "supplement_awaiting_reply", "exit_awaiting_reply", "transcript_unavailable"):
                await endpoint._notify(activity)
                assert _snapshot(runtime, channel)["answer_preparation"] == expected
            await endpoint._notify("understanding_retrying")
            assert _snapshot(runtime, channel)["answer_preparation"] == {**expected, "status": "retrying"}
            ack = [event for event in channel._queue._queue if getattr(event, "type", None) == "floor.changed"
                   and event.payload.get("reason") == "understanding_retrying"]
            assert ack and ack[-1].turn_id == TURN_ID and ack[-1].payload["capture_id"] == capture_id
            assert not _current(runtime)["answers"] and managed.chain.is_open
            await endpoint._notify("understanding_recovered")
            assert _snapshot(runtime, channel)["answer_preparation"] is None
            assert "answer_preparation" not in _current(runtime)["agent_runtime"]
            assert managed._capture_id == capture_id and managed.chain.is_open
    asyncio.run(scenario())


def test_recovery_cannot_leak_between_captures_or_turns(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, ["", ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            old_endpoint, old_capture = managed._answer_endpoint, managed._capture_id
            await old_endpoint._notify("understanding_result_rejected")
            await managed.pause_capture()
            assert _snapshot(runtime, channel)["answer_preparation"] is None
            await channel.send(ClientSignal(type="evidence.stream.open", idempotency_key="replacement_recovery_066",
                turn_id=TURN_ID, payload={"content_type": "audio/pcm", "sample_rate_hz": 16000, "channels": 1, "language": "zh-CN"}))
            capture_id = managed._capture_id
            assert capture_id != old_capture
            await managed._answer_endpoint._notify("understanding_unavailable")
            expected = _snapshot(runtime, channel)["answer_preparation"]
            for reason in ("understanding_result_rejected", "understanding_recovered"):
                await old_endpoint._notify(reason)
                assert _snapshot(runtime, channel)["answer_preparation"] == expected
            for stale_capture, stale_turn in ((old_capture, TURN_ID), (capture_id, "old_turn")):
                managed._persist_answer_preparation(stale_capture, stale_turn, preparing=True, status="retry_required")
                managed._persist_answer_preparation(stale_capture, stale_turn, preparing=False)
                assert _snapshot(runtime, channel)["answer_preparation"] == expected
            current = _current(runtime)
            for mutate in (
                lambda value: value.update(current_turn_id="successor_turn"),
                lambda value: value.update(status="paused"),
                lambda value: value["answers"].append({"turn_id": TURN_ID}),
                lambda value: value["agent_runtime"].update(takeover={"status": "active"}),
            ):
                inactive = deepcopy(current)
                mutate(inactive)
                assert runtime._snapshot(inactive, channel.principal)["answer_preparation"] is None
            assert not _current(runtime)["answers"] and managed.chain.is_open
    asyncio.run(scenario())


def test_real_capture_origin_only_advances_after_accepted_provider_final(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, ["服务端最终确认的合成片段。"], spoken_confirmation=True) as (_, runtime, _, managed):
            endpoint = managed._answer_endpoint
            assert managed.chain.recovery_origin == 0
            await managed.chain.send_audio(bytes(640))
            for _ in range(3):
                endpoint._observe_final_snapshot(None)
            endpoint.speech_started(source="audio")
            assert endpoint.awaiting_transcript and managed.chain.recovery_origin == 0
            final = await managed.chain.transcript_snapshot()
            assert final is not None and managed.chain.recovery_origin == 640
            endpoint.speech_started(source="audio")
            assert not endpoint.awaiting_transcript and endpoint._missing_final_attempts == 0
            assert managed.chain.is_open and not _current(runtime)["answers"]
    asyncio.run(scenario())
