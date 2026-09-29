"""Control reconnection restores the owner's current wait, including remote APIs."""
import asyncio
import pytest
from app.core.errors import ApiError
from app.domain.interview_agent import ClientSignal
from app.services.interview_agent import InterviewAgentRuntime
from app.services.livekit_evidence_ingress import LiveKitEvidenceIngressSupervisor
from test_automatic_turn_integration import _automatic_session, _current, _SyntheticIngress, _CertainAudioEndpoint
from test_evidence_owner_recovery_integration import TURN_ID, _opened, _ticket
from test_transcript_wait_reconnect_059 import _lose_notice, _retry_notices
from test_answer_endpoint import _until


def test_remote_control_reconnect_restores_wait_after_owner_ready(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, owner):
            endpoint = await _lose_notice(channel, owner)
            remote = LiveKitEvidenceIngressSupervisor(store, media_plane=owner.supervisor.media_plane,
                ingress_factory=_SyntheticIngress, turn_detector=_CertainAudioEndpoint(),
                instance_id="synthetic_remote_060", command_poll_seconds=.005)
            remote_runtime = InterviewAgentRuntime(store)
            remote_runtime.evidence_ingress = remote
            _ticket(store, "connection_remote_060")
            replacement = await remote_runtime.open(_opened("connection_remote_060"))
            try:
                assert replacement._evidence_session.ownership is None
                await replacement.send(ClientSignal(type="evidence.stream.open", idempotency_key="remote_reassert_060",
                    turn_id=TURN_ID, payload={"content_type":"audio/pcm", "sample_rate_hz":16000,"channels":1,"language":"zh-CN"}))
                ready = [e for e in replacement._queue._queue if getattr(e,"type",None)=="floor.changed" and e.payload.get("reason")=="evidence_stream_open"]
                assert ready and ready[-1].payload["capture_id"]==owner._capture_id
                assert owner._answer_endpoint is endpoint and endpoint.awaiting_transcript
                assert owner.chain.is_open and not _current(runtime)["answers"]
                assert len(_retry_notices(replacement))==1, "Remote ready lost current wait"
                await replacement.send(ClientSignal(type="evidence.stream.open", idempotency_key="remote_reassert_again_060",
                    turn_id=TURN_ID, payload={"content_type":"audio/pcm", "sample_rate_hz":16000,"channels":1,"language":"zh-CN"}))
                assert len(_retry_notices(replacement)) == 1, "Repeated ready must not duplicate a wait"
                first_generation = owner._transcript_wait_notice_key[-1]
                await replacement.close("second_synthetic_control_drop")
                _ticket(store, "connection_remote_second_060")
                replacement = await remote_runtime.open(_opened("connection_remote_second_060"))
                await replacement.send(ClientSignal(type="evidence.stream.open", idempotency_key="remote_second_generation_060",
                    turn_id=TURN_ID, payload={"content_type":"audio/pcm", "sample_rate_hz":16000,"channels":1,"language":"zh-CN"}))
                assert owner._transcript_wait_notice_key[-1] > first_generation
                assert len(_retry_notices(replacement)) == 1, "A new controller needs its own current notice"
                assert owner._answer_endpoint is endpoint and endpoint.awaiting_transcript
                await replacement.send(ClientSignal(type="evidence.stream.open", idempotency_key="remote_second_generation_again_060",
                    turn_id=TURN_ID, payload={"content_type":"audio/pcm", "sample_rate_hz":16000,"channels":1,"language":"zh-CN"}))
                assert len(_retry_notices(replacement)) == 1
            finally:
                await replacement.close("test_complete")
                await remote.shutdown()
    asyncio.run(scenario())


def test_failed_wait_publication_is_retried_by_next_remote_ready(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, owner):
            endpoint = await _lose_notice(channel, owner)
            remote = LiveKitEvidenceIngressSupervisor(store, media_plane=owner.supervisor.media_plane,
                ingress_factory=_SyntheticIngress, turn_detector=_CertainAudioEndpoint(),
                instance_id="synthetic_remote_retry_060", command_poll_seconds=.005)
            remote_runtime = InterviewAgentRuntime(store)
            remote_runtime.evidence_ingress = remote
            _ticket(store, "connection_remote_retry_060")
            replacement = await remote_runtime.open(_opened("connection_remote_retry_060"))
            emit = owner._event_source._emit
            wait_attempts = 0

            async def fail_once(kind, payload, **kwargs):
                nonlocal wait_attempts
                if kind == "problem" and payload.get("code") == "TRANSCRIPT_RETRY_REQUIRED":
                    wait_attempts += 1
                    if wait_attempts == 1:
                        raise RuntimeError("synthetic publication failure")
                return await emit(kind, payload, **kwargs)

            monkeypatch.setattr(owner._event_source, "_emit", fail_once)
            previous_marker = owner._transcript_wait_notice_key
            try:
                await replacement.send(ClientSignal(type="evidence.stream.open", idempotency_key="remote_failed_notice_060",
                    turn_id=TURN_ID, payload={"content_type":"audio/pcm", "sample_rate_hz":16000,"channels":1,"language":"zh-CN"}))
                assert wait_attempts == 1 and not _retry_notices(replacement)
                assert owner._transcript_wait_notice_key == previous_marker
                await replacement.send(ClientSignal(type="evidence.stream.open", idempotency_key="remote_retry_notice_060",
                    turn_id=TURN_ID, payload={"content_type":"audio/pcm", "sample_rate_hz":16000,"channels":1,"language":"zh-CN"}))
                assert wait_attempts == 2 and len(_retry_notices(replacement)) == 1
                assert owner._transcript_wait_notice_key != previous_marker
                assert owner._answer_endpoint is endpoint and endpoint.awaiting_transcript
                assert owner.chain.is_open and not _current(runtime)["answers"]
            finally:
                await replacement.close("test_complete")
                await remote.shutdown()
    asyncio.run(scenario())


def test_local_attach_and_open_share_one_current_wait_notice(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, owner):
            await _lose_notice(channel, owner)
            _ticket(store, "connection_local_060")
            replacement = await runtime.open(_opened("connection_local_060"))
            try:
                assert len(_retry_notices(replacement)) == 1
                await replacement.send(ClientSignal(type="evidence.stream.open", idempotency_key="local_reassert_060",
                    turn_id=TURN_ID, payload={"content_type":"audio/pcm", "sample_rate_hz":16000,"channels":1,"language":"zh-CN"}))
                assert len(_retry_notices(replacement)) == 1
            finally:
                await replacement.close("test_complete")
    asyncio.run(scenario())


def test_new_wait_in_same_capture_and_generation_is_not_suppressed(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (_, runtime, channel, owner):
            endpoint = owner._answer_endpoint
            for _ in range(3):
                endpoint._observe_final_snapshot(None)
            await _until(lambda: len(_retry_notices(channel)) == 1)
            marker = owner._transcript_wait_notice_key
            endpoint.speech_started(source="audio")
            assert endpoint.awaiting_transcript
            assert len(_retry_notices(channel)) == 1
            endpoint.continue_speaking()
            assert not endpoint.awaiting_transcript
            for _ in range(3):
                endpoint._observe_final_snapshot(None)
            await _until(lambda: len(_retry_notices(channel)) == 2)
            assert owner._transcript_wait_notice_key == marker
            assert endpoint.awaiting_transcript and owner.chain.is_open
            assert not _current(runtime)["answers"]
    asyncio.run(scenario())


def test_closed_endpoint_wait_is_not_replayed(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (_, runtime, channel, owner):
            endpoint = owner._answer_endpoint
            for _ in range(3):
                endpoint._observe_final_snapshot(None)
            await endpoint.close()
            await owner._replay_transcript_wait()
            assert not _retry_notices(channel)
            assert owner.chain.is_open and not _current(runtime)["answers"]
    asyncio.run(scenario())


def test_superseded_owner_cannot_publish_a_wait(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (_, runtime, channel, owner):
            endpoint = owner._answer_endpoint
            for _ in range(3):
                endpoint._observe_final_snapshot(None)
            coordinator = owner.supervisor.coordinator
            assert coordinator.release(owner.ownership)
            successor = coordinator.claim_owner(interview_id=owner.interview_id,
                organization_id=owner.organization_id, local_instance_id="synthetic_successor_owner_060")
            assert successor.ownership_epoch > owner.ownership.ownership_epoch
            with pytest.raises(ApiError) as rejected:
                await owner._replay_transcript_wait()
            assert rejected.value.code == "EVIDENCE_OWNER_FENCED"
            assert not _retry_notices(channel)
            assert not _current(runtime)["answers"]
            # Dispose this fenced synthetic owner without asking it to commit
            # a disconnect checkpoint owned by the successor epoch.
            await endpoint.close()
            await owner.chain.abort()
    asyncio.run(scenario())


@pytest.mark.parametrize("resume", ["audio", "continue", "finish"])
def test_remote_open_keeps_same_origin_vad_wait_but_explicit_retry_resolves_it(tmp_path, monkeypatch, resume):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, owner):
            endpoint = await _lose_notice(channel, owner)
            remote = LiveKitEvidenceIngressSupervisor(store, media_plane=owner.supervisor.media_plane,
                ingress_factory=_SyntheticIngress, turn_detector=_CertainAudioEndpoint(),
                instance_id="synthetic_remote_resolved_060", command_poll_seconds=.005)
            remote_runtime = InterviewAgentRuntime(store)
            remote_runtime.evidence_ingress = remote
            _ticket(store, "connection_remote_resolved_060")
            replacement = await remote_runtime.open(_opened("connection_remote_resolved_060"))
            try:
                if resume == "audio":
                    endpoint.speech_started(source="audio")
                elif resume == "continue":
                    endpoint.continue_speaking()
                else:
                    endpoint.request_finish()
                await replacement.send(ClientSignal(type="evidence.stream.open", idempotency_key="remote_resolved_060",
                    turn_id=TURN_ID, payload={"content_type":"audio/pcm", "sample_rate_hz":16000,"channels":1,"language":"zh-CN"}))
                assert len(_retry_notices(replacement)) == (1 if resume == "audio" else 0)
                assert endpoint.awaiting_transcript is (resume == "audio")
                assert owner.chain.is_open and not _current(runtime)["answers"]
            finally:
                await replacement.close("test_complete")
                await remote.shutdown()
    asyncio.run(scenario())
