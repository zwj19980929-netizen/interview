"""A reconnect restores the current ASR wait without replaying old capture state."""
import asyncio

import pytest

from app.persistence.provider import persistence_for
from test_answer_endpoint import _until
from test_automatic_turn_integration import _automatic_session, _current
from test_evidence_owner_recovery_integration import (
    INTERVIEW_ID, ORGANIZATION_ID, TURN_ID, _opened, _ticket,
)


def _retry_notices(channel):
    return [event for event in channel._queue._queue
            if getattr(event, "type", None) == "problem"
            and event.payload.get("code") == "TRANSCRIPT_RETRY_REQUIRED"]


async def _lose_notice(channel, managed):
    endpoint = managed._answer_endpoint
    await channel.close("synthetic_control_drop")
    for _ in range(3):
        endpoint._observe_final_snapshot(None)
    await _until(lambda: endpoint._missing_final_notice_sent)
    # Complete the disconnected notification before attaching another peer.
    await asyncio.sleep(.01)
    return endpoint


def test_lost_wait_notice_returns_once_on_same_capture_reconnect(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
            capture_id, ingress = managed._capture_id, managed._ingress
            endpoint = await _lose_notice(channel, managed)
            _ticket(store, "connection_wait_reconnect")
            replacement = await runtime.open(_opened("connection_wait_reconnect"))
            try:
                notices = _retry_notices(replacement)
                assert len(notices) == 1
                assert notices[0].turn_id == TURN_ID
                assert notices[0].payload["capture_id"] == capture_id
                assert notices[0].payload["action"] == "continue_listening"
                assert managed._answer_endpoint is endpoint and managed._ingress is ingress
                assert managed.chain.is_open and managed._capture_id == capture_id
                assert _current(runtime)["answers"] == []
                await asyncio.sleep(.16)
                assert len(_retry_notices(replacement)) == 1, "No periodic notice flood"
            finally:
                await replacement.close("test_complete")
    asyncio.run(scenario())


@pytest.mark.parametrize("resume", ["audio", "continue", "finish"])
def test_reconnect_keeps_same_origin_audio_wait_but_explicit_retry_resolves_it(tmp_path, monkeypatch, resume):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
            endpoint = await _lose_notice(channel, managed)
            if resume == "audio":
                endpoint.speech_started(source="audio")
            elif resume == "continue":
                endpoint.continue_speaking()
            else:
                endpoint.request_finish()
            _ticket(store, "connection_wait_resolved")
            replacement = await runtime.open(_opened("connection_wait_resolved"))
            try:
                assert len(_retry_notices(replacement)) == (1 if resume == "audio" else 0)
                assert endpoint.awaiting_transcript is (resume == "audio")
                assert managed.chain.is_open and _current(runtime)["answers"] == []
            finally:
                await replacement.close("test_complete")
    asyncio.run(scenario())


@pytest.mark.parametrize("stale", ["capture", "turn"])
def test_reconnect_never_replays_wait_from_stale_capture_or_turn(tmp_path, monkeypatch, stale):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
            await _lose_notice(channel, managed)
            if stale == "capture":
                managed._capture_id = "synthetic_successor_capture"
            else:
                with persistence_for(store).transaction(ORGANIZATION_ID) as tx:
                    session = tx.interview_sessions.get(INTERVIEW_ID)
                    session["current_turn_id"] = "synthetic_successor_turn"
                    tx.interview_sessions.update(session, expected_version=session["version"])
            _ticket(store, "connection_wait_stale")
            replacement = await runtime.open(_opened("connection_wait_stale"))
            try:
                assert _retry_notices(replacement) == []
                assert _current(runtime)["answers"] == []
            finally:
                await replacement.close("test_complete")
    asyncio.run(scenario())


@pytest.mark.parametrize("resume", ["audio", "continue"])
def test_notice_delivery_rechecks_explicit_retry_without_treating_same_origin_vad_as_recovery(tmp_path, monkeypatch, resume):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (_, runtime, channel, managed):
            endpoint = managed._answer_endpoint
            for _ in range(3):
                endpoint._observe_final_snapshot(None)
            replay = asyncio.create_task(endpoint.replay_waiting_notice())
            # A user retry can win between choosing and delivering a notice;
            # same-origin microphone activity must leave its budget exhausted.
            asyncio.get_running_loop().call_soon(endpoint.speech_started if resume == "audio" else endpoint.continue_speaking)
            await replay
            assert endpoint.awaiting_transcript is (resume == "audio")
            assert len(_retry_notices(channel)) == (1 if resume == "audio" else 0)
            assert managed.chain.is_open and _current(runtime)["answers"] == []
    asyncio.run(scenario())


def test_attach_and_endpoint_tick_do_not_double_send_the_new_wait(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
            await channel.close("synthetic_control_drop")
            endpoint = managed._answer_endpoint
            for _ in range(3):
                endpoint._observe_final_snapshot(None)
            _ticket(store, "connection_wait_immediate")
            replacement = await runtime.open(_opened("connection_wait_immediate"))
            try:
                await asyncio.sleep(.16)
                assert len(_retry_notices(replacement)) == 1
            finally:
                await replacement.close("test_complete")
    asyncio.run(scenario())
