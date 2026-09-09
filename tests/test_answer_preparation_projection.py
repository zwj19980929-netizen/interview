"""Preparation snapshots recover lost notices without opening or sealing audio."""

import asyncio
from copy import deepcopy

import pytest

from app.core.errors import ApiError
from app.domain.interview_agent import ClientSignal
from app.persistence.provider import persistence_for
from app.services.evidence_coordination import EvidenceOwnershipCoordinator

from test_automatic_turn_integration import _automatic_session, _current
from test_evidence_owner_recovery_integration import INTERVIEW_ID, ORGANIZATION_ID, TURN_ID


def _snapshot(runtime, channel):
    return runtime._snapshot(_current(runtime), channel.principal)


def test_preparation_and_exit_survive_lost_transient_notices(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
            endpoint = managed._answer_endpoint
            capture_id = managed._capture_id
            original_emit = channel._emit
            lost = []

            async def lose_floor_notice(event_type, payload, **kwargs):
                if event_type == "floor.changed" and payload.get("reason") in {
                    "answer_preparing", "answer_listening", "supplement_awaiting_reply",
                }:
                    lost.append(payload["reason"])
                    raise RuntimeError("synthetic transient delivery failure")
                return await original_emit(event_type, payload, **kwargs)

            monkeypatch.setattr(channel, "_emit", lose_floor_notice)
            await endpoint._notify("answer_preparing")
            expected = {"status": "preparing", "turn_id": TURN_ID, "capture_id": capture_id}
            assert _snapshot(runtime, channel)["answer_preparation"] == expected
            assert managed.evidence_open and managed.chain.is_open

            # Reconnecting repeats the scoped ready handshake. Same-owner
            # floor deduplication need not update floor_reason, so neither
            # readiness nor that generic reason describes a model wait.
            await managed._project_open_ready(channel, kind="formal", causation_id="open_again")
            assert _current(runtime)["agent_runtime"]["floor_reason"] != "answer_preparing"
            assert _snapshot(runtime, channel)["answer_preparation"] == expected

            await endpoint._notify("supplement_awaiting_reply")
            snapshot = _snapshot(runtime, channel)
            assert snapshot["answer_preparation"] is None
            assert snapshot["supplement_confirmation"] == {
                "status": "awaiting_reply", "turn_id": TURN_ID, "capture_id": capture_id,
            }
            await endpoint._notify("answer_preparing")
            await endpoint._notify("answer_listening")
            assert _snapshot(runtime, channel)["answer_preparation"] is None
            assert _snapshot(runtime, channel)["supplement_confirmation"]["status"] == "listening"
            assert lost == ["answer_preparing", "supplement_awaiting_reply", "answer_preparing", "answer_listening"]
            assert managed._capture_id == capture_id and managed.evidence_open
            assert _current(runtime)["answers"] == []
            assert not any(command["command_type"] == "evidence.seal" for command in store.evidence_commands.values())

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", [
    "understanding_unavailable", "understanding_retry_exhausted", "transcript_unavailable",
])
def test_failed_preparation_clears_truth_even_when_problem_notice_is_lost(tmp_path, monkeypatch, reason):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (_, runtime, channel, managed):
            await managed._answer_endpoint._notify("answer_preparing")

            async def unavailable_delivery(*args, **kwargs):
                raise RuntimeError("synthetic unavailable controller")

            monkeypatch.setattr(channel, "_emit", unavailable_delivery)
            await managed._answer_endpoint._notify(reason)
            assert _snapshot(runtime, channel)["answer_preparation"] is None
            assert "answer_preparation" not in _current(runtime)["agent_runtime"]
            assert managed.evidence_open and _current(runtime)["answers"] == []

    asyncio.run(scenario())


def test_pause_and_new_capture_drop_preparation_and_reject_late_scope(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, ["", ""], spoken_confirmation=True) as (_, runtime, channel, managed):
            old_endpoint = managed._answer_endpoint
            old_capture = managed._capture_id
            await old_endpoint._notify("answer_preparing")
            await managed.pause_capture()
            assert _snapshot(runtime, channel)["answer_preparation"] is None
            assert not managed.evidence_open
            await channel.send(ClientSignal(type="evidence.stream.open", idempotency_key="open_replacement",
                turn_id=TURN_ID, payload={"content_type": "audio/pcm", "sample_rate_hz": 16_000,
                                         "channels": 1, "language": "zh-CN"}))
            assert managed._capture_id and managed._capture_id != old_capture
            assert _snapshot(runtime, channel)["answer_preparation"] is None
            await managed._answer_endpoint._notify("answer_preparing")
            expected = _snapshot(runtime, channel)["answer_preparation"]
            assert expected["capture_id"] == managed._capture_id

            # A delayed old callback or mismatched cleanup cannot erase a
            # later capture's authoritative preparation.
            await old_endpoint._notify("answer_listening")
            managed._persist_answer_preparation(old_capture, TURN_ID, preparing=False)
            managed._persist_answer_preparation(managed._capture_id, "old_turn", preparing=False)
            assert _snapshot(runtime, channel)["answer_preparation"] == expected
            await managed.stop("test_complete")
            assert _snapshot(runtime, channel)["answer_preparation"] is None
            assert _current(runtime)["answers"] == []

    asyncio.run(scenario())


def test_stale_owner_cannot_clear_successor_preparation(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
            await managed._answer_endpoint._notify("answer_preparing")
            old_capture = managed._capture_id
            coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=30)
            old_owner = managed.ownership
            assert coordinator.release(old_owner)
            successor = coordinator.claim_owner(interview_id=INTERVIEW_ID,
                organization_id=ORGANIZATION_ID, local_instance_id="snapshot_successor")
            assert successor.ownership_epoch > old_owner.ownership_epoch
            expected = {"status": "preparing", "turn_id": TURN_ID, "capture_id": "capture_successor"}
            with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
                session = transaction.interview_sessions.get(INTERVIEW_ID)
                session["agent_runtime"]["answer_preparation"] = expected
                transaction.interview_sessions.update(session, expected_version=session["version"])
            managed._persist_answer_preparation(old_capture, TURN_ID, preparing=False)
            with pytest.raises(ApiError):
                managed._persist_answer_preparation(old_capture, TURN_ID, preparing=True)
            assert _snapshot(runtime, channel)["answer_preparation"] == expected
            # Revoke the stale test stream instead of letting context cleanup
            # request disconnect repair under the deliberately lost owner.
            await managed.pause_capture()
            assert _snapshot(runtime, channel)["answer_preparation"] == expected

    asyncio.run(scenario())


@pytest.mark.parametrize("cleanup", ["pause", "stop"])
def test_preparation_state_write_failure_does_not_prevent_audio_shutdown(tmp_path, monkeypatch, cleanup):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (_, runtime, _, managed):
            await managed._answer_endpoint._notify("answer_preparing")
            endpoint = managed._answer_endpoint
            ingress = managed._ingress

            def unavailable_storage(*args, **kwargs):
                raise RuntimeError("synthetic projection storage failure")

            monkeypatch.setattr(managed, "_persist_answer_preparation", unavailable_storage)
            if cleanup == "pause":
                await managed.pause_capture()
            else:
                await managed.stop("test_complete")
                assert not ingress.connected
            assert endpoint._closed
            assert not managed.chain.is_open
            assert not managed.evidence_open
            assert _current(runtime)["answers"] == []

    asyncio.run(scenario())


def test_snapshot_only_projects_safe_current_active_preparation(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (_, runtime, channel, managed):
            await managed._answer_endpoint._notify("answer_preparing")
            current = _current(runtime)
            expected = current["agent_runtime"]["answer_preparation"]
            current["agent_runtime"]["answer_preparation"] = {**expected, "private_debug": "not_public", "ready": True}
            assert runtime._snapshot(current, channel.principal)["answer_preparation"] == expected
            mutations = [
                lambda session: session.update(status="paused"),
                lambda session: session.update(status="completed"),
                lambda session: session.update(current_turn_id="another_turn"),
                lambda session: session["answers"].append({"turn_id": TURN_ID}),
                lambda session: session["agent_runtime"].update(floor="agent"),
                lambda session: session["agent_runtime"].update(calibration_status="listening"),
                lambda session: session["agent_runtime"].update(takeover={"status": "active"}),
                lambda session: session["agent_runtime"].update(capture_recovery={
                    "status": "recovering", "turn_id": TURN_ID, "capture_id": managed._capture_id,
                    "attempt": 1, "max_attempts": 3,
                }),
            ]
            for mutate in mutations:
                inactive = deepcopy(current)
                mutate(inactive)
                assert runtime._snapshot(inactive, channel.principal)["answer_preparation"] is None

    asyncio.run(scenario())
