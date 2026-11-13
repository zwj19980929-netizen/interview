"""Revocation is a precise control event before synthetic media teardown."""
import asyncio
from copy import deepcopy
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.adapters.livekit_audio_output import LiveKitApprovedAudioPublisher
from app.core.errors import ApiError
from app.domain.interview_agent import ClientSignal, FloorOwner
from app.model_gateway.errors import ProviderError
from app.repositories.sqlite import SQLiteStore
from app.services.interview_agent import AgentChannel
from app.services.livekit_evidence_ingress import ManagedLiveKitEvidenceSession
from app.services.speech_output_interruption import revoke_active_performance
from test_livekit_audio_output import _RTC, _Room
from test_streaming_expression_lifecycle import _streaming_harness, _select, _signal, _session, _wait_until
from test_expression_stale_fence import _mutate


def managed_fixture(h, **values):
    evidence = SimpleNamespace(persistence=h.runtime.persistence, ownership=h.grant,
        _capture_id="capture_synthetic", _active_warmup_epoch=1, _answer_endpoint=None,
        _stopped=False, **values)
    if hasattr(ManagedLiveKitEvidenceSession, "_output_transition_guard"):
        evidence._output_transition_guard = MethodType(ManagedLiveKitEvidenceSession._output_transition_guard, evidence)
    return evidence


def native_publisher(before_abort):
    room = _Room()
    rtc = _RTC(room)
    current = {"valid": True}
    def guard():
        if not current["valid"]:
            raise ApiError("EXPRESSION_OWNER_STALE", "Synthetic revocation", status_code=409)
    publisher = LiveKitApprovedAudioPublisher(
        SimpleNamespace(url="wss://local.test", issue_audio_publisher_token=lambda **kwargs: "synthetic"),
        room_name="synthetic_room", performance_id="performance_1", assert_current=guard,
        rtc_module=rtc, operation_timeout_seconds=.1, before_abort=before_abort,
    )
    return publisher, room, rtc, current


@pytest.mark.anyio
async def test_native_stale_cleanup_notifies_before_unpublish_and_clears_audio_immediately():
    entered, release = asyncio.Event(), asyncio.Event()
    async def notify():
        entered.set()
        await release.wait()
    publisher, room, rtc, current = native_publisher(notify)
    await publisher.open()
    current["valid"] = False
    task = asyncio.create_task(publisher.publish(b"\1\0" * 480))
    await asyncio.wait_for(entered.wait(), .3)
    assert rtc.sources[0].cleared > 0
    assert room.local_participant.unpublished == []
    release.set()
    with pytest.raises(ApiError):
        await task
    assert len(room.local_participant.unpublished) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("notify_failure", ["error", "blocked"])
async def test_native_notification_failure_still_releases_only_old_track(notify_failure):
    calls = []
    async def notify():
        calls.append(True)
        if notify_failure == "error":
            raise RuntimeError("Synthetic control transport error")
        await asyncio.Event().wait()
    publisher, room, _rtc, _current = native_publisher(notify)
    await publisher.open()
    await asyncio.wait_for(publisher.abort(), .6)
    await publisher.abort()
    assert len(calls) == 1 and len(room.local_participant.unpublished) == 1


@pytest.mark.anyio
async def test_normal_native_close_never_emits_interruption():
    notify = AsyncMock()
    publisher, room, rtc, _current = native_publisher(notify)
    await publisher.open()
    await publisher.finish()
    await publisher.aclose()
    await publisher.abort()
    notify.assert_not_awaited()
    assert len(room.local_participant.unpublished) == 1


@pytest.mark.anyio
async def test_cancelled_native_notification_waiter_still_releases_resources():
    entered = asyncio.Event()
    async def notify():
        entered.set()
        await asyncio.Event().wait()
    publisher, room, rtc, _current = native_publisher(notify)
    await publisher.open()
    task = asyncio.create_task(publisher.abort())
    await asyncio.wait_for(entered.wait(), .3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, .6)
    assert room.disconnected == 1 and rtc.sources[0].closed
    assert len(room.local_participant.unpublished) == 1


@pytest.mark.anyio
async def test_acknowledged_real_output_cleanup_is_not_an_interruption(monkeypatch):
    h = _streaming_harness(monkeypatch)
    performance = await _select(h)
    item = h.outputs[0]
    await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
    await _wait_until(lambda: item.output.producer_finished)
    await _signal(h, "avatar.performance.stopped", performance.performance_id, item.output.output_id, "drained")
    await _wait_until(lambda: h.channel._speech_output is None)
    assert item.output.playback_drained and item.publisher.close_calls == 1
    assert not [event for event in h.emitted if event["type"] == "avatar.performance.interrupted"]


@pytest.mark.anyio
async def test_real_output_floor_revocation_emits_exact_interrupt_before_abort(monkeypatch):
    h = _streaming_harness(monkeypatch, gate_eof=True)
    performance = await _select(h)
    item = h.outputs[0]
    await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
    await item.raw.eof_reached.wait()
    h.runtime._set_floor(h.channel.interview_id, FloorOwner.NONE, "synthetic_next_topic", h.channel.organization_id)
    await _wait_until(lambda: h.channel._speech_output is None)
    events = [event for event in h.emitted if event["type"] == "avatar.performance.interrupted"]
    assert len(events) == 1 and events[0]["payload"]["performance_id"] == performance.performance_id
    assert item.publisher.interruption_before_abort is True
    assert item.publisher.abort_calls >= 1
    assert _session(h)["status"] == "in_progress"


@pytest.mark.anyio
async def test_genuine_provider_failure_keeps_fatal_behavior_without_interruption(monkeypatch):
    h = _streaming_harness(monkeypatch)
    performance = await _select(h)
    item = h.outputs[0]
    item.raw.error = ProviderError("provider_transport_failed", "Synthetic provider failure", retryable=False)
    await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
    await _wait_until(lambda: h.channel._speech_output is None)
    session = _session(h)
    assert session["status"] == "paused"
    assert not [event for event in h.emitted if event["type"] == "avatar.performance.interrupted"]
    assert item.publisher.abort_calls >= 1


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_continue_orders_precise_revocation_before_output_cleanup_and_floor(monkeypatch, tmp_path, backend):
    if backend == "sqlite":
        monkeypatch.setattr("test_expression_stale_fence.InMemoryStore", lambda: SQLiteStore(str(tmp_path / "continue.sqlite3")))
    h = _streaming_harness(monkeypatch, gate_eof=True)
    performance = await _select(h)
    item = h.outputs[0]
    await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
    await item.raw.eof_reached.wait()
    endpoint = SimpleNamespace(continue_speaking=Mock())
    evidence = managed_fixture(h, supervisor=SimpleNamespace(interviews=h.runtime.interviews),
        interview_id=h.channel.interview_id, organization_id=h.channel.organization_id,
        _matches_capture=lambda signal: True, cancel_endpoint=AsyncMock(),
        _capture_speech_started=False, turn_id="turn_1", chain=SimpleNamespace())
    evidence._answer_endpoint = endpoint
    signal = ClientSignal(type="evidence.continue", idempotency_key="synthetic_continue", turn_id="turn_1")
    await ManagedLiveKitEvidenceSession._apply_continue(evidence, h.channel, signal)
    events = h.emitted
    interrupts = [event for event in events if event["type"] == "avatar.performance.interrupted"]
    assert len(interrupts) == 1 and interrupts[0]["payload"]["performance_id"] == performance.performance_id
    assert h.channel._speech_output is None and item.publisher.interruption_before_abort
    assert _session(h)["agent_runtime"]["floor"] == "candidate"


@pytest.mark.anyio
async def test_explicit_revoke_and_native_stale_cleanup_share_one_notice(monkeypatch):
    h = _streaming_harness(monkeypatch, gate_eof=True)
    performance = await _select(h)
    item = h.outputs[0]
    await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
    await item.raw.eof_reached.wait()
    entered, release = asyncio.Event(), asyncio.Event()
    emit, calls = h.channel._emit, []
    async def delayed_emit(kind, payload, **kwargs):
        if kind == "avatar.performance.interrupted":
            calls.append(payload["performance_id"])
            entered.set()
            await release.wait()
        return await emit(kind, payload, **kwargs)
    h.channel._emit = delayed_emit
    task = asyncio.create_task(revoke_active_performance(h.channel, performance.performance_id,
                                                        reason="candidate_continues", turn_id="turn_1"))
    await asyncio.wait_for(entered.wait(), .5)
    await asyncio.sleep(.06)
    assert calls == [performance.performance_id] and item.publisher.abort_calls == 0
    release.set()
    assert await task
    await _wait_until(lambda: h.channel._speech_output is None)
    assert calls == [performance.performance_id] and item.publisher.interruption_before_abort


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_managed_warmup_reset_revokes_output_before_capture_or_floor_changes(monkeypatch, tmp_path, backend):
    if backend == "sqlite":
        monkeypatch.setattr("test_expression_stale_fence.InMemoryStore", lambda: SQLiteStore(str(tmp_path / "reset.sqlite3")))
    h = _streaming_harness(monkeypatch, gate_eof=True)
    _mutate(h.runtime, h.channel, lambda session: (session.update(current_turn_id=None, turns=[]),
        session["agent_runtime"].update(calibration_status="awaiting_confirmation")))
    performance = await _select(h, turn_id=None)
    item = h.outputs[0]
    await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
    await item.raw.eof_reached.wait()
    async def abort_warmup():
        assert h.channel._speech_output is None
        assert item.publisher.interruption_before_abort
    evidence = managed_fixture(h, _event_source=h.channel,
        interview_id=h.channel.interview_id, organization_id=h.channel.organization_id,
        chain=SimpleNamespace(abort_warmup=abort_warmup),
        _deactivate_partial_projection=AsyncMock(), _warmup_ingress_restart_required=False,
        turn_id=None)
    claimed = SimpleNamespace(command_type="evidence.reset", command_id="reset_synthetic",
                              turn_id=None, causation_id=None, payload={}, control_generation=h.grant.control_generation)
    await ManagedLiveKitEvidenceSession._apply_claimed_command(evidence, claimed)
    assert _session(h)["agent_runtime"]["calibration_status"] == "retrying"
    assert len([event for event in h.emitted if event["type"] == "avatar.performance.interrupted"]) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["unpublish", "source_close"])
async def test_cancellation_in_late_native_cleanup_still_releases_remaining_resources(stage):
    publisher, room, rtc, _current = native_publisher(AsyncMock())
    await publisher.open()
    entered = asyncio.Event()
    async def blocked(*_args):
        entered.set()
        await asyncio.Event().wait()
    if stage == "unpublish":
        room.local_participant.unpublish_track = blocked
    else:
        rtc.sources[0].aclose = blocked
    task = asyncio.create_task(publisher.abort())
    await asyncio.wait_for(entered.wait(), .3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, .6)
    assert room.disconnected == 1
    if stage == "unpublish":
        assert rtc.sources[0].closed


@pytest.mark.anyio
@pytest.mark.parametrize("command", ["continue", "reset"])
@pytest.mark.parametrize("boundary", ["status", "turn", "capture", "owner", "control", "calibration", "performance", "selection"])
async def test_managed_transition_rechecks_after_awaited_revocation(monkeypatch, command, boundary):
    h = _streaming_harness(monkeypatch, gate_eof=True)
    if command == "reset":
        _mutate(h.runtime, h.channel, lambda session: (session.update(current_turn_id=None, turns=[]),
            session["agent_runtime"].update(calibration_status="awaiting_confirmation")))
    turn_id = "turn_1" if command == "continue" else None
    performance = await _select(h, turn_id=turn_id)
    item = h.outputs[0]
    await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
    await item.raw.eof_reached.wait()
    endpoint = SimpleNamespace(continue_speaking=Mock())
    abort_warmup = AsyncMock()
    evidence = managed_fixture(h, _event_source=h.channel,
        supervisor=SimpleNamespace(interviews=h.runtime.interviews),
        interview_id=h.channel.interview_id, organization_id=h.channel.organization_id,
        chain=SimpleNamespace(abort_warmup=abort_warmup), _matches_capture=lambda signal: True,
        cancel_endpoint=AsyncMock(), _capture_speech_started=False, turn_id=turn_id,
        _deactivate_partial_projection=AsyncMock(), _warmup_ingress_restart_required=False)
    evidence._answer_endpoint = endpoint
    entered, release = asyncio.Event(), asyncio.Event()
    emit = h.channel._emit
    async def delayed_emit(kind, payload, **kwargs):
        if kind == "avatar.performance.interrupted":
            entered.set()
            await release.wait()
        return await emit(kind, payload, **kwargs)
    h.channel._emit = delayed_emit
    if command == "continue":
        operation = ManagedLiveKitEvidenceSession._apply_continue(evidence, h.channel,
            ClientSignal(type="evidence.continue", idempotency_key="stale_continue", turn_id=turn_id))
    else:
        operation = ManagedLiveKitEvidenceSession._apply_claimed_command(evidence, SimpleNamespace(
            command_type="evidence.reset", command_id="stale_reset", turn_id=None, causation_id=None,
            payload={}, control_generation=h.grant.control_generation))
    task = asyncio.create_task(operation)
    await asyncio.wait_for(entered.wait(), .5)
    if boundary == "owner":
        h.coordinator.release(h.grant)
        h.coordinator.attach_control(interview_id=h.channel.interview_id, organization_id=h.channel.organization_id,
            connection_id="new_controller", local_instance_id="new_instance")
    elif boundary == "control":
        h.coordinator.attach_control(interview_id=h.channel.interview_id, organization_id=h.channel.organization_id,
            connection_id="new_controller", local_instance_id=h.grant.local_instance_id)
    elif boundary == "capture":
        evidence._capture_id = "capture_replacement"
    else:
        def mutate(session):
            if boundary == "status":
                session.update(status="paused")
            elif boundary == "turn":
                session.update(current_turn_id="replacement_turn")
            elif boundary == "performance":
                session["agent_runtime"]["active_performance_id"] = "replacement_performance"
            elif boundary == "selection":
                replacement = deepcopy(next(event for event in reversed(session["agent_events"])
                    if event["type"] == "conversation.act.selected"))
                replacement["event_id"] = "replacement_selection_same_turn"
                session["agent_events"].append(replacement)
            else:
                session["agent_runtime"]["calibration_status"] = "listening"
        _mutate(h.runtime, h.channel, mutate)
    before = _session(h)
    release.set()
    with pytest.raises(ApiError):
        await asyncio.wait_for(task, 1)
    after = _session(h)
    assert after["status"] == before["status"] and after["current_turn_id"] == before["current_turn_id"]
    assert after["agent_runtime"]["floor"] == before["agent_runtime"]["floor"]
    assert after["agent_runtime"]["calibration_status"] == before["agent_runtime"]["calibration_status"]
    assert after["agent_runtime"].get("active_performance_id") == before["agent_runtime"].get("active_performance_id")
    abort_warmup.assert_not_awaited()
    endpoint.continue_speaking.assert_not_called()
