"""Long-lived LiveKit ingress bound to the InterviewAgent Evidence chain.

The supervisor intentionally outlives one control WebSocket. Within the
configured reconnect grace window, LiveKit audio, streaming STT and the private
recording keep their single authoritative identity while a replacement control
channel attaches and resumes event projection.
"""

from __future__ import annotations

import asyncio
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
from app.services.evidence_coordination import EvidenceOwnershipCoordinator
from app.services.evidence_command_journal import EvidenceCommandJournal
from app.services.browser_audio_backfill import BrowserAudioBackfill
from app.services.interview_evidence import (
    EvidenceAudioResult,
    EvidenceFinishResult,
    InterviewEvidenceChain,
)
from app.services.interviews import InterviewService

if TYPE_CHECKING:
    from app.services.interview_agent import AgentChannel


_FAIL_CLOSED_MEDIA_ERRORS = frozenset(
    {
        "EVIDENCE_MEDIA_INCOMPLETE",
        "EVIDENCE_MEDIA_NOT_RECOVERABLE",
        "EVIDENCE_MEDIA_SEGMENT_GAP",
        "EVIDENCE_MEDIA_SEGMENT_INVALID",
        "EVIDENCE_MEDIA_CHECKSUM_MISMATCH",
        "EVIDENCE_MEDIA_FORMAT_NOT_RECOVERABLE",
        "EVIDENCE_MEDIA_RECOVERY_INVALID",
        "BROWSER_BACKFILL_FRAME_INVALID",
        "BROWSER_BACKFILL_FRAME_UNAVAILABLE",
        "BROWSER_BACKFILL_CHECKSUM_MISMATCH",
    }
)
_TERMINAL_INTERVIEW_STATUSES = frozenset(
    {"completed", "report_generating", "report_ready"}
)
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
        self._expiry_task: Optional[asyncio.Task[None]] = None
        self._endpoint_task: Optional[asyncio.Task[None]] = None
        self._renewal_task: Optional[asyncio.Task[None]] = None
        self._executor_task: Optional[asyncio.Task[None]] = None
        self._owner_recovery_task: Optional[asyncio.Task[None]] = None
        self._command_wake = asyncio.Event()
        self._stopped = False
        self.last_ingress_state = "not_started"

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

    async def _finish_with_channel(
        self, channel: "AgentChannel", signal: ClientSignal
    ) -> None:
        result = await self.chain.finish(signal.payload)
        if result.kind == "warmup":
            await channel._complete_warmup_evidence(result, signal)
            return
        for raw in result.events:
            raw = dict(raw)
            raw.setdefault("turn_id", result.turn_id)
            await channel._project_evidence_event(raw, signal.causation_id)
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

    async def _schedule_endpoint(self, signal: ClientSignal) -> None:
        await self.cancel_endpoint()
        self._endpoint_task = asyncio.create_task(
            self._finish_after_endpoint(signal)
        )

    async def cancel_endpoint(self) -> None:
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
            payload = {}
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
            {"owner": FloorOwner.CANDIDATE.value, "reason": reason},
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
                await self._deliver(
                    _PendingProjection(result.kind, raw, result.turn_id)
                )
            return claimed.turn_id
        if claimed.command_type == "evidence.seal":
            await self.cancel_endpoint()
            # 试音流没有正式 turn_id，但同样必须由 2.5 秒端点命令收口。
            # 先处理 warmup，不能落入下面“正式回答必须绑定题目”的校验。
            if self.chain.is_open and self.chain.kind == "warmup":
                try:
                    result = await self.chain.finish(signal.payload)
                    await source._complete_warmup_evidence(result, signal)
                except Exception as exc:
                    failure = await self._recover_failed_warmup_finalize(
                        source, signal, exc
                    )
                    raise failure from exc
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
                    await self._finish_with_channel(source, signal)
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
    ) -> ApiError:
        """Make one consumed warm-up final terminal but explicitly retryable by user."""

        raw_code = str(getattr(exc, "code", "WARMUP_STT_PROBLEM")).upper()
        error_code = "".join(
            character if character.isalnum() or character == "_" else "_"
            for character in raw_code
        )[:128].strip("_") or "WARMUP_STT_PROBLEM"
        source.runtime._set_calibration(
            self.interview_id,
            "retrying",
            self.organization_id,
            retry_required=True,
        )
        await source._set_floor(
            FloorOwner.CANDIDATE, "warmup_retry", signal.causation_id
        )
        problem = {
            "code": error_code,
            "message": type(exc).__name__,
            "recoverable": True,
            "action": "retry_warmup",
            "calibration": True,
        }
        source.runtime._record_problem(
            self.interview_id, problem, self.organization_id
        )
        await source._emit(
            "problem",
            problem,
            turn_id=None,
            causation_id=signal.causation_id,
            replayability=Replayability.REPLAYABLE,
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

    async def _apply_open(
        self, source: "AgentChannel", signal: ClientSignal
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
        result = await self.chain.open(
            signal.payload,
            turn_id=signal.turn_id,
            calibration_status=calibration,
        )
        for raw in result.events:
            await self._deliver(
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

    async def _apply_speech_started(
        self, source: "AgentChannel", signal: ClientSignal
    ) -> Optional[str]:
        await self.cancel_endpoint()
        session = source.runtime.interviews.get_interview(
            self.interview_id, self.organization_id
        )
        runtime_state = session.get("agent_runtime") or {}
        effective_turn_id = signal.turn_id or session.get("current_turn_id")
        if runtime_state.get("calibration_status") == "opening":
            source.runtime._set_calibration(
                self.interview_id, "listening", self.organization_id
            )
        if runtime_state.get("floor") == FloorOwner.AGENT.value:
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
        effective_turn_id = signal.turn_id or self.turn_id
        await source._emit(
            "speech.stopped",
            {
                "speaker": "candidate",
                "endpoint_countdown_ms": 2500,
                "cancellable": True,
            },
            turn_id=effective_turn_id,
            causation_id=signal.causation_id,
            replayability=Replayability.TRANSIENT,
        )
        await self._schedule_endpoint(signal)
        return effective_turn_id

    async def _apply_continue(
        self, source: "AgentChannel", signal: ClientSignal
    ) -> Optional[str]:
        await self.cancel_endpoint()
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

    async def stop(self, reason: str) -> None:
        if self._stopped:
            return
        self._stopped = True
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
        result = await self.chain.send_audio(frame.pcm_s16le)
        if result is None:
            return
        async with self._delivery_lock:
            if result.first_server_audio:
                await self._deliver_locked(
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
                await self._deliver_locked(
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
            await self._fatal_ingress_problem(
                "LIVEKIT_EVIDENCE_%s" % state.upper(), RuntimeError(state)
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
            if channel is None or not self.chain.is_open:
                return
            signal = ClientSignal(
                type="evidence.finish",
                idempotency_key=new_id("endpoint"),
                turn_id=original.turn_id or self.chain.turn_id,
                causation_id=original.causation_id,
                payload={"endpoint": "semantic_timeout"},
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
                await asyncio.sleep(self.supervisor.renew_interval_seconds)
                ownership = self.ownership
                if ownership is None:
                    return
                self.ownership = self.supervisor.coordinator.renew(ownership)
                if self._channel is None and self._control_grace_expired():
                    await self.stop("control_reconnect_grace_expired")
                    return
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await self._self_fence(exc)

    async def _self_fence(self, exc: Exception) -> None:
        """Stop local side effects without attempting repair under a stale fence."""

        if self._stopped:
            return
        self._stopped = True
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

    async def _fatal_ingress_problem(self, code: str, exc: Exception) -> None:
        interviews = self.supervisor.interviews
        interviews.record_agent_problem(
            self.interview_id,
            {
                "code": code,
                "message": type(exc).__name__,
                "recoverable": False,
                "action": "pause_or_human_takeover",
            },
            self.organization_id,
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
    ) -> None:
        self.store = store
        self.persistence = persistence_for(store)
        self.interviews = InterviewService(store, persistence=self.persistence)
        self.media_plane = media_plane or LiveKitMediaPlane()
        self.ingress_factory = ingress_factory
        self.evidence_chain_factory = evidence_chain_factory
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

    async def shutdown(self) -> None:
        sessions = list(self._sessions.values())
        for session in sessions:
            await session.stop("application_shutdown")

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
