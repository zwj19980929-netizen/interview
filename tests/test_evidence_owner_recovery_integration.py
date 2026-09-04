import asyncio
import base64
from typing import Any, Callable

from app.adapters.livekit_media import LiveKitConfiguration, LiveKitMediaPlane
from app.core.auth import Principal
from app.core.time import utc_now
from app.domain.evidence_coordination import EvidenceCommandSubmission
from app.domain.interview_agent import (
    ClientCapabilities,
    ClientSignal,
    OpenAgentSession,
)
from app.file_storage.provider import reset_private_file_storage_for_tests
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.evidence_command_journal import EvidenceCommandJournal
from app.services.evidence_coordination import EvidenceOwnershipCoordinator
from app.services.evidence_media import DurableEvidenceMedia
from app.services.interview_agent import InterviewAgentRuntime
from app.services.interview_evidence import InterviewEvidenceChain
from app.services.livekit_evidence_ingress import (
    LiveKitEvidenceIngressSupervisor,
)
from app.services.streaming_stt import StreamingInterviewSTT


ORGANIZATION_ID = "org_default"
INTERVIEW_ID = "interview_owner_recovery"
TURN_ID = "turn_owner_recovery"


def _configuration() -> LiveKitConfiguration:
    return LiveKitConfiguration(
        url="wss://livekit.internal.example",
        api_key="test-key",
        api_secret="secret-at-least-sixteen-characters",
        egress_url="https://livekit.internal.example",
        storage_endpoint="",
        storage_bucket="",
        storage_region="auto",
        storage_access_key="",
        storage_secret="",
        authoritative_ingress_enabled=True,
        authoritative_ingress_mode="database_fenced",
        authoritative_ingress_grace_seconds=1,
        authoritative_ingress_lease_seconds=0.2,
        authoritative_ingress_renew_seconds=0.05,
    )


def _turn() -> dict[str, Any]:
    return {
        "id": TURN_ID,
        "interview_id": INTERVIEW_ID,
        "question_id": "question_owner_recovery",
        "question_snapshot_id": "snapshot_owner_recovery",
        "question_snapshot": {
            "id": "snapshot_owner_recovery",
            "question_text": "请说明断线恢复设计。",
            "standard_answer": "使用持久检查点和幂等提交。",
            "key_points": [],
            "rubric": {},
        },
        "order": 1,
        "phase": "position_bank",
        "status": "transcribing",
        "is_followup": False,
        "root_turn_id": TURN_ID,
        "followup_depth": 0,
        "allow_followup": False,
        "weight": 1.0,
        "question_spoken_text": "请说明断线恢复设计。",
        "utterances": [],
        "current_understanding": None,
        "conversation_acts": [],
        "started_at": utc_now(),
        "completed_at": None,
    }


def _session(store: InMemoryStore) -> None:
    now = utc_now()
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        transaction.interview_sessions.add(
            {
                "id": INTERVIEW_ID,
                "organization_id": ORGANIZATION_ID,
                "candidate_id": "candidate_owner_recovery",
                "candidate": {
                    "id": "candidate_owner_recovery",
                    "name": "候选人",
                },
                "settings": {"record_audio": True, "record_video": False},
                "status": "in_progress",
                "phase": "position_bank",
                "current_turn_id": TURN_ID,
                "turn_ids": [TURN_ID],
                "turns": [_turn()],
                "answers": [],
                "evaluation_revisions": [],
                "report_revisions": [],
                "current_report_id": None,
                "report_id": None,
                "followup_policy": {
                    "max_depth": 2,
                    "max_total": 0,
                    "max_per_root": 0,
                },
                "lifecycle_events": [],
                "agent_runtime": {
                    "floor": "candidate",
                    "floor_reason": "candidate_answering",
                    "last_sequence": 0,
                    "processed_signal_keys": [],
                    "active_performance_id": None,
                    "takeover": None,
                    "calibration_status": "completed",
                    "problems": [],
                },
                "agent_events": [],
                "created_at": now,
                "updated_at": now,
                "last_activity_at": now,
            }
        )


def _ticket(store: InMemoryStore, connection_id: str) -> None:
    now = utc_now()
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        transaction.agent_tickets.add(
            {
                "id": "agent_ticket:%s" % connection_id,
                "organization_id": ORGANIZATION_ID,
                "interview_id": INTERVIEW_ID,
                "connection_id": connection_id,
                "participant_identity": "candidate:%s" % connection_id,
                "participant_role": "candidate",
                "actor_id": "candidate:candidate_owner_recovery",
                "roles": ["candidate"],
                "ticket_hash": "not-used",
                "status": "consumed",
                "expires_at": now,
                "consumed_at": now,
                "media": {
                    "room_name": "interview-%s" % INTERVIEW_ID,
                    "evidence_transport": "livekit_server_subscriber",
                    "recovery": {
                        "server_checkpoint": {
                            "enabled": True,
                            "mode": "durable_media_checkpoint",
                        },
                        "browser_backfill": {
                            "enabled": True,
                            "protocol": "agent-json-backfill.v1",
                            "connection_id": connection_id,
                            "audio_epoch": "epoch_%s" % connection_id,
                            "retention_ms": 30_000,
                            "max_bytes": 2 * 1024 * 1024,
                            "max_frame_bytes": 32 * 1024,
                        },
                    },
                },
                "created_at": now,
                "updated_at": now,
            }
        )


def _opened(connection_id: str) -> OpenAgentSession:
    return OpenAgentSession(
        interview_id=INTERVIEW_ID,
        principal=Principal(
            actor_id="candidate:candidate_owner_recovery",
            organization_id=ORGANIZATION_ID,
            roles=frozenset({"candidate"}),
            authenticated=True,
        ),
        connection_id=connection_id,
        recovery_cursor=0,
        capabilities=ClientCapabilities(
            webrtc=True,
            audio_worklet=True,
            webgl=True,
            camera=True,
            microphone=True,
            speaker=True,
            avatar_fps=60,
            media_recorder=True,
            browser="Chrome test",
        ),
    )


class _FakeIngress:
    def __init__(self, _plane, _binding, *, on_audio_frame, on_state) -> None:
        self.on_audio_frame = on_audio_frame
        self.on_state = on_state
        self.connected = False

    async def connect(self) -> None:
        self.connected = True
        await self.on_state("connected")

    async def close(self) -> None:
        self.connected = False


class _DevelopmentRecoveryChain(InterviewEvidenceChain):
    recovery_calls = 0

    async def open(self, payload, *, turn_id, calibration_status):
        return await super().open(
            {
                **payload,
                "development_transcript": "我使用浏览器加密缓冲和幂等键恢复音频。",
                "development_confidence": 0.96,
            },
            turn_id=turn_id,
            calibration_status=calibration_status,
        )

    async def recover_persisted_turn(self, turn_id, payload=None):
        type(self).recovery_calls += 1
        return await super().recover_persisted_turn(
            turn_id,
            {
                "development_transcript": "我使用持久检查点和幂等键恢复任务。",
                "development_confidence": 0.96,
            },
        )


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 2.0
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        await asyncio.sleep(0.01)


async def _start_unknown_seal(
    store: InMemoryStore,
    coordinator: EvidenceOwnershipCoordinator,
    journal: EvidenceCommandJournal,
):
    old_owner = coordinator.attach_control(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        connection_id="connection_old_owner",
        local_instance_id="instance_old_owner",
    )
    _ticket(store, "connection_controller")
    supervisor = LiveKitEvidenceIngressSupervisor(
        store,
        media_plane=LiveKitMediaPlane(_configuration()),
        ingress_factory=_FakeIngress,
        evidence_chain_factory=_DevelopmentRecoveryChain,
        instance_id="instance_successor",
        coordinator=coordinator,
        journal=journal,
        command_poll_seconds=0.005,
    )
    runtime = InterviewAgentRuntime(store)
    runtime.evidence_ingress = supervisor
    channel = await runtime.open(_opened("connection_controller"))
    managed = channel._evidence_session
    assert managed is not None
    assert managed.ownership is None

    signal = ClientSignal(
        type="evidence.finish",
        idempotency_key="seal_unknown_result",
        turn_id=TURN_ID,
        payload={"endpoint": "explicit"},
    )
    sending = asyncio.create_task(channel.send(signal))
    await _wait_until(lambda: _command_count(store) == 1)
    claimed = journal.claim_next(old_owner.commit_fence(), ORGANIZATION_ID)
    assert claimed is not None
    assert claimed.command_type == "evidence.seal"
    return old_owner, supervisor, runtime, channel, managed, sending, signal


async def _reconnect_to_unknown_seal(
    store: InMemoryStore,
    coordinator: EvidenceOwnershipCoordinator,
    journal: EvidenceCommandJournal,
):
    """Create the command before the replacement control generation exists."""

    old_owner = coordinator.attach_control(
        interview_id=INTERVIEW_ID,
        organization_id=ORGANIZATION_ID,
        connection_id="connection_old_owner",
        local_instance_id="instance_old_owner",
    )
    signal = ClientSignal(
        type="evidence.finish",
        idempotency_key="seal_unknown_result",
        turn_id=TURN_ID,
        payload={"endpoint": "explicit"},
    )
    journal.submit(
        old_owner,
        EvidenceCommandSubmission(
            command_type="evidence.seal",
            idempotency_key=signal.idempotency_key,
            turn_id=signal.turn_id,
            payload=signal.payload,
            deadline_seconds=120,
        ),
    )
    claimed = journal.claim_next(old_owner.commit_fence(), ORGANIZATION_ID)
    assert claimed is not None

    _ticket(store, "connection_controller")
    supervisor = LiveKitEvidenceIngressSupervisor(
        store,
        media_plane=LiveKitMediaPlane(_configuration()),
        ingress_factory=_FakeIngress,
        evidence_chain_factory=_DevelopmentRecoveryChain,
        instance_id="instance_successor",
        coordinator=coordinator,
        journal=journal,
        command_poll_seconds=0.005,
    )
    runtime = InterviewAgentRuntime(store)
    runtime.evidence_ingress = supervisor
    channel = await runtime.open(_opened("connection_controller"))
    managed = channel._evidence_session
    assert managed is not None
    assert managed.ownership is None
    sending = asyncio.create_task(channel.send(signal))
    await _wait_until(
        lambda: int(_command(store).get("control_rebind_count", 0)) == 1
    )
    return old_owner, supervisor, runtime, channel, managed, sending, signal


def _command_count(store: InMemoryStore) -> int:
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        return len(transaction.evidence_commands.list())


def _command(store: InMemoryStore) -> dict[str, Any]:
    with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
        commands = transaction.evidence_commands.list()
    assert len(commands) == 1
    return commands[0]


def test_successor_reclaims_unknown_seal_and_repairs_complete_checkpoint_once(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv(
            "INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private")
        )
        reset_private_file_storage_for_tests()
        _DevelopmentRecoveryChain.recovery_calls = 0
        store = InMemoryStore()
        _session(store)
        coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=0.2)
        journal = EvidenceCommandJournal(store, claim_seconds=0.08)
        (
            old_owner,
            supervisor,
            _runtime,
            channel,
            managed,
            sending,
            signal,
        ) = await _reconnect_to_unknown_seal(store, coordinator, journal)
        writer = DurableEvidenceMedia(
            store,
            organization_id=ORGANIZATION_ID,
            segment_target_bytes=4096,
        ).open_writer(
            interview_id=INTERVIEW_ID,
            turn_id=TURN_ID,
            content_type="audio/pcm",
            sample_rate_hz=16_000,
            channels=1,
            fence=old_owner.commit_fence(),
        )
        writer.append(b"\x01\x00" * 2048)
        assert writer.seal(complete=True).recoverability == "complete"
        # No cooperative release: the former process disappeared after the
        # provider effect became unknown. The successor must use DB lease time.

        await asyncio.wait_for(sending, timeout=3)
        await _wait_until(lambda: _command(store)["status"] == "completed")
        current = supervisor.interviews.get_interview(
            INTERVIEW_ID, ORGANIZATION_ID
        )
        assert len(current["answers"]) == 1
        assert _DevelopmentRecoveryChain.recovery_calls == 1
        command = _command(store)
        assert command["attempt_count"] == 2
        assert command["route_revision"] == 2
        assert command["control_rebind_count"] == 1
        assert command["target_ownership_epoch"] == 2
        assert command["outcome"]["disposition"] == "applied"
        assert managed.ownership is not None
        assert managed.ownership.ownership_epoch == 2

        # Both the command journal and AgentChannel idempotency converge on the
        # same effect after the controller retries its unknown response.
        await channel.send(signal)
        current = supervisor.interviews.get_interview(
            INTERVIEW_ID, ORGANIZATION_ID
        )
        assert len(current["answers"]) == 1
        await channel.close("test_complete")
        await supervisor.shutdown()
        reset_private_file_storage_for_tests()

    asyncio.run(scenario())


def test_successor_treats_existing_answer_as_unknown_seal_effect_receipt(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv(
            "INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private")
        )
        reset_private_file_storage_for_tests()
        _DevelopmentRecoveryChain.recovery_calls = 0
        store = InMemoryStore()
        _session(store)
        coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=0.2)
        journal = EvidenceCommandJournal(store, claim_seconds=0.08)
        (
            old_owner,
            supervisor,
            _runtime,
            channel,
            _managed,
            sending,
            _signal,
        ) = await _start_unknown_seal(store, coordinator, journal)
        writer = DurableEvidenceMedia(
            store,
            organization_id=ORGANIZATION_ID,
            segment_target_bytes=4096,
        ).open_writer(
            interview_id=INTERVIEW_ID,
            turn_id=TURN_ID,
            content_type="audio/pcm",
            sample_rate_hz=16_000,
            channels=1,
            fence=old_owner.commit_fence(),
        )
        writer.append(b"\x02\x00" * 2048)
        writer.seal(complete=True)
        old_stream = StreamingInterviewSTT(
            store,
            INTERVIEW_ID,
            organization_id=ORGANIZATION_ID,
            commit_fence=old_owner.commit_fence(),
            commit_guard=lambda: coordinator.assert_current(
                old_owner.commit_fence(), ORGANIZATION_ID
            ),
        )
        await old_stream.recover_persisted(
            turn_id=TURN_ID,
            development_transcript="旧 owner 已提交答案，但应答结果未知。",
        )
        assert len(
            supervisor.interviews.get_interview(
                INTERVIEW_ID, ORGANIZATION_ID
            )["answers"]
        ) == 1
        assert coordinator.release(old_owner) is True

        await asyncio.wait_for(sending, timeout=3)
        current = supervisor.interviews.get_interview(
            INTERVIEW_ID, ORGANIZATION_ID
        )
        assert len(current["answers"]) == 1
        assert _DevelopmentRecoveryChain.recovery_calls == 0
        assert _command(store)["status"] == "completed"
        assert any(
            getattr(event, "type", None) == "session.snapshot"
            and getattr(event, "payload", {}).get("completed_answers") == 1
            for event in list(channel._queue._queue)
        )
        await channel.close("test_complete")
        await supervisor.shutdown()
        reset_private_file_storage_for_tests()

    asyncio.run(scenario())


def test_successor_fails_closed_and_pauses_for_incomplete_checkpoint(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv(
            "INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private")
        )
        reset_private_file_storage_for_tests()
        _DevelopmentRecoveryChain.recovery_calls = 0
        store = InMemoryStore()
        _session(store)
        coordinator = EvidenceOwnershipCoordinator(store, lease_seconds=0.2)
        journal = EvidenceCommandJournal(store, claim_seconds=0.08)
        (
            old_owner,
            supervisor,
            _runtime,
            channel,
            _managed,
            sending,
            _signal,
        ) = await _start_unknown_seal(store, coordinator, journal)
        writer = DurableEvidenceMedia(
            store,
            organization_id=ORGANIZATION_ID,
            segment_target_bytes=4096,
        ).open_writer(
            interview_id=INTERVIEW_ID,
            turn_id=TURN_ID,
            content_type="audio/pcm",
            sample_rate_hz=16_000,
            channels=1,
            fence=old_owner.commit_fence(),
        )
        checkpoint = writer.append(b"\x03\x00" * 2048)
        assert checkpoint.recoverability == "sealed_prefix"
        writer.abort()
        assert coordinator.release(old_owner) is True

        # AgentChannel converts command errors into role-projected problem
        # events; the journal still records one terminal non-retryable result.
        await asyncio.wait_for(sending, timeout=3)
        current = supervisor.interviews.get_interview(
            INTERVIEW_ID, ORGANIZATION_ID
        )
        assert current["answers"] == []
        assert current["status"] == "paused"
        fatal_problems = [
            item
            for item in (current.get("agent_runtime") or {}).get("problems", [])
            if item.get("code") == "EVIDENCE_MEDIA_INCOMPLETE"
        ]
        assert len(fatal_problems) == 1
        assert fatal_problems[0]["recoverable"] is False
        command = _command(store)
        assert command["status"] == "rejected"
        assert command["outcome"]["error_code"] == "EVIDENCE_MEDIA_INCOMPLETE"
        assert command["outcome"]["retryable"] is False
        assert _DevelopmentRecoveryChain.recovery_calls == 1
        assert any(
            getattr(event, "type", None) == "problem"
            and getattr(event, "payload", {}).get("code")
            == "EVIDENCE_MEDIA_INCOMPLETE"
            and getattr(event, "payload", {}).get("recoverable") is False
            for event in list(channel._queue._queue)
        )
        await channel.close("test_complete")
        await supervisor.shutdown()
        reset_private_file_storage_for_tests()

    asyncio.run(scenario())


def test_browser_backfill_and_duplicate_finish_commit_one_candidate_answer(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv(
            "INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private")
        )
        reset_private_file_storage_for_tests()
        store = InMemoryStore()
        _session(store)
        with persistence_for(store).transaction(ORGANIZATION_ID) as transaction:
            session = transaction.interview_sessions.get(INTERVIEW_ID)
            session["turns"][0]["status"] = "asking"
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                session, expected_version=session["version"]
            )
        _ticket(store, "connection_browser")
        supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress,
            evidence_chain_factory=_DevelopmentRecoveryChain,
            instance_id="instance_browser_backfill",
            command_poll_seconds=0.005,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_browser"))
        await channel.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="browser_open",
                turn_id=TURN_ID,
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                    "language": "zh-CN",
                },
            )
        )
        managed = channel._evidence_session
        assert managed is not None and managed.evidence_open, runtime.interviews.get_interview(
            INTERVIEW_ID, ORGANIZATION_ID
        )["agent_runtime"].get("problems")
        await managed._receive_state("candidate_disconnected")
        scope = {
            "source_connection_id": "connection_browser",
            "audio_epoch": "epoch_connection_browser",
        }
        await channel.send(
            ClientSignal(
                type="evidence.recovery.begin",
                idempotency_key="browser_begin",
                turn_id=TURN_ID,
                payload={
                    **scope,
                    "first_sequence": 31,
                    "last_sequence": 32,
                    "total_bytes": 8192,
                    "captured_from_ms": 1000,
                    "captured_to_ms": 1256,
                },
            )
        )
        current = runtime.interviews.get_interview(INTERVIEW_ID, ORGANIZATION_ID)
        batch_id = next(
            iter(current["agent_runtime"]["browser_backfill_batches"])
        )
        for sequence, sample in ((31, b"\x01\x00"), (32, b"\x02\x00")):
            signal = ClientSignal(
                type="evidence.recovery.chunk",
                idempotency_key="browser_chunk_%d" % sequence,
                turn_id=TURN_ID,
                payload={
                    **scope,
                    "batch_id": batch_id,
                    "client_sequence": sequence,
                    "audio_base64": base64.b64encode(sample * 2048).decode("ascii"),
                },
            )
            await channel.send(signal)
            await channel.send(signal)
        await channel.send(
            ClientSignal(
                type="evidence.recovery.complete",
                idempotency_key="browser_complete",
                turn_id=TURN_ID,
                payload={**scope, "batch_id": batch_id},
            )
        )
        finish = ClientSignal(
            type="evidence.finish",
            idempotency_key="browser_finish_once",
            turn_id=TURN_ID,
            payload={"endpoint": "explicit"},
        )
        await channel.send(finish)
        await channel.send(finish)
        current = runtime.interviews.get_interview(INTERVIEW_ID, ORGANIZATION_ID)
        assert len(current["answers"]) == 1
        assert current["answers"][0]["turn_id"] == TURN_ID
        assert current["answers"][0]["audio_uri"]
        commands = [
            item
            for item in store.evidence_commands.values()
            if item.get("interview_id") == INTERVIEW_ID
        ]
        assert len([item for item in commands if item["command_type"] == "evidence.backfill"]) == 2
        assert len([item for item in commands if item["command_type"] == "evidence.seal"]) == 1
        await channel.close("test_complete")
        await supervisor.shutdown()
        reset_private_file_storage_for_tests()

    asyncio.run(scenario())
