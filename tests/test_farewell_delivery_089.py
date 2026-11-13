"""Natural completion preserves live farewell without reopening ended input."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core.time import utc_now
from app.persistence.provider import persistence_for
from test_terminal_cleanup_086 import _without_controller, _mark_terminal


async def _live_terminal():
    store, saved, channel, managed = await _without_controller()
    managed._channel = managed._event_source = channel
    channel._evidence_session = managed
    _mark_terminal(store, saved)
    return store, saved, channel, managed


@pytest.mark.parametrize("fanout", ["local", "system", "fresh_peer"])
def test_only_original_live_controller_defers_completed_snapshot_for_farewell(fanout):
    async def scenario():
        store, saved, channel, managed = await _live_terminal()
        release = asyncio.Event()
        channel._planning_task = asyncio.create_task(release.wait())
        try:
            if fanout == "local":
                await channel._emit_snapshot(None)
            elif fanout == "system":
                await channel.runtime.publish_snapshot(saved["id"])
            else:
                await channel._queue_fresh_snapshot()
            snapshot = [event for event in channel._queue._queue if event.type == "session.snapshot"][-1]
            assert snapshot.payload["completion_presentation"] == "closing"
            assert snapshot.payload["status"] == "report_generating"
            assert snapshot.payload["completion"]["interview_id"] == saved["id"]
            # Unconnected refresh does not inherit live presentation authority.
            session = channel.runtime.interviews.get_interview(saved["id"])
            fresh = channel.runtime._snapshot(session, channel.principal)
            assert "completion_presentation" not in fresh and fresh["completion"]
            managed.supervisor.coordinator.attach_control(interview_id=saved["id"], organization_id="org_default",
                connection_id="replacement", local_instance_id=managed.supervisor.instance_id)
            await channel._emit_snapshot(None)
            snapshot = [event for event in channel._queue._queue if event.type == "session.snapshot"][-1]
            assert "completion_presentation" not in snapshot.payload
        finally:
            release.set()
            await channel.close("test_finished")
            await managed.stop("test_finished")
    asyncio.run(scenario())


def test_terminal_cleanup_waits_for_natural_planning_farewell(monkeypatch):
    async def scenario():
        store, saved, channel, managed = await _live_terminal()
        entered, release = asyncio.Event(), asyncio.Event()
        async def finish_planning():
            entered.set()
            await release.wait()
            channel.runtime._mark_completion_closing(saved["id"], "natural_farewell", "org_default")
        channel._planning_task = asyncio.create_task(finish_planning())
        try:
            await entered.wait()
            managed._schedule_terminal_cleanup()
            await asyncio.sleep(.01)
            assert not managed._stopped
            assert not managed._terminal_stop_task.done()
            release.set()
            await channel._planning_task
            await managed._terminal_stop_task
            assert not managed._stopped
            assert not channel.runtime.interviews.get_interview(saved["id"])["agent_runtime"].get("completion_emitted_at")
        finally:
            release.set()
            await channel.close("test_finished")
            await managed.stop("test_finished")
    asyncio.run(scenario())


@pytest.mark.parametrize("finish_by", ["drained", "timeout", "tts_failure"])
@pytest.mark.parametrize("score_finishes_early", [False, True])
def test_natural_budget_end_survives_snapshot_and_scoring_races(tmp_path, monkeypatch, finish_by, score_finishes_early):
    from test_adaptive_interview import make_session, select, submit
    from test_spoken_supplement_integration import _synthetic_tts
    from test_evidence_owner_recovery_integration import _wait_until
    from app.domain.interview_agent import ClientSignal
    from app.file_storage.provider import reset_private_file_storage_for_tests

    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("INTERVIEWER_MEDIA_PATH", str(tmp_path / "media"))
    reset_private_file_storage_for_tests()

    async def scenario():
        store, saved, channel, managed = await _without_controller()
        managed._channel = managed._event_source = channel
        channel._evidence_session = managed
        runtime = channel.runtime
        with persistence_for(store).transaction("org_default") as tx:
            current = tx.interview_sessions.get(saved["id"])
            adaptive = submit(select(make_session(maximum=1, minimum=1), "q1").session).session
            for key in ("plan_snapshot", "turns", "turn_ids", "answers", "current_turn_id", "decision_revision", "dialogue_state", "question_selections"):
                current[key] = adaptive.get(key)
            current["settings"]["avatar_mode"] = "local"
            current["agent_runtime"].update(calibration_status="completed", floor="none")
            tx.interview_sessions.update(current, expected_version=current["version"])
        spoken = _synthetic_tts(runtime)
        monkeypatch.setattr(runtime.media_captures, "stop_for_interview", AsyncMock())
        entered, release = asyncio.Event(), asyncio.Event()
        original_audio = runtime._expression_audio
        async def delayed_audio(*args, **kwargs):
            entered.set()
            await release.wait()
            if finish_by == "tts_failure":
                raise RuntimeError("PRIVATE_SYNTHETIC_TTS_ERROR")
            return await original_audio(*args, **kwargs)
        monkeypatch.setattr(runtime, "_expression_audio", delayed_audio)
        try:
            channel._schedule_planning(None)
            await asyncio.wait_for(entered.wait(), 1)
            session = runtime.interviews.get_interview(saved["id"])
            assert session["candidate_input_completion_reason"] == "budget_exhausted"
            if score_finishes_early:
                with persistence_for(store).transaction("org_default") as tx:
                    current = tx.interview_sessions.get(saved["id"])
                    current["status"] = "report_ready"
                    tx.interview_sessions.update(current, expected_version=current["version"])
                managed._schedule_terminal_cleanup()
            await runtime.publish_snapshot(saved["id"])
            snapshots = [event.payload for event in channel._queue._queue if event.type == "session.snapshot" and event.payload.get("completion")]
            assert snapshots and all(item.get("completion_presentation") == "closing" for item in snapshots)
            await asyncio.sleep(.01)
            assert not managed._stopped and not channel._closed
            assert not any(event.type == "completed" for event in channel._queue._queue)
            release.set()
            if finish_by != "tts_failure":
                await _wait_until(lambda: runtime.interviews.get_interview(saved["id"])["agent_runtime"].get("completion_closing_performance_id"))
                current = runtime.interviews.get_interview(saved["id"])
                performance_id = current["agent_runtime"]["completion_closing_performance_id"]
                assert sum("感谢你的时间" in text for text in spoken) == 1
                if finish_by == "drained":
                    await channel.send(ClientSignal(type="avatar.performance.stopped", turn_id=None,
                        idempotency_key="finish_closing", payload={"performance_id": performance_id}))
                else:
                    await channel._completion_after_timeout(performance_id, 1, None)
            await _wait_until(lambda: managed._stopped)
            current = runtime.interviews.get_interview(saved["id"])
            assert len([event for event in current["agent_events"] if event["type"] == "completed"]) == 1
            assert current["agent_runtime"]["completion_emitted_at"]
            assert len(current["answers"]) == 1
        finally:
            release.set()
            await channel.close("test_finished")
            await managed.stop("test_finished")
            reset_private_file_storage_for_tests()
    asyncio.run(scenario())


@pytest.mark.parametrize("invalidate", ["owner", "controller"])
def test_delayed_farewell_cannot_publish_after_original_authority_is_replaced(tmp_path, monkeypatch, invalidate):
    from test_spoken_supplement_integration import _synthetic_tts
    from app.file_storage.provider import reset_private_file_storage_for_tests
    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("INTERVIEWER_MEDIA_PATH", str(tmp_path / "media"))
    reset_private_file_storage_for_tests()
    async def scenario():
        store, saved, channel, managed = await _live_terminal()
        runtime = channel.runtime
        with persistence_for(store).transaction("org_default") as tx:
            current = tx.interview_sessions.get(saved["id"])
            current["settings"]["avatar_mode"] = "local"
            tx.interview_sessions.update(current, expected_version=current["version"])
        _synthetic_tts(runtime)
        monkeypatch.setattr(runtime.media_captures, "stop_for_interview", AsyncMock())
        entered, release = asyncio.Event(), asyncio.Event()
        original = runtime._expression_audio
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        monkeypatch.setattr(runtime, "_expression_audio", delayed)
        task = asyncio.create_task(channel._after_formal_evidence_finished(None, None, conversation_action_selected=False))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            coordinator = managed.supervisor.coordinator
            if invalidate == "controller":
                coordinator.attach_control(interview_id=saved["id"], organization_id="org_default",
                    connection_id="replacement", local_instance_id=managed.supervisor.instance_id)
            else:
                assert coordinator.release(managed.ownership)
            release.set()
            await asyncio.wait_for(task, 1)
            current = runtime.interviews.get_interview(saved["id"])
            assert not current["agent_runtime"].get("completion_closing_performance_id")
            assert not current["agent_runtime"].get("completion_emitted_at")
            assert not any(event.type in {"avatar.performance.started", "completed"} for event in channel._queue._queue)
        finally:
            release.set()
            await channel.close("test_finished")
            await managed.stop("test_finished")
            reset_private_file_storage_for_tests()
    asyncio.run(scenario())
