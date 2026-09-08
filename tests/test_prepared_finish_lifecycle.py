"""Synthetic regression probes for the prepared answer's async presentation."""

import asyncio
from types import SimpleNamespace

import pytest

from app.adapters.livekit_media import LiveKitMediaPlane
from app.core.time import utc_now
from app.core.errors import ApiError
from app.domain.interview_agent import ClientSignal
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.interview_agent import InterviewAgentRuntime
from app.services.interview_evidence import EvidenceFinishResult, InterviewEvidenceChain
from app.services.livekit_evidence_ingress import LiveKitEvidenceIngressSupervisor
from app.services import livekit_evidence_ingress
from test_livekit_evidence_supervisor import _FakeEvidenceChain, _FakeIngress, _configuration, _opened, _session


def test_new_voice_while_prepared_commit_waits_for_chain_lock_keeps_capture_and_floor():
    async def scenario():
        store = InMemoryStore()
        saved = _session(store)
        supervisor = LiveKitEvidenceIngressSupervisor(
            store, media_plane=LiveKitMediaPlane(_configuration()), ingress_factory=_FakeIngress,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        chain = InterviewEvidenceChain(store, saved["id"])
        stale = False
        initial_guard = asyncio.Event()

        def guard():
            initial_guard.set()
            if stale:
                raise ApiError("TURN_DECISION_STALE", "New voice invalidated preparation", status_code=409)

        def snapshot_current(_prepared):
            raise AssertionError("Revision fence must reject before inspecting or detaching the stream")

        async def close_stream(**_kwargs):
            pass

        stream = SimpleNamespace(turn_id="turn_1", assert_snapshot_current=snapshot_current, close=close_stream)
        chain._stt = stream
        managed.chain = chain
        managed._capture_id = "capture_original"
        await chain._lock.acquire()
        task = asyncio.create_task(managed._finish_with_channel(
            channel, ClientSignal(type="evidence.finish", idempotency_key="raced_commit", turn_id="turn_1"),
            prepared=("proposal_raced", object(), guard),
        ))
        try:
            await initial_guard.wait()
            stale = True
            chain._lock.release()
            with pytest.raises(ApiError) as exc:
                await task
            assert exc.value.code == "TURN_DECISION_STALE"
            assert chain.is_open and chain._stt is stream and not chain._finishing
            assert managed._capture_id == "capture_original"
            session = runtime.interviews.get_interview(saved["id"])
            assert session["agent_runtime"]["floor"] == "candidate"
            assert not any(event.payload.get("reason") == "answer_processing" for event in channel._queue._queue)
        finally:
            if chain._lock.locked():
                chain._lock.release()
            await chain.abort()
            await channel.close("test_finished")
            await managed.stop("test_finished")

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["normal", "timeout", "owner_lost", "owner_lost_timeout", "paused", "takeover", "shutdown"])
def test_terminal_evaluation_during_prepared_farewell_keeps_completion_authority(monkeypatch, ending):
    async def scenario():
        store = InMemoryStore()
        saved = _session(store)
        supervisor = LiveKitEvidenceIngressSupervisor(
            store, media_plane=LiveKitMediaPlane(_configuration()), ingress_factory=_FakeIngress,
            command_poll_seconds=0.005,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        managed.chain = _FakeEvidenceChain()
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_farewell(**kwargs):
            assert kwargs["act_type"] == "closing"
            entered.set()
            await release.wait()
            return None  # An unavailable farewell must still yield completion.

        async def stop_recording(*_args, **_kwargs):
            pass

        monkeypatch.setattr(channel, "_select_act", held_farewell)
        monkeypatch.setattr(runtime.media_captures, "stop_for_interview", stop_recording)
        monkeypatch.setattr(livekit_evidence_ingress, "_TERMINAL_PRESENTATION_GRACE_SECONDS", 0.06)
        with persistence_for(store).transaction("org_default") as transaction:
            session = transaction.interview_sessions.get(saved["id"])
            session["candidate_input_completed_at"] = utc_now()
            session["current_turn_id"] = None
            transaction.interview_sessions.update(session, expected_version=session["version"])
        result = EvidenceFinishResult("formal", "turn_1", [], None, {"accepted": True})
        managed._presentation_task = asyncio.create_task(managed._present_formal_result(
            channel, ClientSignal(type="evidence.finish", idempotency_key="last_answer", turn_id="turn_1"), result,
        ))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            # A separate evaluation worker can finish while whole TTS awaits.
            with persistence_for(store).transaction("org_default") as transaction:
                session = transaction.interview_sessions.get(saved["id"])
                session["status"] = "report_ready"
                transaction.interview_sessions.update(session, expected_version=session["version"])
            for _ in range(100):
                if getattr(managed, "_terminal_stop_task", None) is not None:
                    break
                await asyncio.sleep(0.001)
            terminal_task = managed._terminal_stop_task
            assert not managed._stopped, "Terminal cleanup must let a current farewell finish before fencing itself"
            if ending.startswith("owner_lost"):
                assert supervisor.coordinator.release(managed.ownership)
                supervisor.coordinator.attach_control(
                    interview_id=saved["id"], organization_id="org_default", connection_id="new_owner_control",
                    local_instance_id="new_owner_instance",
                )
            elif ending in {"paused", "takeover"}:
                with persistence_for(store).transaction("org_default") as transaction:
                    session = transaction.interview_sessions.get(saved["id"])
                    if ending == "paused":
                        session["status"] = "paused"
                        session["agent_runtime"]["floor"] = "none"
                    else:
                        session["agent_runtime"]["takeover"] = {"status": "active"}
                        session["agent_runtime"]["floor"] = "human"
                    transaction.interview_sessions.update(session, expected_version=session["version"])
            elif ending == "shutdown":
                await managed.stop("application_shutdown")
            if ending not in {"timeout", "owner_lost_timeout"}:
                release.set()
            await asyncio.wait_for(terminal_task, timeout=1)
            current = runtime.interviews.get_interview(saved["id"])
            emitted = current["agent_runtime"].get("completion_emitted_at")
            receipts = [item for item in current.get("agent_events", []) if item["type"] == "completed"]
            if ending in {"normal", "timeout"}:
                assert emitted, "A committed final answer needs a receipt even when farewell setup fails"
                assert len(receipts) == 1
            else:
                assert not emitted, "A stopped, superseded or paused owner must never mint completion"
                assert receipts == []
        finally:
            release.set()
            await channel.close("test_finished")
            await managed.stop("test_finished")

    asyncio.run(scenario())
