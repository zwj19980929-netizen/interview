import asyncio
import base64
from types import SimpleNamespace

import pytest
from livekit import rtc

from app.adapters.livekit_audio_ingress import (
    LiveKitAudioIngressBackpressureError,
    LiveKitAudioIngressFailure,
    LiveKitCandidateAudioIngress,
    LiveKitIngressAudioFrame,
)
from app.adapters.livekit_media import LiveKitConfiguration, LiveKitMediaPlane
from app.core.auth import Principal
from app.core.errors import ApiError
from app.core.interview_agent_metrics import InterviewAgentMetrics
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
        self.last_track_failure = None
        self.recovery_count = 0
        self.drain_count = 0
        self.instances.append(self)

    async def connect(self) -> None:
        self.connected = True
        await self.on_state("connected")

    async def close(self) -> None:
        self.connected = False
        self.closed = True

    async def recover_audio_stream(self) -> bool:
        self.recovery_count += 1
        self.last_track_failure = None
        return self.connected

    async def drain(self) -> int:
        self.drain_count += 1
        return 0

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


@pytest.mark.parametrize("replacement_turn", ["turn_1", "followup_turn"])
def test_endpoint_scope_cannot_cross_capture_or_rearm_after_new_speech(replacement_turn) -> None:
    async def scenario():
        store = InMemoryStore()
        saved = _session(store)
        supervisor = LiveKitEvidenceIngressSupervisor(
            store, media_plane=LiveKitMediaPlane(_configuration()), ingress_factory=_FakeIngress,
            endpoint_delay_seconds=0.1, command_poll_seconds=0.001,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        chain = _FakeEvidenceChain()
        managed.chain = chain
        with persistence_for(store).transaction("org_default") as transaction:
            session = transaction.interview_sessions.get(saved["id"])
            session["agent_runtime"]["calibration_status"] = "completed"
            transaction.interview_sessions.update(session, expected_version=session["version"])

        async def send(kind, key, turn_id, payload=None):
            await channel.send(ClientSignal(type=kind, idempotency_key=key, turn_id=turn_id, payload=payload or {}))

        await send("evidence.stream.open", "first_open", "turn_1")
        first_capture = managed._capture_id
        await send("speech.started", "first_start", "turn_1", {"capture_id": first_capture})
        await send("speech.stopped", "first_stop", "turn_1", {"capture_id": first_capture})
        timer_id = "endpoint_scope_old"
        assert managed._endpoint_id is None and managed._endpoint_task is None
        # Mimic the incident: a consumed root stream and delayed VAD stop arrive
        # before the follow-up opens. Neither may attach to the next capture.
        chain.is_open = False
        await send("speech.stopped", "late_unscoped_stop", None)
        await send("evidence.stream.open", "replacement_open", replacement_turn)
        new_capture = managed._capture_id
        assert new_capture != first_capture
        assert managed._endpoint_task is None
        await send("speech.started", "late_old_start", "turn_1", {"capture_id": first_capture})
        await send("speech.stopped", "late_old_stop", "turn_1", {"capture_id": first_capture})
        await send("evidence.finish", "queued_old_timer", "turn_1", {
            "endpoint": "semantic_timeout", "capture_id": first_capture, "endpoint_id": timer_id,
        })
        assert chain.finish_count == 0
        assert managed._endpoint_task is None
        # A stop without a start in this new capture must not arm a countdown.
        await send("speech.stopped", "stop_without_start", replacement_turn, {"capture_id": new_capture})
        assert managed._endpoint_task is None
        await send("speech.started", "new_start", replacement_turn, {"capture_id": new_capture})
        await send("speech.stopped", "new_stop", replacement_turn, {"capture_id": new_capture})
        stopped_timer = "endpoint_scope_cancelled"
        await send("speech.started", "resumed_start", replacement_turn, {"capture_id": new_capture})
        await send("speech.stopped", "resumed_stop", replacement_turn, {"capture_id": new_capture})
        assert managed._endpoint_id is None
        await send("evidence.finish", "queued_cancelled_timer", replacement_turn, {
            "endpoint": "semantic_timeout", "capture_id": new_capture, "endpoint_id": stopped_timer,
        })
        assert chain.finish_count == 0
        await managed.cancel_endpoint()
        await channel.close("test_complete")
        await supervisor.shutdown()

    asyncio.run(scenario())


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


def test_partial_projection_backpressure_does_not_block_authoritative_audio() -> None:
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
            command_poll_seconds=0.001,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        assert managed is not None
        chain = _FakeEvidenceChain()
        managed.chain = chain

        projection_started = asyncio.Event()
        release_projection = asyncio.Event()
        projected_partials = []

        async def slow_projection(raw, _causation_id):
            if raw.get("type") != "transcript.partial":
                return
            projected_partials.append(raw.get("text"))
            if len(projected_partials) == 1:
                projection_started.set()
                await release_projection.wait()

        channel._project_evidence_event = slow_projection
        await channel.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="partial_pressure_open",
                turn_id="turn_1",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                },
            )
        )
        ingress = _FakeIngress.instances[0]
        await ingress.push(sequence=1)
        await asyncio.wait_for(projection_started.wait(), timeout=1)

        async def push_burst() -> None:
            for sequence in range(2, 502):
                await ingress.push(sequence=sequence)

        # A blocked browser/session projection must not consume the LiveKit
        # sink's two-second PCM budget. All 501 frames reach the Evidence chain.
        await asyncio.wait_for(push_burst(), timeout=1)
        assert chain.audio_count == 501
        assert len(managed._partial_projection_pending) == 1
        pending = next(iter(managed._partial_projection_pending.values()))
        assert pending.kind == "formal"
        assert pending.turn_id == "turn_1"
        assert pending.raw["text"] == "服务端实时字幕 501"

        release_projection.set()
        for _ in range(200):
            if (
                not managed._partial_projection_pending
                and len(projected_partials) >= 2
            ):
                break
            await asyncio.sleep(0.001)
        assert projected_partials == [
            "服务端实时字幕 1",
            "服务端实时字幕 501",
        ]

        await channel.close("test_complete")
        await managed.stop("test_complete")
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_formal_seal_waits_for_livekit_ingress_drain_barrier() -> None:
    async def scenario() -> None:
        drain_started = asyncio.Event()
        release_drain = asyncio.Event()

        class BarrierIngress(_FakeIngress):
            async def drain(self) -> int:
                self.drain_count += 1
                drain_started.set()
                await release_drain.wait()
                return 17

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
            ingress_factory=BarrierIngress,
            command_poll_seconds=0.001,
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
                idempotency_key="drain_barrier_open",
                turn_id="turn_1",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                },
            )
        )

        sealing = asyncio.create_task(
            channel.send(
                ClientSignal(
                    type="evidence.finish",
                    idempotency_key="drain_barrier_seal",
                    turn_id="turn_1",
                    payload={"endpoint": "explicit"},
                )
            )
        )
        await asyncio.wait_for(drain_started.wait(), timeout=1)
        assert chain.finish_count == 0
        release_drain.set()
        await asyncio.wait_for(sealing, timeout=1)

        assert chain.finish_count == 1
        assert _FakeIngress.instances[0].drain_count == 1
        await channel.close("test_complete")
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_formal_seal_drain_failure_pauses_without_creating_an_answer() -> None:
    async def scenario() -> None:
        class FailingDrainIngress(_FakeIngress):
            async def drain(self) -> int:
                self.drain_count += 1
                raise LiveKitAudioIngressBackpressureError("blocked sink")

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
            ingress_factory=FailingDrainIngress,
            command_poll_seconds=0.001,
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
                idempotency_key="drain_failure_open",
                turn_id="turn_1",
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
                idempotency_key="drain_failure_seal",
                turn_id="turn_1",
                payload={"endpoint": "explicit"},
            )
        )

        paused = runtime.interviews.get_interview(saved["id"])
        assert paused["status"] == "paused"
        assert paused["answers"] == []
        assert chain.finish_count == 0
        assert _FakeIngress.instances[0].drain_count == 1
        assert paused["agent_runtime"]["problems"][-1]["code"] == (
            "LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED"
        )
        assert paused["agent_runtime"]["problems"][-1]["cause_code"] == (
            "LIVEKIT_INGRESS_SINK_BACKPRESSURE"
        )
        with persistence_for(store).transaction("org_default") as transaction:
            seal = next(
                item
                for item in transaction.evidence_commands.list()
                if item["command_type"] == "evidence.seal"
            )
        assert seal["status"] == "rejected"
        assert seal["last_error_code"] == "LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED"

        await channel.close("test_complete")
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_real_livekit_sink_survives_more_than_two_seconds_of_blocked_partial_projection() -> None:
    async def scenario() -> None:
        store = InMemoryStore()
        saved = _session(store)
        with persistence_for(store).transaction("org_default") as transaction:
            current = transaction.interview_sessions.get(saved["id"])
            current["agent_runtime"]["calibration_status"] = "completed"
            current["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                current, expected_version=current["version"]
            )

        class EagerAudioStream:
            def __init__(self, count: int) -> None:
                self.remaining = count

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.remaining <= 0:
                    raise StopAsyncIteration
                self.remaining -= 1
                return SimpleNamespace(
                    frame=SimpleNamespace(
                        data=b"\x01\x00" * 320,
                        sample_rate=16_000,
                        num_channels=1,
                        samples_per_channel=320,
                    )
                )

            async def aclose(self) -> None:
                return None

        class Room:
            def __init__(self) -> None:
                self.handlers = {}
                self.remote_participants = {}

            def on(self, name, callback):
                self.handlers[name] = callback

            async def connect(self, _url, _token, _options):
                return None

            async def disconnect(self):
                return None

        created = {}

        def ingress_factory(media_plane, binding, *, on_audio_frame, on_state):
            room = Room()
            ingress = LiveKitCandidateAudioIngress(
                media_plane,
                binding,
                on_audio_frame=on_audio_frame,
                on_state=on_state,
                room_factory=lambda: room,
                audio_stream_factory=lambda _track: EagerAudioStream(600),
                audio_track_validator=lambda _track: True,
            )
            created.update(room=room, ingress=ingress)
            return ingress

        supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=ingress_factory,
            command_poll_seconds=0.001,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        assert managed is not None
        chain = _FakeEvidenceChain()
        managed.chain = chain

        projection_started = asyncio.Event()
        release_projection = asyncio.Event()

        async def blocked_projection(raw, _causation_id):
            if raw.get("type") == "transcript.partial":
                projection_started.set()
                await release_projection.wait()

        channel._project_evidence_event = blocked_projection
        await channel.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="real_sink_partial_pressure_open",
                turn_id="turn_1",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                },
            )
        )

        room = created["room"]
        publication = SimpleNamespace(
            sid="mic_1",
            source=rtc.TrackSource.SOURCE_MICROPHONE,
            kind=rtc.TrackKind.KIND_AUDIO,
            track=None,
        )
        publication.set_subscribed = lambda _enabled: None
        participant = SimpleNamespace(identity="candidate:connection_original")
        room.handlers["track_subscribed"](
            object(), publication, participant
        )
        ingress = created["ingress"]
        track_task = ingress._track_task
        assert track_task is not None
        await asyncio.wait_for(projection_started.wait(), timeout=1)
        # 600 x 20 ms represents 12 seconds of audio, six times the explicit
        # sink budget. It must drain while one partial projection is blocked.
        await asyncio.wait_for(track_task, timeout=1)

        assert chain.audio_count == 600
        assert ingress.last_track_failure is None
        assert len(managed._partial_projection_pending) == 1

        release_projection.set()
        await asyncio.sleep(0)
        await channel.close("test_complete")
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_audio_stream_failure_persists_sanitized_root_cause_only_for_diagnostics() -> None:
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
        ingress = _FakeIngress.instances[0]
        ingress.last_track_failure = LiveKitAudioIngressFailure(
            code="PROVIDER_STREAM_FAILED",
            cause_type="ProviderError",
        )

        await ingress.on_state("audio_stream_failed")

        paused = runtime.interviews.get_interview(saved["id"])
        assert paused["status"] == "paused"
        diagnostic = paused["agent_runtime"]["problems"][-1]
        assert diagnostic["code"] == "LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED"
        assert diagnostic["message"] == "RuntimeError"
        assert diagnostic["cause_code"] == "PROVIDER_STREAM_FAILED"
        assert diagnostic["cause_type"] == "ProviderError"
        candidate_problem = next(
            event
            for event in list(channel._queue._queue)
            if event.type == "problem"
            and event.payload.get("code")
            == "LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED"
        )
        assert "cause_code" not in candidate_problem.payload
        assert "cause_type" not in candidate_problem.payload

        await channel.close("test_complete")
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_warmup_audio_stream_backpressure_requires_retry_without_pausing_interview() -> None:
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
        await channel.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="warmup_backpressure_open",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                },
            )
        )
        ingress = _FakeIngress.instances[0]
        ingress.last_track_failure = LiveKitAudioIngressFailure(
            code="PROVIDER_BACKPRESSURE_EXCEEDED",
            cause_type="ProviderError",
        )

        await ingress.on_state("audio_stream_failed")

        retrying = runtime.interviews.get_interview(saved["id"])
        assert retrying["status"] == "in_progress"
        assert retrying["agent_runtime"]["calibration_status"] == "retrying"
        assert retrying["agent_runtime"]["calibration_retry_required"] is True
        assert retrying["answers"] == []
        assert retrying["turns"][0]["status"] == "asking"
        assert chain.is_open is False
        diagnostic = retrying["agent_runtime"]["problems"][-1]
        assert diagnostic["code"] == "LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED"
        assert diagnostic["recoverable"] is True
        assert diagnostic["action"] == "retry_warmup"
        assert diagnostic["cause_code"] == "PROVIDER_BACKPRESSURE_EXCEEDED"
        assert diagnostic["cause_type"] == "ProviderError"
        candidate_problems = [
            event
            for event in list(channel._queue._queue)
            if event.type == "problem"
            and event.payload.get("code")
            == "LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED"
        ]
        assert len(candidate_problems) == 1
        assert candidate_problems[0].payload == {
            "code": "LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED",
            "message": "试音暂时不可用，请重试或等待面试官接管。",
            "recoverable": True,
            "action": "retry_warmup",
            "calibration": True,
        }

        # Reproduce the incident ordering: the track has already failed and
        # recovered to the retry gate, then an old endpoint seal arrives. It
        # must reuse the first terminal receipt instead of completing warm-up
        # or adding EVIDENCE_TURN_REQUIRED as a contradictory second problem.
        await channel.send(
            ClientSignal(
                type="evidence.finish",
                idempotency_key="late_warmup_seal_after_backpressure",
                payload={"endpoint": "explicit"},
            )
        )
        still_retrying = runtime.interviews.get_interview(saved["id"])
        assert still_retrying["agent_runtime"]["calibration_status"] == "retrying"
        assert len(still_retrying["agent_runtime"]["problems"]) == 1
        with persistence_for(store).transaction("org_default") as transaction:
            late_seal = next(
                item
                for item in transaction.evidence_commands.list()
                if item["command_type"] == "evidence.seal"
            )
        assert late_seal["status"] == "rejected"
        assert late_seal["last_error_code"] == (
            "LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED"
        )

        await channel.send(
            ClientSignal(
                type="warmup.retry",
                idempotency_key="warmup_backpressure_retry",
            )
        )
        reset = runtime.interviews.get_interview(saved["id"])
        assert reset["status"] == "in_progress"
        assert reset["agent_runtime"]["calibration_status"] == "retrying"
        assert reset["agent_runtime"]["calibration_retry_required"] is False
        assert ingress.recovery_count == 1
        assert ingress.last_track_failure is None

        await channel.close("test_complete")
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_failed_warmup_epoch_discards_late_successful_seal() -> None:
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
        finish_started = asyncio.Event()
        release_finish = asyncio.Event()

        class LateSuccessfulWarmupChain(_FakeEvidenceChain):
            async def finish(self, _payload):
                self.finish_count += 1
                self.is_open = False
                finish_started.set()
                await release_finish.wait()
                return EvidenceFinishResult(
                    kind="warmup",
                    turn_id=None,
                    events=[],
                    final={
                        "type": "transcript.final",
                        "text": "这条迟到结果不得采用。",
                        "confidence": 0.99,
                    },
                    interview_result=None,
                )

        chain = LateSuccessfulWarmupChain()
        managed.chain = chain
        await channel.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="warmup_race_open",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                },
            )
        )
        sealing = asyncio.create_task(
            channel.send(
                ClientSignal(
                    type="evidence.finish",
                    idempotency_key="warmup_race_seal",
                    payload={"endpoint": "explicit"},
                )
            )
        )
        await asyncio.wait_for(finish_started.wait(), timeout=1)
        ingress = _FakeIngress.instances[0]
        ingress.last_track_failure = LiveKitAudioIngressFailure(
            code="PROVIDER_BACKPRESSURE_EXCEEDED",
            cause_type="ProviderError",
        )
        await ingress.on_state("audio_stream_failed")
        release_finish.set()
        await asyncio.wait_for(sealing, timeout=1)

        current = runtime.interviews.get_interview(saved["id"])
        assert current["status"] == "in_progress"
        assert current["agent_runtime"]["calibration_status"] == "retrying"
        assert current["agent_runtime"]["calibration_retry_required"] is True
        assert current["answers"] == []
        assert chain.finish_count == 1
        assert not any(
            event.type == "transcript.final"
            and event.payload.get("text") == "这条迟到结果不得采用。"
            for event in list(channel._queue._queue)
        )
        assert not any(
            event.type == "conversation.act.selected"
            and event.payload.get("act_type") == "warmup_confirmation"
            for event in list(channel._queue._queue)
        )
        with persistence_for(store).transaction("org_default") as transaction:
            seal = next(
                item
                for item in transaction.evidence_commands.list()
                if item["command_type"] == "evidence.seal"
            )
        assert seal["status"] == "rejected"
        assert seal["last_error_code"] == "LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED"

        await channel.close("test_complete")
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_partial_coalescing_never_drops_non_partial_stream_events() -> None:
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
            command_poll_seconds=0.001,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        assert managed is not None

        class MixedEventChain(_FakeEvidenceChain):
            async def send_audio(self, _audio):
                if not self.is_open:
                    return None
                self.audio_count += 1
                return EvidenceAudioResult(
                    kind=self.kind,
                    turn_id=self.turn_id,
                    first_server_audio=False,
                    events=[
                        {
                            "type": "transcript.partial",
                            "text": "partial %s" % self.audio_count,
                            "confidence": 0.9,
                        },
                        {
                            "type": "stream.error",
                            "error_code": "TRANSIENT_%s" % self.audio_count,
                        },
                    ],
                )

        chain = MixedEventChain()
        managed.chain = chain
        projected = []

        async def capture_projection(raw, _causation_id):
            projected.append(dict(raw))

        channel._project_evidence_event = capture_projection
        await channel.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="mixed_projection_open",
                turn_id="turn_1",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                },
            )
        )
        ingress = _FakeIngress.instances[0]
        for sequence in range(1, 101):
            await ingress.push(sequence=sequence)

        errors = [
            item for item in projected if item.get("type") == "stream.error"
        ]
        assert len(errors) == 100
        assert [item["error_code"] for item in errors] == [
            "TRANSIENT_%s" % sequence for sequence in range(1, 101)
        ]
        assert len(managed._partial_projection_pending) <= 1

        await asyncio.sleep(0)
        partials = [
            item for item in projected if item.get("type") == "transcript.partial"
        ]
        assert len(partials) <= 1
        if partials:
            assert partials[-1]["text"] == "partial 100"

        await channel.close("test_complete")
        await managed.stop("test_complete")
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_final_waits_for_inflight_partial_and_rejects_late_audio_partial() -> None:
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
            command_poll_seconds=0.001,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        assert managed is not None

        late_audio_started = asyncio.Event()
        release_late_audio = asyncio.Event()

        class FinishRaceChain(_FakeEvidenceChain):
            async def send_audio(self, _audio):
                if not self.is_open:
                    return None
                self.audio_count += 1
                if self.audio_count == 2:
                    late_audio_started.set()
                    await release_late_audio.wait()
                return EvidenceAudioResult(
                    kind="formal",
                    turn_id="turn_1",
                    first_server_audio=False,
                    events=[
                        {
                            "type": "transcript.partial",
                            "text": "partial %s" % self.audio_count,
                            "confidence": 0.9,
                        }
                    ],
                )

            async def finish(self, _payload):
                self.finish_count += 1
                self.is_open = False
                return EvidenceFinishResult(
                    kind="formal",
                    turn_id="turn_1",
                    events=[
                        {
                            "type": "transcript.final",
                            "text": "authoritative final",
                            "confidence": 0.98,
                        },
                        {
                            "type": "stream.error",
                            "error_code": "BATCH_REPAIRED",
                        },
                    ],
                    final={
                        "type": "transcript.final",
                        "text": "authoritative final",
                        "confidence": 0.98,
                    },
                    interview_result={"accepted": True},
                )

        chain = FinishRaceChain()
        managed.chain = chain
        partial_started = asyncio.Event()
        release_partial = asyncio.Event()
        projected = []

        async def capture_projection(raw, _causation_id):
            projected.append((raw.get("type"), raw.get("text")))
            if raw.get("type") == "transcript.partial":
                partial_started.set()
                await release_partial.wait()

        async def ignore_after_finish(
            _turn_id,
            _signal,
            *,
            conversation_action_selected,
            stop_evidence_session,
        ):
            assert conversation_action_selected is False
            assert stop_evidence_session is False

        channel._project_evidence_event = capture_projection
        channel._after_formal_evidence_finished = ignore_after_finish
        await channel.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="final_barrier_open",
                turn_id="turn_1",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                },
            )
        )
        ingress = _FakeIngress.instances[0]
        await ingress.push(sequence=1)
        await asyncio.wait_for(partial_started.wait(), timeout=1)
        late_audio = asyncio.create_task(ingress.push(sequence=2))
        await asyncio.wait_for(late_audio_started.wait(), timeout=1)

        finish = asyncio.create_task(
            managed._finish_with_channel(
                channel,
                ClientSignal(
                    type="evidence.finish",
                    idempotency_key="final_barrier_finish",
                    turn_id="turn_1",
                    payload={"duration_seconds": 10},
                ),
            )
        )
        await asyncio.sleep(0)
        assert finish.done() is False

        # This result belongs to audio accepted before finish, but it resumes
        # after the stream key was fenced. It must not appear behind final.
        release_late_audio.set()
        await asyncio.wait_for(late_audio, timeout=1)
        assert finish.done() is False
        release_partial.set()
        await asyncio.wait_for(finish, timeout=1)
        await asyncio.sleep(0)

        assert projected == [
            ("transcript.partial", "partial 1"),
            ("transcript.final", "authoritative final"),
            ("stream.error", None),
        ]
        assert managed._partial_projection_pending == {}
        assert ("formal", "turn_1") not in (
            managed._partial_projection_active_keys
        )

        await channel.close("test_complete")
        await managed.stop("test_complete")
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_stop_joins_inflight_partial_and_drops_latest_pending_partial() -> None:
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
            command_poll_seconds=0.001,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        assert managed is not None
        chain = _FakeEvidenceChain()
        managed.chain = chain
        partial_started = asyncio.Event()
        release_partial = asyncio.Event()
        projected = []

        async def slow_projection(raw, _causation_id):
            if raw.get("type") == "transcript.partial":
                projected.append(raw.get("text"))
                partial_started.set()
                await release_partial.wait()

        channel._project_evidence_event = slow_projection
        await channel.send(
            ClientSignal(
                type="evidence.stream.open",
                idempotency_key="stop_barrier_open",
                turn_id="turn_1",
                payload={
                    "content_type": "audio/pcm",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                },
            )
        )
        ingress = _FakeIngress.instances[0]
        await ingress.push(sequence=1)
        await asyncio.wait_for(partial_started.wait(), timeout=1)
        await ingress.push(sequence=2)
        assert len(managed._partial_projection_pending) == 1

        stopping = asyncio.create_task(managed.stop("test_complete"))
        await asyncio.sleep(0)
        assert stopping.done() is False
        release_partial.set()
        await asyncio.wait_for(stopping, timeout=1)
        await asyncio.sleep(0)

        assert projected == ["服务端实时字幕 1"]
        assert managed._partial_projection_pending == {}
        assert managed._partial_projection_active_keys == set()
        assert managed._partial_projection_task is None
        assert supervisor._sessions == {}

        await channel.close("test_complete")
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_formal_pause_and_control_disconnect_keep_audio_until_explicit_finish() -> None:
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
        await channel.send(ClientSignal(type="speech.started", idempotency_key="endpoint_speech_started", turn_id="turn_1", payload={"capture_id": managed._capture_id}))
        await channel.send(
            ClientSignal(
                type="speech.stopped",
                idempotency_key="endpoint_speech_stopped",
                turn_id="turn_1",
                payload={"capture_id": managed._capture_id},
            )
        )
        await channel.close("control_lost_before_endpoint")
        await asyncio.sleep(0.02)
        assert chain.finish_count == 0
        assert chain.is_open
        await _FakeIngress.instances[0].push()
        assert chain.audio_count == 1
        channel = await runtime.open(_opened("connection_replacement"))
        # A stale explicit completion also cannot close the current capture.
        await channel.send(ClientSignal(type="finish_answer", idempotency_key="stale_finish", turn_id="turn_1", payload={"capture_id": "capture_old", "endpoint": "explicit"}))
        assert chain.finish_count == 0
        await channel.send(ClientSignal(type="finish_answer", idempotency_key="explicit_finish", turn_id="turn_1", payload={"capture_id": managed._capture_id, "endpoint": "explicit"}))
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
        await channel.send(ClientSignal(type="speech.started", idempotency_key="warmup_endpoint_speech_started", payload={"capture_id": managed._capture_id}))
        await channel.send(
            ClientSignal(
                type="speech.stopped",
                idempotency_key="warmup_endpoint_speech_stopped",
                payload={"capture_id": managed._capture_id},
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
                payload={"endpoint": "explicit"},
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
                    payload={"endpoint": "explicit"},
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
                payload={"capture_id": owner_session._capture_id},
            )
        )
        await controller.send(
            ClientSignal(
                type="speech.stopped",
                idempotency_key="remote_speech_stopped",
                turn_id="turn_1",
                payload={"capture_id": owner_session._capture_id},
            )
        )
        assert owner_session._endpoint_task is None
        await controller.send(
            ClientSignal(
                type="continue_speaking",
                idempotency_key="remote_continue",
                turn_id="turn_1",
                payload={"capture_id": owner_session._capture_id},
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


def test_owner_renews_across_multiple_lease_cycles_with_process_metrics(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        _FakeIngress.instances.clear()
        metrics = InterviewAgentMetrics()
        monkeypatch.setattr(
            "app.services.livekit_evidence_ingress.interview_agent_metrics",
            lambda: metrics,
        )
        store = InMemoryStore()
        _session(store)
        supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(
                _configuration(lease_seconds=0.3, renew_seconds=0.05)
            ),
            ingress_factory=_FakeIngress,
            instance_id="renewing_owner",
            command_poll_seconds=0.005,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor

        renewal_count = 0
        multiple_renewals = asyncio.Event()
        original_renew = supervisor.coordinator.renew

        def counted_renew(grant):
            nonlocal renewal_count
            renewed = original_renew(grant)
            renewal_count += 1
            if renewal_count >= 3:
                multiple_renewals.set()
            return renewed

        monkeypatch.setattr(supervisor.coordinator, "renew", counted_renew)
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        assert managed is not None

        await asyncio.wait_for(multiple_renewals.wait(), timeout=1)

        assert managed._stopped is False
        assert managed.ingress_connected is True
        assert renewal_count >= 3
        snapshot = metrics.snapshot()
        assert snapshot["evidence_owner_renew_scheduler_lag_ms"]["count"] >= 3
        assert snapshot["evidence_owner_renew_scheduler_lag_ms"]["max"] >= 0
        assert snapshot["evidence_owner_renew_db_latency_ms"]["count"] >= 3
        assert snapshot["evidence_owner_renew_db_latency_ms"]["max"] >= 0
        assert snapshot["evidence_owner_renew_success"] == {
            "count": renewal_count,
            "p50": 1.0,
            "p95": 1.0,
            "max": 1.0,
        }

        await channel.close("test_complete")
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_metrics_backend_failure_never_interrupts_owner_renewal(monkeypatch) -> None:
    class FailingMetrics:
        def observe(self, _name, _value) -> None:
            raise RuntimeError("metrics backend unavailable")

    async def scenario() -> None:
        _FakeIngress.instances.clear()
        monkeypatch.setattr(
            "app.services.livekit_evidence_ingress.interview_agent_metrics",
            lambda: FailingMetrics(),
        )
        store = InMemoryStore()
        _session(store)
        supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(
                _configuration(lease_seconds=0.3, renew_seconds=0.05)
            ),
            ingress_factory=_FakeIngress,
            instance_id="owner_with_unavailable_metrics",
            command_poll_seconds=0.005,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor

        renewed = asyncio.Event()
        original_renew = supervisor.coordinator.renew

        def observed_renew(grant):
            result = original_renew(grant)
            renewed.set()
            return result

        monkeypatch.setattr(supervisor.coordinator, "renew", observed_renew)
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        assert managed is not None

        await asyncio.wait_for(renewed.wait(), timeout=1)

        assert managed._stopped is False
        assert managed.ingress_connected is True
        current = runtime.interviews.get_interview(managed.interview_id)
        assert current["status"] == "in_progress"
        assert not current["agent_runtime"].get("problems")

        await channel.close("test_complete")
        await supervisor.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("replacement_claimed", [False, True])
def test_expired_owner_still_self_fences_with_or_without_successor(
    monkeypatch, replacement_claimed
) -> None:
    class RecordingMetrics:
        def __init__(self) -> None:
            self.values = {}

        def observe(self, name, value) -> None:
            self.values.setdefault(name, []).append(value)

    async def scenario() -> None:
        _FakeIngress.instances.clear()
        metrics = RecordingMetrics()
        monkeypatch.setattr(
            "app.services.livekit_evidence_ingress.interview_agent_metrics",
            lambda: metrics,
        )
        store = InMemoryStore()
        saved = _session(store)
        supervisor = LiveKitEvidenceIngressSupervisor(
            store,
            media_plane=LiveKitMediaPlane(
                _configuration(lease_seconds=0.3, renew_seconds=0.1)
            ),
            ingress_factory=_FakeIngress,
            instance_id="stale_owner",
            # Keep the command poller parked so this regression exercises the
            # renewal task's strict expiry fence and its failure metric.
            command_poll_seconds=0.5,
        )
        runtime = InterviewAgentRuntime(store)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        assert managed is not None
        old_grant = managed.ownership
        assert old_grant is not None
        # Let the command executor enter its long idle wait before expiring the
        # row, so the renewal task is the component that observes the fence.
        await asyncio.sleep(0.01)

        with persistence_for(store).transaction("org_default") as transaction:
            current = transaction.evidence_ownerships.get(old_grant.ownership_id)
            current["lease_expires_at"] = "2000-01-01T00:00:00Z"
            current["updated_at"] = utc_now()
            transaction.evidence_ownerships.update(
                current, expected_version=current["version"]
            )

        if replacement_claimed:
            successor = supervisor.coordinator.claim_owner(
                interview_id=saved["id"],
                organization_id="org_default",
                local_instance_id="successor_owner",
            )
            assert successor.ownership_epoch == old_grant.ownership_epoch + 1
            assert successor.lease_id != old_grant.lease_id

        async def wait_until_fenced() -> None:
            while True:
                current = runtime.interviews.get_interview(saved["id"])
                if (
                    managed._stopped
                    and metrics.values.get("evidence_owner_renew_success")
                ):
                    return
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_until_fenced(), timeout=1)

        assert _FakeIngress.instances[0].closed is True
        assert metrics.values["evidence_owner_renew_success"][-1] == 0.0
        assert metrics.values["evidence_owner_renew_db_latency_ms"][-1] >= 0
        assert metrics.values["evidence_owner_renew_scheduler_lag_ms"][-1] >= 0
        current = runtime.interviews.get_interview(saved["id"])
        # A stale owner must revoke local side effects, not pause a session
        # now controlled by its successor (or write after its lease expired).
        assert current["status"] == "in_progress"
        assert not any(p.get("code") == "EVIDENCE_OWNER_FENCED"
                       for p in current["agent_runtime"].get("problems", []))
        with persistence_for(store).transaction("org_default") as transaction:
            ownership = transaction.evidence_ownerships.get(
                old_grant.ownership_id
            )
        if replacement_claimed:
            assert ownership["owner_instance_id"] == "successor_owner"
            assert ownership["ownership_epoch"] == old_grant.ownership_epoch + 1
        else:
            assert ownership["lease_expires_at"] == "2000-01-01T00:00:00Z"

        await supervisor.shutdown()

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
