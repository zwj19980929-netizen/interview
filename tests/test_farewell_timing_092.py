"""Farewell watchdog must outlast actual audio without becoming renewable."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.domain.interview_agent import ClientSignal
from app.persistence.provider import persistence_for
import app.services.interview_agent as agent_module
from test_farewell_delivery_089 import _live_terminal
from test_terminal_cleanup_086 import _without_controller


class _PlaybackClock:
    """Advance only this module's sleeps; owner heartbeat time remains real."""

    def __init__(self):
        self.now = 0.0
        self.waiters = []

    async def sleep(self, seconds):
        if seconds <= 0:
            return await asyncio.sleep(0)
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + seconds, future))
        await future

    async def advance(self, seconds):
        self.now += seconds
        for deadline, future in self.waiters:
            if deadline <= self.now and not future.done():
                future.set_result(None)
        for _ in range(20):
            await asyncio.sleep(0)

    def __getattr__(self, name):
        return getattr(asyncio, name)


async def _start_farewell(monkeypatch, *, duration_ms=10_240, sparse_visemes=False, late_viseme=False):
    store, saved, channel, managed = await _live_terminal()
    with persistence_for(store).transaction("org_default") as transaction:
        session = transaction.interview_sessions.get(saved["id"])
        session["settings"]["avatar_mode"] = "local"
        transaction.interview_sessions.update(session, expected_version=session["version"])
    monkeypatch.setattr(channel.runtime.media_captures, "stop_for_interview", AsyncMock())
    monkeypatch.setattr(channel.runtime, "_expression_audio", AsyncMock(return_value={
        "audio_uri": "private-file://synthetic-farewell-092",
        "duration_ms": duration_ms,
        # A provider may supply only an early mouth cue. The materialized
        # audio still lasts duration_ms and the composer retains that fact.
        "visemes": [{"at_ms": 3_600_000 if late_viseme else 0, "duration_ms": 100, "shape": "aa", "weight": .8}]
        if sparse_visemes or late_viseme else [],
    }))
    clock = _PlaybackClock()
    monkeypatch.setattr(agent_module, "asyncio", clock)
    await channel._after_formal_evidence_finished(None, None,
        conversation_action_selected=False, stop_evidence_session=False)
    await clock.advance(0)
    performance_id = _current(channel)["agent_runtime"]["completion_closing_performance_id"]
    assert performance_id
    return channel, managed, clock, performance_id


def _current(channel):
    return channel.runtime.interviews.get_interview(channel.interview_id)


def _receipts(channel):
    return [event for event in _current(channel)["agent_events"] if event["type"] == "completed"]


async def _playback(channel, performance_id, status, key):
    await channel.send(ClientSignal(type="avatar.performance.playback", turn_id=None,
        idempotency_key=key, payload={"performance_id": performance_id, "status": status}))


async def _ended(channel, performance_id, key="farewell_ended_092"):
    await channel.send(ClientSignal(type="avatar.performance.stopped", turn_id=None,
        idempotency_key=key, payload={"performance_id": performance_id}))


@pytest.mark.parametrize("duration_ms", [10_240, 35_000, 60_000])
@pytest.mark.parametrize("sparse_visemes", [False, True])
def test_complete_audio_survives_startup_and_buffering_before_real_ended(monkeypatch, duration_ms, sparse_visemes):
    async def scenario():
        channel, managed, clock, performance_id = await _start_farewell(
            monkeypatch, duration_ms=duration_ms, sparse_visemes=sparse_visemes)
        try:
            # Both delays remain below the browser's existing 8 s watchdog.
            await clock.advance(7)
            await _playback(channel, performance_id, "playing", "started_092")
            await clock.advance(duration_ms / 2000)
            assert not _receipts(channel), "Farewell was cut off while still playing"
            await _playback(channel, performance_id, "buffering", "buffering_092")
            await clock.advance(7)
            await _playback(channel, performance_id, "playing", "resumed_092")
            await clock.advance(duration_ms / 2000 + .5)
            assert not _receipts(channel)
            assert not managed._stopped
            await _ended(channel, performance_id)
            assert len(_receipts(channel)) == 1
            assert _current(channel)["agent_runtime"]["completion_reason"] == "farewell_completed"
            await _ended(channel, performance_id, "duplicate_ended_092")
            await clock.advance(120)
            assert len(_receipts(channel)) == 1
        finally:
            await channel.close("test_finished")
            await managed.stop("test_finished")
    asyncio.run(scenario())


def test_missing_ended_has_one_bounded_timeout_not_renewed_by_playback_status(monkeypatch):
    async def scenario():
        channel, managed, clock, performance_id = await _start_farewell(monkeypatch)
        try:
            await clock.advance(10.25)
            assert not _receipts(channel), "10 s cap must not truncate a 10.24 s asset"
            for index in range(20):
                await _playback(channel, performance_id, "playing" if index % 2 else "buffering",
                    "status_092_%d" % index)
                await clock.advance(5)
            assert len(_receipts(channel)) == 1
            assert _current(channel)["agent_runtime"]["completion_reason"] == "farewell_timeout"
            assert managed._stopped
        finally:
            await channel.close("test_finished")
            await managed.stop("test_finished")
    asyncio.run(scenario())


def test_stale_end_does_not_complete_current_farewell(monkeypatch):
    async def scenario():
        channel, managed, clock, performance_id = await _start_farewell(monkeypatch)
        try:
            await _ended(channel, "old_performance_092", "old_ended_092")
            assert not _receipts(channel)
            assert _current(channel)["agent_runtime"]["active_performance_id"] == performance_id
            await _ended(channel, performance_id)
            assert len(_receipts(channel)) == 1
        finally:
            await channel.close("test_finished")
            await managed.stop("test_finished")
    asyncio.run(scenario())


def test_late_provider_mouth_cue_cannot_extend_short_audio_cleanup_for_hours(monkeypatch):
    async def scenario():
        channel, managed, clock, _ = await _start_farewell(monkeypatch, late_viseme=True)
        try:
            await clock.advance(60)
            assert len(_receipts(channel)) == 1
            assert _current(channel)["agent_runtime"]["completion_reason"] == "farewell_timeout"
            assert managed._stopped
        finally:
            await channel.close("test_finished")
            await managed.stop("test_finished")
    asyncio.run(scenario())


def test_disconnect_cancels_long_watchdog_without_waiting_for_audio_duration(monkeypatch):
    async def scenario():
        channel, managed, clock, _ = await _start_farewell(monkeypatch, duration_ms=60_000)
        try:
            await asyncio.wait_for(channel.close("websocket_disconnected"), .5)
            assert channel._completion_task is None
            managed._schedule_terminal_cleanup()
            await asyncio.wait_for(managed._terminal_stop_task, .5)
            assert managed._stopped
            await clock.advance(120)
            assert not _receipts(channel)
        finally:
            await channel.close("test_finished")
            await managed.stop("test_finished")
    asyncio.run(scenario())


@pytest.mark.parametrize("invalidate", ["owner", "controller"])
def test_lost_authority_releases_media_now_and_cannot_emit_at_later_deadline(monkeypatch, invalidate):
    async def scenario():
        channel, managed, clock, _ = await _start_farewell(monkeypatch, duration_ms=60_000)
        try:
            coordinator = managed.supervisor.coordinator
            if invalidate == "owner":
                assert coordinator.release(managed.ownership)
            else:
                coordinator.attach_control(interview_id=channel.interview_id, organization_id="org_default",
                    connection_id="replacement_092", local_instance_id=managed.supervisor.instance_id)
            before = _current(channel)
            managed._schedule_terminal_cleanup()
            await asyncio.wait_for(managed._terminal_stop_task, .5)
            assert managed._stopped, "Lost authority must not wait for the longer farewell deadline"
            await clock.advance(120)
            assert _current(channel) == before
            assert not _receipts(channel)
        finally:
            await channel.close("test_finished")
            await managed.stop("test_finished")
    asyncio.run(scenario())


def test_timeout_projection_cannot_finalize_after_controller_replacement(monkeypatch):
    async def scenario():
        channel, managed, clock, _ = await _start_farewell(monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        original_emit = channel._emit

        async def held_emit(kind, payload, **kwargs):
            if kind == "avatar.performance.stopped" and payload.get("reason") == "completion_timeout":
                entered.set()
                await release.wait()
            return await original_emit(kind, payload, **kwargs)

        monkeypatch.setattr(channel, "_emit", held_emit)
        try:
            await clock.advance(60)
            assert entered.is_set()
            managed.supervisor.coordinator.attach_control(interview_id=channel.interview_id,
                organization_id="org_default", connection_id="replaced_during_projection_092",
                local_instance_id=managed.supervisor.instance_id)
            release.set()
            await clock.advance(0)
            assert not _receipts(channel)
        finally:
            release.set()
            await channel.close("test_finished")
            await managed.stop("test_finished")
    asyncio.run(scenario())


def test_real_managed_without_confirmation_keeps_normal_playback_floor_release():
    async def scenario():
        store, saved, channel, managed = await _without_controller()
        managed._channel = managed._event_source = channel
        channel._evidence_session = managed
        try:
            assert managed._answer_endpoint is None
            with persistence_for(store).transaction("org_default") as transaction:
                session = transaction.interview_sessions.get(saved["id"])
                session["agent_runtime"].update(calibration_status="completed", floor="agent")
                transaction.interview_sessions.update(session, expected_version=session["version"])
            channel.runtime._set_active_performance(channel.interview_id,
                "ordinary_question_audio_092", channel.organization_id)
            await channel.send(ClientSignal(type="avatar.performance.stopped",
                turn_id=_current(channel)["current_turn_id"], idempotency_key="ordinary_question_drained_092",
                payload={"performance_id": "ordinary_question_audio_092"}))
            current = _current(channel)
            assert current["status"] == "in_progress"
            assert current["agent_runtime"]["floor"] == "candidate"
            assert current["agent_runtime"]["active_performance_id"] is None
            assert getattr(managed, "_reply_audio_handoff", None) is None
            assert not _receipts(channel)
        finally:
            await channel.close("test_finished")
            await managed.stop("test_finished")
    asyncio.run(scenario())
