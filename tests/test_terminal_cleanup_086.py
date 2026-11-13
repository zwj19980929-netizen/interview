"""Terminal owner fallback stays bounded and never exposes provider errors."""

import asyncio

import pytest

from app.adapters.livekit_media import LiveKitMediaPlane
from app.core.time import utc_now
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.interview_agent import InterviewAgentRuntime
from app.services.livekit_evidence_ingress import LiveKitEvidenceIngressSupervisor
from test_livekit_evidence_supervisor import _FakeEvidenceChain, _FakeIngress, _configuration, _opened, _session


async def _without_controller():
    store = InMemoryStore()
    saved = _session(store)
    supervisor = LiveKitEvidenceIngressSupervisor(
        store, media_plane=LiveKitMediaPlane(_configuration()), ingress_factory=_FakeIngress,
    )
    runtime = InterviewAgentRuntime(store)
    runtime.evidence_ingress = supervisor
    channel = await runtime.open(_opened("connection_original"))
    managed = channel._evidence_session
    managed.chain = _FakeEvidenceChain()
    channel._evidence_session = None
    managed._channel = managed._event_source = None
    return store, saved, channel, managed


def _mark_terminal(store, saved):
    with persistence_for(store).transaction("org_default") as tx:
        session = tx.interview_sessions.get(saved["id"])
        session.update(status="report_generating", current_turn_id=None, candidate_input_completed_at=utc_now())
        tx.interview_sessions.update(session, expected_version=session["version"])


def test_controllerless_terminal_owner_does_not_wait_forever_for_cancel_suppressing_presentation():
    async def scenario():
        store, saved, channel, managed = await _without_controller()
        entered, release = asyncio.Event(), asyncio.Event()

        async def uncooperative_output():
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        output = asyncio.create_task(uncooperative_output())
        managed._presentation_task = output
        try:
            await entered.wait()
            _mark_terminal(store, saved)
            managed._schedule_terminal_cleanup()
            cleanup = managed._terminal_stop_task
            managed._schedule_terminal_cleanup()
            assert managed._terminal_stop_task is cleanup
            await asyncio.wait_for(asyncio.shield(cleanup), .5)
            assert managed._stopped and managed._presentation_task is None
            assert not output.done(), "The detached provider must not block media/owner release"
            current = managed.supervisor.interviews.get_interview(saved["id"])
            assert not any(event["type"] == "completed" for event in current["agent_events"])
        finally:
            release.set()
            await output
            await channel.close("test_finished")
            await managed.stop("test_finished")

    asyncio.run(scenario())


def test_terminal_fallback_failure_is_observed_and_retryable_without_sensitive_message(monkeypatch, caplog):
    async def scenario():
        store, saved, channel, managed = await _without_controller()
        original_stop = managed.stop
        attempts = []

        async def fail_once(reason):
            attempts.append(reason)
            if len(attempts) == 1:
                raise RuntimeError("PRIVATE_TERMINAL_ERROR")
            return await original_stop(reason)

        monkeypatch.setattr(managed, "stop", fail_once)
        try:
            _mark_terminal(store, saved)
            managed._schedule_terminal_cleanup()
            await managed._terminal_stop_task
            assert not managed._stopped and attempts == ["interview_completed"]
            assert "error_type=RuntimeError" in caplog.text
            assert "PRIVATE_TERMINAL_ERROR" not in caplog.text
            assert "Task exception was never retrieved" not in caplog.text
            managed._schedule_terminal_cleanup()
            await managed._terminal_stop_task
            assert managed._stopped and attempts == ["interview_completed", "interview_completed"]
        finally:
            await channel.close("test_finished")
            await original_stop("test_finished")

    asyncio.run(scenario())


@pytest.mark.parametrize("invalidated", ["owner", "controller", "paused", "takeover"])
@pytest.mark.parametrize("old_marker", [None, "completion_closing_performance_id", "completion_emitted_at"])
def test_invalid_terminal_controller_cannot_delegate_cleanup_or_block_on_old_output(invalidated, old_marker):
    async def scenario():
        store, saved, channel, managed = await _without_controller()
        managed._channel = managed._event_source = channel
        channel._evidence_session = managed
        entered, release = asyncio.Event(), asyncio.Event()

        async def uncooperative_output():
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        output = asyncio.create_task(uncooperative_output())
        managed._presentation_task = output
        coordinator = managed.supervisor.coordinator
        successor = None
        try:
            await entered.wait()
            _mark_terminal(store, saved)
            with persistence_for(store).transaction("org_default") as tx:
                session = tx.interview_sessions.get(saved["id"])
                if old_marker:
                    session["agent_runtime"][old_marker] = "stale_or_successor_value"
                if invalidated == "paused":
                    session["status"] = "paused"
                elif invalidated == "takeover":
                    session["agent_runtime"].update(floor="human", takeover={"status": "active"})
                tx.interview_sessions.update(session, expected_version=session["version"])
            if invalidated == "owner":
                assert coordinator.release(managed.ownership)
                successor = coordinator.claim_owner(interview_id=saved["id"],
                    organization_id="org_default", local_instance_id="successor_086")
            elif invalidated == "controller":
                coordinator.attach_control(interview_id=saved["id"], organization_id="org_default",
                    connection_id="successor_control_086", local_instance_id=managed.supervisor.instance_id)
            before = managed.supervisor.interviews.get_interview(saved["id"])
            managed._schedule_terminal_cleanup()
            await asyncio.wait_for(asyncio.shield(managed._terminal_stop_task), .5)
            assert managed._stopped and managed._presentation_task is None
            assert not output.done(), "Invalid output cannot keep the old local owner alive"
            assert channel._completion_evidence_cleanup_task is None
            assert managed.supervisor.interviews.get_interview(saved["id"]) == before
            if successor:
                coordinator.assert_current(successor.commit_fence(), "org_default")
        finally:
            release.set()
            await output
            await channel.close("test_finished")
            await managed.stop("test_finished")

    asyncio.run(scenario())


@pytest.mark.parametrize("pending_score", [False, True])
def test_existing_receipt_cleanup_bounds_its_original_presentation_wait(monkeypatch, pending_score):
    monkeypatch.setattr("app.services.livekit_evidence_ingress._TERMINAL_PRESENTATION_GRACE_SECONDS", .03)

    async def scenario():
        store, saved, channel, managed = await _without_controller()
        managed._channel = managed._event_source = channel
        channel._evidence_session = managed
        entered, release = asyncio.Event(), asyncio.Event()

        async def uncooperative_output():
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        output = asyncio.create_task(uncooperative_output())
        managed._presentation_task = output
        try:
            await entered.wait()
            _mark_terminal(store, saved)
            with persistence_for(store).transaction("org_default") as tx:
                session = tx.interview_sessions.get(saved["id"])
                if pending_score:
                    session["status"] = "in_progress"
                session["agent_runtime"]["completion_emitted_at"] = utc_now()
                tx.interview_sessions.update(session, expected_version=session["version"])
            before = managed.supervisor.interviews.get_interview(saved["id"])
            channel._schedule_completion_evidence_cleanup()
            await asyncio.sleep(0)  # The channel has captured the original output task.
            if not pending_score:
                managed._schedule_terminal_cleanup()
                await asyncio.wait_for(asyncio.shield(managed._terminal_stop_task), .5)
            await asyncio.wait_for(asyncio.shield(channel._completion_evidence_cleanup_task), .5)
            assert managed._stopped and managed._presentation_task is None
            assert channel._evidence_session is None and not output.done()
            assert managed.supervisor.interviews.get_interview(saved["id"]) == before
        finally:
            release.set()
            await output
            await channel.close("test_finished")
            await managed.stop("test_finished")

    asyncio.run(scenario())
