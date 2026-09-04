import asyncio
import base64

import pytest

from app.adapters.livekit_audio_ingress import LiveKitIngressAudioFrame
from app.adapters.livekit_media import LiveKitConfiguration, LiveKitMediaPlane
from app.core.auth import Principal
from app.core.errors import ApiError
from app.core.time import utc_now
from app.domain.evidence_coordination import EvidenceCommandSubmission
from app.domain.interview_agent import (
    ClientCapabilities,
    ClientSignal,
    FloorOwner,
    OpenAgentSession,
    Replayability,
)
from app.persistence.provider import persistence_for
from app.file_storage.provider import reset_private_file_storage_for_tests
from app.repositories.memory import InMemoryStore
from app.services.interview_agent import InterviewAgentRuntime
from app.services.interview_evidence import (
    EvidenceAudioResult,
    EvidenceFinishResult,
    EvidenceOpenResult,
)
from app.services.livekit_evidence_ingress import (
    LiveKitEvidenceIngressSupervisor,
)


def _configuration(
    *,
    grace_seconds: float = 30,
    lease_seconds: float = 15,
    renew_seconds: float = 5,
) -> LiveKitConfiguration:
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
        authoritative_ingress_grace_seconds=grace_seconds,
        authoritative_ingress_lease_seconds=lease_seconds,
        authoritative_ingress_renew_seconds=renew_seconds,
    )


def _session(store: InMemoryStore) -> dict:
    now = utc_now()
    item = {
        "id": "interview_livekit_supervisor",
        "organization_id": "org_default",
        "candidate_id": "candidate_1",
        "candidate": {"id": "candidate_1", "name": "候选人"},
        "settings": {"record_audio": True, "record_video": False},
        "status": "in_progress",
        "phase": "position_bank",
        "current_turn_id": "turn_1",
        "turns": [
            {
                "id": "turn_1",
                "order": 1,
                "status": "asking",
                "phase": "position_bank",
                "is_followup": False,
                "question_spoken_text": "请说明断线恢复。",
            }
        ],
        "answers": [],
        "evaluation_revisions": [],
        "report_revisions": [],
        "lifecycle_events": [],
        "agent_runtime": {
            "floor": "candidate",
            "floor_reason": "warmup_listening",
            "last_sequence": 0,
            "processed_signal_keys": [],
            "active_performance_id": None,
            "takeover": None,
            "calibration_status": "listening",
        },
        "agent_events": [],
        "created_at": now,
        "updated_at": now,
    }
    with persistence_for(store).transaction("org_default") as transaction:
        saved = transaction.interview_sessions.add(item)
        transaction.agent_tickets.add(
            {
                "id": "agent_ticket_livekit_supervisor",
                "organization_id": "org_default",
                "interview_id": saved["id"],
                "connection_id": "connection_original",
                "participant_identity": "candidate:connection_original",
                "participant_role": "candidate",
                "actor_id": "candidate:candidate_1",
                "roles": ["candidate"],
                "ticket_hash": "not-used",
                "status": "consumed",
                "expires_at": now,
                "consumed_at": now,
                "media": {
                    "room_name": "interview-%s" % saved["id"],
                    "evidence_transport": "livekit_server_subscriber",
                    "recovery": {
                        "server_checkpoint": {
                            "enabled": True,
                            "mode": "durable_media_checkpoint",
                        },
                        "browser_backfill": {
                            "enabled": True,
                            "protocol": "agent-json-backfill.v1",
                            "connection_id": "connection_original",
                            "audio_epoch": "epoch_original",
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
    return saved


def _opened(connection_id: str) -> OpenAgentSession:
    return OpenAgentSession(
        interview_id="interview_livekit_supervisor",
        principal=Principal(
            actor_id="candidate:candidate_1",
            organization_id="org_default",
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
    instances = []

    def __init__(self, _media_plane, binding, *, on_audio_frame, on_state) -> None:
        self.binding = binding
        self.on_audio_frame = on_audio_frame
        self.on_state = on_state
        self.connected = False
        self.closed = False
        self.instances.append(self)

    async def connect(self) -> None:
        self.connected = True
        await self.on_state("connected")

    async def close(self) -> None:
        self.connected = False
        self.closed = True

    async def push(
        self, sequence: int = 1, pcm_s16le: bytes = b"\x01\x00" * 320
    ) -> None:
        await self.on_audio_frame(
            LiveKitIngressAudioFrame(
                sequence=sequence,
                track_sid="mic_1",
                sample_rate_hz=16_000,
                channels=1,
                samples_per_channel=len(pcm_s16le) // 2,
                captured_at=utc_now(),
                pcm_s16le=pcm_s16le,
            )
        )


class _FakeEvidenceChain:
    def __init__(self) -> None:
        self.open_count = 0
        self.audio_count = 0
        self.is_open = False
        self.turn_id = None
        self.kind = None
        self.finish_count = 0

    async def open(self, payload, *, turn_id, calibration_status):
        self.open_count += 1
        self.is_open = True
        self.kind = "warmup" if calibration_status != "completed" else "formal"
        self.turn_id = turn_id if self.kind == "formal" else None
        return EvidenceOpenResult(self.kind, self.turn_id, [])

    async def send_audio(self, _audio):
        if not self.is_open:
            return None
        self.audio_count += 1
        return EvidenceAudioResult(
            kind=self.kind,
            turn_id=self.turn_id,
            first_server_audio=self.audio_count == 1,
            events=[
                {
                    "type": "transcript.partial",
                    "text": "服务端实时字幕 %s" % self.audio_count,
                    "confidence": 0.9,
                }
            ],
        )

    async def finish(self, _payload):
        self.finish_count += 1
        self.is_open = False
        return EvidenceFinishResult(
            kind=self.kind,
            turn_id=self.turn_id,
            events=[],
            final=None,
            interview_result=None,
        )

    async def abort_warmup(self):
        self.is_open = False

    async def close_for_disconnect(self):
        self.is_open = False
        return None


def test_open_ready_ack_covers_same_floor_duplicate_existing_and_reconnect() -> None:
    async def scenario() -> None:
        _FakeIngress.instances.clear()
        store = InMemoryStore()
        _session(store)
        supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress,
            command_poll_seconds=0.001,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        first = await runtime.open(_opened("connection_original"))
        managed = first._evidence_session
        assert managed is not None

        class CountingResetChain(_FakeEvidenceChain):
            def __init__(self) -> None:
                super().__init__()
                self.reset_count = 0

            async def abort_warmup(self):
                self.reset_count += 1
                await super().abort_warmup()

        chain = CountingResetChain()
        managed.chain = chain

        first_open = ClientSignal(
            type="evidence.stream.open",
            idempotency_key="ready_ack_first",
            causation_id="ready_ack_first_cause",
            payload={
                "content_type": "audio/pcm",
                "sample_rate_hz": 16_000,
                "channels": 1,
            },
        )
        await first.send(first_open)
        await first.send(first_open)
        await first.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="ready_ack_existing",
                causation_id="ready_ack_existing_cause",
                payload=dict(first_open.payload),
            )
        )

        assert chain.open_count == 1
        first_acks = [
            event
            for event in list(first._queue._queue)
            if event.type == "floor.changed"
            and event.payload.get("reason") == "warmup_stream_open"
        ]
        assert len(first_acks) == 2
        assert all(
            event.replayability == Replayability.TRANSIENT
            for event in first_acks
        )
        assert sum(
            event.causation_id == "ready_ack_first_cause"
            for event in first_acks
        ) == 1
        assert sum(
            event.causation_id == "ready_ack_existing_cause"
            for event in first_acks
        ) == 1

        await first.close("control_lost")
        replacement = await runtime.open(_opened("connection_replacement"))
        await replacement.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="ready_ack_reconnect",
                causation_id="ready_ack_reconnect_cause",
                payload=dict(first_open.payload),
            )
        )
        assert chain.open_count == 1
        replacement_acks = [
            event
            for event in list(replacement._queue._queue)
            if event.type == "floor.changed"
            and event.payload.get("reason") == "warmup_stream_open"
            and event.causation_id == "ready_ack_reconnect_cause"
        ]
        assert len(replacement_acks) == 1
        assert replacement_acks[0].replayability == Replayability.TRANSIENT

        retry_signal = ClientSignal(
            type="warmup.retry",
            idempotency_key="ready_ack_retry",
            causation_id="ready_ack_retry_cause",
        )
        await replacement.send(retry_signal)
        await replacement.send(retry_signal)
        retry_acks = [
            event
            for event in list(replacement._queue._queue)
            if event.type == "floor.changed"
            and event.payload.get("reason") == "warmup_retry"
            and event.causation_id == "ready_ack_retry_cause"
        ]
        assert len(retry_acks) == 1
        assert retry_acks[0].replayability == Replayability.TRANSIENT
        assert chain.reset_count == 1

        ready_count_before_late_duplicate = len(
            [
                event
                for event in list(replacement._queue._queue)
                if event.type == "floor.changed"
                and event.payload.get("reason") == "warmup_stream_open"
            ]
        )
        runtime._set_floor(
            "interview_livekit_supervisor",
            FloorOwner.AGENT,
            "test_agent_speaking_after_reset",
            "org_default",
        )
        await replacement.send(first_open)
        after_late_duplicate = runtime.interviews.get_interview(
            "interview_livekit_supervisor"
        )
        assert after_late_duplicate["agent_runtime"]["floor"] == "agent"
        assert chain.open_count == 1
        assert len(
            [
                event
                for event in list(replacement._queue._queue)
                if event.type == "floor.changed"
                and event.payload.get("reason") == "warmup_stream_open"
            ]
        ) == ready_count_before_late_duplicate
        persisted = runtime.interviews.get_interview(
            "interview_livekit_supervisor"
        )
        assert not any(
            event.get("type") == "floor.changed"
            and (event.get("payload") or {}).get("reason")
            in {
                "warmup_stream_open",
                "evidence_stream_open",
                "warmup_retry",
            }
            for event in persisted.get("agent_events", [])
        )

        await replacement.close("test_complete")
        await managed.stop("test_complete")

    asyncio.run(scenario())


def test_open_ready_is_owner_ordered_before_reset_and_never_replayed_by_poll(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        _FakeIngress.instances.clear()
        store = InMemoryStore()
        saved = _session(store)
        supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress,
            command_poll_seconds=0.001,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        assert managed is not None
        chain = _FakeEvidenceChain()
        managed.chain = chain
        grant = managed._require_attached(channel)

        original_get_receipt = supervisor.journal.get_receipt
        release_open_poll = False

        def delayed_get_receipt(current_grant, command_id):
            receipt = original_get_receipt(current_grant, command_id)
            if not release_open_poll and receipt.status == "completed":
                return receipt.model_copy(
                    update={"status": "pending", "outcome": None}
                )
            return receipt

        monkeypatch.setattr(
            supervisor.journal, "get_receipt", delayed_get_receipt
        )
        open_task = asyncio.create_task(
            channel.send(
                ClientSignal(
                    type="evidence.stream.open",
                    idempotency_key="owner_ordered_open",
                    causation_id="owner_ordered_open_cause",
                    payload={
                        "content_type": "audio/pcm",
                        "sample_rate_hz": 16_000,
                        "channels": 1,
                    },
                )
            )
        )

        for _ in range(200):
            with persistence_for(store).transaction(
                "org_default"
            ) as transaction:
                open_commands = [
                    item
                    for item in transaction.evidence_commands.list()
                    if item["command_type"] == "evidence.open"
                ]
            if open_commands and open_commands[0]["status"] == "completed":
                break
            await asyncio.sleep(0.001)
        assert chain.open_count == 1
        assert open_commands[0]["status"] == "completed"
        ready_before_reset = [
            event
            for event in list(channel._queue._queue)
            if event.type == "floor.changed"
            and event.payload.get("reason") == "warmup_stream_open"
        ]
        assert len(ready_before_reset) == 1

        reset_receipt = supervisor.journal.submit(
            grant,
            EvidenceCommandSubmission(
                command_type="evidence.reset",
                idempotency_key="reset_before_open_poll_recovers",
                causation_id="reset_before_open_poll_recovers_cause",
                deadline_seconds=30,
            ),
        )
        managed._command_wake.set()
        for _ in range(200):
            reset_result = original_get_receipt(
                grant, reset_receipt.command_id
            )
            if reset_result.status == "completed":
                break
            await asyncio.sleep(0.001)
        assert reset_result.status == "completed"
        assert chain.is_open is False

        runtime._set_floor(
            saved["id"],
            FloorOwner.AGENT,
            "agent_after_reset",
            "org_default",
        )
        release_open_poll = True
        await asyncio.wait_for(open_task, timeout=1)

        after_poll = runtime.interviews.get_interview(saved["id"])
        assert after_poll["agent_runtime"]["floor"] == "agent"
        ready_after_poll = [
            event
            for event in list(channel._queue._queue)
            if event.type == "floor.changed"
            and event.payload.get("reason") == "warmup_stream_open"
        ]
        assert len(ready_after_poll) == 1

        await channel.close("test_complete")
        await managed.stop("test_complete")

    asyncio.run(scenario())


def test_warmup_reset_side_effects_are_owner_ordered_before_controller_poll(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        _FakeIngress.instances.clear()
        store = InMemoryStore()
        saved = _session(store)
        supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress,
            command_poll_seconds=0.001,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        original = await runtime.open(_opened("connection_original"))
        managed = original._evidence_session
        assert managed is not None

        class CountingResetChain(_FakeEvidenceChain):
            def __init__(self) -> None:
                super().__init__()
                self.reset_count = 0

            async def abort_warmup(self):
                self.reset_count += 1
                await super().abort_warmup()

        chain = CountingResetChain()
        chain.is_open = True
        chain.kind = "warmup"
        managed.chain = chain

        original_get_receipt = supervisor.journal.get_receipt
        release_controller_poll = False

        def delayed_get_receipt(current_grant, command_id):
            receipt = original_get_receipt(current_grant, command_id)
            if not release_controller_poll and receipt.status == "completed":
                return receipt.model_copy(
                    update={"status": "pending", "outcome": None}
                )
            return receipt

        monkeypatch.setattr(
            supervisor.journal, "get_receipt", delayed_get_receipt
        )
        reset_signal = ClientSignal(
            type="warmup.retry",
            idempotency_key="owner_ordered_reset",
            causation_id="owner_ordered_reset_cause",
        )
        reset_task = asyncio.create_task(original.send(reset_signal))

        reset_row = None
        for _ in range(200):
            with persistence_for(store).transaction(
                "org_default"
            ) as transaction:
                reset_row = next(
                    (
                        item
                        for item in transaction.evidence_commands.list()
                        if item["command_type"] == "evidence.reset"
                    ),
                    None,
                )
            if reset_row is not None and reset_row["status"] == "completed":
                break
            await asyncio.sleep(0.001)
        assert reset_row is not None
        assert reset_row["status"] == "completed"
        assert chain.reset_count == 1
        reset_state = runtime.interviews.get_interview(saved["id"])
        assert reset_state["agent_runtime"]["calibration_status"] == "retrying"
        assert reset_state["agent_runtime"]["calibration_retry_required"] is False
        assert len(
            [
                event
                for event in list(original._queue._queue)
                if event.type == "floor.changed"
                and event.payload.get("reason") == "warmup_retry"
                and event.causation_id == "owner_ordered_reset_cause"
            ]
        ) == 1

        replacement = await runtime.open(_opened("connection_replacement"))
        snapshots = [
            event
            for event in list(replacement._queue._queue)
            if event.type == "session.snapshot"
        ]
        assert snapshots[-1].payload["calibration_status"] == "retrying"
        assert snapshots[-1].payload["calibration_retry_required"] is False

        release_controller_poll = True
        await asyncio.wait_for(reset_task, timeout=1)
        assert chain.reset_count == 1
        assert not any(
            event.type == "floor.changed"
            and event.payload.get("reason") == "warmup_retry"
            and event.causation_id == "owner_ordered_reset_cause"
            for event in list(replacement._queue._queue)
        )

        # A duplicate receipt is outcome-only: it cannot abort again or emit a
        # second transient ACK into the replacement controller's fresh gate.
        await replacement.send(reset_signal)
        assert chain.reset_count == 1
        assert not any(
            event.type == "floor.changed"
            and event.payload.get("reason") == "warmup_retry"
            and event.causation_id == "owner_ordered_reset_cause"
            for event in list(replacement._queue._queue)
        )

        replacement_reset = ClientSignal(
            type="warmup.retry",
            idempotency_key="replacement_owner_reset",
            causation_id="replacement_owner_reset_cause",
        )
        await replacement.send(replacement_reset)
        await replacement.send(replacement_reset)
        assert chain.reset_count == 2
        replacement_acks = [
            event
            for event in list(replacement._queue._queue)
            if event.type == "floor.changed"
            and event.payload.get("reason") == "warmup_retry"
            and event.causation_id == "replacement_owner_reset_cause"
        ]
        assert len(replacement_acks) == 1
        assert replacement_acks[0].replayability == Replayability.TRANSIENT

        await original.close("test_complete")
        await replacement.close("test_complete")
        await managed.stop("test_complete")

    asyncio.run(scenario())


def test_superseded_controller_vad_terminates_without_polluting_history() -> None:
    async def scenario() -> None:
        _FakeIngress.instances.clear()
        store = InMemoryStore()
        saved = _session(store)
        supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress,
            command_poll_seconds=0.001,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        stale = await runtime.open(_opened("connection_original"))
        current = await runtime.open(_opened("connection_replacement"))
        before = runtime.interviews.get_interview(saved["id"])
        problems_before = list(
            (before.get("agent_runtime") or {}).get("problems", [])
        )
        history_before = list(before.get("agent_events", []))

        await stale.send(
            ClientSignal(
                type="speech.started",
                idempotency_key="stale_vad_0",
            )
        )
        termination = stale._termination_task
        for index in range(1, 20):
            with pytest.raises(ApiError) as closed:
                await stale.send(
                    ClientSignal(
                        type=(
                            "speech.started"
                            if index % 2
                            else "speech.stopped"
                        ),
                        idempotency_key="stale_vad_%d" % index,
                    )
                )
            assert closed.value.code == "AGENT_CHANNEL_CLOSED"
        if termination is not None:
            await asyncio.wait_for(termination, timeout=1)

        after = runtime.interviews.get_interview(saved["id"])
        assert (after.get("agent_runtime") or {}).get(
            "problems", []
        ) == problems_before
        assert after.get("agent_events", []) == history_before
        assert current._closed is False
        assert not any(
            event.type == "problem"
            and event.payload.get("code")
            in {
                "LIVEKIT_EVIDENCE_CONTROLLER_STALE",
                "EVIDENCE_CONTROL_STALE",
                "INTERVIEW_NOT_IN_PROGRESS",
            }
            for event in list(current._queue._queue)
        )

        await current.close("test_complete")
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_authoritative_ingress_survives_control_reconnect_and_rejects_ws_pcm() -> None:
    async def scenario() -> None:
        _FakeIngress.instances.clear()
        store = InMemoryStore()
        _session(store)
        supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        first = await runtime.open(_opened("connection_original"))
        managed = first._evidence_session
        assert managed is not None
        fake_chain = _FakeEvidenceChain()
        managed.chain = fake_chain
        await managed.ensure_ingress("connection_original")
        assert len(_FakeIngress.instances) == 1
        assert _FakeIngress.instances[0].binding.candidate_identity == "candidate:connection_original"

        await first.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="open_warmup_once",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                },
            )
        )
        assert fake_chain.open_count == 1
        with pytest.raises(ApiError) as websocket_pcm:
            await first._send_audio(
                ClientSignal(
                    type="evidence.audio.chunk",
                    idempotency_key="ws_pcm_forbidden",
                    audio=b"\x00\x00",
                )
            )
        assert websocket_pcm.value.code == "LIVEKIT_EVIDENCE_AUDIO_REQUIRED"

        await first.close("control_lost")
        assert managed.evidence_open is True
        assert _FakeIngress.instances[0].closed is False
        await _FakeIngress.instances[0].push()
        assert fake_chain.audio_count == 1
        assert any(
            item.raw.get("type") == "transcript.partial"
            for item in managed._pending
        )

        replacement = await runtime.open(_opened("connection_replacement"))
        assert replacement._evidence_session is managed
        queued = list(replacement._queue._queue)
        assert any(
            event.type == "transcript.partial"
            and event.payload.get("text") == "服务端实时字幕 1"
            for event in queued
        )
        assert any(
            event.type == "speech.started"
            and event.payload.get("server_audio_received") is True
            and event.payload.get("media_transport")
            == "livekit_server_subscriber"
            for event in queued
        )
        assert len(_FakeIngress.instances) == 1

        await replacement.close("test_complete")
        await managed.stop("test_complete")
        assert _FakeIngress.instances[0].closed is True
        assert supervisor._sessions == {}

    asyncio.run(scenario())


def test_authoritative_endpoint_finishes_after_control_disconnect() -> None:
    async def scenario() -> None:
        _FakeIngress.instances.clear()
        store = InMemoryStore()
        saved = _session(store)
        with persistence_for(store).transaction("org_default") as transaction:
            current = transaction.interview_sessions.get(saved["id"])
            current["agent_runtime"]["calibration_status"] = "completed"
            current["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                current, expected_version=current["version"]
            )
        supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress,
            endpoint_delay_seconds=0.001,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        assert managed is not None

        class EndpointChain(_FakeEvidenceChain):
            async def finish(self, _payload):
                self.finish_count += 1
                self.is_open = False
                return EvidenceFinishResult(
                    kind="formal",
                    turn_id="turn_1",
                    events=[
                        {
                            "type": "utterance.not_accepted",
                            "suggested_action": "continue_listening",
                            "confidence": 0.9,
                        }
                    ],
                    final=None,
                    interview_result={"accepted": False},
                )

        chain = EndpointChain()
        managed.chain = chain
        await channel.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="endpoint_open",
                turn_id="turn_1",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                },
            )
        )
        assert any(
            event.type == "floor.changed"
            and event.payload.get("owner") == "candidate"
            and event.payload.get("reason") == "evidence_stream_open"
            and event.replayability == Replayability.TRANSIENT
            for event in list(channel._queue._queue)
        )
        await channel.send(
            ClientSignal(
                type="speech.stopped",
                idempotency_key="endpoint_speech_stopped",
                turn_id="turn_1",
            )
        )
        await channel.close("control_lost_before_endpoint")
        await asyncio.sleep(0.02)
        assert chain.finish_count == 1
        current = runtime.interviews.get_interview(saved["id"])
        assert (current.get("agent_runtime") or {}).get("floor") == "candidate"
        await managed.stop("test_complete")

    asyncio.run(scenario())


def test_warmup_endpoint_finishes_without_a_formal_turn_id() -> None:
    async def scenario() -> None:
        _FakeIngress.instances.clear()
        store = InMemoryStore()
        saved = _session(store)
        supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress,
            endpoint_delay_seconds=0.001,
            command_poll_seconds=0.001,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        assert managed is not None

        class WarmupEndpointChain(_FakeEvidenceChain):
            async def finish(self, _payload):
                self.finish_count += 1
                self.is_open = False
                return EvidenceFinishResult(
                    kind="warmup",
                    turn_id=None,
                    events=[],
                    final={"type": "transcript.final", "text": "试音识别成功。", "confidence": 0.96},
                    interview_result=None,
                )

        chain = WarmupEndpointChain()
        managed.chain = chain
        await channel.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="warmup_endpoint_open",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                },
            )
        )
        await channel.send(
            ClientSignal(
                type="speech.stopped",
                idempotency_key="warmup_endpoint_speech_stopped",
            )
        )
        for _ in range(100):
            if chain.finish_count:
                break
            await asyncio.sleep(0.001)

        assert chain.finish_count == 1
        current = runtime.interviews.get_interview(saved["id"])
        assert current["agent_runtime"]["calibration_status"] == "awaiting_confirmation"
        assert any(
            event.type == "transcript.final"
            and event.payload.get("calibration") is True
            and event.payload.get("text") == "试音识别成功。"
            for event in list(channel._queue._queue)
        )
        await managed.stop("test_complete")

    asyncio.run(scenario())


def test_failed_warmup_finalize_is_not_retried_or_overwritten() -> None:
    async def scenario() -> None:
        _FakeIngress.instances.clear()
        store = InMemoryStore()
        saved = _session(store)
        supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress,
            command_poll_seconds=0.001,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        assert managed is not None

        class MissingFinalWarmupChain(_FakeEvidenceChain):
            async def finish(self, _payload):
                self.finish_count += 1
                self.is_open = False
                self.kind = None
                raise ApiError(
                    "PROVIDER_FINAL_TRANSCRIPT_MISSING",
                    "provider detail must not be projected",
                    status_code=502,
                )

        chain = MissingFinalWarmupChain()
        managed.chain = chain
        await channel.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="failed_warmup_open",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                },
            )
        )
        await channel.send(
            ClientSignal(
                type="evidence.finish",
                idempotency_key="failed_warmup_finish",
                payload={"endpoint": "semantic_timeout"},
            )
        )

        assert chain.finish_count == 1
        current = runtime.interviews.get_interview(saved["id"])
        assert current["agent_runtime"]["calibration_status"] == "retrying"
        assert current["agent_runtime"]["calibration_retry_required"] is True
        assert current["answers"] == []
        assert current["turns"][0]["status"] == "asking"
        problems = [
            event
            for event in list(channel._queue._queue)
            if event.type == "problem"
            and event.payload.get("code")
            == "PROVIDER_FINAL_TRANSCRIPT_MISSING"
        ]
        assert len(problems) == 1
        assert problems[0].payload == {
            "code": "PROVIDER_FINAL_TRANSCRIPT_MISSING",
            "message": "试音暂时不可用，请重试或等待面试官接管。",
            "recoverable": True,
            "action": "retry_warmup",
            "calibration": True,
        }
        with persistence_for(store).transaction("org_default") as transaction:
            command = next(
                item
                for item in transaction.evidence_commands.list()
                if item["command_type"] == "evidence.seal"
            )
        assert command["status"] == "rejected"
        assert command["attempt_count"] == 1
        assert command["last_error_code"] == "PROVIDER_FINAL_TRANSCRIPT_MISSING"
        assert command["outcome"] == {
            "disposition": "rejected",
            "effective_turn_id": None,
            "event_cursor": None,
            "error_code": "PROVIDER_FINAL_TRANSCRIPT_MISSING",
            "retryable": False,
        }

        await channel.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="open_blocked_until_explicit_retry",
                causation_id="open_blocked_until_explicit_retry_cause",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                },
            )
        )
        assert chain.open_count == 1
        blocked = runtime.interviews.get_interview(saved["id"])
        assert blocked["agent_runtime"]["calibration_retry_required"] is True
        assert any(
            item.get("code") == "WARMUP_RETRY_REQUIRED"
            for item in blocked["agent_runtime"]["problems"]
        )
        persisted_problems = blocked["agent_runtime"]["problems"]
        with persistence_for(store).transaction("org_default") as transaction:
            blocked_open = next(
                item
                for item in transaction.evidence_commands.list()
                if item.get("last_error_code") == "WARMUP_RETRY_REQUIRED"
            )
        assert blocked_open["status"] == "rejected"
        assert blocked_open["attempt_count"] == 1
        assert blocked_open["outcome"]["retryable"] is False

        await channel.close("reload_after_warmup_failure")
        replacement = await runtime.open(_opened("connection_replacement"))
        snapshots = [
            event
            for event in list(replacement._queue._queue)
            if event.type == "session.snapshot"
        ]
        assert snapshots[-1].payload["calibration_status"] == "retrying"
        assert snapshots[-1].payload["calibration_retry_required"] is True

        await replacement.send(
            ClientSignal(
                type="warmup.retry",
                idempotency_key="explicit_retry_after_failure",
                causation_id="explicit_retry_after_failure_cause",
            )
        )
        retried = runtime.interviews.get_interview(saved["id"])
        assert retried["agent_runtime"]["calibration_status"] == "retrying"
        assert retried["agent_runtime"]["calibration_retry_required"] is False
        assert retried["agent_runtime"]["problems"] == persisted_problems
        assert any(
            event.type == "floor.changed"
            and event.payload.get("reason") == "warmup_retry"
            and event.causation_id == "explicit_retry_after_failure_cause"
            for event in list(replacement._queue._queue)
        )

        await replacement.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="open_after_explicit_retry",
                causation_id="open_after_explicit_retry_cause",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                },
            )
        )
        assert chain.open_count == 2
        assert any(
            event.type == "floor.changed"
            and event.payload.get("reason") == "warmup_stream_open"
            and event.causation_id == "open_after_explicit_retry_cause"
            for event in list(replacement._queue._queue)
        )

        await replacement.close("test_complete")
        await managed.stop("test_complete")

    asyncio.run(scenario())


def test_partially_projected_warmup_finalize_returns_to_retrying_once() -> None:
    async def scenario() -> None:
        _FakeIngress.instances.clear()
        store = InMemoryStore()
        saved = _session(store)
        supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress,
            command_poll_seconds=0.001,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        assert managed is not None

        class CompletedProviderWarmupChain(_FakeEvidenceChain):
            async def finish(self, _payload):
                self.finish_count += 1
                self.is_open = False
                self.kind = None
                return EvidenceFinishResult(
                    kind="warmup",
                    turn_id=None,
                    events=[],
                    final={
                        "type": "transcript.final",
                        "text": "试音识别成功。",
                        "confidence": 0.96,
                    },
                    interview_result=None,
                )

        chain = CompletedProviderWarmupChain()
        managed.chain = chain
        original_complete = channel._complete_warmup_evidence

        async def fail_after_partial_completion(result, signal):
            runtime._set_calibration(
                saved["id"], "awaiting_confirmation", "org_default"
            )
            raise ApiError(
                "AGENT_EXPRESSION_TTS_UNAVAILABLE",
                "downstream provider detail must not be projected",
                status_code=503,
            )

        channel._complete_warmup_evidence = fail_after_partial_completion
        try:
            await channel.send(
                ClientSignal(
                    type="evidence.stream.open",
                    idempotency_key="partial_warmup_open",
                    payload={
                        "content_type": "audio/pcm",
                        "sample_rate_hz": 16_000,
                        "channels": 1,
                    },
                )
            )
            await channel.send(
                ClientSignal(
                    type="evidence.finish",
                    idempotency_key="partial_warmup_finish",
                    payload={"endpoint": "semantic_timeout"},
                )
            )
        finally:
            channel._complete_warmup_evidence = original_complete

        assert chain.finish_count == 1
        current = runtime.interviews.get_interview(saved["id"])
        assert current["agent_runtime"]["calibration_status"] == "retrying"
        assert current["agent_runtime"]["calibration_retry_required"] is True
        assert current["answers"] == []
        assert current["turns"][0]["status"] == "asking"
        problems = [
            event
            for event in list(channel._queue._queue)
            if event.type == "problem"
            and event.payload.get("code")
            == "AGENT_EXPRESSION_TTS_UNAVAILABLE"
        ]
        assert len(problems) == 1
        assert problems[0].payload.get("recoverable") is True
        assert problems[0].payload.get("action") == "retry_warmup"
        assert problems[0].payload.get("calibration") is True
        with persistence_for(store).transaction("org_default") as transaction:
            command = next(
                item
                for item in transaction.evidence_commands.list()
                if item["command_type"] == "evidence.seal"
            )
        assert command["status"] == "rejected"
        assert command["attempt_count"] == 1
        assert command["last_error_code"] == "AGENT_EXPRESSION_TTS_UNAVAILABLE"
        assert command["outcome"]["error_code"] == (
            "AGENT_EXPRESSION_TTS_UNAVAILABLE"
        )
        assert command["outcome"]["retryable"] is False

        await managed.stop("test_complete")

    asyncio.run(scenario())


def test_browser_gap_backfill_is_bounded_deduplicated_and_finish_is_once(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv(
            "INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private")
        )
        reset_private_file_storage_for_tests()
        _FakeIngress.instances.clear()
        store = InMemoryStore()
        saved = _session(store)
        with persistence_for(store).transaction("org_default") as transaction:
            current = transaction.interview_sessions.get(saved["id"])
            current["agent_runtime"]["calibration_status"] = "completed"
            current["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                current, expected_version=current["version"]
            )
        supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress,
            command_poll_seconds=0.005,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        assert managed is not None
        chain = _FakeEvidenceChain()
        managed.chain = chain
        await channel.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="formal_open_for_backfill",
                turn_id="turn_1",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                },
            )
        )
        scope = {
            "source_connection_id": "connection_original",
            "audio_epoch": "epoch_original",
        }
        not_authorized = ClientSignal(
            type="evidence.recovery.begin",
            idempotency_key="gap_not_authorized",
            turn_id="turn_1",
            payload={
                **scope,
                "first_sequence": 21,
                "last_sequence": 22,
                "total_bytes": 8,
                "captured_from_ms": 1_000,
                "captured_to_ms": 1_020,
            },
        )
        with pytest.raises(ApiError) as rejected_gap:
            await managed.begin_browser_backfill(channel, not_authorized)
        assert rejected_gap.value.code == "BROWSER_BACKFILL_GAP_NOT_AUTHORIZED"
        await managed._receive_state("candidate_disconnected")
        wrong_connection = ClientSignal(
            type="evidence.recovery.begin",
            idempotency_key="wrong_connection",
            turn_id="turn_1",
            payload={
                **scope,
                "source_connection_id": "connection_unknown",
                "first_sequence": 21,
                "last_sequence": 22,
                "total_bytes": 8,
                "captured_from_ms": 1_000,
                "captured_to_ms": 1_020,
            },
        )
        with pytest.raises(ApiError) as rejected_connection:
            await managed.begin_browser_backfill(channel, wrong_connection)
        assert rejected_connection.value.code == "BROWSER_BACKFILL_CONNECTION_INVALID"

        wrong_epoch = wrong_connection.model_copy(
            update={
                "idempotency_key": "wrong_epoch",
                "payload": {
                    **wrong_connection.payload,
                    "source_connection_id": "connection_original",
                    "audio_epoch": "epoch_wrong",
                },
            }
        )
        with pytest.raises(ApiError) as rejected_epoch:
            await managed.begin_browser_backfill(channel, wrong_epoch)
        assert rejected_epoch.value.code == "BROWSER_BACKFILL_EPOCH_INVALID"

        begin = ClientSignal(
            type="evidence.recovery.begin",
            idempotency_key="begin_backfill",
            turn_id="turn_1",
            payload={
                **scope,
                "first_sequence": 21,
                "last_sequence": 22,
                "total_bytes": 8,
                "captured_from_ms": 1_000,
                "captured_to_ms": 1_020,
            },
        )
        await channel.send(begin)
        current = runtime.interviews.get_interview(saved["id"])
        batches = current["agent_runtime"]["browser_backfill_batches"]
        assert len(batches) == 1
        batch_id = next(iter(batches))

        for sequence, content in ((21, b"\x01\x00\x02\x00"), (22, b"\x03\x00\x04\x00")):
            chunk = ClientSignal(
                type="evidence.recovery.chunk",
                idempotency_key="client_chunk_%d" % sequence,
                turn_id="turn_1",
                payload={
                    **scope,
                    "batch_id": batch_id,
                    "client_sequence": sequence,
                    "audio_base64": base64.b64encode(content).decode("ascii"),
                },
            )
            await channel.send(chunk)
            await channel.send(chunk)
        assert chain.audio_count == 2

        complete = ClientSignal(
            type="evidence.recovery.complete",
            idempotency_key="complete_backfill",
            turn_id="turn_1",
            payload={**scope, "batch_id": batch_id},
        )
        await channel.send(complete)
        current = runtime.interviews.get_interview(saved["id"])
        batch = current["agent_runtime"]["browser_backfill_batches"][batch_id]
        assert batch["ack_through"] == 22
        assert batch["status"] == "complete"

        finish = ClientSignal(
            type="evidence.finish",
            idempotency_key="finish_after_backfill_once",
            turn_id="turn_1",
            payload={"endpoint": "explicit"},
        )
        await channel.send(finish)
        await channel.send(finish)
        assert chain.finish_count == 1

        await channel.close("test_complete")
        await supervisor.shutdown()
        reset_private_file_storage_for_tests()

    asyncio.run(scenario())


def test_remote_controller_dispatches_to_database_fenced_owner() -> None:
    async def scenario() -> None:
        _FakeIngress.instances.clear()
        store = InMemoryStore()
        saved = _session(store)
        with persistence_for(store).transaction("org_default") as transaction:
            current = transaction.interview_sessions.get(saved["id"])
            current["agent_runtime"]["calibration_status"] = "completed"
            current["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                current, expected_version=current["version"]
            )
        owner_supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress,
            instance_id="instance_owner",
            command_poll_seconds=0.005,
        )
        owner_runtime = InterviewAgentRuntime(store)
        owner_runtime.evidence_ingress = owner_supervisor
        owner_channel = await owner_runtime.open(_opened("connection_original"))
        owner_session = owner_channel._evidence_session
        assert owner_session is not None

        class RemoteChain(_FakeEvidenceChain):
            def __init__(self) -> None:
                super().__init__()
                self.reset_count = 0

            async def finish(self, _payload):
                self.finish_count += 1
                self.is_open = False
                return EvidenceFinishResult(
                    kind="formal",
                    turn_id="turn_1",
                    events=[
                        {
                            "type": "utterance.not_accepted",
                            "suggested_action": "continue_listening",
                            "confidence": 0.9,
                        }
                    ],
                    final=None,
                    interview_result={"accepted": False},
                )

            async def abort_warmup(self):
                self.reset_count += 1
                await super().abort_warmup()

        chain = RemoteChain()
        owner_session.chain = chain
        await owner_channel.close("moved_to_remote_controller")

        controller_supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress,
            instance_id="instance_controller",
            command_poll_seconds=0.005,
        )
        controller_runtime = InterviewAgentRuntime(store)
        controller_runtime.evidence_ingress = controller_supervisor
        controller = await controller_runtime.open(_opened("connection_remote"))
        proxy = controller._evidence_session
        assert proxy is not None
        assert proxy.ownership is None
        assert len(_FakeIngress.instances) == 1

        open_signal = ClientSignal(
            type="evidence.stream.open",
            idempotency_key="remote_open_once",
            turn_id="turn_1",
            payload={
                "content_type": "audio/pcm",
                "sample_rate_hz": 16_000,
                "channels": 1,
            },
        )
        await controller.send(open_signal)
        await controller.send(open_signal)
        assert chain.open_count == 1

        await controller.send(
            ClientSignal(
                type="speech.started",
                idempotency_key="remote_speech_started",
                turn_id="turn_1",
            )
        )
        await controller.send(
            ClientSignal(
                type="speech.stopped",
                idempotency_key="remote_speech_stopped",
                turn_id="turn_1",
            )
        )
        assert owner_session._endpoint_task is not None
        await controller.send(
            ClientSignal(
                type="continue_speaking",
                idempotency_key="remote_continue",
                turn_id="turn_1",
            )
        )
        assert owner_session._endpoint_task is None
        await controller.send(
            ClientSignal(
                type="evidence.finish",
                idempotency_key="remote_seal",
                turn_id="turn_1",
                payload={"endpoint": "explicit"},
            )
        )
        assert chain.finish_count == 1

        with persistence_for(store).transaction("org_default") as transaction:
            current = transaction.interview_sessions.get(saved["id"])
            current["agent_runtime"]["calibration_status"] = (
                "awaiting_confirmation"
            )
            current["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                current, expected_version=current["version"]
            )
        await controller.send(
            ClientSignal(
                type="warmup.retry",
                idempotency_key="remote_reset",
            )
        )
        assert chain.reset_count == 1

        with persistence_for(store).transaction("org_default") as transaction:
            commands = transaction.evidence_commands.list()
        assert len(commands) == 6
        assert {item["status"] for item in commands} == {"completed"}
        assert all(item["attempt_count"] == 1 for item in commands)

        await controller.close("test_complete")
        await controller_supervisor.shutdown()
        await owner_supervisor.shutdown()

    asyncio.run(scenario())


def test_remote_controller_backfills_private_audio_to_the_fenced_owner(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv(
            "INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private")
        )
        reset_private_file_storage_for_tests()
        _FakeIngress.instances.clear()
        store = InMemoryStore()
        saved = _session(store)
        with persistence_for(store).transaction("org_default") as transaction:
            current = transaction.interview_sessions.get(saved["id"])
            current["agent_runtime"]["calibration_status"] = "completed"
            current["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                current, expected_version=current["version"]
            )
            transaction.agent_tickets.add(
                {
                    "id": "agent_ticket_remote_backfill",
                    "organization_id": "org_default",
                    "interview_id": saved["id"],
                    "connection_id": "connection_remote_backfill",
                    "participant_identity": "candidate:connection_remote_backfill",
                    "participant_role": "candidate",
                    "actor_id": "candidate:candidate_1",
                    "roles": ["candidate"],
                    "ticket_hash": "not-used",
                    "status": "consumed",
                    "expires_at": utc_now(),
                    "consumed_at": utc_now(),
                    "media": {
                        "room_name": "interview-%s" % saved["id"],
                        "evidence_transport": "livekit_server_subscriber",
                        "recovery": {
                            "server_checkpoint": {
                                "enabled": True,
                                "mode": "durable_media_checkpoint",
                            },
                            "browser_backfill": {
                                "enabled": True,
                                "protocol": "agent-json-backfill.v1",
                                "connection_id": "connection_remote_backfill",
                                "audio_epoch": "epoch_remote",
                                "retention_ms": 30_000,
                                "max_bytes": 2 * 1024 * 1024,
                                "max_frame_bytes": 32 * 1024,
                            },
                        },
                    },
                    "created_at": utc_now(),
                    "updated_at": utc_now(),
                }
            )
        owner_supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress,
            instance_id="backfill_owner",
            command_poll_seconds=0.005,
        )
        owner_runtime = InterviewAgentRuntime(store)
        owner_runtime.evidence_ingress = owner_supervisor
        owner = await owner_runtime.open(_opened("connection_original"))
        owner_session = owner._evidence_session
        assert owner_session is not None
        chain = _FakeEvidenceChain()
        owner_session.chain = chain
        await owner.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="remote_backfill_open",
                turn_id="turn_1",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                },
            )
        )
        await owner_session._receive_state("candidate_disconnected")
        await owner.close("controller_reconnected_elsewhere")

        controller_supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress,
            instance_id="backfill_controller",
            command_poll_seconds=0.005,
        )
        controller_runtime = InterviewAgentRuntime(store)
        controller_runtime.evidence_ingress = controller_supervisor
        controller = await controller_runtime.open(
            _opened("connection_remote_backfill")
        )
        scope = {
            "source_connection_id": "connection_original",
            "audio_epoch": "epoch_original",
        }
        await controller.send(
            ClientSignal(
                type="evidence.recovery.begin",
                idempotency_key="remote_backfill_begin",
                turn_id="turn_1",
                payload={
                    **scope,
                    "first_sequence": 50,
                    "last_sequence": 50,
                    "total_bytes": 4,
                    "captured_from_ms": 1000,
                    "captured_to_ms": 1010,
                },
            )
        )
        current = controller_runtime.interviews.get_interview(saved["id"])
        batch_id = next(
            iter(current["agent_runtime"]["browser_backfill_batches"])
        )
        await controller.send(
            ClientSignal(
                type="evidence.recovery.chunk",
                idempotency_key="remote_backfill_chunk",
                turn_id="turn_1",
                payload={
                    **scope,
                    "batch_id": batch_id,
                    "client_sequence": 50,
                    "audio_base64": base64.b64encode(b"\x01\x00\x02\x00").decode(
                        "ascii"
                    ),
                },
            )
        )
        assert chain.audio_count == 1
        await controller.send(
            ClientSignal(
                type="evidence.recovery.complete",
                idempotency_key="remote_backfill_complete",
                turn_id="turn_1",
                payload={**scope, "batch_id": batch_id},
            )
        )
        assert (
            controller_runtime.interviews.get_interview(saved["id"])[
                "agent_runtime"
            ]["browser_backfill_batches"][batch_id]["status"]
            == "complete"
        )
        await controller.close("test_complete")
        await controller_supervisor.shutdown()
        await owner_supervisor.shutdown()
        reset_private_file_storage_for_tests()

    asyncio.run(scenario())


def test_remote_attach_cancels_owner_control_expiry_via_database_truth() -> None:
    async def scenario() -> None:
        _FakeIngress.instances.clear()
        store = InMemoryStore()
        _session(store)
        configuration = _configuration(
            grace_seconds=0.02,
            lease_seconds=0.3,
            renew_seconds=0.02,
        )
        owner_supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(configuration),
            ingress_factory=_FakeIngress,
            instance_id="instance_owner",
            command_poll_seconds=0.005,
        )
        owner_runtime = InterviewAgentRuntime(store)
        owner_runtime.evidence_ingress = owner_supervisor
        owner = await owner_runtime.open(_opened("connection_original"))
        owner_session = owner._evidence_session
        assert owner_session is not None
        await owner.close("remote_reconnect")

        controller_supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(configuration),
            ingress_factory=_FakeIngress,
            instance_id="instance_controller",
            command_poll_seconds=0.005,
        )
        controller_runtime = InterviewAgentRuntime(store)
        controller_runtime.evidence_ingress = controller_supervisor
        controller = await controller_runtime.open(_opened("connection_remote"))

        await asyncio.sleep(0.06)
        assert owner_session._stopped is False
        assert owner_session.ingress_connected is True
        assert _FakeIngress.instances[0].closed is False

        await controller.close("test_complete")
        await controller_supervisor.shutdown()
        await owner_supervisor.shutdown()

    asyncio.run(scenario())


def test_unknown_owner_effect_is_never_recorded_as_completed() -> None:
    async def scenario() -> None:
        _FakeIngress.instances.clear()
        store = InMemoryStore()
        _session(store)
        supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress,
            instance_id="instance_owner",
            command_poll_seconds=0.005,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        assert managed is not None

        class UnknownResultChain(_FakeEvidenceChain):
            async def open(self, payload, *, turn_id, calibration_status):
                self.open_count += 1
                raise RuntimeError("provider outcome unknown")

        chain = UnknownResultChain()
        managed.chain = chain
        sending = asyncio.create_task(
            channel.send(
                ClientSignal(
                    type="evidence.stream.open",
                    idempotency_key="unknown_open_result",
                    payload={
                        "content_type": "audio/pcm",
                        "sample_rate_hz": 16_000,
                        "channels": 1,
                    },
                )
            )
        )
        await asyncio.sleep(0.04)
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)

        with persistence_for(store).transaction("org_default") as transaction:
            commands = transaction.evidence_commands.list()
        assert len(commands) == 1
        assert commands[0]["status"] in {"pending", "running"}
        assert commands[0]["outcome"] is None
        assert chain.open_count >= 1

        await channel.close("test_complete")
        await supervisor.shutdown()

    asyncio.run(scenario())
