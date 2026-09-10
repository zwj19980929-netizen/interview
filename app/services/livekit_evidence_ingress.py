"""Long-lived LiveKit ingress bound to the InterviewAgent Evidence chain.

The supervisor intentionally outlives one control WebSocket. Within the
configured reconnect grace window, LiveKit audio, streaming STT and the private
recording keep their single authoritative identity while a replacement control
channel attaches and resumes event projection.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from datetime import datetime, timezone
import weakref
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from app.adapters.livekit_audio_ingress import (
    LiveKitAudioIngressBinding,
    LiveKitCandidateAudioIngress,
    LiveKitIngressAudioFrame,
)
from app.adapters.livekit_media import LiveKitMediaPlane
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.interview_agent_metrics import interview_agent_metrics
from app.core.time import utc_now
from app.domain.interview_agent import ClientSignal, FloorOwner, Replayability
from app.domain.evidence_coordination import (
    ClaimedEvidenceCommand,
    EvidenceCommandOutcome,
    EvidenceCommandSubmission,
    EvidenceOwnershipGrant,
)
from app.persistence.provider import persistence_for
from app.realtime_bus import realtime_event_bus
from app.services.evidence_coordination import EvidenceOwnershipCoordinator, assert_current_evidence_fence
from app.services.evidence_command_journal import EvidenceCommandJournal
from app.services.browser_audio_backfill import BrowserAudioBackfill
from app.services.interview_evidence import (
    EvidenceAudioResult,
    EvidenceFinishResult,
    InterviewEvidenceChain,
)
from app.services.interviews import InterviewService
from app.services.answer_endpoint import AnswerEndpoint
from app.services.spoken_supplement import SpokenSupplementConfirmation
from app.adapters.speech_activity import ServerSpeechActivity
from app.core.prompt.contracts import SUPPLEMENT_SPEECH
from app.services.capture_recovery import classify_capture_failure
from app.services.evidence_media import DurableEvidenceMedia
from app.adapters.audio_turn_detector import LocalAudioTurnDetector

if TYPE_CHECKING:
    from app.services.interview_agent import AgentChannel

_LOG = logging.getLogger(__name__)


_FAIL_CLOSED_MEDIA_ERRORS = frozenset(
    {
        "EVIDENCE_MEDIA_INCOMPLETE",
        "EVIDENCE_MEDIA_NOT_RECOVERABLE",
        "EVIDENCE_MEDIA_SEGMENT_GAP",
        "EVIDENCE_MEDIA_SEGMENT_INVALID",
        "EVIDENCE_MEDIA_CHECKSUM_MISMATCH",
        "EVIDENCE_MEDIA_FORMAT_NOT_RECOVERABLE",
        "EVIDENCE_MEDIA_RECOVERY_INVALID",
        "LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED",
        "BROWSER_BACKFILL_FRAME_INVALID",
        "BROWSER_BACKFILL_FRAME_UNAVAILABLE",
        "BROWSER_BACKFILL_CHECKSUM_MISMATCH",
    }
)
_TERMINAL_INTERVIEW_STATUSES = frozenset(
    {"completed", "report_generating", "report_ready"}
)
_PARTIAL_PROJECTION_CAPACITY = 16
_TERMINAL_PRESENTATION_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class _PendingProjection:
    kind: str
    raw: Dict[str, Any]
    turn_id: Optional[str]


class ManagedLiveKitEvidenceSession:
    def __init__(
        self,
        supervisor: "LiveKitEvidenceIngressSupervisor",
        interview_id: str,
        organization_id: str,
    ) -> None:
        self.supervisor = supervisor
        self.store = supervisor.store
        self.persistence = supervisor.persistence
        self.media_plane = supervisor.media_plane
        self.interview_id = interview_id
        self.organization_id = organization_id
        self.chain: Any = None
        self.ownership: Optional[EvidenceOwnershipGrant] = None
        self._control_grants: Dict[int, EvidenceOwnershipGrant] = {}
        self._channel: Optional["AgentChannel"] = None
        self._event_source: Optional["AgentChannel"] = None
        self._ingress: Optional[LiveKitCandidateAudioIngress] = None
        self._candidate_identity: Optional[str] = None
        self._start_lock = asyncio.Lock()
        self._delivery_lock = asyncio.Lock()
        self._pending: List[_PendingProjection] = []
        # Partial transcripts are a lossy UI projection, never authoritative
        # Evidence.  Keep them off the receive-only audio callback so a slow
        # session projection cannot consume LiveKit's bounded PCM budget.
        # There may be one in-flight value plus one latest pending value for
        # each active (kind, turn) key; finals and all other events continue to
        # use the lossless delivery path below.
        self._partial_projection_pending: Dict[
            tuple[str, Optional[str]], _PendingProjection
        ] = {}
        self._partial_projection_active_keys: set[
            tuple[str, Optional[str]]
        ] = set()
        self._partial_projection_wake = asyncio.Event()
        self._partial_projection_task: Optional[asyncio.Task[None]] = None
        self._partial_projection_shutdown = False
        self._partial_projection_failure: Optional[Exception] = None
        self._expiry_task: Optional[asyncio.Task[None]] = None
        self._endpoint_task: Optional[asyncio.Task[None]] = None
        self._capture_id: Optional[str] = None
        self._capture_speech_started = False
        self._endpoint_id: Optional[str] = None
        self._renewal_task: Optional[asyncio.Task[None]] = None
        self._executor_task: Optional[asyncio.Task[None]] = None
        self._owner_recovery_task: Optional[asyncio.Task[None]] = None
        self._command_wake = asyncio.Event()
        # Warm-up is disposable, but its endpoint command and the receive-only
        # track fail on different tasks. A local epoch lets the media failure
        # invalidate an already-running seal before it can publish a stale
        # calibration final or confirmation act.
        self._warmup_epoch = 0
        self._active_warmup_epoch: Optional[int] = None
        self._failed_warmup_epoch: Optional[int] = None
        self._recovered_warmup_epoch: Optional[int] = None
        self._warmup_failure: Optional[Dict[str, Any]] = None
        self._warmup_ingress_restart_required = False
        self._warmup_recovery_lock = asyncio.Lock()
        self._stopped = False
        self.last_ingress_state = "not_started"
        self._answer_endpoint: Optional[AnswerEndpoint] = None
        self._prepared_finish: Any = None
        self._presentation_task: Optional[asyncio.Task] = None

    @property
    def turn_id(self) -> Optional[str]:
        return self.chain.turn_id if self.chain is not None else None

    @property
    def evidence_open(self) -> bool:
        return bool(self.chain is not None and self.chain.is_open)

    @property
    def ingress_connected(self) -> bool:
        return bool(self._ingress and self._ingress.connected)

    async def attach(self, channel: "AgentChannel") -> None:
        if self._stopped:
            raise ApiError(
                "LIVEKIT_EVIDENCE_SESSION_CLOSED",
                "Authoritative evidence session is already closed.",
                status_code=409,
            )
        grant = self.supervisor.coordinator.attach_control(
            interview_id=self.interview_id,
            organization_id=self.organization_id,
            connection_id=channel.opened.connection_id,
            local_instance_id=self.supervisor.instance_id,
        )
        self._control_grants[id(channel)] = grant
        if grant.is_local_owner and self.ownership is None:
            self._install_owner(grant)
        elif grant.is_local_owner and (
            self.ownership.lease_id != grant.lease_id
            or self.ownership.ownership_epoch != grant.ownership_epoch
        ):
            raise ApiError(
                "EVIDENCE_OWNER_FENCED",
                "The local Evidence session no longer matches the database ownership epoch.",
                status_code=409,
            )
        elif grant.is_local_owner:
            self.ownership = grant
        expiry = self._expiry_task
        self._expiry_task = None
        if expiry is not None:
            expiry.cancel()
            await asyncio.gather(expiry, return_exceptions=True)
        async with self._delivery_lock:
            self._channel = channel
            self._event_source = channel
            pending = list(self._pending)
            self._pending.clear()
            for item in pending:
                await self._project(channel, item)
        if not grant.is_local_owner:
            # A remote controller is a durable proxy only. It never creates a
            # second subscriber, STT stream or recording; commands are claimed
            # by the database-fenced owner executor.
            if self._owner_recovery_task is None:
                self._owner_recovery_task = asyncio.create_task(
                    self._recover_owner_after_loss()
                )
            return
        # The candidate joins LiveKit before opening the control channel. A
        # subscriber may therefore be established immediately from the
        # consumed server ticket; client metadata is never trusted for this
        # binding. On reconnect, the frozen original media identity is reused.
        try:
            await self.ensure_ingress(channel.opened.connection_id)
        except BaseException:
            async with self._delivery_lock:
                if self._channel is channel:
                    self._channel = None
                if self._event_source is channel:
                    self._event_source = None
            self._stopped = True
            await self._shutdown_partial_projection()
            renewal = self._renewal_task
            self._renewal_task = None
            if renewal is not None:
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)
            executor = self._executor_task
            self._executor_task = None
            if executor is not None:
                executor.cancel()
                await asyncio.gather(executor, return_exceptions=True)
            if self.chain is not None:
                await self.chain.abort()
            if self.ownership is not None:
                self.supervisor.coordinator.release(self.ownership)
            self.supervisor._remove(self)
            raise

    async def detach(self, channel: "AgentChannel") -> None:
        grant = self._control_grants.pop(id(channel), None)
        async with self._delivery_lock:
            if self._channel is channel:
                self._channel = None
        detached = bool(
            grant
            and self.supervisor.coordinator.detach_control(
                grant,
                grace_seconds=self.media_plane.configuration.authoritative_ingress_grace_seconds,
            )
        )
        if not detached or self._stopped or self._expiry_task is not None:
            return
        self._expiry_task = asyncio.create_task(self._expire_after_grace())

    async def ensure_ingress(self, connection_id: str) -> None:
        if self._stopped:
            raise ApiError(
                "LIVEKIT_EVIDENCE_SESSION_CLOSED",
                "Authoritative evidence session is already closed.",
                status_code=409,
            )
        if self.ownership is None:
            # Remote controllers never subscribe to media. The current
            # database owner already holds the one authoritative subscriber.
            return
        async with self._start_lock:
            self._assert_owner()
            if self._ingress is not None and self._ingress.connected:
                return
            stale_ingress = self._ingress
            self._ingress = None
            if stale_ingress is not None:
                await stale_ingress.close()
            ticket = self._candidate_ticket(connection_id)
            frozen = self._frozen_binding()
            media = ticket.get("media") or {}
            identity = str(
                (frozen or {}).get("candidate_identity")
                or ticket.get("participant_identity")
                or ""
            )
            room_name = str(
                (frozen or {}).get("room_name") or media.get("room_name") or ""
            )
            if not identity or not room_name:
                raise ApiError(
                    "LIVEKIT_EVIDENCE_BINDING_INVALID",
                    "Consumed candidate ticket has no authoritative media binding.",
                    status_code=409,
                )
            if self._candidate_identity and self._candidate_identity != identity:
                raise ApiError(
                    "LIVEKIT_EVIDENCE_IDENTITY_CONFLICT",
                    "Authoritative ingress is already bound to another candidate media identity.",
                    status_code=409,
                )
            if frozen is None:
                self._freeze_binding(
                    candidate_identity=identity,
                    room_name=room_name,
                    connection_id=connection_id,
                )
            binding = LiveKitAudioIngressBinding(
                room_name=room_name,
                candidate_identity=identity,
                subscriber_identity=(
                    "evidence:%s:%s"
                    % (self.interview_id[:80], new_id("subscriber")[-24:])
                )[:255],
            )
            ingress = self.supervisor.ingress_factory(
                self.media_plane,
                binding,
                on_audio_frame=self._receive_audio,
                on_state=self._receive_state,
            )
            try:
                await ingress.connect()
            except Exception as exc:
                await ingress.close()
                await self._fatal_ingress_problem(
                    "LIVEKIT_EVIDENCE_INGRESS_CONNECT_FAILED", exc
                )
                raise ApiError(
                    "LIVEKIT_EVIDENCE_INGRESS_CONNECT_FAILED",
                    "Server could not subscribe to the candidate microphone; the interview was paused.",
                    status_code=503,
                ) from exc
            if self._stopped:
                await ingress.close()
                raise ApiError(
                    "LIVEKIT_EVIDENCE_SESSION_CLOSED",
                    "Authoritative Evidence stopped while media was connecting.",
                    status_code=409,
                )
            self._candidate_identity = identity
            self._ingress = ingress

    async def dispatch(
        self, channel: "AgentChannel", signal: "ClientSignal"
    ) -> EvidenceCommandOutcome:
        """Route every connection-independent control through one Interface."""

        command_type = {
            "evidence.stream.open": "evidence.open",
            "evidence.finish": "evidence.seal",
            "finish_answer": "evidence.seal",
            "speech.started": "speech.started",
            "speech.stopped": "speech.stopped",
            "continue_speaking": "evidence.continue",
            "meta.not_finished": "evidence.continue",
            "warmup.retry": "evidence.reset",
        }.get(signal.type)
        if command_type is None:
            raise ApiError(
                "EVIDENCE_COMMAND_UNSUPPORTED",
                "The signal is not part of the authoritative Evidence Interface.",
                status_code=422,
            )
        return await self._dispatch_command(channel, signal, command_type)

    async def begin_browser_backfill(
        self, channel: "AgentChannel", signal: "ClientSignal"
    ) -> None:
        """Authorize one explicit recovery window; normal PCM stays on LiveKit."""

        self._require_attached(channel)
        if not self._browser_backfill_turn_available(signal.turn_id):
            raise ApiError(
                "BROWSER_BACKFILL_EVIDENCE_NOT_OPEN",
                "Browser recovery requires the currently open formal Evidence turn.",
                status_code=409,
            )
        batch = self.supervisor.browser_backfill.begin(
            interview_id=self.interview_id,
            organization_id=self.organization_id,
            actor_id=channel.principal.actor_id,
            current_connection_id=channel.opened.connection_id,
            turn_id=signal.turn_id,
            payload=signal.payload,
        )
        await channel._emit(
            "speech.started",
            {
                "speaker": "candidate",
                "server_audio_received": True,
                "media_transport": "browser_backfill",
                "browser_backfill": {
                    "status": "ready",
                    "batch_id": batch.batch_id,
                    "audio_epoch": batch.audio_epoch,
                    "ack_through": batch.ack_through,
                },
            },
            turn_id=signal.turn_id,
            causation_id=signal.causation_id,
            replayability=Replayability.TRANSIENT,
        )

    async def dispatch_browser_backfill(
        self, channel: "AgentChannel", signal: "ClientSignal"
    ) -> None:
        """Stage private PCM, then route its metadata through the owner journal."""

        grant = self._require_attached(channel)
        if not self._browser_backfill_turn_available(signal.turn_id):
            raise ApiError(
                "BROWSER_BACKFILL_EVIDENCE_NOT_OPEN",
                "Browser recovery requires the currently open formal Evidence turn.",
                status_code=409,
            )
        frame = self.supervisor.browser_backfill.stage(
            interview_id=self.interview_id,
            organization_id=self.organization_id,
            actor_id=channel.principal.actor_id,
            current_connection_id=channel.opened.connection_id,
            turn_id=signal.turn_id,
            payload=signal.payload,
        )
        durable = ClientSignal(
            type="evidence.backfill",
            idempotency_key=(
                "backfill.%s.%d"
                % (frame.batch_id.removeprefix("browser_backfill_"), frame.client_sequence)
            )[:128],
            turn_id=signal.turn_id,
            causation_id=signal.causation_id,
            payload=frame.command_payload(),
        )
        await self._dispatch_command(channel, durable, "evidence.backfill")
        await channel._emit(
            "speech.started",
            {
                "speaker": "candidate",
                "server_audio_received": True,
                "server_received_at": utc_now(),
                "media_transport": "browser_backfill",
                "browser_backfill": {
                    "status": "acknowledged",
                    "batch_id": frame.batch_id,
                    "audio_epoch": frame.audio_epoch,
                    "ack_through": frame.client_sequence,
                    "duplicate": frame.duplicate,
                },
            },
            turn_id=signal.turn_id,
            causation_id=signal.causation_id,
            replayability=Replayability.TRANSIENT,
        )

    async def complete_browser_backfill(
        self, channel: "AgentChannel", signal: "ClientSignal"
    ) -> None:
        self._require_attached(channel)
        ack_through = self.supervisor.browser_backfill.complete(
            interview_id=self.interview_id,
            organization_id=self.organization_id,
            actor_id=channel.principal.actor_id,
            current_connection_id=channel.opened.connection_id,
            turn_id=signal.turn_id,
            payload=signal.payload,
        )
        await channel._emit(
            "speech.started",
            {
                "speaker": "candidate",
                "server_audio_received": True,
                "media_transport": "browser_backfill",
                "browser_backfill": {
                    "status": "complete",
                    "batch_id": signal.payload.get("batch_id"),
                    "audio_epoch": signal.payload.get("audio_epoch"),
                    "ack_through": ack_through,
                },
            },
            turn_id=signal.turn_id,
            causation_id=signal.causation_id,
            replayability=Replayability.TRANSIENT,
        )

    def _browser_backfill_turn_available(self, turn_id: Optional[str]) -> bool:
        """Check local execution state or the remote controller's domain fact."""

        if not turn_id:
            return False
        if self.ownership is not None:
            return bool(self.evidence_open and self.turn_id == turn_id)
        session = self.supervisor.interviews.get_interview(
            self.interview_id, self.organization_id
        )
        return bool(
            session.get("status") == "in_progress"
            and str(session.get("current_turn_id") or "") == turn_id
            and not self._turn_has_answer(turn_id)
        )

    def _start_answer_endpoint(self) -> None:
        capture_id, turn_id = self._capture_id, self.chain.turn_id
        from app.core.speech_diagnostics import record_turn_control

        def trace(event: str, **details) -> None:
            record_turn_control(event, interview_id=self.interview_id, turn_id=turn_id,
                                capture_id=capture_id, **details)
        with self.persistence.transaction(self.organization_id) as transaction:
            if self.ownership is not None:
                assert_current_evidence_fence(transaction, self.ownership.commit_fence())
            session = transaction.interview_sessions.get(self.interview_id)
            if session and any((session.get("agent_runtime") or {}).get(key)
                               for key in ("supplement_confirmation", "answer_preparation")):
                session["agent_runtime"].pop("supplement_confirmation", None)
                session["agent_runtime"].pop("answer_preparation", None)
                transaction.interview_sessions.update(session, expected_version=session["version"])

        async def notify(reason: str) -> None:
            if self._stopped or self._capture_id != capture_id or not self.chain.is_open:
                return
            if reason == "answer_preparing":
                self._persist_answer_preparation(capture_id, turn_id, preparing=True)
            elif reason in {"answer_listening", "supplement_awaiting_reply", "understanding_unavailable",
                            "understanding_retry_exhausted", "transcript_unavailable", "capture_failed"}:
                self._persist_answer_preparation(capture_id, turn_id, preparing=False)
            source = self._channel or self._event_source
            if reason in {"capture_recovering", "capture_recovered", "capture_retry_required"}:
                await self._capture_recovery_notice(reason, capture_id, turn_id, source)
                return
            if reason == "capture_failed":
                failure = self._answer_endpoint.failure if self._answer_endpoint else None
                try:
                    await self._fatal_ingress_problem(
                        "CONTINUOUS_CAPTURE_FAILED",
                        ApiError("CONTINUOUS_CAPTURE_FAILED", "Authoritative capture failed.", status_code=503),
                        cause_code=failure.cause_code if failure else "CAPTURE_INTERNAL_ERROR",
                        cause_type=failure.cause_type if failure else "InternalError",
                        stage=failure.stage if failure else "unknown",
                    )
                finally:
                    await self.chain.abort()
                if source is not None:
                    await source._emit_snapshot(None)
                return
            if reason in {"understanding_unavailable", "understanding_retry_exhausted", "transcript_unavailable", "detector_unavailable", "endpoint_uncertain"}:
                if source is None:
                    return
                message = {
                    "understanding_unavailable": "已收到语音，暂时无法完成回答理解；正在重试，你也可以继续补充。",
                    "understanding_retry_exhausted": "语音已保留，但回答理解暂时不可用。恢复后请说话或点击提前结束回答重试。",
                    "transcript_unavailable": "已收到音频，仍在等待本段最终转写；已有字幕和回答会保留。",
                    "detector_unavailable": "自动接话暂时不可用，仍在收音；可以继续说或点击提前结束回答。",
                    "endpoint_uncertain": "我还在听。你可以继续补充；如果已说完，也可以点击提前结束回答。",
                }[reason]
                await source._emit("problem", {
                    "code": reason.upper(), "message": message,
                    "recoverable": True, "action": "continue_listening", "capture_id": capture_id,
                }, turn_id=turn_id, causation_id=None, replayability=Replayability.TRANSIENT)
                return
            if reason in {"supplement_awaiting_reply", "answer_listening"}:
                with self.persistence.transaction(self.organization_id) as transaction:
                    if self.ownership is not None:
                        assert_current_evidence_fence(transaction, self.ownership.commit_fence())
                    session = transaction.interview_sessions.get(self.interview_id)
                    if not session or session.get("status") != "in_progress" or session.get("current_turn_id") != turn_id:
                        return
                    session.setdefault("agent_runtime", {})["supplement_confirmation"] = {
                        "status": "awaiting_reply" if reason == "supplement_awaiting_reply" else "listening",
                        "turn_id": turn_id, "capture_id": capture_id,
                    }
                    transaction.interview_sessions.update(session, expected_version=session["version"])
            if source is None:
                return
            # Preparation is reversible; it must never close the capture gate.
            source.runtime._set_floor(self.interview_id, FloorOwner.CANDIDATE, reason, self.organization_id)
            await source._emit("floor.changed", {
                "owner": "candidate", "reason": reason, "capture_id": capture_id,
            }, turn_id=turn_id, causation_id=None, replayability=Replayability.TRANSIENT)

        async def commit(decision: Any, guard: Any) -> None:
            guard()
            if self._capture_id != capture_id:
                raise ApiError("TURN_DECISION_STALE", "Capture changed.", status_code=409)
            source = self._channel or self._event_source
            if source is None:
                return
            proposal_id = new_id("answer_proposal")
            prepared = (proposal_id, decision, guard)
            self._prepared_finish = prepared
            try:
                await self.dispatch(source, ClientSignal(
                    type="evidence.finish", idempotency_key=proposal_id, turn_id=turn_id,
                    payload={"endpoint": "prepared_turn", "capture_id": capture_id,
                             "proposal_id": proposal_id},
                ))
            finally:
                if self._prepared_finish is prepared:
                    self._prepared_finish = None

        async def on_failure(exc: BaseException) -> None:
            # This path is for failed durable recovery transitions, not a slow
            # browser. Revoke input even if persistence itself is unavailable.
            if self._capture_id != capture_id:
                return
            if hasattr(self.chain, "revoke_audio_input"):
                self.chain.revoke_audio_input()
            failure = classify_capture_failure(exc, "commit")
            await self._fatal_ingress_problem(
                "CONTINUOUS_CAPTURE_FAILED",
                ApiError("CONTINUOUS_CAPTURE_FAILED", "Capture recovery state unavailable.", status_code=503),
                cause_code=failure.cause_code, cause_type=failure.cause_type, stage=failure.stage,
            )
            source = self._channel or self._event_source
            if source is not None:
                await source._emit_snapshot(None)

        async def speak(kind: str, guard: Any, *, focus_quote: str = "") -> bool:
            guard()
            if self._capture_id != capture_id or self._stopped:
                raise ApiError("TURN_DECISION_STALE", "Capture changed.", status_code=409)
            source = self._channel or self._event_source
            if source is None:
                return False
            if kind == "playback_timeout":
                self._assert_owner()
                session = self.supervisor.interviews.get_interview(self.interview_id, self.organization_id)
                state = session.get("agent_runtime") or {}
                if session.get("status") != "in_progress" or session.get("current_turn_id") != turn_id:
                    return False
                performance_id = state.get("active_performance_id")
                if performance_id != getattr(self, "_supplement_performance_id", None):
                    return False
                await source._cancel_speech_output()
                source.runtime._clear_active_performance(self.interview_id, performance_id, self.organization_id)
                await source._emit("avatar.performance.interrupted", {
                    "performance_id": performance_id, "reason": "confirmation_playback_timeout", "deadline_ms": 200,
                }, turn_id=turn_id, causation_id=None, replayability=Replayability.TRANSIENT)
                await source._set_floor(FloorOwner.CANDIDATE, "confirmation_playback_timeout", None)
                return True
            if kind == "pause":
                source.runtime.interviews.pause_interview(
                    self.interview_id, reason="candidate_requested_pause", organization_id=self.organization_id,
                )
                await self.pause_capture()
                await source._set_floor(FloorOwner.NONE, "candidate_pause", None)
                await source._emit_snapshot(None)
                return True
            from app.core.prompt.contracts import clarification_speech, CLARIFICATION_SPEECH_VERSION
            performance = await source._select_act(
                act_type="clarification" if kind == "answer_clarify" else "supplement_" + kind,
                text=clarification_speech(focus_quote) if kind == "answer_clarify" else SUPPLEMENT_SPEECH[kind],
                turn_id=turn_id, causation_id=None, evidence_refs=[], gesture="listen",
                approval_guard=guard,
                **({"prompt_version": CLARIFICATION_SPEECH_VERSION} if kind == "answer_clarify" else {}),
            )
            self._supplement_performance_id = performance.performance_id if performance else None
            return performance is not None

        self._answer_endpoint = AnswerEndpoint(
            detector=self.supervisor.turn_detector, capture=self.chain, commit=commit, notify=notify,
            on_failure=on_failure,
            trace=trace,
            confirmation=SpokenSupplementConfirmation(speak=speak),
            speech_activity=ServerSpeechActivity(),
            threshold=float(os.getenv("INTERVIEWER_TURN_END_THRESHOLD", "0.6")),
            rms_threshold=float(os.getenv("INTERVIEWER_TURN_VOICE_RMS", "0.006")),
            min_silence_seconds=float(os.getenv("INTERVIEWER_TURN_MIN_SILENCE_SECONDS", "0.7")),
        )
        self._answer_endpoint.start()

    async def confirmation_floor_returned(self, reason: str) -> None:
        endpoint = self._answer_endpoint
        confirmation = endpoint.confirmation if endpoint else None
        if confirmation is None or not confirmation.speaking:
            return
        if reason == "candidate_continues":
            confirmation._after_speech = "listening"
        confirmation.floor_returned(endpoint)
        preroll = getattr(self, "_supplement_preroll", [])
        self._supplement_preroll = []
        if reason == "barge_in":
            for pcm in preroll:
                endpoint.observe_audio(pcm)
                await self.chain.send_audio(pcm)
            endpoint.speech_started()
        await endpoint._notify("supplement_awaiting_reply" if confirmation.phase == "awaiting_reply" else "answer_listening")

    def _persist_answer_preparation(self, capture_id: Optional[str], turn_id: Optional[str], *, preparing: bool) -> None:
        """Persist the active wait before lossy UI delivery; never change capture readiness."""
        if not capture_id:
            return
        try:
            with self.persistence.transaction(self.organization_id) as transaction:
                if self.ownership is not None:
                    assert_current_evidence_fence(transaction, self.ownership.commit_fence())
                session = transaction.interview_sessions.get(self.interview_id)
                if session is None:
                    return
                state = session.setdefault("agent_runtime", {})
                previous = state.get("answer_preparation") or {}
                if preparing:
                    if (self._stopped or self._capture_id != capture_id or not self.chain.is_open
                            or not turn_id or session.get("status") != "in_progress"
                            or session.get("current_turn_id") != turn_id
                            or (state.get("takeover") or {}).get("status") == "active"):
                        return
                    value = {"status": "preparing", "turn_id": turn_id, "capture_id": capture_id}
                    if previous == value:
                        return
                    state["answer_preparation"] = value
                else:
                    if (previous.get("capture_id") != capture_id
                            or (turn_id is not None and previous.get("turn_id") != turn_id)):
                        return
                    state.pop("answer_preparation", None)
                transaction.interview_sessions.update(session, expected_version=session["version"])
        except ApiError as exc:
            # A stale process must not erase its successor's state. Local
            # shutdown can still proceed after ownership was already lost.
            if preparing or exc.code not in {"EVIDENCE_OWNER_FENCED", "EVIDENCE_OWNERSHIP_LOST"}:
                raise

    def _clear_answer_preparation_for_cleanup(self, capture_id: Optional[str], turn_id: Optional[str]) -> None:
        # Display-state persistence must never prevent media revocation,
        # resource shutdown, or completion of an already committed answer.
        try:
            self._persist_answer_preparation(capture_id, turn_id, preparing=False)
        except Exception as exc:
            _LOG.warning("Answer preparation cleanup unavailable: error_type=%s", type(exc).__name__)

    async def _capture_recovery_notice(self, reason: str, capture_id: str, turn_id: str, source: Any) -> None:
        endpoint = self._answer_endpoint
        if self._capture_id != capture_id or self._stopped or endpoint is None:
            return
        failure = endpoint.failure
        status = "retry_required" if reason == "capture_retry_required" else "recovering"
        checkpoint = self.chain.checkpoint_incomplete() if status == "retry_required" else None
        with self.persistence.transaction(self.organization_id) as transaction:
            if self.ownership is not None:
                assert_current_evidence_fence(transaction, self.ownership.commit_fence())
            session = transaction.interview_sessions.get(self.interview_id)
            if (not session or session.get("status") != "in_progress"
                    or session.get("current_turn_id") != turn_id
                    or any(a.get("turn_id") == turn_id for a in session.get("answers", []))):
                return
            state = session.setdefault("agent_runtime", {})
            if (state.get("takeover") or {}).get("status") == "active":
                return
            if (state.get("answer_preparation") or {}).get("capture_id") == capture_id:
                state.pop("answer_preparation", None)
            if reason == "capture_recovered":
                existing = state.get("capture_recovery") or {}
                if existing and existing.get("capture_id") != capture_id:
                    return
                state.pop("capture_recovery", None)
            else:
                state["capture_recovery"] = {
                    "status": status, "turn_id": turn_id, "capture_id": capture_id,
                    "attempt": endpoint.recovery_attempt, "max_attempts": endpoint.max_recovery_attempts,
                    "cause_code": failure.cause_code if failure else "CAPTURE_INTERNAL_ERROR",
                    "stage": failure.stage if failure else "unknown",
                    "capture_revision": (checkpoint or {}).get("capture_revision"),
                }
            owner = "none" if status == "retry_required" else "candidate"
            resumed_reason = ("supplement_awaiting_reply"
                if endpoint.confirmation and endpoint.confirmation.phase in {"awaiting_reply", "classifying"}
                else "answer_listening")
            floor_reason = (resumed_reason if reason == "capture_recovered" else
                            "answer_retry_required" if status == "retry_required" else "answer_recovering")
            state.update(floor=owner, floor_reason=floor_reason)
            if reason != "capture_recovered":
                problems = state.setdefault("problems", [])
                problems.append({
                    "code": "CAPTURE_RETRY_REQUIRED" if status == "retry_required" else "CAPTURE_RECOVERING",
                    "message": "Recognition capture recovery.", "recoverable": True,
                    "action": "retry_answer" if status == "retry_required" else "continue_listening",
                    "cause_code": failure.cause_code if failure else "CAPTURE_INTERNAL_ERROR",
                    "cause_type": failure.cause_type if failure else "InternalError",
                    "stage": failure.stage if failure else "unknown",
                    "attempt": endpoint.recovery_attempt, "capture_id": capture_id,
                    "occurred_at": utc_now(),
                })
                del problems[:-20]
            transaction.interview_sessions.update(session, expected_version=session["version"])
        if status == "retry_required":
            # Flush durable PCM above, then close without batch repair or answer
            # submission. The LiveKit subscription remains, the Evidence gate does not.
            await self.chain.abort()
        async def project() -> None:
            if status == "retry_required":
                await self._deactivate_partial_projection("formal", turn_id)
            if source is None or self._capture_id != capture_id or self._stopped:
                return
            latest = self.supervisor.interviews.get_interview(self.interview_id, self.organization_id)
            if latest.get("status") != "in_progress" or latest.get("current_turn_id") != turn_id:
                return
            await source._emit("floor.changed", {
                "owner": owner, "reason": floor_reason, "capture_id": capture_id,
            }, turn_id=turn_id, causation_id=None, replayability=Replayability.TRANSIENT)
            if reason != "capture_recovered":
                await source._emit("problem", {
                    "code": "CAPTURE_RETRY_REQUIRED" if status == "retry_required" else "CAPTURE_RECOVERING",
                    "message": "本题需要重试。" if status == "retry_required" else "正在恢复语音识别。",
                    "recoverable": True, "capture_id": capture_id,
                    "action": "retry_answer" if status == "retry_required" else "continue_listening",
                }, turn_id=turn_id, causation_id=None, replayability=Replayability.TRANSIENT)
            await source._emit_snapshot(None)
        # Durable state and the capture gate have already changed. A slow or
        # disconnected control channel must not turn a healthy recovery fatal.
        try:
            await asyncio.wait_for(project(), timeout=1)
        except Exception as exc:
            _LOG.warning("Capture recovery projection unavailable: error_type=%s", type(exc).__name__)

    async def pause_capture(self) -> None:
        """A candidate pause revokes capture, never batch-submits its prefix."""
        self._clear_answer_preparation_for_cleanup(self._capture_id, self.chain.turn_id if self.chain else None)
        self._capture_id = None
        if self.chain is not None and hasattr(self.chain, "revoke_audio_input"):
            self.chain.revoke_audio_input()
        if self._answer_endpoint is not None:
            await self._answer_endpoint.close()
            self._answer_endpoint = None
        if self.chain is not None:
            await asyncio.wait_for(self.chain.abort(), timeout=3)

    async def _finish_with_channel(
        self, channel: "AgentChannel", signal: ClientSignal, *, prepared: Any = None
    ) -> None:
        partial_kind = str(self.chain.kind or "")
        partial_turn_id = self.chain.turn_id
        completed_capture_id = self._capture_id
        if prepared is not None:
            prepared[2]()
        else:
            await channel._set_floor(FloorOwner.NONE, "answer_processing", signal.causation_id)
        try:
            result = (await self.chain.finish(signal.payload, prepared_decision=prepared[1], commit_guard=prepared[2])
                      if prepared is not None else await self.chain.finish(signal.payload))
        finally:
            if partial_kind and not self.chain.is_open:
                self._clear_answer_preparation_for_cleanup(completed_capture_id, partial_turn_id)
                try:
                    await asyncio.wait_for(self._deactivate_partial_projection(
                        partial_kind, partial_turn_id
                    ), timeout=1)
                except Exception as exc:
                    _LOG.warning("Post-capture projection drain unavailable: error_type=%s", type(exc).__name__)
        self._capture_id = None
        self._capture_speech_started = False
        if prepared is not None:
            channel.runtime._set_floor(
                self.interview_id, FloorOwner.NONE, "answer_processing", self.organization_id,
            )
        if result.kind == "warmup":
            await channel._complete_warmup_evidence(result, signal)
            return
        if prepared is not None:
            # Domain commit is complete; expression network work must not hold
            # up the durable control executor or the next barge-in signal.
            async def present_committed() -> None:
                try:
                    await asyncio.wait_for(channel._emit("floor.changed", {
                        "owner": "none", "reason": "answer_processing", "capture_id": completed_capture_id,
                    }, turn_id=partial_turn_id, causation_id=signal.causation_id,
                        replayability=Replayability.TRANSIENT), timeout=1)
                except Exception as exc:
                    _LOG.warning("Committed answer floor projection unavailable: error_type=%s", type(exc).__name__)
                await self._present_formal_result(channel, signal, result)
            self._presentation_task = asyncio.create_task(present_committed())
            self._presentation_task.add_done_callback(self._observe_presentation_result)
            return
        await self._present_formal_result(channel, signal, result)

    async def _present_formal_result(self, channel: "AgentChannel", signal: ClientSignal, result: Any) -> None:
        for raw in result.events:
            raw = dict(raw)
            raw.setdefault("turn_id", result.turn_id)
            if raw.get("type") in {"followup.selected", "utterance.not_accepted"}:
                # These handlers select an approved act and may synthesize
                # speech. They are business work with their own lifecycle,
                # not UI fanout subject to the short projection deadline.
                await channel._project_evidence_event(raw, signal.causation_id)
                continue
            try:
                await asyncio.wait_for(channel._project_evidence_event(raw, signal.causation_id), timeout=1)
            except Exception as exc:
                _LOG.warning("Committed Evidence event unavailable: error_type=%s", type(exc).__name__)
        await channel._after_formal_evidence_finished(
            result.turn_id,
            signal,
            conversation_action_selected=any(
                item.get("type")
                in {"followup.selected", "utterance.not_accepted"}
                for item in result.events
            ),
            stop_evidence_session=False,
        )

    @staticmethod
    def _observe_presentation_result(task: asyncio.Task) -> None:
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                _LOG.warning("Committed answer presentation failed: error_type=%s", type(error).__name__)

    async def _schedule_endpoint(self, signal: ClientSignal) -> None:
        await self.cancel_endpoint()
        self._endpoint_id = new_id("endpoint_scope")
        signal = signal.model_copy(update={"payload": {**signal.payload, "endpoint_id": self._endpoint_id}})
        self._endpoint_task = asyncio.create_task(
            self._finish_after_endpoint(signal)
        )

    async def cancel_endpoint(self) -> None:
        self._endpoint_id = None
        task = self._endpoint_task
        self._endpoint_task = None
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def assert_controller(self, channel: "AgentChannel") -> None:
        self._require_attached(channel)

    async def _dispatch_command(
        self,
        channel: "AgentChannel",
        signal: ClientSignal,
        command_type: str,
    ) -> EvidenceCommandOutcome:
        """Persist, then await one fenced owner result through the DB seam."""

        grant = self._require_attached(channel)
        payload = dict(signal.payload)
        if command_type not in {"evidence.open", "evidence.seal", "evidence.backfill"}:
            payload = (
                {"capture_id": payload["capture_id"]}
                if command_type in {"speech.started", "speech.stopped", "evidence.continue"} and "capture_id" in payload
                else {}
            )
        deadline_seconds = 120.0 if command_type == "evidence.seal" else 30.0
        receipt = self.supervisor.journal.submit(
            grant,
            EvidenceCommandSubmission(
                command_type=command_type,
                idempotency_key=signal.idempotency_key,
                turn_id=signal.turn_id,
                causation_id=signal.causation_id,
                payload=payload,
                deadline_seconds=deadline_seconds,
            ),
        )
        self._command_wake.set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + deadline_seconds
        while receipt.status in {"pending", "running"}:
            if loop.time() >= deadline:
                raise ApiError(
                    "EVIDENCE_COMMAND_RESULT_TIMEOUT",
                    "The Evidence command is durable but its owner result is not available yet.",
                    status_code=503,
                    details={"command_id": receipt.command_id},
                )
            await asyncio.sleep(self.supervisor.command_poll_seconds)
            receipt = self.supervisor.journal.get_receipt(
                grant, receipt.command_id
            )
        outcome = receipt.outcome
        if outcome is None:
            raise ApiError(
                "EVIDENCE_COMMAND_RESULT_INVALID",
                "The Evidence command reached a terminal state without a safe result.",
                status_code=503,
            )
        if outcome.disposition != "applied":
            error_code = outcome.error_code or "EVIDENCE_COMMAND_REJECTED"
            fail_closed = error_code in _FAIL_CLOSED_MEDIA_ERRORS
            if fail_closed:
                # The owner already persisted the fatal problem, paused the
                # interview and projected one non-recoverable event. Returning
                # its terminal receipt avoids a contradictory second generic
                # problem from AgentChannel's exception boundary.
                return outcome
            if command_type == "evidence.seal" and signal.turn_id is None:
                session = self.supervisor.interviews.get_interview(
                    self.interview_id, self.organization_id
                )
                calibration = (session.get("agent_runtime") or {}).get(
                    "calibration_status"
                )
                if (
                    calibration == "retrying"
                    and error_code != "EVIDENCE_TURN_REQUIRED"
                ):
                    # A failed warm-up final is consumed exactly once.  The
                    # owner already projected its explicit retry instruction;
                    # do not replace it with AgentChannel's generic problem.
                    return outcome
            raise ApiError(
                error_code,
                "The authoritative Evidence owner rejected the command.",
                status_code=503 if outcome.retryable else 409,
            )
        return outcome

    async def _project_open_ready(
        self,
        channel: "AgentChannel",
        *,
        kind: str,
        causation_id: Optional[str],
    ) -> None:
        """Project owner-ordered readiness before the open receipt completes."""

        reason = (
            "warmup_stream_open" if kind == "warmup" else "evidence_stream_open"
        )
        # This acknowledgement is local to the Evidence open contract.  Do not
        # weaken AgentChannel._set_floor's global same-owner deduplication:
        # update the domain floor if necessary, then always project one ready
        # event to currently attached controllers for this newly executed
        # command.
        channel.runtime._set_floor(
            self.interview_id,
            FloorOwner.CANDIDATE,
            reason,
            self.organization_id,
        )
        await channel._emit(
            "floor.changed",
            {"owner": FloorOwner.CANDIDATE.value, "reason": reason, "capture_id": self._capture_id},
            turn_id=self.chain.turn_id,
            causation_id=causation_id,
            # Ready is a command handshake, not durable floor history.  A
            # replay after a later reconnect could incorrectly reopen the
            # browser capture gate for a stream that no longer exists.
            replayability=Replayability.TRANSIENT,
        )

    async def _execute_commands(self) -> None:
        """Poll durable truth and execute only under this owner's fence."""

        try:
            while not self._stopped:
                ownership = self.ownership
                if ownership is None:
                    return
                claimed = self.supervisor.journal.claim_next(
                    ownership.commit_fence(), self.organization_id
                )
                if claimed is None:
                    if (
                        self._interview_is_terminal()
                        and not self.supervisor.journal.has_unsettled(
                            self.interview_id, self.organization_id
                        )
                    ):
                        asyncio.create_task(self.stop("interview_completed"))
                        return
                    self._command_wake.clear()
                    try:
                        await asyncio.wait_for(
                            self._command_wake.wait(),
                            timeout=self.supervisor.command_poll_seconds,
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self._execute_claim(ownership, claimed)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await self._self_fence(exc)

    async def _execute_claim(
        self,
        ownership: EvidenceOwnershipGrant,
        claimed: ClaimedEvidenceCommand,
    ) -> None:
        fence = ownership.commit_fence()
        try:
            turn_id = await self._apply_claimed_command(claimed)
        except asyncio.CancelledError:
            # Cancellation leaves the running claim recoverable after its TTL;
            # an unknown side effect is never recorded as processed.
            raise
        except Exception as exc:
            status_code = int(getattr(exc, "status_code", 500))
            error_code = str(
                getattr(exc, "code", "EVIDENCE_OWNER_TEMPORARY_FAILURE")
            )[:128]
            self.supervisor.journal.fail(
                fence,
                self.organization_id,
                claimed.command_id,
                claimed.claim_id,
                error_code=error_code,
                retryable=status_code >= 500,
                retry_after_seconds=min(
                    2.0,
                    0.05 * (2 ** min(max(claimed.attempt_count - 1, 0), 5)),
                ),
            )
            if self._interview_is_terminal():
                asyncio.create_task(self.stop("interview_completed"))
            return
        session = self.supervisor.interviews.get_interview(
            self.interview_id, self.organization_id
        )
        cursor = int((session.get("agent_runtime") or {}).get("last_sequence", 0))
        self.supervisor.journal.complete(
            fence,
            self.organization_id,
            claimed.command_id,
            claimed.claim_id,
            EvidenceCommandOutcome(
                disposition="applied",
                effective_turn_id=turn_id,
                event_cursor=cursor,
            ),
        )
        if session.get("status") in _TERMINAL_INTERVIEW_STATUSES:
            asyncio.create_task(self.stop("interview_completed"))

    async def _apply_claimed_command(
        self, claimed: ClaimedEvidenceCommand
    ) -> Optional[str]:
        source = self._event_source
        if source is None or self.chain is None:
            raise ApiError(
                "EVIDENCE_OWNER_EXECUTOR_NOT_READY",
                "The Evidence owner has not restored its execution context.",
                status_code=503,
            )
        signal = ClientSignal(
            type=claimed.command_type,
            idempotency_key=claimed.command_id,
            turn_id=claimed.turn_id,
            causation_id=claimed.causation_id,
            payload=dict(claimed.payload),
        )
        if claimed.command_type == "evidence.open":
            return await self._apply_open(source, signal)
        if claimed.command_type == "speech.started":
            return await self._apply_speech_started(source, signal)
        if claimed.command_type == "speech.stopped":
            return await self._apply_speech_stopped(source, signal)
        if claimed.command_type == "evidence.continue":
            return await self._apply_continue(source, signal)
        if claimed.command_type == "evidence.backfill":
            if not claimed.turn_id or self.turn_id != claimed.turn_id or not self.chain.is_open:
                raise ApiError(
                    "BROWSER_BACKFILL_EVIDENCE_NOT_OPEN",
                    "The Evidence owner no longer has the recovery turn open.",
                    status_code=409,
                )
            content = self.supervisor.browser_backfill.read_for_owner(
                organization_id=self.organization_id,
                interview_id=self.interview_id,
                turn_id=claimed.turn_id,
                payload=claimed.payload,
                fence=self.ownership.commit_fence(),
            )
            if content is None:
                return claimed.turn_id
            if self._answer_endpoint is not None:
                # Recovery frames are authoritative new input too. Invalidate
                # even quiet backfill before allowing the commit executor on.
                self._answer_endpoint.speech_started()
            result = await self.chain.send_audio(content)
            if result is None:
                raise ApiError(
                    "BROWSER_BACKFILL_EVIDENCE_NOT_OPEN",
                    "The Evidence gate closed before browser recovery was applied.",
                    status_code=409,
                )
            self.supervisor.browser_backfill.mark_applied(
                organization_id=self.organization_id,
                interview_id=self.interview_id,
                payload=claimed.payload,
                fence=self.ownership.commit_fence(),
            )
            for raw in result.events:
                await self._route_stream_projection(
                    _PendingProjection(result.kind, raw, result.turn_id)
                )
            return claimed.turn_id
        if claimed.command_type == "evidence.seal":
            if self.chain.is_open and signal.payload.get("capture_id") and not self._matches_capture(signal):
                return claimed.turn_id
            if claimed.payload.get("endpoint") == "semantic_timeout" and self.chain.kind != "warmup":
                # Silence is not an authoritative answer-complete decision.
                return claimed.turn_id
            if claimed.payload.get("endpoint") == "semantic_timeout" and not self._matches_endpoint(signal):
                # A durable timer command may outlive the capture even after
                # its process-local timer was cancelled. It is a harmless no-op.
                return claimed.turn_id
            prepared = None
            if claimed.payload.get("endpoint") == "prepared_turn":
                prepared = self._prepared_finish
                if (prepared is None or signal.payload.get("proposal_id") != prepared[0]
                        or not self._matches_capture(signal)):
                    # A lost owner cannot replay a process-local speculative
                    # decision. It has no domain effects to recover.
                    return claimed.turn_id
                prepared[2]()
            elif self.chain.kind == "formal" and self._answer_endpoint is not None:
                if self._matches_capture(signal):
                    self._answer_endpoint.request_finish()
                return claimed.turn_id
            await self.cancel_endpoint()
            if claimed.turn_id is None and not self.chain.is_open:
                session = self.supervisor.interviews.get_interview(
                    self.interview_id, self.organization_id
                )
                runtime_state = session.get("agent_runtime") or {}
                if (
                    runtime_state.get("calibration_status") == "retrying"
                    and bool(runtime_state.get("calibration_retry_required"))
                    and self._warmup_failure is not None
                ):
                    raise self._warmup_retry_error()
            # 试音流没有正式 turn_id，但同样必须由 2.5 秒端点命令收口。
            # 先处理 warmup，不能落入下面“正式回答必须绑定题目”的校验。
            if self.chain.is_open and self.chain.kind == "warmup":
                partial_kind = str(self.chain.kind or "warmup")
                partial_turn_id = self.chain.turn_id
                warmup_epoch = self._active_warmup_epoch
                ingress_failure = self._current_ingress_failure()
                if warmup_epoch is not None and ingress_failure is not None:
                    failure = await self._recover_failed_warmup_ingress(
                        source,
                        warmup_epoch=warmup_epoch,
                        cause_code=ingress_failure.code,
                        cause_type=ingress_failure.cause_type,
                        causation_id=signal.causation_id,
                    )
                    raise failure
                try:
                    await self._drain_ingress_before_seal()
                except Exception as exc:
                    failure = await self._recover_failed_warmup_ingress(
                        source,
                        warmup_epoch=warmup_epoch or self._warmup_epoch,
                        cause_code=self._safe_problem_code(
                            getattr(exc, "code", "LIVEKIT_INGRESS_SINK_BACKPRESSURE")
                        ),
                        cause_type=type(exc).__name__,
                        causation_id=signal.causation_id,
                    )
                    raise failure from exc
                try:
                    try:
                        result = await self.chain.finish(signal.payload)
                    finally:
                        await self._deactivate_partial_projection(
                            partial_kind, partial_turn_id
                        )
                except Exception as exc:
                    failure = await self._recover_failed_warmup_finalize(
                        source,
                        signal,
                        exc,
                        warmup_epoch=warmup_epoch,
                    )
                    raise failure from exc
                ingress_failure = self._current_ingress_failure()
                if (
                    warmup_epoch is not None
                    and (
                        self._failed_warmup_epoch == warmup_epoch
                        or ingress_failure is not None
                    )
                ):
                    failure = await self._recover_failed_warmup_ingress(
                        source,
                        warmup_epoch=warmup_epoch,
                        cause_code=(
                            ingress_failure.code
                            if ingress_failure is not None
                            else str(
                                (self._warmup_failure or {}).get("cause_code")
                                or "LIVEKIT_INGRESS_AUDIO_STREAM_FAILED"
                            )
                        ),
                        cause_type=(
                            ingress_failure.cause_type
                            if ingress_failure is not None
                            else str(
                                (self._warmup_failure or {}).get("cause_type")
                                or "RuntimeError"
                            )
                        ),
                        causation_id=signal.causation_id,
                    )
                    raise failure
                try:
                    await source._complete_warmup_evidence(result, signal)
                except Exception as exc:
                    failure = await self._recover_failed_warmup_finalize(
                        source,
                        signal,
                        exc,
                        warmup_epoch=warmup_epoch,
                    )
                    raise failure from exc
                if self._active_warmup_epoch == warmup_epoch:
                    self._active_warmup_epoch = None
                return None
            effective_turn_id = claimed.turn_id or self.turn_id
            if not effective_turn_id:
                raise ApiError(
                    "EVIDENCE_TURN_REQUIRED",
                    "Evidence seal requires a frozen interview turn.",
                    status_code=409,
                )
            if self._turn_has_answer(effective_turn_id):
                # The previous execution may have committed CandidateAnswer and
                # died before acknowledging the journal claim. The domain row is
                # the effect receipt; never invoke STT or create another answer.
                await source._emit_snapshot(claimed.causation_id)
                return effective_turn_id
            if self.chain.is_open:
                try:
                    await self._drain_ingress_before_seal()
                except Exception as exc:
                    ingress_failure = self._current_ingress_failure()
                    cause_code = str(
                        getattr(ingress_failure, "code", None)
                        or getattr(exc, "code", None)
                        or "LIVEKIT_INGRESS_SINK_BACKPRESSURE"
                    )
                    cause_type = str(
                        getattr(ingress_failure, "cause_type", None)
                        or type(exc).__name__
                    )
                    await self._fatal_ingress_problem(
                        "LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED",
                        exc,
                        cause_code=cause_code,
                        cause_type=cause_type,
                    )
                    raise ApiError(
                        "LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED",
                        "The authoritative audio queue did not drain before the turn was sealed.",
                        status_code=409,
                    ) from exc
                try:
                    await self._finish_with_channel(source, signal, prepared=prepared)
                except ApiError as exc:
                    if exc.code in _FAIL_CLOSED_MEDIA_ERRORS:
                        await self._fail_closed_media_recovery(
                            source, effective_turn_id, exc
                        )
                    raise
                return effective_turn_id
            try:
                outcome = await self.chain.recover_persisted_turn(
                    effective_turn_id,
                    claimed.payload,
                )
            except ApiError as exc:
                if exc.code in _FAIL_CLOSED_MEDIA_ERRORS:
                    await self._fail_closed_media_recovery(
                        source, effective_turn_id, exc
                    )
                raise
            await self._project_repaired_disconnect(outcome, source)
            return effective_turn_id
        if claimed.command_type == "evidence.reset":
            await self.chain.abort_warmup()
            await self._deactivate_partial_projection("warmup", None)
            if self._warmup_ingress_restart_required:
                await self._restore_ingress_for_warmup_retry(source)
                self._warmup_ingress_restart_required = False
            self._active_warmup_epoch = None
            source.runtime._set_calibration(
                self.interview_id,
                "retrying",
                self.organization_id,
                retry_required=False,
            )
            source.runtime._set_floor(
                self.interview_id,
                FloorOwner.CANDIDATE,
                "warmup_retry",
                self.organization_id,
            )
            await source._emit(
                "floor.changed",
                {
                    "owner": FloorOwner.CANDIDATE.value,
                    "reason": "warmup_retry",
                },
                causation_id=signal.causation_id,
                replayability=Replayability.TRANSIENT,
            )
            return self.turn_id
        raise ApiError(
            "EVIDENCE_COMMAND_UNSUPPORTED",
            "The Evidence owner does not support this durable command.",
            status_code=422,
        )

    def _turn_has_answer(self, turn_id: str) -> bool:
        session = self.supervisor.interviews.get_interview(
            self.interview_id, self.organization_id
        )
        return any(
            str(answer.get("turn_id") or "") == turn_id
            for answer in session.get("answers", [])
        )

    def _interview_is_terminal(self) -> bool:
        session = self.supervisor.interviews.get_interview(
            self.interview_id, self.organization_id
        )
        return session.get("status") in _TERMINAL_INTERVIEW_STATUSES

    async def _fail_closed_media_recovery(
        self,
        source: "AgentChannel",
        turn_id: str,
        exc: ApiError,
    ) -> None:
        await self._fatal_ingress_problem(exc.code, exc)
        await source._emit(
            "problem",
            {
                "code": exc.code,
                "message": "完整的服务端音频检查点不可用，面试已暂停并等待人工处理。",
                "recoverable": False,
                "action": "pause_or_human_takeover",
            },
            turn_id=turn_id,
            causation_id=None,
            replayability=Replayability.REPLAYABLE,
        )

    async def _recover_failed_warmup_finalize(
        self,
        source: "AgentChannel",
        signal: ClientSignal,
        exc: Exception,
        *,
        warmup_epoch: Optional[int],
    ) -> ApiError:
        """Make one consumed warm-up final terminal but explicitly retryable by user."""

        return await self._recover_warmup_failure(
            source,
            warmup_epoch=warmup_epoch,
            error_code=self._safe_problem_code(
                getattr(exc, "code", "WARMUP_STT_PROBLEM")
            ),
            cause_code=self._safe_problem_code(
                getattr(exc, "code", "WARMUP_STT_PROBLEM")
            ),
            cause_type=type(exc).__name__,
            causation_id=signal.causation_id,
            restart_ingress=False,
        )

    async def _recover_failed_warmup_ingress(
        self,
        source: "AgentChannel",
        *,
        warmup_epoch: int,
        cause_code: str,
        cause_type: str,
        causation_id: Optional[str],
    ) -> ApiError:
        return await self._recover_warmup_failure(
            source,
            warmup_epoch=warmup_epoch,
            error_code="LIVEKIT_EVIDENCE_AUDIO_STREAM_FAILED",
            cause_code=cause_code,
            cause_type=cause_type,
            causation_id=causation_id,
            restart_ingress=True,
        )

    async def _recover_warmup_failure(
        self,
        source: "AgentChannel",
        *,
        warmup_epoch: Optional[int],
        error_code: str,
        cause_code: str,
        cause_type: str,
        causation_id: Optional[str],
        restart_ingress: bool,
    ) -> ApiError:
        """Invalidate one disposable warm-up exactly once across async tasks."""

        epoch = warmup_epoch or self._active_warmup_epoch
        if epoch is None:
            return ApiError(
                self._safe_problem_code(error_code),
                "Warm-up transcription could not be finalized; retry calibration.",
                status_code=409,
            )
        if self._failed_warmup_epoch != epoch:
            self._failed_warmup_epoch = epoch
            self._warmup_failure = {
                "epoch": epoch,
                "error_code": self._safe_problem_code(error_code),
                "cause_code": self._safe_problem_code(cause_code),
                "cause_type": str(cause_type or "Exception")[:96],
            }
        if restart_ingress:
            self._warmup_ingress_restart_required = True

        async with self._warmup_recovery_lock:
            if self._recovered_warmup_epoch != epoch:
                await self.cancel_endpoint()
                if self.chain is not None:
                    await self.chain.abort_warmup()
                await self._deactivate_partial_projection("warmup", None)
                if self._active_warmup_epoch == epoch:
                    self._active_warmup_epoch = None
                source.runtime._set_calibration(
                    self.interview_id,
                    "retrying",
                    self.organization_id,
                    retry_required=True,
                )
                await source._set_floor(
                    FloorOwner.CANDIDATE, "warmup_retry", causation_id
                )
                failure = dict(self._warmup_failure or {})
                problem = {
                    "code": str(failure.get("error_code") or error_code),
                    "message": str(failure.get("cause_type") or "Exception"),
                    "recoverable": True,
                    "action": "retry_warmup",
                    "calibration": True,
                    "cause_code": str(failure.get("cause_code") or cause_code),
                    "cause_type": str(failure.get("cause_type") or cause_type),
                }
                source.runtime._record_problem(
                    self.interview_id, problem, self.organization_id
                )
                await source._emit(
                    "problem",
                    problem,
                    turn_id=None,
                    causation_id=causation_id,
                    replayability=Replayability.REPLAYABLE,
                )
                self._recovered_warmup_epoch = epoch
        return self._warmup_retry_error()

    def _warmup_retry_error(self) -> ApiError:
        error_code = str(
            (self._warmup_failure or {}).get("error_code")
            or "WARMUP_STT_PROBLEM"
        )
        # The warm-up Provider stream has already been consumed/aborted by
        # InterviewEvidenceChain.finish().  Returning a 409 makes the journal
        # preserve this original error as a terminal receipt instead of
        # re-entering the now-closed chain and overwriting it.
        return ApiError(
            error_code,
            "Warm-up transcription could not be finalized; retry calibration.",
            status_code=409,
        )

    @staticmethod
    def _safe_problem_code(value: Any) -> str:
        raw_code = str(value or "WARMUP_STT_PROBLEM").upper()
        return (
            "".join(
                character if character.isalnum() or character == "_" else "_"
                for character in raw_code
            )[:128].strip("_")
            or "WARMUP_STT_PROBLEM"
        )

    def _current_ingress_failure(self) -> Optional[Any]:
        if self._ingress is None:
            return None
        return getattr(self._ingress, "last_track_failure", None)

    async def _drain_ingress_before_seal(self) -> None:
        """Fence turn finalization behind the current LiveKit sink watermark."""

        ingress = self._ingress
        drain = getattr(ingress, "drain", None)
        if callable(drain):
            await drain()

    async def _restore_ingress_for_warmup_retry(
        self, source: "AgentChannel"
    ) -> None:
        ingress = self._ingress
        recover = getattr(ingress, "recover_audio_stream", None)
        if callable(recover) and await recover():
            return
        if ingress is not None:
            self._ingress = None
            await ingress.close()
        await self.ensure_ingress(source.opened.connection_id)

    async def _apply_open(
        self, source: "AgentChannel", signal: ClientSignal, *, retry_capture_id: Optional[str] = None
    ) -> Optional[str]:
        if not self.ingress_connected:
            raise ApiError(
                "LIVEKIT_EVIDENCE_INGRESS_NOT_CONNECTED",
                "Candidate microphone has not reached the authoritative server subscriber.",
                status_code=409,
            )
        session = source.runtime.interviews.get_interview(
            self.interview_id, self.organization_id
        )
        runtime_state = session.get("agent_runtime") or {}
        recovery = runtime_state.get("capture_recovery") or {}
        if recovery.get("status") == "retry_required" and (
            recovery.get("capture_id") != retry_capture_id or recovery.get("turn_id") != signal.turn_id
        ):
            raise ApiError("CAPTURE_RETRY_REQUIRED", "Retry the current failed capture explicitly.", status_code=409)
        calibration = runtime_state.get("calibration_status", "pending")
        if (
            calibration == "retrying"
            and bool(runtime_state.get("calibration_retry_required"))
        ):
            raise ApiError(
                "WARMUP_RETRY_REQUIRED",
                "Warm-up must be explicitly reset before another Evidence stream opens.",
                status_code=409,
            )
        if self.chain.is_open:
            if self.chain.turn_id == signal.turn_id or (
                self.chain.kind == "warmup" and signal.turn_id is None
            ):
                if (
                    self.chain.kind == "warmup"
                    and self._active_warmup_epoch is None
                ):
                    self._warmup_epoch += 1
                    self._active_warmup_epoch = self._warmup_epoch
                    self._warmup_failure = None
                self._activate_partial_projection(
                    str(self.chain.kind or "formal"), self.chain.turn_id
                )
                await self._project_open_ready(
                    source,
                    kind=str(self.chain.kind or "formal"),
                    causation_id=signal.causation_id,
                )
                return self.chain.turn_id
            raise ApiError(
                "AGENT_EVIDENCE_ALREADY_OPEN",
                "Another Evidence stream is already open.",
                status_code=409,
            )
        expected_kind = "warmup" if calibration != "completed" else "formal"
        await self.cancel_endpoint()
        self._clear_answer_preparation_for_cleanup(self._capture_id, self.chain.turn_id)
        self._capture_id = None
        self._capture_speech_started = False
        expected_turn_id = None if expected_kind == "warmup" else signal.turn_id
        self._activate_partial_projection(expected_kind, expected_turn_id)
        try:
            result = await self.chain.open(
                signal.payload,
                turn_id=signal.turn_id,
                calibration_status=calibration,
            )
        except BaseException:
            await self._deactivate_partial_projection(
                expected_kind, expected_turn_id
            )
            raise
        self._capture_id = new_id("capture")
        if retry_capture_id is not None:
            with self.persistence.transaction(self.organization_id) as transaction:
                assert_current_evidence_fence(transaction, self.ownership.commit_fence())
                latest = transaction.interview_sessions.get(self.interview_id)
                state = latest.setdefault("agent_runtime", {})
                if ((state.get("capture_recovery") or {}).get("capture_id") == retry_capture_id):
                    state.pop("capture_recovery", None)
                    transaction.interview_sessions.update(latest, expected_version=latest["version"])
        if self._answer_endpoint is not None:
            await self._answer_endpoint.close()
            self._answer_endpoint = None
        if result.kind == "formal" and hasattr(self.chain, "transcript_snapshot"):
            self._start_answer_endpoint()
        if (result.kind, result.turn_id) != (expected_kind, expected_turn_id):
            await self._deactivate_partial_projection(
                expected_kind, expected_turn_id
            )
            self._activate_partial_projection(result.kind, result.turn_id)
        if result.kind == "warmup":
            self._warmup_epoch += 1
            self._active_warmup_epoch = self._warmup_epoch
            self._warmup_failure = None
        for raw in result.events:
            await self._route_stream_projection(
                _PendingProjection(result.kind, raw, result.turn_id)
            )
        # This transient fact is emitted by the single fenced owner while the
        # command is still running.  The journal receipt is completed only
        # after this returns, so a controller's delayed outcome poll can never
        # manufacture readiness after a later seal/reset.
        await self._project_open_ready(
            source,
            kind=result.kind,
            causation_id=signal.causation_id,
        )
        return result.turn_id

    def _matches_capture(self, signal: ClientSignal) -> bool:
        return bool(
            self.chain.is_open
            and self._capture_id
            and signal.payload.get("capture_id") == self._capture_id
            and signal.turn_id == self.chain.turn_id
        )

    def _matches_endpoint(self, signal: ClientSignal) -> bool:
        return bool(
            self._matches_capture(signal)
            and self._endpoint_id
            and signal.payload.get("endpoint_id") == self._endpoint_id
            and not self._capture_speech_started
        )

    async def _apply_speech_started(
        self, source: "AgentChannel", signal: ClientSignal
    ) -> Optional[str]:
        session = source.runtime.interviews.get_interview(
            self.interview_id, self.organization_id
        )
        runtime_state = session.get("agent_runtime") or {}
        matches = self._matches_capture(signal)
        if not matches:
            # An unscoped barge-in may interrupt the current agent performance,
            # but cannot arm a stop timer for a stream opened afterwards.
            if (
                self.chain.is_open
                or runtime_state.get("floor") != FloorOwner.AGENT.value
                or not (
                    signal.turn_id == session.get("current_turn_id")
                    or (signal.turn_id is None and runtime_state.get("calibration_status") != "completed")
                )
            ):
                return signal.turn_id
        else:
            await self.cancel_endpoint()
            self._capture_speech_started = True
            if self._answer_endpoint is not None:
                self._answer_endpoint.speech_started()
        effective_turn_id = signal.turn_id or session.get("current_turn_id")
        if runtime_state.get("calibration_status") == "opening":
            source.runtime._set_calibration(
                self.interview_id, "listening", self.organization_id
            )
        if runtime_state.get("floor") == FloorOwner.AGENT.value:
            await source._cancel_speech_output()
            if self._presentation_task is not None and not self._presentation_task.done():
                self._presentation_task.cancel()
                await asyncio.gather(self._presentation_task, return_exceptions=True)
            await source._emit(
                "avatar.performance.interrupted",
                {
                    "reason": "barge_in",
                    "deadline_ms": 200,
                    "performance_id": runtime_state.get("active_performance_id"),
                },
                turn_id=effective_turn_id,
                causation_id=signal.causation_id,
                replayability=Replayability.TRANSIENT,
            )
            source.runtime._clear_active_performance(
                self.interview_id,
                runtime_state.get("active_performance_id"),
                self.organization_id,
            )
        await source._set_floor(
            FloorOwner.CANDIDATE, "barge_in", signal.causation_id
        )
        await source._emit(
            "speech.started",
            {
                "speaker": "candidate",
                "local_detected": True,
                "server_audio_received": False,
            },
            turn_id=effective_turn_id,
            causation_id=signal.causation_id,
            replayability=Replayability.TRANSIENT,
        )
        return effective_turn_id

    async def _apply_speech_stopped(
        self, source: "AgentChannel", signal: ClientSignal
    ) -> Optional[str]:
        if not self._matches_capture(signal) or not self._capture_speech_started:
            return signal.turn_id
        self._capture_speech_started = False
        effective_turn_id = signal.turn_id
        automatic = self.chain.kind == "warmup"
        await source._emit(
            "speech.stopped",
            {
                "speaker": "candidate",
                "endpoint_countdown_ms": 2500 if automatic else 0,
                "cancellable": True,
            },
            turn_id=effective_turn_id,
            causation_id=signal.causation_id,
            replayability=Replayability.TRANSIENT,
        )
        if automatic:
            await self._schedule_endpoint(signal)
        else:
            await self.cancel_endpoint()
        return effective_turn_id

    async def _apply_continue(
        self, source: "AgentChannel", signal: ClientSignal
    ) -> Optional[str]:
        current = self.supervisor.interviews.get_interview(self.interview_id, self.organization_id)
        recovery = (current.get("agent_runtime") or {}).get("capture_recovery") or {}
        if recovery.get("status") == "retry_required":
            capture_id = str(signal.payload.get("capture_id") or "")
            if (not signal.turn_id or recovery.get("turn_id") != signal.turn_id
                    or recovery.get("capture_id") != capture_id or self.chain.is_open):
                return signal.turn_id
            if self._answer_endpoint is not None:
                await self._answer_endpoint.close()
                self._answer_endpoint = None
            media = DurableEvidenceMedia(self.store, organization_id=self.organization_id, persistence=self.persistence)
            if not media.prepare_candidate_retry(
                interview_id=self.interview_id, turn_id=signal.turn_id,
                capture_id=capture_id, fence=self.ownership.commit_fence(),
            ):
                return signal.turn_id
            try:
                # Retry signals contain only the failed capture token. The
                # authoritative LiveKit format is server-owned, not the STT
                # request's generic WebM default or arbitrary client metadata.
                retry_signal = signal.model_copy(update={"payload": {
                    "content_type": "audio/pcm", "sample_rate_hz": 16000,
                    "channels": 1, "language": "zh-CN",
                }})
                return await self._apply_open(source, retry_signal, retry_capture_id=capture_id)
            except Exception as exc:
                failure = classify_capture_failure(exc, "retry")
                if not failure.requires_new_capture:
                    raise
                # Failed retries keep the same durable retry token/revision;
                # no new answer or orphaned second reset is created.
                self._capture_id = capture_id
                with self.persistence.transaction(self.organization_id) as transaction:
                    assert_current_evidence_fence(transaction, self.ownership.commit_fence())
                    latest = transaction.interview_sessions.get(self.interview_id)
                    state = latest.setdefault("agent_runtime", {})
                    marker = state.get("capture_recovery") or {}
                    if (latest.get("status") != "in_progress" or latest.get("current_turn_id") != signal.turn_id
                            or marker.get("capture_id") != capture_id or marker.get("status") != "retry_required"):
                        return signal.turn_id
                    marker.update(cause_code=failure.cause_code, stage="retry")
                    problems = state.setdefault("problems", [])
                    problems.append({
                        "code": "CAPTURE_RETRY_REQUIRED", "message": "Recognition retry failed.",
                        "recoverable": True, "action": "retry_answer", "capture_id": capture_id,
                        "cause_code": failure.cause_code, "cause_type": failure.cause_type,
                        "stage": "retry", "occurred_at": utc_now(),
                    })
                    del problems[:-20]
                    transaction.interview_sessions.update(latest, expected_version=latest["version"])
                await source._emit_snapshot(signal.causation_id)
                await source._emit("problem", {
                    "code": "CAPTURE_RETRY_REQUIRED", "message": "语音识别仍未恢复，请稍后重试本题。",
                    "recoverable": True, "action": "retry_answer", "capture_id": capture_id,
                }, turn_id=signal.turn_id, causation_id=signal.causation_id, replayability=Replayability.TRANSIENT)
                return signal.turn_id
        if not self._matches_capture(signal):
            return signal.turn_id
        await self.cancel_endpoint()
        self._capture_speech_started = True
        if self._answer_endpoint is not None:
            self._answer_endpoint.continue_speaking()
        await source._set_floor(
            FloorOwner.CANDIDATE, "candidate_continues", signal.causation_id
        )
        await source._emit(
            "speech.started",
            {"speaker": "candidate", "continued": True, "local_detected": True},
            turn_id=signal.turn_id or self.turn_id,
            causation_id=signal.causation_id,
            replayability=Replayability.TRANSIENT,
        )
        return signal.turn_id or self.turn_id

    def _install_owner(self, grant: EvidenceOwnershipGrant) -> None:
        """Install exactly one process-local executor for a claimed epoch."""

        if self.ownership is not None:
            if (
                self.ownership.lease_id != grant.lease_id
                or self.ownership.ownership_epoch != grant.ownership_epoch
            ):
                raise ApiError(
                    "EVIDENCE_OWNER_FENCED",
                    "The local Evidence executor already belongs to another ownership epoch.",
                    status_code=409,
                )
            self.ownership = grant
            return
        self.ownership = grant
        commit_fence = grant.commit_fence()
        self.chain = self.supervisor.evidence_chain_factory(
            self.store,
            self.interview_id,
            organization_id=self.organization_id,
            persistence=self.persistence,
            commit_fence=commit_fence,
            commit_guard=lambda: self.supervisor.coordinator.assert_current(
                commit_fence, self.organization_id
            ),
        )
        self._renewal_task = asyncio.create_task(self._renew_ownership())
        self._executor_task = asyncio.create_task(self._execute_commands())
        if isinstance(self.chain, InterviewEvidenceChain):
            self.supervisor.prewarm_turn_detector()

    async def _recover_owner_after_loss(self) -> None:
        """Promote the current remote controller after the old lease expires.

        ``claim_owner`` preserves the control generation, so an already durable
        command remains eligible after failover. A controller superseded on a
        different instance is never allowed to acquire or execute the epoch.
        """

        try:
            while not self._stopped and self.ownership is None:
                await asyncio.sleep(
                    max(0.05, self.supervisor.renew_interval_seconds)
                )
                channel = self._channel
                local_grant = (
                    self._control_grants.get(id(channel))
                    if channel is not None
                    else None
                )
                if local_grant is None:
                    return
                try:
                    self.supervisor.coordinator.assert_control_current(local_grant)
                except ApiError:
                    return
                if (
                    self._interview_is_terminal()
                    and not self.supervisor.journal.has_unsettled(
                        self.interview_id, self.organization_id
                    )
                ):
                    return
                grant = self.supervisor.coordinator.claim_owner(
                    interview_id=self.interview_id,
                    organization_id=self.organization_id,
                    local_instance_id=self.supervisor.instance_id,
                )
                if not grant.is_local_owner:
                    continue
                same_control = bool(
                    grant.control_connection_id
                    == local_grant.control_connection_id
                    and grant.control_generation == local_grant.control_generation
                )
                if not same_control:
                    self.supervisor.coordinator.release(grant)
                    return
                for key, current in list(self._control_grants.items()):
                    if (
                        current.control_connection_id
                        == grant.control_connection_id
                        and current.control_generation == grant.control_generation
                    ):
                        self._control_grants[key] = grant
                self._install_owner(grant)
                if self._interview_is_terminal():
                    # A terminal interview may still need one unknown command
                    # receipt settled, but must never reconnect candidate media.
                    self._command_wake.set()
                    return
                try:
                    await self.ensure_ingress(grant.control_connection_id)
                except Exception as exc:
                    await self._self_fence(exc)
                    self.supervisor.coordinator.release(grant)
                return
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if not self._stopped:
                await self._fatal_ingress_problem(
                    "EVIDENCE_OWNER_RECOVERY_FAILED", exc
                )
        finally:
            if self._owner_recovery_task is asyncio.current_task():
                self._owner_recovery_task = None

    async def _finish_terminal_presentation(self) -> None:
        """Allow a bounded farewell setup before releasing its live owner.

        Completion of asynchronous evaluation is not cancellation of the
        already-committed answer's presentation. Pause/fence/shutdown still
        set ``_stopped`` immediately and cancel the presentation through their
        existing paths; this grace must never restore that lost authority.
        """
        task = self._presentation_task
        source = self._channel or self._event_source
        ownership = self.ownership
        if task is None or task is asyncio.current_task() or source is None or ownership is None:
            return
        fence = ownership.commit_fence()

        def current_terminal_state() -> Optional[Dict[str, Any]]:
            if self._stopped or source._closed or source._terminating:
                return None
            if self._presentation_task is not task or self.ownership is None:
                return None
            try:
                with self.persistence.transaction(self.organization_id) as transaction:
                    assert_current_evidence_fence(transaction, fence)
                    session = transaction.interview_sessions.get(self.interview_id)
                    if not session or session.get("status") not in _TERMINAL_INTERVIEW_STATUSES:
                        return None
                    state = session.get("agent_runtime") or {}
                    if (state.get("takeover") or {}).get("status") == "active":
                        return None
                    return state
            except ApiError:
                return None

        if current_terminal_state() is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_TERMINAL_PRESENTATION_GRACE_SECONDS)
        except asyncio.TimeoutError:
            task.cancel()
            # A misbehaving provider may suppress cancellation. It must not
            # hold terminal cleanup indefinitely; the stopped/owner fence in
            # the expression seam rejects any eventual late result.
            await asyncio.wait({task}, timeout=0.1)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
        except Exception:
            # The answer is already committed. Failure to speak a farewell
            # must still leave the candidate with its completion receipt.
            pass
        state = current_terminal_state()
        if state is not None and not (
            state.get("completion_emitted_at") or state.get("completion_closing_performance_id")
        ):
            try:
                await source._finalize_completion(
                    causation_id=None, reason="farewell_preparation_unavailable",
                )
            except Exception:
                # The completion marker is durable even if its live fanout
                # fails; normal snapshot/replay recovery remains available.
                pass
        if not task.done() and self._presentation_task is task:
            self._presentation_task = None
            task.add_done_callback(lambda completed: None if completed.cancelled() else completed.exception())

    async def stop(self, reason: str) -> None:
        if self._stopped:
            return
        if reason == "interview_completed":
            previous = getattr(self, "_terminal_stop_task", None)
            if previous is not None and previous is not asyncio.current_task() and not previous.done():
                return
            self._terminal_stop_task = asyncio.current_task()
            await self._finish_terminal_presentation()
            if self._stopped:
                return
        self._stopped = True
        self._clear_answer_preparation_for_cleanup(self._capture_id, self.chain.turn_id if self.chain else None)
        if self._answer_endpoint is not None:
            await self._answer_endpoint.close()
        if self._presentation_task is not None and not self._presentation_task.done():
            self._presentation_task.cancel()
            await asyncio.gather(self._presentation_task, return_exceptions=True)
        await self._shutdown_partial_projection()
        expiry = self._expiry_task
        self._expiry_task = None
        if expiry is not None and expiry is not asyncio.current_task():
            expiry.cancel()
            await asyncio.gather(expiry, return_exceptions=True)
        await self.cancel_endpoint()
        ingress = self._ingress
        self._ingress = None
        if ingress is not None:
            await ingress.close()
        failure: Optional[BaseException] = None
        try:
            outcome = (
                await self.chain.close_for_disconnect()
                if self.chain is not None
                else None
            )
            if outcome is not None and self._event_source is not None:
                await self._project_repaired_disconnect(outcome, self._event_source)
        except BaseException as exc:
            failure = exc
        finally:
            self._channel = None
            self._event_source = None
            self._control_grants.clear()
            renewal = self._renewal_task
            self._renewal_task = None
            if renewal is not None and renewal is not asyncio.current_task():
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)
            executor = self._executor_task
            self._executor_task = None
            if executor is not None and executor is not asyncio.current_task():
                executor.cancel()
                await asyncio.gather(executor, return_exceptions=True)
            recovery = self._owner_recovery_task
            self._owner_recovery_task = None
            if recovery is not None and recovery is not asyncio.current_task():
                recovery.cancel()
                await asyncio.gather(recovery, return_exceptions=True)
            if self.ownership is not None:
                self.supervisor.coordinator.release(self.ownership)
            self.supervisor._remove(self)
        if failure is not None:
            raise failure

    async def _receive_audio(self, frame: LiveKitIngressAudioFrame) -> None:
        if self._stopped or self.chain is None:
            return
        if self._partial_projection_failure is not None:
            raise RuntimeError(
                "Authoritative transcript projection failed closed"
            ) from self._partial_projection_failure
        endpoint = self._answer_endpoint
        capture_id = self._capture_id
        if endpoint and endpoint.confirmation and endpoint.confirmation.speaking:
            # Do not recognise the interviewer's own confirmation prompt as a
            # candidate reply. Retain only 200 ms to restore a confirmed barge-in.
            preroll = getattr(self, "_supplement_preroll", [])
            preroll.append(frame.pcm_s16le)
            self._supplement_preroll = preroll[-10:]
            return
        if self.chain.kind == "formal" and self._answer_endpoint is not None:
            self._answer_endpoint.observe_audio(frame.pcm_s16le)
        try:
            result = await self.chain.send_audio(frame.pcm_s16le)
        except Exception as exc:
            # Recognition's bounded buffer/stream can fail while the media
            # subscription is healthy. Let its existing single endpoint worker
            # resolve that sticky failure; do not kill the LiveKit consumer or
            # retry this frame (accepted PCM was already recorded exactly once).
            failure = classify_capture_failure(exc, "send")
            if (self.chain.kind == "formal" and self._answer_endpoint is not None
                    and self.chain.recovery_required and failure.requires_new_capture):
                return
            raise
        if result is None:
            return
        if result.first_server_audio:
            await self._deliver(
                _PendingProjection(
                    "server_audio",
                    {
                        "speaker": "candidate",
                        "local_detected": True,
                        "server_audio_received": True,
                        "server_received_at": utc_now(),
                        "media_transport": "livekit_server_subscriber",
                        "ingress_sequence": frame.sequence,
                    },
                    result.turn_id,
                )
            )
        for raw in result.events:
            if (endpoint is not None and endpoint is self._answer_endpoint
                    and capture_id == self._capture_id and result.kind == "formal"
                    and result.turn_id == self.chain.turn_id
                    and not (endpoint.confirmation and endpoint.confirmation.speaking)
                    and raw.get("type") in {"transcript.partial", "transcript.final"}
                    and isinstance(raw.get("text"), str)):
                endpoint.observe_transcript(raw["text"])
            await self._route_stream_projection(
                _PendingProjection(result.kind, raw, result.turn_id)
            )

    async def _receive_state(self, state: str) -> None:
        self.last_ingress_state = state
        if state not in {
            "audio_stream_failed",
            "microphone_subscription_failed",
            "room_disconnected",
            "candidate_disconnected",
            "track_unsubscribed",
            "track_unpublished",
            "track_muted",
        }:
            return
        recoverable = state in {
            "candidate_disconnected",
            "track_unsubscribed",
            "track_unpublished",
            "track_muted",
        }
        source = self._event_source
        if source is None:
            return
        if state == "audio_stream_failed" and self._active_warmup_epoch is not None:
            ingress_failure = self._current_ingress_failure()
            await self._recover_failed_warmup_ingress(
                source,
                warmup_epoch=self._active_warmup_epoch,
                cause_code=(
                    getattr(ingress_failure, "code", None)
                    or "LIVEKIT_INGRESS_AUDIO_STREAM_FAILED"
                ),
                cause_type=(
                    getattr(ingress_failure, "cause_type", None)
                    or "RuntimeError"
                ),
                causation_id=None,
            )
            return
        if recoverable:
            self.supervisor.browser_backfill.authorize_gap(
                interview_id=self.interview_id,
                organization_id=self.organization_id,
                turn_id=self.turn_id,
                fence=(
                    self.ownership.commit_fence()
                    if self.ownership is not None
                    else None
                ),
                reason=state,
            )
        if not recoverable:
            ingress_failure = (
                getattr(self._ingress, "last_track_failure", None)
                if state == "audio_stream_failed" and self._ingress is not None
                else None
            )
            await self._fatal_ingress_problem(
                "LIVEKIT_EVIDENCE_%s" % state.upper(),
                RuntimeError(state),
                cause_code=getattr(ingress_failure, "code", None),
                cause_type=getattr(ingress_failure, "cause_type", None),
            )
        await source._emit(
            "problem",
            {
                "code": "LIVEKIT_EVIDENCE_%s" % state.upper(),
                "message": (
                    "候选人媒体轨道暂时断开，系统将在恢复窗口内等待重连。"
                    if recoverable
                    else "服务端权威收音链路已中断，面试已暂停并等待人工接管。"
                ),
                "recoverable": recoverable,
                "action": "reconnect_media" if recoverable else "pause_or_human_takeover",
            },
            turn_id=self.turn_id,
            causation_id=None,
            replayability=Replayability.REPLAYABLE,
        )

    async def _deliver(self, item: _PendingProjection) -> None:
        async with self._delivery_lock:
            await self._deliver_locked(item)

    async def _route_stream_projection(self, item: _PendingProjection) -> None:
        """Keep lossy partial UI work out of authoritative audio intake."""

        if item.raw.get("type") == "transcript.partial":
            self._queue_partial_projection(item)
            return
        # Finals, errors and conversation actions are lossless.  They retain
        # their existing synchronous ordering and failure behavior.
        await self._deliver(item)

    @staticmethod
    def _partial_projection_key(
        kind: str, turn_id: Optional[str]
    ) -> tuple[str, Optional[str]]:
        return (str(kind), turn_id)

    def _activate_partial_projection(
        self, kind: str, turn_id: Optional[str]
    ) -> None:
        if self._partial_projection_shutdown:
            return
        self._partial_projection_active_keys.add(
            self._partial_projection_key(kind, turn_id)
        )

    def _queue_partial_projection(self, item: _PendingProjection) -> None:
        key = self._partial_projection_key(item.kind, item.turn_id)
        if (
            self._stopped
            or self._partial_projection_shutdown
            or self._partial_projection_failure is not None
            or key not in self._partial_projection_active_keys
        ):
            return
        if self._channel is None:
            # With no controller there is no I/O or database projection to
            # decouple. Preserve one reconnect snapshot in the existing
            # coalescing buffer without scheduling background work.
            self._buffer(item)
            return
        if (
            key not in self._partial_projection_pending
            and len(self._partial_projection_pending)
            >= _PARTIAL_PROJECTION_CAPACITY
        ):
            # Every value in this structure is transient. Dropping the oldest
            # key is safe and keeps even malformed/multi-turn Provider output
            # within a hard process-memory bound.
            oldest = next(iter(self._partial_projection_pending))
            self._partial_projection_pending.pop(oldest, None)
        self._partial_projection_pending[key] = item
        self._partial_projection_wake.set()
        task = self._partial_projection_task
        if task is None or task.done():
            self._partial_projection_task = asyncio.create_task(
                self._run_partial_projection()
            )

    async def _run_partial_projection(self) -> None:
        current = asyncio.current_task()
        try:
            while not self._partial_projection_shutdown:
                await self._partial_projection_wake.wait()
                self._partial_projection_wake.clear()
                while (
                    self._partial_projection_pending
                    and not self._partial_projection_shutdown
                ):
                    key = next(iter(self._partial_projection_pending))
                    item = self._partial_projection_pending.pop(key)
                    async with self._delivery_lock:
                        if (
                            self._stopped
                            or self._partial_projection_shutdown
                            or key not in self._partial_projection_active_keys
                        ):
                            continue
                        await self._deliver_locked(item)
                    # _project may complete without a scheduling point when
                    # Redis is disabled. Give the audio pump and lease renewal
                    # an explicit chance before projecting another partial.
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            # Projection failure used to escape _receive_audio and terminate
            # the LiveKit sink. Preserve that fail-closed outcome even though
            # the lossy projection now runs independently.
            self._partial_projection_failure = exc
            self._partial_projection_pending.clear()
            self._partial_projection_active_keys.clear()
            if not self._stopped:
                await self._fatal_ingress_problem(
                    "LIVEKIT_EVIDENCE_PROJECTION_FAILED", exc
                )
        finally:
            if self._partial_projection_task is current:
                self._partial_projection_task = None

    async def _deactivate_partial_projection(
        self, kind: str, turn_id: Optional[str]
    ) -> None:
        """Fence one stream's partials before projecting its final/reset."""

        key = self._partial_projection_key(kind, turn_id)
        self._partial_projection_active_keys.discard(key)
        self._partial_projection_pending.pop(key, None)
        # An in-flight partial owns _delivery_lock for its complete projection.
        # Waiting for that boundary guarantees it cannot arrive after final.
        async with self._delivery_lock:
            self._pending = [
                item
                for item in self._pending
                if not (
                    item.raw.get("type") == "transcript.partial"
                    and self._partial_projection_key(item.kind, item.turn_id)
                    == key
                )
            ]

    async def _shutdown_partial_projection(self) -> None:
        """Drop only transient partial work and join any in-flight emission."""

        if self._partial_projection_shutdown:
            task = self._partial_projection_task
            if task is not None and task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)
            return
        self._partial_projection_shutdown = True
        self._partial_projection_active_keys.clear()
        self._partial_projection_pending.clear()
        self._partial_projection_wake.set()
        # Join an already-started projection before stop returns. A worker that
        # has not acquired the lock re-checks the shutdown fence and drops its
        # stale item instead.
        async with self._delivery_lock:
            self._pending = [
                item
                for item in self._pending
                if item.raw.get("type") != "transcript.partial"
            ]
        task = self._partial_projection_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._partial_projection_task = None

    async def _deliver_locked(self, item: _PendingProjection) -> None:
        channel = self._channel
        if channel is None:
            self._buffer(item)
            return
        await self._project(channel, item)

    async def _project(
        self, channel: "AgentChannel", item: _PendingProjection
    ) -> None:
        if item.kind == "server_audio":
            await channel._emit(
                "speech.started",
                item.raw,
                turn_id=item.turn_id,
                causation_id=None,
                replayability=Replayability.TRANSIENT,
            )
            return
        raw = dict(item.raw)
        raw.setdefault("turn_id", item.turn_id)
        if item.kind == "warmup":
            await channel._project_warmup_event(raw, None)
        else:
            await channel._project_evidence_event(raw, None)

    def _buffer(self, item: _PendingProjection) -> None:
        if item.raw.get("type") == "transcript.partial":
            self._pending = [
                current
                for current in self._pending
                if not (
                    current.kind == item.kind
                    and current.turn_id == item.turn_id
                    and current.raw.get("type") == "transcript.partial"
                )
            ]
        self._pending.append(item)
        if len(self._pending) > 256:
            # Only transient partials should accumulate without a controller.
            # Drop the oldest partial; never discard finals or errors.
            for index, current in enumerate(self._pending):
                if current.raw.get("type") == "transcript.partial":
                    del self._pending[index]
                    break
            if len(self._pending) > 256:
                raise RuntimeError(
                    "Authoritative evidence projection backlog exceeded its safety bound"
                )

    async def _expire_after_grace(self) -> None:
        try:
            await asyncio.sleep(
                self.media_plane.configuration.authoritative_ingress_grace_seconds
            )
            if self._channel is None and self._control_grace_expired():
                await self.stop("control_reconnect_grace_expired")
        except asyncio.CancelledError:
            return
        finally:
            if self._expiry_task is asyncio.current_task():
                self._expiry_task = None

    async def _finish_after_endpoint(self, original: ClientSignal) -> None:
        try:
            await asyncio.sleep(self.supervisor.endpoint_delay_seconds)
            channel = self._channel or self._event_source
            if channel is None or not self._matches_endpoint(original):
                return
            signal = ClientSignal(
                type="evidence.finish",
                idempotency_key=new_id("endpoint"),
                turn_id=original.turn_id,
                causation_id=original.causation_id,
                payload={"endpoint": "semantic_timeout", "capture_id": original.payload["capture_id"], "endpoint_id": original.payload["endpoint_id"]},
            )
            await self.dispatch(channel, signal)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            source = self._event_source
            if source is not None:
                await source._problem(
                    exc,
                    turn_id=original.turn_id or self.chain.turn_id,
                    causation_id=original.causation_id,
                )
        finally:
            if self._endpoint_task is asyncio.current_task():
                self._endpoint_task = None

    async def _renew_ownership(self) -> None:
        try:
            while not self._stopped:
                loop = asyncio.get_running_loop()
                scheduled_at = loop.time() + self.supervisor.renew_interval_seconds
                await asyncio.sleep(self.supervisor.renew_interval_seconds)
                ownership = self.ownership
                if ownership is None:
                    return
                renew_started_at = loop.time()
                self._observe_renewal_metric(
                    "evidence_owner_renew_scheduler_lag_ms",
                    max(0.0, (renew_started_at - scheduled_at) * 1000.0),
                )
                renewed = False
                try:
                    self.ownership = self.supervisor.coordinator.renew(ownership)
                    renewed = True
                finally:
                    self._observe_renewal_metric(
                        "evidence_owner_renew_db_latency_ms",
                        max(0.0, (loop.time() - renew_started_at) * 1000.0),
                    )
                    self._observe_renewal_metric(
                        "evidence_owner_renew_success", 1.0 if renewed else 0.0
                    )
                if self._channel is None and self._control_grace_expired():
                    await self.stop("control_reconnect_grace_expired")
                    return
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await self._self_fence(exc)

    @staticmethod
    def _observe_renewal_metric(name: str, value: float) -> None:
        """Keep process observability outside the lease correctness boundary."""

        try:
            interview_agent_metrics().observe(name, value)
        except Exception:
            # A stale metrics vocabulary must never stop renewal or weaken the
            # database fence. Metrics carry no identifiers and are best effort.
            return

    async def _self_fence(self, exc: Exception) -> None:
        """Stop local side effects without attempting repair under a stale fence."""
        if self._answer_endpoint is not None:
            await self._answer_endpoint.close()
        if self._presentation_task is not None and self._presentation_task is not asyncio.current_task():
            self._presentation_task.cancel()
            await asyncio.gather(self._presentation_task, return_exceptions=True)

        if self._stopped:
            return
        self._stopped = True
        await self._shutdown_partial_projection()
        ingress = self._ingress
        self._ingress = None
        if ingress is not None:
            await ingress.close()
        await self.cancel_endpoint()
        if self.chain is not None:
            await self.chain.abort()
        renewal = self._renewal_task
        self._renewal_task = None
        if renewal is not None and renewal is not asyncio.current_task():
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
        executor = self._executor_task
        self._executor_task = None
        if executor is not None and executor is not asyncio.current_task():
            executor.cancel()
            await asyncio.gather(executor, return_exceptions=True)
        recovery = self._owner_recovery_task
        self._owner_recovery_task = None
        if recovery is not None and recovery is not asyncio.current_task():
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
        await self._fatal_ingress_problem("EVIDENCE_OWNER_FENCED", exc)
        self.supervisor._remove(self)

    async def _project_repaired_disconnect(
        self,
        outcome: EvidenceFinishResult,
        channel: "AgentChannel",
    ) -> None:
        await self._deactivate_partial_projection("formal", outcome.turn_id)
        for raw in outcome.events:
            raw = dict(raw)
            raw.setdefault("turn_id", outcome.turn_id)
            await channel._project_evidence_event(raw, None)
        await channel._after_formal_evidence_finished(
            outcome.turn_id,
            None,
            conversation_action_selected=any(
                item.get("type")
                in {"followup.selected", "utterance.not_accepted"}
                for item in outcome.events
            ),
            # The owner executor must first commit the durable command result.
            # It schedules session shutdown immediately after that receipt.
            stop_evidence_session=False,
        )

    async def _fatal_ingress_problem(
        self,
        code: str,
        exc: Exception,
        *,
        cause_code: Optional[str] = None,
        cause_type: Optional[str] = None,
        stage: Optional[str] = None,
    ) -> None:
        if self.ownership is not None:
            try:
                self.supervisor.coordinator.assert_current(self.ownership.commit_fence(), self.organization_id)
            except ApiError as fence_error:
                if fence_error.code in {"EVIDENCE_OWNER_FENCED", "EVIDENCE_OWNERSHIP_LOST"}:
                    return
                raise
        interviews = self.supervisor.interviews
        payload = {
            "code": code,
            "message": type(exc).__name__,
            "recoverable": False,
            "action": "pause_or_human_takeover",
        }
        if cause_code:
            payload["cause_code"] = cause_code
        if cause_type:
            payload["cause_type"] = cause_type
        if stage:
            payload["stage"] = stage
        interview = interviews.get_interview(
            self.interview_id, self.organization_id
        )
        existing = (interview.get("agent_runtime") or {}).get("problems") or []
        last = existing[-1] if existing else {}
        duplicate = bool(
            last.get("code") == payload["code"]
            and (not cause_code or last.get("cause_code") == cause_code)
            and interview.get("status") != "in_progress"
        )
        if not duplicate:
            interviews.record_agent_problem(
                self.interview_id, payload, self.organization_id
            )
            interview = interviews.get_interview(
                self.interview_id, self.organization_id
            )
        if interview.get("status") == "in_progress":
            interviews.pause_interview(
                self.interview_id,
                reason="authoritative_audio_ingress_failed",
                organization_id=self.organization_id,
            )

    def _candidate_ticket(self, connection_id: str) -> Dict[str, Any]:
        with self.persistence.transaction(self.organization_id) as transaction:
            ticket = next(
                (
                    item
                    for item in transaction.agent_tickets.list()
                    if item.get("interview_id") == self.interview_id
                    and item.get("connection_id") == connection_id
                    and item.get("participant_role") == "candidate"
                    and item.get("status") == "consumed"
                ),
                None,
            )
        if ticket is None:
            raise ApiError(
                "LIVEKIT_EVIDENCE_CONNECTION_INVALID",
                "Authoritative ingress requires the consumed candidate connection.",
                status_code=403,
            )
        return ticket

    def _control_grace_expired(self) -> bool:
        """Use database time so a remote attach/detach controls owner lifetime."""

        ownership = self.ownership
        if ownership is None:
            return False
        with self.persistence.transaction(self.organization_id) as transaction:
            now = transaction.database_now()
            current = transaction.evidence_ownerships.get(
                ownership.ownership_id
            )
        if current is None:
            return True
        if (
            current.get("owner_instance_id") != ownership.owner_instance_id
            or current.get("lease_id") != ownership.lease_id
            or int(current.get("ownership_epoch", 0))
            != ownership.ownership_epoch
        ):
            return True
        grace = current.get("reconnect_grace_expires_at")
        if not grace:
            return False
        try:
            expires = datetime.fromisoformat(
                str(grace).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            return True
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires.astimezone(timezone.utc) <= now.astimezone(timezone.utc)

    def _frozen_binding(self) -> Optional[Dict[str, Any]]:
        with self.persistence.transaction(self.organization_id) as transaction:
            interview = transaction.interview_sessions.get(self.interview_id)
        if interview is None:
            return None
        value = (interview.get("agent_runtime") or {}).get(
            "authoritative_media_binding"
        )
        return dict(value) if isinstance(value, dict) else None

    def _freeze_binding(
        self,
        *,
        candidate_identity: str,
        room_name: str,
        connection_id: str,
    ) -> None:
        with self.persistence.transaction(self.organization_id) as transaction:
            interview = transaction.interview_sessions.get(self.interview_id)
            if interview is None:
                raise ApiError(
                    "INTERVIEW_NOT_FOUND",
                    "Interview session does not exist.",
                    status_code=404,
                )
            runtime = interview.setdefault("agent_runtime", {})
            existing = runtime.get("authoritative_media_binding")
            if existing:
                if (
                    existing.get("candidate_identity") != candidate_identity
                    or existing.get("room_name") != room_name
                ):
                    raise ApiError(
                        "LIVEKIT_EVIDENCE_IDENTITY_CONFLICT",
                        "Frozen authoritative media binding cannot be replaced.",
                        status_code=409,
                    )
                return
            runtime["authoritative_media_binding"] = {
                "provider": "livekit",
                "room_name": room_name,
                "candidate_identity": candidate_identity,
                "connection_id": connection_id,
                "bound_at": utc_now(),
                "version": 1,
            }
            interview["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                interview, expected_version=interview["version"]
            )

    def _require_attached(
        self, channel: "AgentChannel"
    ) -> EvidenceOwnershipGrant:
        if self._channel is not channel:
            raise ApiError(
                "LIVEKIT_EVIDENCE_CONTROLLER_STALE",
                "This control connection no longer owns the candidate evidence commands.",
                status_code=409,
            )
        grant = self._control_grants.get(id(channel))
        if grant is None:
            raise ApiError(
                "EVIDENCE_CONTROL_STALE",
                "This candidate control connection has no active Evidence generation.",
                status_code=409,
            )
        self.supervisor.coordinator.assert_control_current(grant)
        if grant.is_local_owner:
            self._assert_owner()
        return grant

    def _assert_owner(self) -> None:
        if self.ownership is None:
            raise ApiError(
                "EVIDENCE_OWNER_UNAVAILABLE",
                "No authoritative Evidence ownership lease is active.",
                status_code=503,
            )
        self.supervisor.coordinator.assert_current(
            self.ownership.commit_fence(), self.organization_id
        )


class LiveKitEvidenceIngressSupervisor:
    """Process singleton for connection-independent candidate Evidence sessions."""

    def __init__(
        self,
        store: Any,
        *,
        media_plane: Optional[LiveKitMediaPlane] = None,
        ingress_factory: Any = LiveKitCandidateAudioIngress,
        evidence_chain_factory: Any = InterviewEvidenceChain,
        endpoint_delay_seconds: float = 2.5,
        instance_id: Optional[str] = None,
        coordinator: Optional[EvidenceOwnershipCoordinator] = None,
        journal: Optional[EvidenceCommandJournal] = None,
        command_poll_seconds: float = 0.02,
        turn_detector: Any = None,
    ) -> None:
        self.store = store
        self.persistence = persistence_for(store)
        self.interviews = InterviewService(store, persistence=self.persistence)
        self.media_plane = media_plane or LiveKitMediaPlane()
        self.ingress_factory = ingress_factory
        self.evidence_chain_factory = evidence_chain_factory
        runtime_python = os.getenv("INTERVIEWER_TURN_DETECTOR_PYTHON") or str(
            Path(__file__).resolve().parents[2] / ".runtime/turn-detector/bin/python")
        self.turn_detector = turn_detector or LocalAudioTurnDetector(runtime_python)
        self._detector_warmup_task: Optional[asyncio.Task] = None
        self.endpoint_delay_seconds = max(
            0.001, min(float(endpoint_delay_seconds), 2.5)
        )
        self.instance_id = instance_id or realtime_event_bus().instance_id
        lease_seconds = float(
            self.media_plane.configuration.authoritative_ingress_lease_seconds
        )
        self.renew_interval_seconds = max(
            0.05,
            min(
                float(
                    self.media_plane.configuration.authoritative_ingress_renew_seconds
                ),
                lease_seconds / 3.0,
            ),
        )
        self.coordinator = coordinator or EvidenceOwnershipCoordinator(
            store,
            persistence=self.persistence,
            lease_seconds=lease_seconds,
        )
        self.journal = journal or EvidenceCommandJournal(
            store,
            persistence=self.persistence,
            claim_seconds=max(lease_seconds, 10.0),
        )
        self.browser_backfill = BrowserAudioBackfill(
            store, persistence=self.persistence
        )
        self.command_poll_seconds = max(
            0.005, min(float(command_poll_seconds), 0.5)
        )
        self._sessions: Dict[tuple[str, str], ManagedLiveKitEvidenceSession] = {}

    @property
    def enabled(self) -> bool:
        return self.media_plane.configuration.authoritative_audio_ingress_ready()

    async def attach(self, channel: "AgentChannel") -> ManagedLiveKitEvidenceSession:
        key = (channel.organization_id, channel.interview_id)
        session = self._sessions.get(key)
        if session is None or session._stopped:
            session = ManagedLiveKitEvidenceSession(
                self, channel.interview_id, channel.organization_id
            )
            self._sessions[key] = session
        try:
            await session.attach(channel)
        except BaseException:
            if session.ownership is None or session._stopped:
                self._remove(session)
            raise
        return session

    def prewarm_turn_detector(self) -> None:
        if self._detector_warmup_task is None:
            self._detector_warmup_task = asyncio.create_task(self.turn_detector.predict(bytes(38_400)))

    async def stop_for_interview(
        self,
        interview_id: str,
        organization_id: str = "org_default",
        *,
        reason: str,
    ) -> None:
        """Stop the process-local authoritative Evidence owner, if present."""

        session = self._sessions.get((organization_id, interview_id))
        if session is not None:
            await session.stop(reason)

    async def shutdown(self) -> None:
        sessions = list(self._sessions.values())
        for session in sessions:
            await session.stop("application_shutdown")
        if self._detector_warmup_task is not None and not self._detector_warmup_task.done():
            self._detector_warmup_task.cancel()
            await asyncio.gather(self._detector_warmup_task, return_exceptions=True)
        await self.turn_detector.close()

    def _remove(self, session: ManagedLiveKitEvidenceSession) -> None:
        key = (session.organization_id, session.interview_id)
        if self._sessions.get(key) is session:
            self._sessions.pop(key, None)


_SUPERVISORS: "weakref.WeakSet[LiveKitEvidenceIngressSupervisor]" = (
    weakref.WeakSet()
)


def livekit_evidence_supervisor(store: Any) -> LiveKitEvidenceIngressSupervisor:
    supervisor = getattr(store, "_livekit_evidence_supervisor", None)
    if supervisor is None:
        supervisor = LiveKitEvidenceIngressSupervisor(store)
        setattr(store, "_livekit_evidence_supervisor", supervisor)
        _SUPERVISORS.add(supervisor)
    return supervisor


async def shutdown_livekit_evidence_supervisors() -> None:
    supervisors = list(_SUPERVISORS)
    _SUPERVISORS.clear()
    for supervisor in supervisors:
        await supervisor.shutdown()
