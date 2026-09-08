"""Real-time InterviewAgentRuntime deep module.

This module owns floor, evidence orchestration, replay/idempotency and audited
takeover. WebSocket handlers and React are intentionally thin adapters.
"""

from __future__ import annotations

import asyncio
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from app.core.auth import Principal
from app.core.errors import ApiError
from app.services.capture_recovery import candidate_capture_recovery
from app.core.ids import new_id
from app.core.interview_agent_metrics import (
    CANDIDATE_INTERVIEW_AGENT_METRICS,
    interview_agent_metrics,
    measure_interview_agent_stage,
)
from app.core.time import utc_now
from app.domain.interview_agent import (
    ApprovedConversationAct,
    AgentEvent,
    AvatarPerformance,
    ClientSignal,
    FloorOwner,
    OpenAgentSession,
    Replayability,
    TakeoverLease,
)
from app.model_gateway import capabilities as cap
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import TTSSynthesizeRequest
from app.persistence.provider import persistence_for
from app.realtime_bus import realtime_event_bus
from app.services.avatar import AvatarService
from app.services.avatar_performance import AvatarPerformanceComposer
from app.services.agent_expression_audio import AgentExpressionAudioService
from app.services.approved_speech_output import ApprovedSpeechOutput
from app.adapters.livekit_audio_output import LiveKitApprovedAudioPublisher
from app.adapters.livekit_media import LiveKitMediaPlane
from app.services.interviews import InterviewService
from app.services.interview_evidence import EvidenceFinishResult
from app.services.livekit_evidence_ingress import (
    ManagedLiveKitEvidenceSession,
    livekit_evidence_supervisor,
)
from app.services.livekit_room_binding import interview_room_name
from app.services.media_capture import InterviewMediaCaptureService
from app.services.streaming_stt import StreamingInterviewSTT
from app.services.warmup_calibration import WarmupCalibrationStream


_CLOSE = object()
_AGENT_QUEUE_CAPACITY = 2048
_DURABLE_EVIDENCE_SIGNALS = {
    "evidence.stream.open",
    "evidence.finish",
    "finish_answer",
    "speech.started",
    "speech.stopped",
    "continue_speaking",
    "meta.not_finished",
    "warmup.retry",
    "evidence.recovery.chunk",
}
_STALE_EVIDENCE_CONTROL_ERRORS = frozenset(
    {"EVIDENCE_CONTROL_STALE", "LIVEKIT_EVIDENCE_CONTROLLER_STALE"}
)


class _AgentChannelHub:
    """Process-local fan-out with per-recipient security projection.

    Redis remains the cross-process deployment seam. Keeping the hub here
    prevents one WebSocket connection from becoming an event silo and, more
    importantly, prevents a privileged snapshot from entering shared replay
    history.
    """

    def __init__(self) -> None:
        self._channels: Dict[tuple[int, str, str], set["AgentChannel"]] = {}
        self._bus = realtime_event_bus()

    @staticmethod
    def _key(channel: "AgentChannel") -> tuple[int, str, str]:
        return (
            id(channel.runtime.store),
            channel.organization_id,
            channel.interview_id,
        )

    def register(self, channel: "AgentChannel") -> None:
        self._channels.setdefault(self._key(channel), set()).add(channel)

    def unregister(self, channel: "AgentChannel") -> None:
        key = self._key(channel)
        values = self._channels.get(key)
        if values is None:
            return
        values.discard(channel)
        if not values:
            self._channels.pop(key, None)

    async def publish(
        self,
        source: "AgentChannel",
        event: AgentEvent,
        *,
        live_payload: Dict[str, Any],
    ) -> None:
        for channel in list(self._channels.get(self._key(source), ())):
            if channel._closed or channel._terminating:
                continue
            try:
                projected = channel.runtime._project_event_for_principal(
                    event,
                    channel.principal,
                    interview_id=channel.interview_id,
                    live_payload=live_payload,
                )
                channel._enqueue_event(projected)
            except Exception:
                # Projection is a security boundary. Never fall back to the raw
                # payload, and never let one bad recipient block healthy peers.
                channel._terminate_transport("event_projection_failed")

    async def publish_cross_instance(
        self, source: "AgentChannel", event: AgentEvent
    ) -> None:
        if not self._bus.enabled:
            return
        try:
            await self._bus.publish(
                source.interview_id,
                {
                    "transport": "interview_agent.v1",
                    "organization_id": source.organization_id,
                    # _emit persisted this exact least-privileged payload. The
                    # privileged live payload never enters Redis Pub/Sub.
                    "event": event.model_dump(mode="json"),
                },
            )
        except Exception as exc:
            source.runtime._record_problem(
                source.interview_id,
                {
                    "code": "AGENT_CROSS_INSTANCE_FANOUT_FAILED",
                    "message": type(exc).__name__,
                    "recoverable": False,
                    "action": "pause_or_human_takeover",
                },
                source.organization_id,
            )

    async def receive_cross_instance(
        self,
        interview_id: str,
        organization_id: str,
        event: AgentEvent,
    ) -> None:
        for key, channels in list(self._channels.items()):
            _, channel_organization_id, channel_interview_id = key
            if (
                channel_organization_id != organization_id
                or channel_interview_id != interview_id
            ):
                continue
            for channel in list(channels):
                if channel._closed or channel._terminating:
                    continue
                try:
                    projected = channel.runtime._project_event_for_principal(
                        event,
                        channel.principal,
                        interview_id=channel.interview_id,
                    )
                    channel._enqueue_event(projected)
                except Exception:
                    channel._terminate_transport("remote_event_projection_failed")


_AGENT_CHANNEL_HUB = _AgentChannelHub()


async def receive_remote_agent_event(
    interview_id: str, envelope: Dict[str, Any]
) -> bool:
    """Route one Redis event to local AgentChannels when it is agent.v1."""

    if envelope.get("transport") != "interview_agent.v1":
        return False
    organization_id = str(envelope.get("organization_id") or "")
    if not organization_id:
        return True
    try:
        event = AgentEvent.model_validate(envelope.get("event") or {})
    except Exception:
        # Untrusted/old cross-instance payloads fail closed and are never sent
        # to candidates or enterprise monitors.
        return True
    event = event.model_copy(
        update={
            "payload": InterviewAgentRuntime._safe_replay_payload(
                event.type, event.payload
            )
        }
    )
    await _AGENT_CHANNEL_HUB.receive_cross_instance(
        interview_id, organization_id, event
    )
    return True


async def consume_interview_agent_event_bus() -> None:
    """Subscribe only to the stable Agent envelope; legacy events are dropped."""

    bus = realtime_event_bus()

    async def dispatch(interview_id: str, envelope: Dict[str, Any]) -> None:
        await receive_remote_agent_event(interview_id, envelope)

    await bus.subscribe(dispatch)


class AgentChannel:
    """One authenticated connection to the runtime."""

    def __init__(self, runtime: "InterviewAgentRuntime", opened: OpenAgentSession) -> None:
        self.runtime = runtime
        self.opened = opened
        self.interview_id = opened.interview_id
        self.principal: Principal = opened.principal
        self.organization_id = self.principal.organization_id
        self._queue: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=_AGENT_QUEUE_CAPACITY
        )
        self._lock = asyncio.Lock()
        self._closed = False
        self._terminating = False
        self._termination_task: Optional[asyncio.Task[None]] = None
        self._endpoint_task: Optional[asyncio.Task[None]] = None
        self._completion_task: Optional[asyncio.Task[None]] = None
        self._stt: Optional[StreamingInterviewSTT] = None
        self._warmup: Optional[WarmupCalibrationStream] = None
        self._evidence_session: Optional[ManagedLiveKitEvidenceSession] = None
        self._server_audio_seen = False
        self._takeover_watchdog_task: Optional[asyncio.Task[None]] = None
        self._speech_output: Optional[ApprovedSpeechOutput] = None
        self._speech_output_task: Optional[asyncio.Task[None]] = None

    async def initialize(self) -> None:
        session = self.runtime.interviews.get_interview(
            self.interview_id, self.organization_id
        )
        self.runtime._validate_open(session, self.opened)
        runtime_state = session.get("agent_runtime") or {}
        history_floor = int(runtime_state.get("replay_history_floor", 0))
        history = session.get("agent_events", [])
        if not history_floor and len(history) >= 1000:
            history_floor = max(
                0, int(history[0].get("session_sequence", 1)) - 1
            )
        recovery_gap = self.opened.recovery_cursor < history_floor
        replay = [
            AgentEvent.model_validate(item)
            for item in history
            if int(item.get("session_sequence", 0)) > self.opened.recovery_cursor
            and item.get("replayability") == Replayability.REPLAYABLE.value
            and item.get("type") != "session.snapshot"
        ]
        active_performance_id = (session.get("agent_runtime") or {}).get(
            "active_performance_id"
        )
        for event in replay:
            if (
                event.type == "avatar.performance.started"
                and event.payload.get("performance_id") != active_performance_id
            ):
                continue
            self._enqueue_event(
                self.runtime._project_event_for_principal(
                    event, self.principal, interview_id=self.interview_id
                )
            )
        if recovery_gap:
            gap_event = self.runtime._append_event(
                self.interview_id,
                "problem",
                {
                    "code": "AGENT_RECOVERY_GAP",
                    "message": "部分旧实时事件已过期，客户端必须以最新会话快照重新同步。",
                    "recoverable": True,
                    "action": "resync_from_snapshot",
                },
                turn_id=None,
                causation_id=None,
                replayability=Replayability.TRANSIENT,
                organization_id=self.organization_id,
            )
            self._enqueue_event(
                self.runtime._project_event_for_principal(
                    gap_event,
                    self.principal,
                    interview_id=self.interview_id,
                )
            )
        await self._queue_fresh_snapshot()
        # Do not expose the channel to live fan-out until its ordered recovery
        # stream and fresh role-projected snapshot have been queued. None of
        # the operations above yields, so a live event cannot overtake them.
        _AGENT_CHANNEL_HUB.register(self)
        takeover = runtime_state.get("takeover")
        if takeover:
            self._schedule_takeover_watchdog(takeover)
        if (
            "candidate" in self.principal.roles
            and self.runtime.evidence_ingress.enabled
            and session.get("status") == "in_progress"
        ):
            try:
                self._evidence_session = await self.runtime.evidence_ingress.attach(
                    self
                )
            except BaseException:
                _AGENT_CHANNEL_HUB.unregister(self)
                raise

    async def send(self, signal: ClientSignal) -> None:
        if self._closed or self._terminating:
            raise ApiError("AGENT_CHANNEL_CLOSED", "Agent channel is closed.", status_code=409)
        if signal.type == "telemetry.observe":
            await self._observe_process_telemetry(signal)
            return
        async with self._lock:
            durable_evidence = bool(
                self._evidence_session is not None
                and signal.type in _DURABLE_EVIDENCE_SIGNALS
            )
            if not durable_evidence and self.runtime._signal_seen(
                self.interview_id,
                signal.idempotency_key,
                self.organization_id,
            ):
                if signal.type == "evidence.audio.chunk":
                    await self._emit_audio_ack(signal, duplicate=True)
                return
            await self._expire_takeover_if_needed(signal.causation_id)
            try:
                if durable_evidence:
                    # Check control ownership before lifecycle validation so a
                    # superseded socket cannot spam INTERVIEW_NOT_IN_PROGRESS
                    # after a replacement connection has taken over.
                    self._evidence_session.assert_controller(self)
                await self._handle(signal)
            except Exception as exc:
                if getattr(exc, "code", None) in _STALE_EVIDENCE_CONTROL_ERRORS:
                    # This is a connection-local fencing result, not a domain
                    # problem. End only the superseded transport; persisting or
                    # broadcasting one problem per stale VAD signal would bury
                    # useful bounded history and disturb the new controller.
                    self._terminate_transport("superseded_evidence_controller")
                    return
                await self._problem(
                    exc,
                    turn_id=signal.turn_id,
                    causation_id=signal.causation_id,
                )
            if not durable_evidence:
                self.runtime._remember_signal(
                    self.interview_id,
                    signal.idempotency_key,
                    self.organization_id,
                )

    async def _observe_process_telemetry(self, signal: ClientSignal) -> None:
        """Sample best-effort process telemetry without touching domain state."""

        try:
            self.runtime._require_candidate(self.principal)
            metric = signal.payload.get("metric")
            value = signal.payload.get("value")
            if (
                not isinstance(metric, str)
                or metric not in CANDIDATE_INTERVIEW_AGENT_METRICS
                or isinstance(value, bool)
                or value is None
            ):
                return
            interview_agent_metrics().observe(metric, float(value))
        except ApiError:
            raise
        except (TypeError, ValueError, OverflowError):
            # Browser telemetry is untrusted and non-authoritative. Invalid
            # samples must neither become a domain problem nor pause a session.
            return
        finally:
            # A buffered client can deliver hundreds of viseme samples at once.
            # Yield explicitly so ownership renewal and media tasks stay fair.
            await asyncio.sleep(0)

    async def events(self) -> AsyncIterator[AgentEvent]:
        while True:
            item = await self._queue.get()
            if item is _CLOSE:
                return
            yield item

    async def close(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self._terminating = True
        failure: Optional[BaseException] = None
        if self._speech_output is not None:
            try:
                self.runtime._mark_streamed_expression_for_replay(
                    self.interview_id, self.organization_id,
                    performance_id=self._speech_output.performance_id,
                    output_id=self._speech_output.output_id,
                )
            except BaseException as exc:
                failure = exc
        try:
            await self._cancel_speech_output()
        except BaseException as exc:
            if failure is None:
                failure = exc
        endpoint_task = self._endpoint_task
        self._endpoint_task = None
        if endpoint_task:
            endpoint_task.cancel()
            if endpoint_task is not asyncio.current_task():
                try:
                    await asyncio.gather(endpoint_task, return_exceptions=True)
                except BaseException as exc:
                    failure = exc
        completion_task = self._completion_task
        self._completion_task = None
        if completion_task:
            completion_task.cancel()
            if completion_task is not asyncio.current_task():
                try:
                    await asyncio.gather(completion_task, return_exceptions=True)
                except BaseException as exc:
                    if failure is None:
                        failure = exc
        try:
            if self._evidence_session is not None:
                evidence_session = self._evidence_session
                self._evidence_session = None
                await evidence_session.detach(self)
        except BaseException as exc:
            if failure is None:
                failure = exc
        try:
            if self._stt:
                stt = self._stt
                self._stt = None
                recovered = await stt.close(repair_disconnect=True)
                for raw in recovered:
                    await self._project_evidence_event(raw, causation_id=None)
        except BaseException as exc:  # cleanup must also survive cancellation
            failure = exc
        try:
            if self._warmup:
                warmup = self._warmup
                self._warmup = None
                await warmup.abort()
        except BaseException as exc:  # preserve the first shutdown failure
            if failure is None:
                failure = exc
        try:
            self.runtime._record_disconnect(
                self.interview_id,
                self.opened.connection_id,
                reason,
                self.organization_id,
            )
        except BaseException as exc:
            if failure is None:
                failure = exc
        finally:
            _AGENT_CHANNEL_HUB.unregister(self)
            self._clear_queue()
            self._queue.put_nowait(_CLOSE)
        if failure is not None:
            raise failure

    def _enqueue_event(self, event: AgentEvent) -> None:
        """Bound one connection without ever discarding replayable history.

        High-frequency transient updates may replace an older transient item.
        If a slow consumer fills the queue entirely with replayable facts, its
        transport is terminated; it can recover those facts from its last
        acknowledged cursor instead of exhausting server memory.
        """

        if self._closed or self._terminating:
            return
        try:
            self._queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        if self._drop_oldest_transient():
            self._queue.put_nowait(event)
            return
        if event.replayability == Replayability.TRANSIENT:
            return
        self._terminate_transport("replayable_event_backpressure")

    def _drop_oldest_transient(self) -> bool:
        # asyncio.Queue has no public selective-discard operation. This small,
        # synchronous section owns its queue and removes one deque entry in
        # place, avoiding an O(capacity) drain/reinsert cycle for every partial.
        for index, item in enumerate(self._queue._queue):
            if (
                isinstance(item, AgentEvent)
                and item.replayability == Replayability.TRANSIENT
            ):
                del self._queue._queue[index]
                if self._queue._unfinished_tasks > 0:
                    self._queue._unfinished_tasks -= 1
                    if self._queue._unfinished_tasks == 0:
                        self._queue._finished.set()
                return True
        return False

    def _clear_queue(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()

    def _terminate_transport(self, reason: str) -> None:
        if self._closed or self._terminating:
            return
        self._terminating = True
        _AGENT_CHANNEL_HUB.unregister(self)
        self._clear_queue()
        self._queue.put_nowait(_CLOSE)
        # Queue backpressure and projection failures are synchronous fan-out
        # boundaries, but Evidence/endpoint cleanup is async. Schedule the
        # complete close path instead of leaking STT, warmup or endpoint tasks.
        self._termination_task = asyncio.create_task(
            self._finish_transport_termination(reason)
        )

    async def _finish_transport_termination(self, reason: str) -> None:
        try:
            await self.close(reason)
        except BaseException as exc:
            # This runs without a request waiter. Keep the failure observable,
            # but never emit raw exception text to role-projected clients.
            try:
                self.runtime._record_problem(
                    self.interview_id,
                    {
                        "code": "AGENT_TRANSPORT_CLEANUP_FAILED",
                        "message": type(exc).__name__,
                        "recoverable": False,
                        "action": "pause_or_human_takeover",
                    },
                    self.organization_id,
                )
            except Exception:
                pass
        finally:
            self._termination_task = None

    async def _handle(self, signal: ClientSignal) -> None:
        kind = signal.type
        if kind == "client.ready":
            session = self.runtime.interviews.get_interview(
                self.interview_id, self.organization_id
            )
            if session.get("status") != "in_progress":
                # 暂停或已完成会话仍允许只读重连查看状态，但不能重新触发开场。
                await self._emit_snapshot(signal.causation_id)
                return
            self.runtime.interviews.mark_participant_ready(
                self.interview_id,
                participant=self.runtime._participant_role(self.principal),
                source="agent_channel",
                organization_id=self.organization_id,
            )
            await self._emit_snapshot(signal.causation_id)
            if "candidate" in self.principal.roles:
                if self._evidence_session is not None:
                    self._evidence_session.assert_controller(self)
                if self._speech_output is None:
                    replay = self.runtime._recover_streamed_expression(
                        self.interview_id, self.organization_id,
                    )
                    if replay is not None:
                        await self._select_act(
                            act_type=replay["payload"]["act_type"],
                            text=replay["payload"]["text"], turn_id=replay.get("turn_id"),
                            causation_id=signal.causation_id, evidence_refs=[],
                            gesture="look_at_candidate",
                        )
                        return
                session = self.runtime.interviews.get_interview(
                    self.interview_id, self.organization_id
                )
                calibration = (session.get("agent_runtime") or {}).get(
                    "calibration_status", "pending"
                )
                if calibration == "pending":
                    self.runtime._set_calibration(
                        self.interview_id,
                        "opening",
                        self.organization_id,
                    )
                    name = session.get("candidate", {}).get("name") or "你好"
                    estimated_minutes = int(
                        (session.get("plan_snapshot") or {}).get("estimated_minutes") or 0
                    )
                    duration_notice = (
                        "预计用时约%d分钟。" % estimated_minutes
                        if estimated_minutes > 0
                        else "面试时长以当前题目进度为准。"
                    )
                    recording_notice = (
                        "本场将按你已同意的范围录制%s。"
                        % (
                            "音频和视频"
                            if session.get("settings", {}).get("record_video", False)
                            else "音频"
                        )
                        if session.get("settings", {}).get("record_audio", True)
                        or session.get("settings", {}).get("record_video", False)
                        else "本场不录音录像。"
                    )
                    await self._select_act(
                        act_type="opening",
                        text=(
                            "%s，欢迎参加本次面试。%s%s结果会由企业人员最终审核。"
                            "你可以随时打断、要求重读或暂停。我们先进行一轮不评分、"
                            "不进入正式录像的试音，确认我确实听懂了你。"
                            % (name, duration_notice, recording_notice)
                        ),
                        turn_id=None,
                        causation_id=signal.causation_id,
                        evidence_refs=[],
                        gesture="look_at_candidate",
                    )
            return
        if kind == "media.published":
            self.runtime._require_candidate(self.principal)
            self._require_active_interview()
            if self._evidence_session is not None:
                self._evidence_session.assert_controller(self)
                await self._evidence_session.ensure_ingress(
                    self.opened.connection_id
                )
            # Attach the receive-only Evidence subscriber immediately, but do
            # not start Participant Egress here. Warm-up calibration must be
            # disposable and is explicitly outside the consented formal
            # recording. Egress starts only after warmup.confirm below.
            await self._emit_snapshot(signal.causation_id)
            return
        if kind == "warmup.confirm":
            await self._confirm_warmup(signal)
            return
        if kind == "warmup.retry":
            await self._retry_warmup(signal)
            return
        if kind in {"request_repeat", "meta.request_repeat"}:
            await self._repeat_current(signal)
            return
        if kind in {"pause", "meta.pause"}:
            if not self.principal.roles.intersection(
                {"candidate", "admin", "interviewer"}
            ):
                raise ApiError(
                    "INTERVIEW_PAUSE_FORBIDDEN",
                    "This principal cannot pause an interview.",
                    status_code=403,
                )
            self.runtime.interviews.pause_interview(
                self.interview_id,
                reason="candidate_requested_pause",
                organization_id=self.organization_id,
            )
            if self._evidence_session is not None:
                await self._evidence_session.pause_capture()
            await self._cancel_speech_output()
            self.runtime._clear_active_performance(self.interview_id, None, self.organization_id)
            await self._set_floor(FloorOwner.NONE, "candidate_pause", signal.causation_id)
            await self._emit_snapshot(signal.causation_id)
            return
        if kind in {"continue_speaking", "meta.not_finished"}:
            self.runtime._require_candidate(self.principal)
            self._require_active_interview()
            if self._evidence_session is not None:
                await self._evidence_session.dispatch(self, signal)
                return
            if self._endpoint_task:
                self._endpoint_task.cancel()
                self._endpoint_task = None
            await self._set_floor(FloorOwner.CANDIDATE, "candidate_continues", signal.causation_id)
            await self._emit(
                "speech.started",
                {"speaker": "candidate", "continued": True, "local_detected": True},
                turn_id=signal.turn_id,
                causation_id=signal.causation_id,
                replayability=Replayability.TRANSIENT,
            )
            return
        if kind == "speech.started":
            await self._candidate_speech_started(signal)
            return
        if kind == "speech.stopped":
            await self._candidate_speech_stopped(signal)
            return
        if kind == "evidence.stream.open":
            await self._open_evidence(signal)
            return
        if kind == "evidence.audio.chunk":
            await self._send_audio(signal)
            return
        if kind == "evidence.recovery.begin":
            self.runtime._require_candidate(self.principal)
            if self._evidence_session is None:
                raise ApiError(
                    "BROWSER_BACKFILL_LIVEKIT_REQUIRED",
                    "Browser recovery exists only beside the formal LiveKit Evidence path.",
                    status_code=409,
                )
            await self._evidence_session.begin_browser_backfill(self, signal)
            return
        if kind == "evidence.recovery.chunk":
            self.runtime._require_candidate(self.principal)
            if self._evidence_session is None:
                raise ApiError(
                    "BROWSER_BACKFILL_LIVEKIT_REQUIRED",
                    "Browser recovery exists only beside the formal LiveKit Evidence path.",
                    status_code=409,
                )
            await self._evidence_session.dispatch_browser_backfill(self, signal)
            return
        if kind == "evidence.recovery.complete":
            self.runtime._require_candidate(self.principal)
            if self._evidence_session is None:
                raise ApiError(
                    "BROWSER_BACKFILL_LIVEKIT_REQUIRED",
                    "Browser recovery exists only beside the formal LiveKit Evidence path.",
                    status_code=409,
                )
            await self._evidence_session.complete_browser_backfill(self, signal)
            return
        if kind in {"evidence.finish", "finish_answer"}:
            await self._finish_evidence(signal)
            return
        if kind == "avatar.performance.playback":
            self.runtime._require_candidate(self.principal)
            if self._evidence_session is not None:
                self._evidence_session.assert_controller(self)
            session = self.runtime.interviews.get_interview(self.interview_id, self.organization_id)
            state = session.get("agent_runtime") or {}
            if (session.get("current_turn_id") == signal.turn_id
                    and state.get("active_performance_id") == signal.payload["performance_id"]):
                from app.core.speech_diagnostics import record_turn_control
                record_turn_control("audio_playback", interview_id=self.interview_id,
                                    turn_id=signal.turn_id, **signal.payload)
                await self._emit("avatar.performance.playback", dict(signal.payload),
                    turn_id=signal.turn_id, causation_id=signal.causation_id,
                    replayability=Replayability.TRANSIENT)
            return
        if kind == "avatar.performance.ready":
            self.runtime._require_candidate(self.principal)
            if self._evidence_session is not None:
                self._evidence_session.assert_controller(self)
            if self._speech_output is not None:
                self._speech_output.acknowledge_ready(
                    str(signal.payload.get("performance_id") or ""),
                    str(signal.payload.get("output_id") or ""),
                )
            return
        if kind == "avatar.performance.stopped":
            self.runtime._require_candidate(self.principal)
            session = self.runtime.interviews.get_interview(
                self.interview_id, self.organization_id
            )
            runtime_state = session.get("agent_runtime") or {}
            stopped_performance_id = str(signal.payload.get("performance_id") or "")
            closing_performance_id = str(
                runtime_state.get("completion_closing_performance_id") or ""
            )
            if runtime_state.get("completion_emitted_at"):
                return
            if not stopped_performance_id:
                return
            if runtime_state.get("active_output_id"):
                if self._evidence_session is not None:
                    self._evidence_session.assert_controller(self)
                if self._speech_output is None or not self._speech_output.acknowledge_drained(
                    stopped_performance_id, str(signal.payload.get("output_id") or ""),
                    str(signal.payload.get("reason") or ""),
                ):
                    # A legacy or early stop cannot finish a streamed utterance.
                    return
            if not self.runtime._clear_active_performance(
                self.interview_id,
                stopped_performance_id,
                self.organization_id,
            ):
                # Browser media callbacks may arrive after a replacement
                # performance has started.  A stale stop must have no effect on
                # the current floor, calibration, or completion state.
                return
            await self._emit(
                "avatar.performance.stopped",
                {"performance_id": signal.payload.get("performance_id"), "reason": "completed"},
                turn_id=signal.turn_id,
                causation_id=signal.causation_id,
                replayability=Replayability.TRANSIENT,
            )
            if closing_performance_id and stopped_performance_id == closing_performance_id:
                await self._finalize_completion(
                    causation_id=signal.causation_id,
                    reason="farewell_completed",
                )
                return
            session = self.runtime.interviews.get_interview(
                self.interview_id, self.organization_id
            )
            calibration = (session.get("agent_runtime") or {}).get(
                "calibration_status"
            )
            if calibration == "opening":
                self.runtime._set_calibration(
                    self.interview_id,
                    "listening",
                    self.organization_id,
                )
                await self._set_floor(
                    FloorOwner.CANDIDATE, "warmup_listening", signal.causation_id
                )
            elif calibration == "awaiting_confirmation":
                await self._set_floor(
                    FloorOwner.CANDIDATE,
                    "warmup_awaiting_confirmation",
                    signal.causation_id,
                )
            else:
                await self._set_floor(
                    FloorOwner.CANDIDATE, "agent_finished", signal.causation_id
                )
            return
        if kind == "takeover.acquire":
            await self._acquire_takeover(signal)
            return
        if kind == "takeover.renew":
            await self._renew_takeover(signal)
            return
        if kind == "takeover.release":
            await self._release_takeover(signal)
            return
        if kind == "human.speech":
            await self._human_speech(signal)
            return
        if kind == "ping":
            self.runtime.interviews.record_heartbeat(
                self.interview_id,
                participant=self.runtime._participant_role(self.principal),
                organization_id=self.organization_id,
            )
            await self._emit_snapshot(signal.causation_id)
            return
        raise ApiError(
            "AGENT_SIGNAL_UNSUPPORTED",
            "Client signal is not supported by InterviewAgentRuntime.",
            status_code=422,
            details={"type": kind},
        )

    async def _candidate_speech_started(self, signal: ClientSignal) -> None:
        self.runtime._require_candidate(self.principal)
        self._require_active_interview()
        if self._evidence_session is not None:
            await self._evidence_session.dispatch(self, signal)
            return
        await self._cancel_speech_output()
        if self._endpoint_task:
            self._endpoint_task.cancel()
            self._endpoint_task = None
        session = self.runtime.interviews.get_interview(self.interview_id, self.organization_id)
        runtime_state = session.get("agent_runtime") or {}
        if runtime_state.get("calibration_status") == "opening":
            # Speaking over the opening is a valid barge-in into the unscored
            # calibration turn; it must never be mistaken for question one.
            self.runtime._set_calibration(
                self.interview_id, "listening", self.organization_id
            )
        if runtime_state.get("floor") == FloorOwner.AGENT.value:
            await self._emit(
                "avatar.performance.interrupted",
                {
                    "reason": "barge_in",
                    "deadline_ms": 200,
                    "performance_id": runtime_state.get("active_performance_id"),
                },
                turn_id=signal.turn_id or session.get("current_turn_id"),
                causation_id=signal.causation_id,
                replayability=Replayability.TRANSIENT,
            )
            self.runtime._clear_active_performance(
                self.interview_id,
                runtime_state.get("active_performance_id"),
                self.organization_id,
            )
        await self._set_floor(FloorOwner.CANDIDATE, "barge_in", signal.causation_id)
        await self._emit(
            "speech.started",
            {"speaker": "candidate", "local_detected": True, "server_audio_received": False},
            turn_id=signal.turn_id or session.get("current_turn_id"),
            causation_id=signal.causation_id,
            replayability=Replayability.TRANSIENT,
        )

    async def _candidate_speech_stopped(self, signal: ClientSignal) -> None:
        self.runtime._require_candidate(self.principal)
        self._require_active_interview()
        if self._evidence_session is not None:
            await self._evidence_session.dispatch(self, signal)
            return
        automatic = self._warmup is not None
        await self._emit(
            "speech.stopped",
            {
                "speaker": "candidate",
                "endpoint_countdown_ms": 2500 if automatic else 0,
                "cancellable": True,
            },
            turn_id=signal.turn_id or (self._stt.turn_id if self._stt else None),
            causation_id=signal.causation_id,
            replayability=Replayability.TRANSIENT,
        )
        if self._endpoint_task:
            self._endpoint_task.cancel()
            self._endpoint_task = None
        if not automatic:
            return
        self._endpoint_task = asyncio.create_task(
            self._endpoint_after_delay(signal.turn_id, signal.causation_id)
        )

    async def _endpoint_after_delay(
        self, turn_id: Optional[str], causation_id: Optional[str]
    ) -> None:
        try:
            await asyncio.sleep(2.5)
            signal = ClientSignal(
                type="evidence.finish",
                idempotency_key=new_id("endpoint"),
                turn_id=turn_id,
                causation_id=causation_id,
                payload={"endpoint": "semantic_timeout"},
            )
            async with self._lock:
                await self._finish_evidence(signal)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await self._problem(exc, turn_id=turn_id, causation_id=causation_id)
        finally:
            self._endpoint_task = None

    async def _open_evidence(self, signal: ClientSignal) -> None:
        self.runtime._require_candidate(self.principal)
        self._require_active_interview()
        if self._evidence_session is not None:
            await self._evidence_session.dispatch(self, signal)
            return
        if self._stt is not None or self._warmup is not None:
            raise ApiError("AGENT_EVIDENCE_ALREADY_OPEN", "Evidence stream is already open.", status_code=409)
        session = self.runtime.interviews.get_interview(
            self.interview_id, self.organization_id
        )
        calibration = (session.get("agent_runtime") or {}).get(
            "calibration_status", "pending"
        )
        if calibration != "completed":
            if calibration not in {"listening", "retrying"}:
                raise ApiError(
                    "WARMUP_NOT_LISTENING",
                    "Finish the opening before starting microphone calibration.",
                    status_code=409,
                )
            self._warmup = WarmupCalibrationStream(
                self.runtime.store,
                self.interview_id,
                organization_id=self.organization_id,
                persistence=self.runtime.persistence,
            )
            self._server_audio_seen = False
            events = await self._warmup.open(signal.payload)
            await self._set_floor(
                FloorOwner.CANDIDATE,
                "warmup_stream_open",
                signal.causation_id,
            )
            for raw in events:
                await self._project_warmup_event(raw, signal.causation_id)
            return
        self._stt = StreamingInterviewSTT(
            self.runtime.store,
            self.interview_id,
            organization_id=self.organization_id,
            persistence=self.runtime.persistence,
        )
        self._server_audio_seen = False
        events = await self._stt.open({**signal.payload, "turn_id": signal.turn_id})
        await self._set_floor(FloorOwner.CANDIDATE, "evidence_stream_open", signal.causation_id)
        for raw in events:
            await self._project_evidence_event(raw, signal.causation_id)

    async def _send_audio(self, signal: ClientSignal) -> None:
        self.runtime._require_candidate(self.principal)
        if self._evidence_session is not None:
            raise ApiError(
                "LIVEKIT_EVIDENCE_AUDIO_REQUIRED",
                "WebSocket PCM cannot feed the authoritative Evidence chain while LiveKit ingress is active.",
                status_code=409,
            )
        if not self._stt and not self._warmup:
            raise ApiError("AGENT_EVIDENCE_NOT_OPEN", "Evidence stream is not open.", status_code=409)
        if self._warmup:
            events = await self._warmup.send_audio(signal.audio or b"")
            active_turn_id = None
        else:
            events = await self._stt.send_audio(signal.audio or b"")
            active_turn_id = self._stt.turn_id
        if not self._server_audio_seen and signal.audio:
            self._server_audio_seen = True
            if signal.payload.get("client_audio_sequence") is None:
                await self._emit(
                    "speech.started",
                    {
                        "speaker": "candidate",
                        "local_detected": True,
                        "server_audio_received": True,
                        "server_received_at": utc_now(),
                    },
                    turn_id=signal.turn_id or active_turn_id,
                    causation_id=signal.causation_id,
                    replayability=Replayability.TRANSIENT,
                )
        if signal.payload.get("client_audio_sequence") is not None:
            await self._emit_audio_ack(signal, duplicate=False)
        for raw in events:
            if self._warmup:
                await self._project_warmup_event(raw, signal.causation_id)
            else:
                await self._project_evidence_event(raw, signal.causation_id)

    async def _emit_audio_ack(
        self, signal: ClientSignal, *, duplicate: bool
    ) -> None:
        await self._emit(
            "speech.started",
            {
                "speaker": "candidate",
                "local_detected": True,
                "server_audio_received": True,
                "acknowledged_audio_sequence": signal.payload.get(
                    "client_audio_sequence"
                ),
                "audio_epoch": signal.payload.get("audio_epoch"),
                "duplicate": duplicate,
                "server_received_at": utc_now(),
            },
            turn_id=signal.turn_id or (self._stt.turn_id if self._stt else None),
            causation_id=signal.causation_id,
            replayability=Replayability.TRANSIENT,
        )

    async def _finish_evidence(self, signal: ClientSignal) -> None:
        self.runtime._require_candidate(self.principal)
        if self._evidence_session is not None:
            await self._evidence_session.dispatch(self, signal)
            return
        if self._warmup:
            warmup = self._warmup
            self._warmup = None
            events = await warmup.finish()
            final = next(
                item for item in events if item.get("type") == "transcript.final"
            )
            for raw in events:
                await self._project_warmup_event(raw, signal.causation_id)
            self.runtime._set_calibration(
                self.interview_id,
                "awaiting_confirmation",
                self.organization_id,
            )
            await self._emit(
                "transcript.final",
                {
                    "text": final.get("text", ""),
                    "confidence": final.get("confidence", 0.0),
                    "authoritative": False,
                    "persisted_audio": False,
                    "calibration": True,
                    "ephemeral": True,
                },
                causation_id=signal.causation_id,
                replayability=Replayability.TRANSIENT,
            )
            await self._select_act(
                act_type="warmup_confirmation",
                text="我已经形成了试音字幕。请确认是否听写正确；不正确可以立即重试。",
                turn_id=None,
                causation_id=signal.causation_id,
                evidence_refs=[],
                gesture="nod",
            )
            return
        if not self._stt:
            raise ApiError("AGENT_EVIDENCE_NOT_OPEN", "Evidence stream is not open.", status_code=409)
        stt = self._stt
        self._stt = None
        finished_turn_id = stt.turn_id
        await self._set_floor(FloorOwner.NONE, "answer_processing", signal.causation_id)
        events = await stt.finish(signal.payload)
        for raw in events:
            raw.setdefault("turn_id", finished_turn_id)
            await self._project_evidence_event(raw, signal.causation_id)
        await self._after_formal_evidence_finished(
            finished_turn_id,
            signal,
            conversation_action_selected=any(
                item.get("type")
                in {"followup.selected", "utterance.not_accepted"}
                for item in events
            ),
        )

    async def _complete_warmup_evidence(
        self, result: EvidenceFinishResult, signal: ClientSignal
    ) -> None:
        final = result.final or {}
        for raw in result.events:
            await self._project_warmup_event(raw, signal.causation_id)
        self.runtime._set_calibration(
            self.interview_id,
            "awaiting_confirmation",
            self.organization_id,
        )
        await self._emit(
            "transcript.final",
            {
                "text": final.get("text", ""),
                "confidence": final.get("confidence", 0.0),
                "authoritative": False,
                "persisted_audio": False,
                "calibration": True,
                "ephemeral": True,
            },
            causation_id=signal.causation_id,
            replayability=Replayability.TRANSIENT,
        )
        await self._select_act(
            act_type="warmup_confirmation",
            text="我已经形成了试音字幕。请确认是否听写正确；不正确可以立即重试。",
            turn_id=None,
            causation_id=signal.causation_id,
            evidence_refs=[],
            gesture="nod",
        )

    async def _after_formal_evidence_finished(
        self,
        finished_turn_id: Optional[str],
        signal: Optional[ClientSignal],
        *,
        conversation_action_selected: bool,
        stop_evidence_session: bool = True,
    ) -> None:
        causation_id = signal.causation_id if signal is not None else None
        session = self.runtime.interviews.get_interview(self.interview_id, self.organization_id)
        if session.get("status") in {"completed", "report_generating", "report_ready"} or session.get(
            "candidate_input_completed_at"
        ):
            from app.services.evidence_coordination import assert_current_evidence_fence

            closing_evidence_session = self._evidence_session
            closing_fence = (
                closing_evidence_session.ownership.commit_fence()
                if closing_evidence_session is not None and closing_evidence_session.ownership is not None
                else None
            )
            closing_turn_id = session.get("current_turn_id")

            def closing_is_current() -> bool:
                if self._closed or self._terminating:
                    return False
                if closing_evidence_session is not None and (
                    self._evidence_session is not closing_evidence_session
                    or closing_evidence_session._stopped or closing_fence is None
                ):
                    return False
                try:
                    with self.runtime.persistence.transaction(self.organization_id) as transaction:
                        if closing_fence is not None:
                            assert_current_evidence_fence(transaction, closing_fence)
                        current = transaction.interview_sessions.get(self.interview_id)
                        if not current or current.get("current_turn_id") != closing_turn_id:
                            return False
                        state = current.get("agent_runtime") or {}
                        if state.get("floor") == "human" or (state.get("takeover") or {}).get("status") == "active":
                            return False
                        return current.get("status") in {"completed", "report_generating", "report_ready"} or (
                            current.get("status") == "in_progress" and bool(current.get("candidate_input_completed_at"))
                        )
                except ApiError:
                    return False

            runtime_state = session.get("agent_runtime") or {}
            if runtime_state.get("completion_emitted_at"):
                return
            if runtime_state.get("completion_closing_performance_id"):
                return
            terminal_takeover = self.runtime._consume_takeover_for_terminal(
                self.interview_id, self.organization_id
            )
            if terminal_takeover:
                await self.runtime._disconnect_takeover_media(
                    self.interview_id,
                    terminal_takeover,
                    self.organization_id,
                )
            await self.runtime.media_captures.stop_for_interview(
                self.interview_id,
                actor_id=self.principal.actor_id,
                organization_id=self.organization_id,
            )
            if stop_evidence_session and self._evidence_session is not None:
                await self._evidence_session.stop("interview_completed")
                self._evidence_session = None
            if not closing_is_current():
                return
            performance = await self._select_act(
                act_type="closing",
                text=(
                    "本次面试已经完成，感谢你的时间。你的回答已按同意范围提交，"
                    "后续将由企业人员进行最终审核。再见。"
                ),
                turn_id=None,
                causation_id=causation_id,
                evidence_refs=[],
                gesture="farewell",
            )
            # None can mean lost authority, not merely unavailable TTS. A
            # paused/replaced owner must not mint a completion receipt or arm
            # a timer after its delayed farewell result returns.
            if not closing_is_current():
                return
            if performance is None:
                await self._finalize_completion(
                    causation_id=causation_id,
                    reason="farewell_expression_unavailable",
                )
                return
            self.runtime._mark_completion_closing(
                self.interview_id,
                performance.performance_id,
                self.organization_id,
            )
            timeout_ms = min(
                10_000,
                max(
                    cue.at_ms + cue.duration_ms
                    for cue in performance.visemes
                )
                + 2_000,
            )
            self._completion_task = asyncio.create_task(
                self._completion_after_timeout(
                    performance.performance_id,
                    timeout_ms,
                    causation_id,
                )
            )
            return
        if conversation_action_selected:
            return
        current = self.runtime._current_turn(session)
        if current and current.get("status") == "asking":
            await self._select_turn(current, causation_id)
        else:
            await self._set_floor(FloorOwner.NONE, "understanding", causation_id)

    async def _completion_after_timeout(
        self,
        performance_id: str,
        timeout_ms: int,
        causation_id: Optional[str],
    ) -> None:
        try:
            await asyncio.sleep(timeout_ms / 1000)
            async with self._lock:
                if self._closed:
                    return
                session = self.runtime.interviews.get_interview(
                    self.interview_id, self.organization_id
                )
                state = session.get("agent_runtime") or {}
                if state.get("completion_closing_performance_id") != performance_id:
                    return
                self.runtime._clear_active_performance(
                    self.interview_id, performance_id, self.organization_id
                )
                await self._emit(
                    "avatar.performance.stopped",
                    {"performance_id": performance_id, "reason": "completion_timeout"},
                    causation_id=causation_id,
                    replayability=Replayability.TRANSIENT,
                )
                await self._finalize_completion(
                    causation_id=causation_id,
                    reason="farewell_timeout",
                )
        except asyncio.CancelledError:
            return
        finally:
            if self._completion_task is asyncio.current_task():
                self._completion_task = None

    async def _finalize_completion(
        self, *, causation_id: Optional[str], reason: str
    ) -> None:
        task = self._completion_task
        self._completion_task = None
        if task and task is not asyncio.current_task():
            task.cancel()
        session = self.runtime.interviews.get_interview(
            self.interview_id, self.organization_id
        )
        if not self.runtime._mark_completion_emitted(
            self.interview_id, reason, self.organization_id
        ):
            return
        await self._emit(
            "completed",
            self.runtime._completion_receipt(session),
            causation_id=causation_id,
            replayability=Replayability.REPLAYABLE,
        )
        await self._set_floor(FloorOwner.NONE, "completed", causation_id)

    async def _project_warmup_event(
        self, raw: Dict[str, Any], causation_id: Optional[str]
    ) -> None:
        if raw.get("type") == "transcript.partial":
            await self._emit(
                "transcript.partial",
                {
                    "text": raw.get("text", ""),
                    "confidence": raw.get("confidence", 0.0),
                    "server_received": True,
                    "calibration": True,
                    "ephemeral": True,
                },
                causation_id=causation_id,
                replayability=Replayability.TRANSIENT,
            )
        elif raw.get("type") == "stream.error":
            await self._emit(
                "problem",
                {
                    "code": raw.get("error_code") or "WARMUP_STT_PROBLEM",
                    "message": "试音转写暂时不可用，请重试或由面试官接管。",
                    "recoverable": True,
                    "calibration": True,
                },
                causation_id=causation_id,
                replayability=Replayability.REPLAYABLE,
            )

    async def _confirm_warmup(self, signal: ClientSignal) -> None:
        self.runtime._require_candidate(self.principal)
        session = self.runtime.interviews.get_interview(
            self.interview_id, self.organization_id
        )
        if (session.get("agent_runtime") or {}).get("calibration_status") != "awaiting_confirmation":
            raise ApiError(
                "WARMUP_CONFIRMATION_NOT_EXPECTED",
                "There is no warm-up transcript awaiting confirmation.",
                status_code=409,
            )
        settings = session.get("settings") or {}
        if bool(settings.get("record_audio", True) or settings.get("record_video", False)):
            # This call is the formal recording boundary. A required capture
            # failure pauses the interview and no question is selected.
            capture = await self.runtime.media_captures.start_for_connection(
                self.interview_id,
                self.opened.connection_id,
                actor_id=self.principal.actor_id,
                organization_id=self.organization_id,
            )
            if capture.get("status") != "recording":
                self.runtime.interviews.pause_interview(
                    self.interview_id,
                    reason="required_media_capture_not_recording",
                    organization_id=self.organization_id,
                )
                raise ApiError(
                    "MEDIA_CAPTURE_NOT_RECORDING",
                    "Formal questions remain blocked until recording is confirmed active.",
                    status_code=503,
                )
        self.runtime._set_calibration(
            self.interview_id,
            "completed",
            self.organization_id,
            completed=True,
        )
        session = self.runtime.interviews.get_interview(
            self.interview_id, self.organization_id
        )
        current = self.runtime._current_turn(session)
        if current is None:
            await self._emit(
                "problem",
                {
                    "code": "INTERVIEW_TURN_NOT_ACTIVE",
                    "message": "试音已完成，但正式题目尚未就绪。",
                    "recoverable": False,
                    "action": "pause_or_human_takeover",
                },
                causation_id=signal.causation_id,
                replayability=Replayability.REPLAYABLE,
            )
            await self._set_floor(FloorOwner.NONE, "question_unavailable", signal.causation_id)
            return
        await self._select_turn(current, signal.causation_id)

    async def _retry_warmup(self, signal: ClientSignal) -> None:
        self.runtime._require_candidate(self.principal)
        session = self.runtime.interviews.get_interview(
            self.interview_id, self.organization_id
        )
        status = (session.get("agent_runtime") or {}).get("calibration_status")
        if status not in {"awaiting_confirmation", "listening", "retrying"}:
            raise ApiError(
                "WARMUP_RETRY_NOT_ALLOWED",
                "Warm-up calibration cannot be retried from its current state.",
                status_code=409,
            )
        if self._evidence_session is not None:
            await self._evidence_session.dispatch(self, signal)
            return
        elif self._warmup:
            await self._warmup.abort()
            self._warmup = None
        self.runtime._set_calibration(
            self.interview_id,
            "retrying",
            self.organization_id,
            retry_required=False,
        )
        # Retry readiness is an explicit handshake.  The candidate commonly
        # already owns the floor here, so the global same-owner floor dedupe
        # cannot be used as the acknowledgement contract.
        self.runtime._set_floor(
            self.interview_id,
            FloorOwner.CANDIDATE,
            "warmup_retry",
            self.organization_id,
        )
        await self._emit(
            "floor.changed",
            {"owner": FloorOwner.CANDIDATE.value, "reason": "warmup_retry"},
            causation_id=signal.causation_id,
            replayability=Replayability.TRANSIENT,
        )

    async def _project_evidence_event(
        self, raw: Dict[str, Any], causation_id: Optional[str]
    ) -> None:
        raw_type = str(raw.get("type") or "")
        turn_id = (
            self._stt.turn_id
            if self._stt
            else (
                self._evidence_session.turn_id
                if self._evidence_session is not None
                else None
            )
            or raw.get("turn_id")
        )
        if raw_type == "transcript.partial":
            await self._emit(
                "transcript.partial",
                {
                    "text": raw.get("text", ""),
                    "confidence": raw.get("confidence", 0.0),
                    "server_received": True,
                },
                turn_id=turn_id,
                causation_id=causation_id,
                replayability=Replayability.TRANSIENT,
            )
            return
        if raw_type == "transcript.final":
            await self._emit(
                "transcript.final",
                {
                    "text": raw.get("text", ""),
                    "confidence": raw.get("confidence", 0.0),
                    "authoritative": True,
                    "persisted_audio": True,
                },
                turn_id=turn_id,
                causation_id=causation_id,
                replayability=Replayability.REPLAYABLE,
            )
            return
        if raw_type == "followup.selected":
            payload = raw.get("payload") or {}
            expression = raw.get("expression")
            if expression is not None and not isinstance(expression, dict):
                raise ApiError(
                    "AGENT_EXPRESSION_AUDIO_INVALID",
                    "S2S expression metadata is invalid.",
                    status_code=502,
                )
            await self._select_act(
                act_type="followup",
                text=str(payload.get("question_text") or ""),
                turn_id=payload.get("turn_id"),
                causation_id=causation_id,
                evidence_refs=[],
                gesture="nod",
                expression=expression,
            )
            return
        if raw_type == "utterance.not_accepted":
            action = str(raw.get("suggested_action") or "clarify")
            problem = raw.get("problem") if isinstance(raw.get("problem"), dict) else None
            if problem:
                code = str(
                    problem.get("code") or "UNDERSTANDING_RESULT_REJECTED"
                )
                user_message = (
                    "这段录音未得到有效转写，请继续说话或重新说一次。"
                    if code == "STT_TRANSCRIPT_UNAVAILABLE"
                    else "系统暂时无法确认这段回答，请重新回答一次。"
                    if action == "clarify"
                    else "系统无法安全理解这段回答，面试已暂停并等待人工处理。"
                )
                self.runtime.interviews.record_agent_problem(
                    self.interview_id,
                    {
                        "code": code,
                        "message": user_message,
                        "recoverable": bool(problem.get("recoverable")),
                        "action": action,
                    },
                    self.organization_id,
                )
                await self._emit(
                    "problem",
                    {
                        "code": code,
                        "message": user_message,
                        "recoverable": bool(problem.get("recoverable")),
                        "action": action,
                    },
                    turn_id=turn_id,
                    causation_id=causation_id,
                    replayability=Replayability.REPLAYABLE,
                )
            if action == "repeat":
                session = self.runtime.interviews.get_interview(
                    self.interview_id, self.organization_id
                )
                current = self.runtime._current_turn(session)
                if current:
                    await self._select_turn(current, causation_id, act_type="repeat")
            elif action == "continue_listening":
                await self._set_floor(FloorOwner.CANDIDATE, "candidate_not_finished", causation_id)
                if problem and problem.get("code") == "STT_TRANSCRIPT_UNAVAILABLE":
                    # Same-owner floor deduplication must not leave the client
                    # waiting for a final from the now-consumed old stream.
                    await self._emit_snapshot(causation_id)
            elif action == "pause":
                session = self.runtime.interviews.get_interview(
                    self.interview_id, self.organization_id
                )
                if session.get("status") == "in_progress":
                    self.runtime.interviews.pause_interview(
                        self.interview_id,
                        reason=(
                            "understanding_safety_pause"
                            if problem
                            else "candidate_requested_pause"
                        ),
                        organization_id=self.organization_id,
                    )
                await self._set_floor(
                    FloorOwner.NONE,
                    "understanding_safety_pause" if problem else "candidate_pause",
                    causation_id,
                )
            else:
                await self._select_act(
                    act_type="clarification",
                    text=(
                        "系统暂时无法确认这段回答，为避免误记，请你重新回答一次。"
                        if problem
                        else "我没有完全听清刚才的回答，请你再说一次。"
                    ),
                    turn_id=turn_id,
                    causation_id=causation_id,
                    evidence_refs=[],
                    gesture="listen",
                )
            return
        if raw_type in {"stream.error", "dialogue.error"}:
            await self._emit(
                "problem",
                {
                    "code": raw.get("error_code") or "EVIDENCE_STREAM_PROBLEM",
                    "message": raw.get("message") or "Evidence stream reported a problem.",
                    "recoverable": True,
                },
                turn_id=turn_id,
                causation_id=causation_id,
                replayability=Replayability.REPLAYABLE,
            )

    async def _repeat_current(self, signal: ClientSignal) -> None:
        self.runtime._require_candidate(self.principal)
        session = self.runtime.interviews.get_interview(self.interview_id, self.organization_id)
        current = self.runtime._current_turn(session)
        if current is None:
            raise ApiError("INTERVIEW_TURN_NOT_ACTIVE", "There is no question to repeat.", status_code=409)
        await self._select_turn(current, signal.causation_id, act_type="repeat")

    async def _select_turn(
        self,
        turn: Dict[str, Any],
        causation_id: Optional[str],
        *,
        act_type: str = "question",
    ) -> None:
        await self._select_act(
            act_type=act_type,
            text=str(turn.get("question_spoken_text") or ""),
            turn_id=turn["id"],
            causation_id=causation_id,
            evidence_refs=[],
            gesture="look_at_candidate",
        )

    async def _select_act(
        self,
        *,
        act_type: str,
        text: str,
        turn_id: Optional[str],
        causation_id: Optional[str],
        evidence_refs: List[str],
        gesture: str,
        expression: Optional[Dict[str, Any]] = None,
        approval_guard: Any = None,
    ) -> Optional[AvatarPerformance]:
        if not text.strip():
            raise ApiError("CONVERSATION_ACT_EMPTY", "Approved conversation act is empty.", status_code=409)
        if approval_guard is not None:
            approval_guard()
        # Expression I/O can outlive a pause, another act, or this Evidence
        # owner. Bind completion to the original authority, not whichever
        # owner/turn happens to be current when the provider finally returns.
        from app.services.evidence_coordination import assert_current_evidence_fence

        evidence_session = self._evidence_session
        if (self._closed or self._terminating
                or (evidence_session is not None and evidence_session._stopped)):
            return None
        owner_fence = None
        if evidence_session is not None:
            try:
                grant = evidence_session.ownership or evidence_session._require_attached(self)
                owner_fence = grant.commit_fence()
            except ApiError:
                return None
        try:
            with self.runtime.persistence.transaction(self.organization_id) as transaction:
                if owner_fence is not None:
                    assert_current_evidence_fence(transaction, owner_fence)
                initial_session = transaction.interview_sessions.get(self.interview_id)
        except ApiError:
            return None
        if not initial_session:
            return None
        expected_turn_id = initial_session.get("current_turn_id")
        allowed_statuses = {"in_progress"}
        if act_type == "closing":
            # The existing completion protocol deliberately speaks its one
            # farewell after answers are sealed but before issuing a receipt.
            allowed_statuses.update({"completed", "report_generating", "report_ready"})
        if (initial_session.get("status") not in allowed_statuses
                or (turn_id is not None and turn_id != expected_turn_id)):
            return None
        await self._cancel_speech_output()
        expected_performance_id = None
        if approval_guard is not None:
            approval_guard()

        def expression_is_current() -> bool:
            if (self._closed or self._terminating
                    or self._evidence_session is not evidence_session
                    or (evidence_session is not None and evidence_session._stopped)):
                return False
            try:
                if approval_guard is not None:
                    approval_guard()
                with self.runtime.persistence.transaction(self.organization_id) as transaction:
                    if owner_fence is not None:
                        assert_current_evidence_fence(transaction, owner_fence)
                    session = transaction.interview_sessions.get(self.interview_id)
                    if not session or session.get("status") not in allowed_statuses:
                        return False
                    state = session.get("agent_runtime") or {}
                    if act_type == "closing" and state.get("completion_emitted_at"):
                        return False
                    if (session.get("current_turn_id") != expected_turn_id
                            or state.get("floor") != FloorOwner.AGENT.value):
                        return False
                    if (expected_performance_id is not None
                            and state.get("active_performance_id") != expected_performance_id):
                        return False
                    selected = next((item for item in reversed(session.get("agent_events", []))
                                     if item.get("type") == "conversation.act.selected"), None)
                    return bool(selected
                                and selected.get("event_id") == selected_event.event_id
                                and selected.get("payload", {}).get("act_id") == act.act_id
                                and selected.get("payload", {}).get("approved") is True)
            except ApiError:
                return False

        act = self.runtime._approve_conversation_act(
            self.interview_id,
            act_type=act_type,
            text=text,
            turn_id=turn_id,
            evidence_quotes=evidence_refs,
            approved_by="interview_agent_runtime",
            organization_id=self.organization_id,
        )
        await self._set_floor(FloorOwner.AGENT, "approved_conversation_act", causation_id)
        selected_event = await self._emit(
            "conversation.act.selected",
            {
                "act_id": act.act_id,
                "act_type": act.act_type,
                "text": act.text,
                "evidence_refs": act.evidence_quotes,
                "target_capability_points": act.target_capability_points,
                "evaluative": act.evaluative,
                "approved": True,
            },
            turn_id=turn_id,
            causation_id=causation_id,
            replayability=Replayability.REPLAYABLE,
        )
        try:
            if not expression_is_current():
                return None
            selected_expression = expression
            if selected_expression is None:
                # Existing question assets remain the fastest path. Dynamic
                # approved acts can use the same TTS route's PCM transport.
                stream_performance_id = new_id("performance")

                def assert_stream_current() -> None:
                    if not expression_is_current():
                        raise ApiError("AGENT_SPEECH_OUTPUT_STALE", "Approved speech is no longer current.", status_code=409)

                output = None
                if (isinstance(self._evidence_session, ManagedLiveKitEvidenceSession)
                        and act_type != "closing"):
                    output = await self.runtime._open_streamed_expression(
                        self.interview_id, turn_id, text, self.organization_id,
                        performance_id=stream_performance_id,
                        assert_current=assert_stream_current,
                    )
                if output is not None:
                    try:
                        binding = await output.open()
                        assert_stream_current()
                        stream_performance = self.runtime.performance.compose(
                            text, turn_id=turn_id, gesture=gesture,
                        )
                        performance = AvatarPerformance.model_validate({
                            **stream_performance.model_dump(),
                            "performance_id": stream_performance_id,
                            "delivery": "streaming_tts", "live_audio": binding,
                        })
                        self._speech_output = output
                        self.runtime._set_active_performance(
                            self.interview_id, performance.performance_id, self.organization_id,
                            output_id=output.output_id, act_event_id=selected_event.event_id,
                        )
                        expected_performance_id = performance.performance_id
                        await self._emit(
                            "avatar.performance.started", performance.model_dump(mode="json"),
                            turn_id=turn_id, causation_id=causation_id,
                            replayability=Replayability.TRANSIENT,
                        )
                        assert_stream_current()
                        self._speech_output_task = asyncio.create_task(
                            self._run_speech_output(output, expression_is_current, turn_id, causation_id)
                        )
                        return performance
                    except BaseException:
                        await output.abort()
                        if self._speech_output is output:
                            self._speech_output = None
                        raise
                selected_expression = await self.runtime._expression_audio(
                    self.interview_id,
                    turn_id,
                    text,
                    self.principal.actor_id,
                    self.organization_id,
                )
            if not expression_is_current():
                return None
            audio_uri = str(selected_expression.get("audio_uri") or "")
            if not audio_uri:
                raise ApiError(
                    "AGENT_EXPRESSION_AUDIO_REQUIRED",
                    "Approved conversation act has no playable audio.",
                    status_code=503,
                )
            delivery = str(selected_expression.get("delivery") or "cascade")
            if delivery not in {"pre_generated", "cascade", "s2s"}:
                raise ApiError(
                    "AGENT_EXPRESSION_DELIVERY_INVALID",
                    "Agent expression delivery path is invalid.",
                    status_code=502,
                )
            performance = self.runtime.performance.compose(
                text,
                turn_id=turn_id,
                audio_uri=audio_uri,
                audio_duration_ms=selected_expression.get("duration_ms"),
                provider_visemes=selected_expression.get("visemes") or None,
                gesture=gesture,
                delivery=delivery,
            )
        except Exception as exc:
            if not expression_is_current():
                return None
            await self._problem(exc, turn_id=turn_id, causation_id=causation_id)
            # Publishing the problem can yield to a new act or a takeover.
            # An old synthesis failure must never pause that newer state.
            if not expression_is_current():
                return None
            self.runtime._clear_active_performance(
                self.interview_id, None, self.organization_id
            )
            self.runtime._pause_for_fatal_expression(
                self.interview_id, self.organization_id
            )
            await self._set_floor(FloorOwner.NONE, "expression_failure", causation_id)
            return None
        if not expression_is_current():
            return None
        self.runtime._set_active_performance(
            self.interview_id, performance.performance_id, self.organization_id
        )
        await self._emit(
            "avatar.performance.started",
            performance.model_dump(mode="json"),
            turn_id=turn_id,
            causation_id=causation_id,
            replayability=Replayability.REPLAYABLE,
        )
        return performance

    async def _run_speech_output(self, output, is_current, turn_id, causation_id) -> None:
        async def finished(payload):
            await self._emit(
                "avatar.performance.producer_finished", payload,
                turn_id=turn_id, causation_id=causation_id,
                replayability=Replayability.TRANSIENT,
            )

        try:
            await output.run(finished)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if is_current():
                await self._problem(exc, turn_id=turn_id, causation_id=causation_id)
                if is_current():
                    self.runtime._clear_active_performance(
                        self.interview_id, output.performance_id, self.organization_id,
                    )
                    self.runtime._pause_for_fatal_expression(self.interview_id, self.organization_id)
                    await self._set_floor(FloorOwner.NONE, "expression_failure", causation_id)
                    await self._emit_snapshot(causation_id)
        finally:
            if self._speech_output is output:
                self._speech_output = None
                self._speech_output_task = None

    async def _cancel_speech_output(self) -> None:
        output, task = self._speech_output, self._speech_output_task
        self._speech_output = self._speech_output_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        try:
            if output is not None:
                await output.abort()
        finally:
            if task is not None and task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)

    async def _acquire_takeover(self, signal: ClientSignal) -> None:
        self.runtime._require_human(self.principal)
        reason = str(signal.payload.get("reason") or "human_takeover").strip()
        if not reason:
            raise ApiError("TAKEOVER_REASON_REQUIRED", "Takeover requires a reason.", status_code=422)
        session = self.runtime.interviews.get_interview(self.interview_id, self.organization_id)
        if session.get("status") == "in_progress":
            self.runtime.interviews.pause_interview(
                self.interview_id,
                reason="human_takeover",
                organization_id=self.organization_id,
            )
        lease, replaced = self.runtime._acquire_takeover_lease(
            self.interview_id,
            actor_id=self.principal.actor_id,
            reason=reason,
            organization_id=self.organization_id,
        )
        await self._cancel_speech_output()
        if replaced:
            await self.runtime._disconnect_takeover_media(
                self.interview_id, replaced, self.organization_id
            )
        self.runtime._clear_active_performance(
            self.interview_id, None, self.organization_id
        )
        await self._emit(
            "avatar.performance.interrupted",
            {"reason": "human_takeover", "deadline_ms": 200},
            causation_id=signal.causation_id,
            replayability=Replayability.TRANSIENT,
        )
        await self._set_floor(FloorOwner.HUMAN, "takeover_acquired", signal.causation_id)
        await self._emit(
            "takeover.changed",
            {"status": "active", **lease.model_dump()},
            causation_id=signal.causation_id,
            replayability=Replayability.REPLAYABLE,
        )
        self._schedule_takeover_watchdog(lease.model_dump())

    async def _renew_takeover(self, signal: ClientSignal) -> None:
        self.runtime._require_human(self.principal)
        try:
            expected_version = int(signal.payload.get("expected_version", -1))
        except (TypeError, ValueError):
            expected_version = -1
        value = self.runtime._renew_takeover_lease(
            self.interview_id,
            actor_id=self.principal.actor_id,
            lease_id=str(signal.payload.get("lease_id") or ""),
            expected_version=expected_version,
            organization_id=self.organization_id,
        )
        await self._emit(
            "takeover.changed",
            {"status": "active", **value},
            causation_id=signal.causation_id,
            replayability=Replayability.REPLAYABLE,
        )
        self._schedule_takeover_watchdog(value)

    async def _release_takeover(self, signal: ClientSignal) -> None:
        self.runtime._require_human(self.principal)
        try:
            expected_version = int(signal.payload.get("expected_version", -1))
        except (TypeError, ValueError):
            expected_version = -1
        value = self.runtime._release_takeover_lease(
            self.interview_id,
            actor_id=self.principal.actor_id,
            lease_id=str(signal.payload.get("lease_id") or ""),
            expected_version=expected_version,
            organization_id=self.organization_id,
        )
        self._cancel_takeover_watchdog()
        await self.runtime._disconnect_takeover_media(
            self.interview_id, value, self.organization_id
        )
        await self._set_floor(FloorOwner.NONE, "takeover_released_paused", signal.causation_id)
        await self._emit(
            "takeover.changed",
            {"status": "released", "actor_id": self.principal.actor_id, "ai_resumed": False},
            causation_id=signal.causation_id,
            replayability=Replayability.REPLAYABLE,
        )

    async def _human_speech(self, signal: ClientSignal) -> None:
        self.runtime._require_human(self.principal)
        transcript = str(signal.payload.get("transcript") or "").strip()
        if not transcript or len(transcript) > 1000:
            raise ApiError(
                "HUMAN_INTERVENTION_TEXT_INVALID",
                "Human intervention transcript must contain 1 to 1000 characters.",
                status_code=422,
            )
        act = self.runtime._approve_conversation_act(
            self.interview_id,
            act_type="unscored_intervention",
            text=transcript,
            turn_id=signal.turn_id,
            evidence_quotes=[],
            approved_by=self.principal.actor_id,
            organization_id=self.organization_id,
        )
        await self._emit(
            "conversation.act.selected",
            {
                "act_id": act.act_id,
                "act_type": act.act_type,
                "text": act.text,
                "actor_id": self.principal.actor_id,
                "evaluative": act.evaluative,
                "approved": True,
            },
            turn_id=signal.turn_id,
            causation_id=signal.causation_id,
            replayability=Replayability.REPLAYABLE,
        )

    async def _expire_takeover_if_needed(self, causation_id: Optional[str]) -> None:
        lease = self.runtime._expire_takeover_if_due(
            self.interview_id, self.organization_id
        )
        if not lease:
            return
        session = self.runtime.interviews.get_interview(
            self.interview_id, self.organization_id
        )
        if session.get("status") == "in_progress":
            self.runtime.interviews.pause_interview(
                self.interview_id,
                reason="takeover_lease_lost",
                organization_id=self.organization_id,
            )
        await self.runtime._disconnect_takeover_media(
            self.interview_id, lease, self.organization_id
        )
        await self._set_floor(FloorOwner.NONE, "takeover_lease_lost", causation_id)
        await self._emit(
            "takeover.changed",
            {"status": "lost", "ai_resumed": False, "previous_actor_id": lease.get("actor_id")},
            causation_id=causation_id,
            replayability=Replayability.REPLAYABLE,
        )

    def _schedule_takeover_watchdog(self, lease: Dict[str, Any]) -> None:
        self._cancel_takeover_watchdog()
        self._takeover_watchdog_task = asyncio.create_task(
            self._run_takeover_watchdog(str(lease.get("lease_id") or ""))
        )

    def _cancel_takeover_watchdog(self) -> None:
        task = self._takeover_watchdog_task
        self._takeover_watchdog_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _run_takeover_watchdog(self, lease_id: str) -> None:
        try:
            while True:
                delay = self.runtime._takeover_seconds_remaining(
                    self.interview_id,
                    self.organization_id,
                    lease_id=lease_id,
                )
                if delay is None:
                    return
                if delay > 0:
                    await asyncio.sleep(delay)
                async with self._lock:
                    await self._expire_takeover_if_needed(None)
                if self.runtime._takeover_seconds_remaining(
                    self.interview_id,
                    self.organization_id,
                    lease_id=lease_id,
                ) is None:
                    return
                # A database clock has finite resolution. Avoid a tight loop
                # when expiry and the authoritative comparison are equal.
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            return
        finally:
            if self._takeover_watchdog_task is asyncio.current_task():
                self._takeover_watchdog_task = None

    def _require_active_interview(self) -> Dict[str, Any]:
        session = self.runtime.interviews.get_interview(
            self.interview_id, self.organization_id
        )
        if session.get("status") != "in_progress":
            raise ApiError(
                "INTERVIEW_NOT_IN_PROGRESS",
                "Paused or completed interviews cannot accept candidate media commands.",
                status_code=409,
            )
        return session

    async def _set_floor(
        self, owner: FloorOwner, reason: str, causation_id: Optional[str]
    ) -> None:
        changed = self.runtime._set_floor(
            self.interview_id, owner, reason, self.organization_id
        )
        if changed:
            await self._emit(
                "floor.changed",
                {"owner": owner.value, "reason": reason},
                causation_id=causation_id,
                replayability=Replayability.REPLAYABLE,
            )
        if owner == FloorOwner.CANDIDATE and hasattr(self._evidence_session, "confirmation_floor_returned"):
            await self._evidence_session.confirmation_floor_returned(reason)

    async def _emit_snapshot(self, causation_id: Optional[str]) -> None:
        await self._emit(
            "session.snapshot",
            {},
            causation_id=causation_id,
            replayability=Replayability.TRANSIENT,
        )

    async def _queue_fresh_snapshot(self) -> None:
        event = self.runtime._append_event(
            self.interview_id,
            "session.snapshot",
            {},
            turn_id=None,
            causation_id=None,
            replayability=Replayability.TRANSIENT,
            organization_id=self.organization_id,
        )
        self._enqueue_event(
            self.runtime._project_event_for_principal(
                event,
                self.principal,
                interview_id=self.interview_id,
            )
        )

    async def _problem(
        self,
        exc: Exception,
        *,
        turn_id: Optional[str],
        causation_id: Optional[str],
    ) -> None:
        status_code = int(getattr(exc, "status_code", 500))
        recoverable = status_code < 500
        payload = {
            "code": getattr(exc, "code", type(exc).__name__.upper()),
            "message": getattr(exc, "message", str(exc))[:500],
            "recoverable": recoverable,
            "action": "retry_or_correct_signal"
            if recoverable
            else "pause_or_human_takeover",
        }
        self.runtime._record_problem(
            self.interview_id, payload, self.organization_id
        )
        await self._emit(
            "problem",
            payload,
            turn_id=turn_id,
            causation_id=causation_id,
            replayability=Replayability.REPLAYABLE,
        )

    async def _emit(
        self,
        event_type: str,
        payload: Dict[str, Any],
        *,
        turn_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        replayability: Replayability,
    ) -> AgentEvent:
        safe_payload = self.runtime._safe_replay_payload(event_type, payload)
        event = self.runtime._append_event(
            self.interview_id,
            event_type,
            safe_payload,
            turn_id=turn_id,
            causation_id=causation_id,
            replayability=replayability,
            organization_id=self.organization_id,
        )
        await _AGENT_CHANNEL_HUB.publish(
            self,
            event,
            live_payload=payload,
        )
        await _AGENT_CHANNEL_HUB.publish_cross_instance(self, event)
        return event


class InterviewAgentRuntime:
    """High-depth facade for the full real-time interview conversation."""

    def __init__(
        self, store: Any, *, media_plane: Optional[LiveKitMediaPlane] = None
    ) -> None:
        self.store = store
        self.persistence = persistence_for(store)
        self.interviews = InterviewService(store)
        self.avatar = AvatarService(store, persistence=self.persistence)
        self.gateway = ModelGateway(store, persistence=self.persistence)
        self.performance = AvatarPerformanceComposer()
        self.expression_audio = AgentExpressionAudioService(self.persistence)
        self.media_plane = media_plane or LiveKitMediaPlane()
        self.media_captures = InterviewMediaCaptureService(
            store, persistence=self.persistence, media_plane=self.media_plane
        )
        self.evidence_ingress = livekit_evidence_supervisor(store)

    async def open(self, opened: OpenAgentSession) -> AgentChannel:
        channel = AgentChannel(self, opened)
        await channel.initialize()
        return channel

    async def publish_snapshot(
        self,
        interview_id: str,
        organization_id: str = "org_default",
    ) -> None:
        """Fan out one role-projected state refresh through the Agent seam."""

        await self._publish_system_event(
            interview_id,
            organization_id,
            "session.snapshot",
            {},
        )

    async def sweep_expired_takeovers(
        self, organization_id: str = "org_default"
    ) -> int:
        """Recover persisted lease expiry after disconnect or process restart."""

        expired_count = 0
        for summary in self.interviews.list_interviews(organization_id):
            interview_id = str(summary.get("id") or "")
            if not interview_id:
                continue
            lease = self._expire_takeover_if_due(interview_id, organization_id)
            if not lease:
                continue
            expired_count += 1
            current = self.interviews.get_interview(interview_id, organization_id)
            if current.get("status") == "in_progress":
                self.interviews.pause_interview(
                    interview_id,
                    reason="takeover_lease_lost",
                    organization_id=organization_id,
                )
            await self._disconnect_takeover_media(
                interview_id, lease, organization_id
            )
            floor_changed = self._set_floor(
                interview_id,
                FloorOwner.NONE,
                "takeover_lease_lost",
                organization_id,
            )
            if floor_changed:
                await self._publish_system_event(
                    interview_id,
                    organization_id,
                    "floor.changed",
                    {"owner": FloorOwner.NONE.value, "reason": "takeover_lease_lost"},
                )
            await self._publish_system_event(
                interview_id,
                organization_id,
                "takeover.changed",
                {"status": "lost", "ai_resumed": False},
            )
        return expired_count

    async def _publish_system_event(
        self,
        interview_id: str,
        organization_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> None:
        safe_payload = self._safe_replay_payload(event_type, payload)
        event = self._append_event(
            interview_id,
            event_type,
            safe_payload,
            turn_id=None,
            causation_id=None,
            replayability=Replayability.REPLAYABLE,
            organization_id=organization_id,
        )
        await _AGENT_CHANNEL_HUB.receive_cross_instance(
            interview_id, organization_id, event
        )
        bus = realtime_event_bus()
        if bus.enabled:
            try:
                await bus.publish(
                    interview_id,
                    {
                        "transport": "interview_agent.v1",
                        "organization_id": organization_id,
                        "event": event.model_dump(mode="json"),
                    },
                )
            except Exception as exc:
                self._record_problem(
                    interview_id,
                    {
                        "code": "TAKEOVER_WATCHDOG_FANOUT_FAILED",
                        "message": type(exc).__name__,
                        "recoverable": False,
                        "action": "reconnect_from_authoritative_snapshot",
                    },
                    organization_id,
                )

    def _validate_open(self, session: Dict[str, Any], opened: OpenAgentSession) -> None:
        principal: Principal = opened.principal
        if principal.organization_id != session.get("organization_id"):
            raise ApiError("AGENT_SESSION_SCOPE_INVALID", "Agent session belongs to another organization.", status_code=403)
        if not principal.roles.intersection({"candidate", "admin", "interviewer", "reviewer"}):
            raise ApiError("AGENT_SESSION_FORBIDDEN", "Principal cannot open an agent session.", status_code=403)
        last_sequence = int(
            (session.get("agent_runtime") or {}).get("last_sequence", 0)
        )
        if opened.recovery_cursor > last_sequence:
            raise ApiError(
                "AGENT_RECOVERY_CURSOR_INVALID",
                "Recovery cursor is ahead of the authoritative session sequence.",
                status_code=409,
                details={"last_sequence": last_sequence},
            )
        if "candidate" not in principal.roles:
            return
        caps = opened.capabilities
        failures = []
        for field in ("webrtc", "audio_worklet", "webgl", "camera", "microphone", "speaker", "media_recorder"):
            if not getattr(caps, field):
                failures.append(field)
        if caps.avatar_fps < 30:
            failures.append("avatar_fps")
        if failures:
            raise ApiError(
                "CANDIDATE_AGENT_CAPABILITIES_NOT_READY",
                "Candidate device does not satisfy the real-time interview contract.",
                status_code=409,
                details={"failed": failures},
            )

    def _snapshot(self, session: Dict[str, Any], principal: Principal) -> Dict[str, Any]:
        current = self._current_turn(session)
        capture = self.media_captures.get_for_interview(
            session["id"], session.get("organization_id", "org_default")
        )
        candidate_view = {
            "interview_id": session["id"],
            "status": session["status"],
            "phase": session.get("phase"),
            "current_turn_id": session.get("current_turn_id"),
            "floor": (session.get("agent_runtime") or {}).get("floor", FloorOwner.NONE.value),
            "active_performance_id": (session.get("agent_runtime") or {}).get("active_performance_id"),
            "capture_recovery": candidate_capture_recovery(session),
            "supplement_confirmation": self._supplement_projection(session),
            "takeover": self._takeover_projection(
                (session.get("agent_runtime") or {}).get("takeover"),
                privileged=False,
            ),
            "calibration_status": (session.get("agent_runtime") or {}).get(
                "calibration_status", "pending"
            ),
            "calibration_retry_required": bool(
                (session.get("agent_runtime") or {}).get(
                    "calibration_retry_required", False
                )
            ),
            "recording": {
                "audio": bool(session.get("settings", {}).get("record_audio", True)),
                "video": bool(session.get("settings", {}).get("record_video", False)),
                "status": (capture or {}).get("status", "not_requested"),
            },
            "current_question": (
                {
                    "turn_id": current["id"],
                    "order": current.get("order"),
                    "is_followup": bool(current.get("is_followup")),
                    "question_text": current.get("question_spoken_text", ""),
                }
                if current
                else None
            ),
            "completed_answers": len({answer.get("turn_id") for answer in session.get("answers", [])}
                                     & {turn["id"] for turn in session.get("turns", [])
                                        if not turn.get("is_followup")}),
            "total_primary_questions": len([item for item in session.get("turns", []) if not item.get("is_followup")]),
        }
        if "candidate" in principal.roles:
            return candidate_view
        capture_status = (
            {
                key: deepcopy(value)
                for key, value in capture.items()
                if key
                in {
                    "id",
                    "status",
                    "requested_scopes",
                    "consented_scopes",
                    "retention_expires_at",
                    "started_at",
                    "stopped_at",
                    "failure_code",
                }
            }
            if capture
            else None
        )
        if not principal.roles.intersection({"admin", "interviewer"}):
            return {
                **candidate_view,
                "candidate": {
                    "id": session.get("candidate_id"),
                    "name": session.get("candidate", {}).get("name"),
                },
                "active_performance_id": (
                    session.get("agent_runtime") or {}
                ).get("active_performance_id"),
                "problems": [
                    self._public_problem_payload(item)
                    for item in deepcopy(
                        (session.get("agent_runtime") or {}).get("problems", [])
                    )[-20:]
                ],
                "media_capture": capture_status,
            }
        return {
            **candidate_view,
            "takeover": self._takeover_projection(
                (session.get("agent_runtime") or {}).get("takeover"),
                privileged=True,
            ),
            "candidate": {"id": session.get("candidate_id"), "name": session.get("candidate", {}).get("name")},
            "active_performance_id": (session.get("agent_runtime") or {}).get("active_performance_id"),
            "problems": deepcopy((session.get("agent_runtime") or {}).get("problems", []))[-20:],
            "media_capture": (
                {
                    key: deepcopy(value)
                    for key, value in capture.items()
                    if key
                    in {
                        "id",
                        "status",
                        "requested_scopes",
                        "consented_scopes",
                        "participant_identity",
                        "egress_id",
                        "private_uri",
                        "content_hash",
                        "encryption",
                        "retention_expires_at",
                        "started_at",
                        "stopped_at",
                        "failure_code",
                    }
                }
                if capture
                else None
            ),
        }

    @staticmethod
    def _safe_replay_payload(
        event_type: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Build the least-privileged representation stored in shared history.

        Candidate and enterprise principals recover from the same ordered event
        log. Privileged snapshots, takeover lease secrets, actor identities,
        evidence bindings and frozen capability points therefore never belong
        in that log. Their authoritative values remain in audited domain state
        and are projected live only to an authorized enterprise connection.
        """

        allowed_by_type = {
            "session.snapshot": set(),
            "floor.changed": {"owner", "reason", "capture_id"},
            "speech.started": {
                "speaker",
                "continued",
                "local_detected",
                "server_audio_received",
                "server_received_at",
                "acknowledged_audio_sequence",
                "audio_epoch",
                "duplicate",
                "media_transport",
                "ingress_sequence",
                "browser_backfill",
            },
            "speech.stopped": {
                "speaker",
                "endpoint_countdown_ms",
                "cancellable",
            },
            "transcript.partial": {
                "text",
                "confidence",
                "server_received",
                "calibration",
            },
            "transcript.final": {
                "text",
                "confidence",
                "authoritative",
                "persisted_audio",
                "calibration",
                "ephemeral",
            },
            "conversation.act.selected": {
                "act_id",
                "act_type",
                "text",
                "evaluative",
                "approved",
            },
            "avatar.performance.started": {
                "performance_id",
                "turn_id",
                "audio_uri",
                "audio_clock_origin_ms",
                "text",
                "visemes",
                "gestures",
                "alignment_source",
                "delivery",
                "interruptible",
                "live_audio",
            },
            "avatar.performance.producer_finished": {
                "performance_id", "output_id", "total_samples", "sample_rate_hz",
            },
            "avatar.performance.cue": {
                "performance_id",
                "at_ms",
                "duration_ms",
                "shape",
                "weight",
                "gesture",
                "intensity",
            },
            "avatar.performance.interrupted": {
                "performance_id",
                "reason",
                "deadline_ms",
            },
            "avatar.performance.stopped": {"performance_id", "reason"},
            "avatar.performance.playback": {"performance_id", "status"},
            "takeover.changed": {
                "status",
                "expires_at",
                "version",
                "ai_resumed",
            },
            "problem": {"code", "message", "recoverable", "action", "calibration", "capture_id"},
            "completed": {
                "interview_id",
                "status",
                "submitted_at",
                "recording_retention_notice",
                "human_review_required",
            },
        }
        allowed = allowed_by_type.get(event_type, set())
        result = {
            key: deepcopy(value)
            for key, value in payload.items()
            if key in allowed
        }
        if event_type == "problem":
            return InterviewAgentRuntime._public_problem_payload(result)
        return result

    def _project_event_for_principal(
        self,
        event: AgentEvent,
        principal: Principal,
        *,
        interview_id: str,
        live_payload: Optional[Dict[str, Any]] = None,
    ) -> AgentEvent:
        """Project one event without mutating its shared persisted envelope."""

        if event.type == "session.snapshot":
            session = self.interviews.get_interview(
                interview_id, principal.organization_id
            )
            payload = self._snapshot(session, principal)
        else:
            source = live_payload if live_payload is not None else event.payload
            restricted = (
                "candidate" in principal.roles
                or not principal.roles.intersection({"admin", "interviewer"})
            )
            payload = (
                self._safe_replay_payload(event.type, source)
                if restricted
                else deepcopy(source)
            )
        if event.type == "avatar.performance.started":
            # Normalize pre-delivery development history to the stable v1
            # projection without retaining a second event implementation.
            payload.setdefault("delivery", "cascade")
            if payload.get("audio_uri"):
                payload["audio_uri"] = self.expression_audio.issue_access(
                    str(payload["audio_uri"]),
                    organization_id=principal.organization_id,
                    interview_id=interview_id,
                    actor_id=principal.actor_id,
                )
        return event.model_copy(update={"payload": payload})

    def _append_event(
        self,
        interview_id: str,
        event_type: str,
        payload: Dict[str, Any],
        *,
        turn_id: Optional[str],
        causation_id: Optional[str],
        replayability: Replayability,
        organization_id: str,
    ) -> AgentEvent:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            state = session.setdefault("agent_runtime", {})
            sequence = int(state.get("last_sequence", 0)) + 1
            state["last_sequence"] = sequence
            state["last_event_at"] = utc_now()
            event = AgentEvent(
                event_id=new_id("agent_event"),
                session_sequence=sequence,
                type=event_type,
                turn_id=turn_id,
                causation_id=causation_id,
                occurred_at=utc_now(),
                replayability=replayability,
                payload=deepcopy(payload),
            )
            if replayability == Replayability.REPLAYABLE:
                history = session.setdefault("agent_events", [])
                history.append(event.model_dump(mode="json"))
                removed = history[:-1000]
                if removed:
                    state["replay_history_floor"] = max(
                        int(state.get("replay_history_floor", 0)),
                        int(removed[-1].get("session_sequence", 0)),
                    )
                    del history[:-1000]
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(session, expected_version=session["version"])
        return event

    def _signal_seen(self, interview_id: str, key: str, organization_id: str) -> bool:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            return key in (session.get("agent_runtime") or {}).get("processed_signal_keys", [])

    def _remember_signal(self, interview_id: str, key: str, organization_id: str) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            state = session.setdefault("agent_runtime", {})
            keys = state.setdefault("processed_signal_keys", [])
            if key not in keys:
                keys.append(key)
                del keys[:-2048]
                session["updated_at"] = utc_now()
                transaction.interview_sessions.update(session, expected_version=session["version"])

    def _set_floor(
        self,
        interview_id: str,
        owner: FloorOwner,
        reason: str,
        organization_id: str,
    ) -> bool:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            state = session.setdefault("agent_runtime", {})
            if state.get("floor") == owner.value:
                return False
            state["floor"] = owner.value
            state["floor_reason"] = reason
            state["floor_changed_at"] = utc_now()
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(session, expected_version=session["version"])
            return True

    def _set_active_performance(
        self, interview_id: str, performance_id: Optional[str], organization_id: str,
        *, output_id: Optional[str] = None, act_event_id: Optional[str] = None,
    ) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            session.setdefault("agent_runtime", {})["active_performance_id"] = performance_id
            session["agent_runtime"]["active_output_id"] = output_id
            session["agent_runtime"]["active_expression_act_event_id"] = act_event_id
            session["agent_runtime"]["expression_replay_act_event_id"] = None
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(session, expected_version=session["version"])

    def _clear_active_performance(
        self,
        interview_id: str,
        expected_performance_id: Optional[str],
        organization_id: str,
    ) -> bool:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(
                transaction.interview_sessions.get(interview_id)
            )
            state = session.setdefault("agent_runtime", {})
            active = state.get("active_performance_id")
            if expected_performance_id and active != expected_performance_id:
                return False
            if active is None:
                return False
            state["active_performance_id"] = None
            state["active_output_id"] = None
            state["active_expression_act_event_id"] = None
            state["expression_replay_act_event_id"] = None
            state["performance_cleared_at"] = utc_now()
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                session, expected_version=session["version"]
            )
            return True

    def _mark_streamed_expression_for_replay(
        self, interview_id: str, organization_id: str, *, performance_id: str, output_id: str,
    ) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            state = session.setdefault("agent_runtime", {})
            if (state.get("active_performance_id") != performance_id
                    or state.get("active_output_id") != output_id):
                return
            state["expression_replay_act_event_id"] = (
                state.get("active_expression_act_event_id")
                if session.get("status") == "in_progress" and state.get("floor") == FloorOwner.AGENT.value
                else None
            )
            state["active_performance_id"] = None
            state["active_output_id"] = None
            state["active_expression_act_event_id"] = None
            if state.get("floor") == FloorOwner.AGENT.value:
                state["floor"] = FloorOwner.NONE.value
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(session, expected_version=session["version"])

    def _recover_streamed_expression(self, interview_id: str, organization_id: str) -> Optional[dict]:
        """A current controller may restart an interrupted act with a NEW track.

        A worker crash has no close callback, so it can also leave orphan active
        IDs. The approved event reference, current turn/status and controller
        fence (checked by the caller) bind this recovery, never raw client text.
        """
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            state = session.setdefault("agent_runtime", {})
            reference = state.get("expression_replay_act_event_id")
            if not reference and state.get("active_output_id"):
                reference = state.get("active_expression_act_event_id")
            if not reference:
                return None
            latest = next((item for item in reversed(session.get("agent_events", []))
                           if item.get("type") == "conversation.act.selected"), None)
            replay = (
                deepcopy(latest) if latest and latest.get("event_id") == reference
                and latest.get("payload", {}).get("approved") is True
                and (latest.get("turn_id") == session.get("current_turn_id")
                     or (latest.get("turn_id") is None and latest.get("payload", {}).get("act_type") == "opening"))
                and session.get("status") == "in_progress"
                and state.get("floor") in {FloorOwner.AGENT.value, FloorOwner.NONE.value}
                else None
            )
            state["expression_replay_act_event_id"] = None
            state["active_expression_act_event_id"] = None
            state["active_performance_id"] = None
            state["active_output_id"] = None
            if state.get("floor") == FloorOwner.AGENT.value:
                state["floor"] = FloorOwner.NONE.value
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(session, expected_version=session["version"])
            return replay

    def _mark_completion_closing(
        self,
        interview_id: str,
        performance_id: str,
        organization_id: str,
    ) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            state = session.setdefault("agent_runtime", {})
            if state.get("completion_emitted_at"):
                return
            state["completion_closing_performance_id"] = performance_id
            state["completion_closing_started_at"] = utc_now()
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                session, expected_version=session["version"]
            )

    def _mark_completion_emitted(
        self,
        interview_id: str,
        reason: str,
        organization_id: str,
    ) -> bool:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            state = session.setdefault("agent_runtime", {})
            if state.get("completion_emitted_at"):
                return False
            state["completion_emitted_at"] = utc_now()
            state["completion_reason"] = reason
            state["completion_closing_performance_id"] = None
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                session, expected_version=session["version"]
            )
            return True

    def _set_calibration(
        self,
        interview_id: str,
        status: str,
        organization_id: str,
        *,
        completed: bool = False,
        retry_required: Optional[bool] = None,
    ) -> None:
        allowed = {
            "pending",
            "opening",
            "listening",
            "retrying",
            "awaiting_confirmation",
            "completed",
        }
        if status not in allowed:
            raise ValueError("unsupported warm-up calibration status")
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(
                transaction.interview_sessions.get(interview_id)
            )
            state = session.setdefault("agent_runtime", {})
            state["calibration_status"] = status
            state["calibration_updated_at"] = utc_now()
            if retry_required is not None:
                state["calibration_retry_required"] = retry_required
            if status == "completed":
                state["calibration_retry_required"] = False
            if completed:
                # Deliberately record only the deletion boundary, never the
                # warm-up transcript or audio.
                state["calibration_media_deleted_at"] = utc_now()
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                session, expected_version=session["version"]
            )

    def _approve_conversation_act(
        self,
        interview_id: str,
        *,
        act_type: str,
        text: str,
        turn_id: Optional[str],
        evidence_quotes: List[str],
        approved_by: str,
        organization_id: str,
    ) -> ApprovedConversationAct:
        """Persist the exact strong act that is eligible for expression.

        Controlled-followup decisions already freeze an act on the generated
        turn. Reusing that fact prevents a second, semantically duplicate act
        from appearing when the expression chain selects it.
        """

        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(
                transaction.interview_sessions.get(interview_id)
            )
            turn = next(
                (
                    item
                    for item in session.get("turns", [])
                    if item.get("id") == turn_id
                ),
                None,
            )
            target = (
                turn.setdefault("conversation_acts", [])
                if turn is not None
                else session.setdefault("agent_runtime", {}).setdefault(
                    "session_conversation_acts", []
                )
            )
            if act_type == "unscored_intervention":
                lease = (session.get("agent_runtime") or {}).get("takeover")
                if (
                    not lease
                    or lease.get("actor_id") != approved_by
                    or not self._lease_active(
                        lease, now=transaction.database_now()
                    )
                ):
                    raise ApiError(
                        "TAKEOVER_LEASE_REQUIRED",
                        "Human intervention requires the current active takeover lease.",
                        status_code=409,
                    )
            existing = next(
                (
                    item
                    for item in reversed(target)
                    if item.get("act_type") == act_type
                    and str(item.get("text") or "").strip() == text.strip()
                ),
                None,
            )
            if existing is not None:
                if turn is not None and existing.get("turn_id") != turn_id:
                    existing["turn_id"] = turn_id
                    existing["root_turn_id"] = (
                        turn.get("root_turn_id") or turn.get("id")
                    )
                    session["updated_at"] = utc_now()
                    transaction.interview_sessions.update(
                        session, expected_version=session["version"]
                    )
                return ApprovedConversationAct.model_validate(existing)

            if act_type == "followup":
                raise ApiError(
                    "FOLLOWUP_ACT_NOT_FROZEN",
                    "A follow-up may be expressed only from the exact act frozen by the controlled decision gate.",
                    status_code=409,
                )

            root_turn_id = (
                (turn.get("root_turn_id") or turn.get("id"))
                if turn is not None
                else None
            )
            target_points = (
                list(turn.get("target_key_points") or [])[:2]
                if turn is not None
                else []
            )
            act = ApprovedConversationAct(
                act_id=new_id("conversation_act"),
                act_type=act_type,
                text=text.strip(),
                turn_id=turn_id,
                root_turn_id=root_turn_id,
                evidence_quotes=[
                    str(item).strip()
                    for item in evidence_quotes
                    if str(item).strip()
                ][:4],
                target_capability_points=target_points,
                followup_depth=int(
                    (turn or {}).get("followup_depth", 0)
                ),
                evaluative=False,
                approved_by=approved_by,
                prompt_version=(
                    "controlled_followup.v1"
                    if act_type == "followup"
                    else "supplement_confirmation.v1" if act_type.startswith("supplement_")
                    else None
                ),
                created_at=utc_now(),
            )
            target.append(act.model_dump(mode="json"))
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                session, expected_version=session["version"]
            )
            return act

    def _acquire_takeover_lease(
        self,
        interview_id: str,
        *,
        actor_id: str,
        reason: str,
        organization_id: str,
    ) -> tuple[TakeoverLease, Optional[Dict[str, Any]]]:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(
                transaction.interview_sessions.get(interview_id)
            )
            existing = deepcopy(
                (session.get("agent_runtime") or {}).get("takeover")
            )
            database_now = transaction.database_now()
            if existing and self._lease_active(existing, now=database_now):
                raise ApiError(
                    "TAKEOVER_LEASE_HELD",
                    "An operator already owns the takeover lease; renew it instead of acquiring again.",
                    status_code=409,
                )
            lease = TakeoverLease(
                lease_id=new_id("takeover"),
                actor_id=actor_id,
                reason=reason,
                expires_at=(
                    database_now + timedelta(seconds=60)
                ).isoformat().replace("+00:00", "Z"),
                version=int((existing or {}).get("version", 0)) + 1,
            )
            session.setdefault("agent_runtime", {})["takeover"] = lease.model_dump(
                mode="json"
            )
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                session, expected_version=session["version"]
            )
            self._add_takeover_audit(
                transaction,
                interview_id=interview_id,
                organization_id=organization_id,
                lease=lease.model_dump(mode="json"),
                active=True,
                transition="acquired",
            )
            return lease, existing

    def _renew_takeover_lease(
        self,
        interview_id: str,
        *,
        actor_id: str,
        lease_id: str,
        expected_version: int,
        organization_id: str,
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(
                transaction.interview_sessions.get(interview_id)
            )
            lease = deepcopy(
                (session.get("agent_runtime") or {}).get("takeover")
            )
            database_now = transaction.database_now()
            if (
                not lease
                or lease.get("actor_id") != actor_id
                or lease.get("lease_id") != lease_id
                or int(lease.get("version", 0)) != int(expected_version)
                or not self._lease_active(lease, now=database_now)
            ):
                raise ApiError(
                    "TAKEOVER_LEASE_LOST",
                    "Takeover lease is not owned or has expired.",
                    status_code=409,
                )
            lease["expires_at"] = (
                database_now + timedelta(seconds=60)
            ).isoformat().replace("+00:00", "Z")
            lease["version"] = int(lease.get("version", 1)) + 1
            session.setdefault("agent_runtime", {})["takeover"] = lease
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                session, expected_version=session["version"]
            )
            self._add_takeover_audit(
                transaction,
                interview_id=interview_id,
                organization_id=organization_id,
                lease=lease,
                active=True,
                transition="renewed",
            )
            return lease

    def _release_takeover_lease(
        self,
        interview_id: str,
        *,
        actor_id: str,
        lease_id: str,
        expected_version: int,
        organization_id: str,
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(
                transaction.interview_sessions.get(interview_id)
            )
            lease = deepcopy(
                (session.get("agent_runtime") or {}).get("takeover")
            )
            if (
                not lease
                or lease.get("actor_id") != actor_id
                or lease.get("lease_id") != lease_id
                or int(lease.get("version", 0)) != int(expected_version)
                or not self._lease_active(
                    lease, now=transaction.database_now()
                )
            ):
                raise ApiError(
                    "TAKEOVER_LEASE_LOST",
                    "Takeover lease is not owned.",
                    status_code=409,
                )
            session.setdefault("agent_runtime", {})["takeover"] = None
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                session, expected_version=session["version"]
            )
            self._add_takeover_audit(
                transaction,
                interview_id=interview_id,
                organization_id=organization_id,
                lease=lease,
                active=False,
                transition="released",
            )
            return lease

    @staticmethod
    def _add_takeover_audit(
        transaction: Any,
        *,
        interview_id: str,
        organization_id: str,
        lease: Dict[str, Any],
        active: bool,
        transition: str,
    ) -> None:
        transaction.audit_events.add(
            {
                "id": new_id("audit"),
                "organization_id": organization_id,
                "actor_id": lease.get("actor_id", "interview_runtime"),
                "action": "interview.takeover.changed",
                "resource_type": "interview",
                "resource_id": interview_id,
                "metadata": {
                    "active": active,
                    "transition": transition,
                    "lease_id": lease.get("lease_id"),
                    "reason": lease.get("reason"),
                    "lease_version": lease.get("version"),
                },
                "created_at": utc_now(),
            }
        )

    def _store_takeover(
        self,
        interview_id: str,
        lease: Optional[Dict[str, Any]],
        organization_id: str,
        *,
        previous_lease: Optional[Dict[str, Any]] = None,
        transition: str = "changed",
    ) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            session.setdefault("agent_runtime", {})["takeover"] = deepcopy(lease)
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(session, expected_version=session["version"])
            audit_lease = lease or previous_lease or {}
            self._add_takeover_audit(
                transaction,
                interview_id=interview_id,
                organization_id=organization_id,
                lease=audit_lease,
                active=bool(lease),
                transition=transition,
            )

    def _expire_takeover_if_due(
        self, interview_id: str, organization_id: str
    ) -> Optional[Dict[str, Any]]:
        """Atomically consume one expired lease so duplicate watchdogs are safe."""

        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(
                transaction.interview_sessions.get(interview_id)
            )
            lease = deepcopy(
                (session.get("agent_runtime") or {}).get("takeover")
            )
            if not lease or self._lease_active(
                lease, now=transaction.database_now()
            ):
                return None
            session.setdefault("agent_runtime", {})["takeover"] = None
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                session, expected_version=session["version"]
            )
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": lease.get("actor_id", "interview_runtime"),
                    "action": "interview.takeover.changed",
                    "resource_type": "interview",
                    "resource_id": interview_id,
                    "metadata": {
                        "active": False,
                        "transition": "lost",
                        "lease_id": lease.get("lease_id"),
                        "reason": lease.get("reason"),
                        "lease_version": lease.get("version"),
                    },
                    "created_at": utc_now(),
                }
            )
            return lease

    def _takeover_seconds_remaining(
        self,
        interview_id: str,
        organization_id: str,
        *,
        lease_id: str,
    ) -> Optional[float]:
        """Read watchdog delay from the same database clock as lease mutation."""

        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(
                transaction.interview_sessions.get(interview_id)
            )
            lease = (session.get("agent_runtime") or {}).get("takeover")
            if not lease or lease.get("lease_id") != lease_id:
                return None
            try:
                expires = datetime.fromisoformat(
                    str(lease.get("expires_at") or "").replace("Z", "+00:00")
                )
            except ValueError:
                return 0.0
            return max(
                0.0,
                (
                    expires
                    - transaction.database_now().astimezone(timezone.utc)
                ).total_seconds(),
            )

    def _consume_takeover_for_terminal(
        self, interview_id: str, organization_id: str
    ) -> Optional[Dict[str, Any]]:
        """Atomically revoke any human media lease when the interview ends."""

        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(
                transaction.interview_sessions.get(interview_id)
            )
            lease = deepcopy(
                (session.get("agent_runtime") or {}).get("takeover")
            )
            if not lease:
                return None
            session.setdefault("agent_runtime", {})["takeover"] = None
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                session, expected_version=session["version"]
            )
            self._add_takeover_audit(
                transaction,
                interview_id=interview_id,
                organization_id=organization_id,
                lease=lease,
                active=False,
                transition="interview_completed",
            )
            return lease

    async def _disconnect_takeover_media(
        self,
        interview_id: str,
        lease: Dict[str, Any],
        organization_id: str,
    ) -> None:
        identity = str(lease.get("media_participant_identity") or "")
        if not identity:
            return
        try:
            await self.media_plane.remove_participant(
                room_name=interview_room_name(organization_id, interview_id),
                participant_identity=identity,
            )
            outcome = "removed"
        except Exception as exc:
            # Lease revocation remains authoritative even when the SFU is
            # unreachable. The short permit cannot be refreshed and the failed
            # hard-stop is made visible to operators without persisting secrets.
            outcome = "remove_failed:%s" % type(exc).__name__
            self._record_problem(
                interview_id,
                {
                    "code": "TAKEOVER_MEDIA_REVOKE_FAILED",
                    "message": type(exc).__name__,
                    "recoverable": False,
                    "action": "verify_livekit_participant_removed",
                },
                organization_id,
            )
        with self.persistence.transaction(organization_id) as transaction:
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": lease.get("actor_id", "interview_runtime"),
                    "action": "interview.takeover.media_participant.revoked",
                    "resource_type": "interview",
                    "resource_id": interview_id,
                    "metadata": {
                        "lease_id": lease.get("lease_id"),
                        "lease_version": lease.get("version"),
                        "outcome": outcome,
                    },
                    "created_at": utc_now(),
                }
            )

    async def _open_streamed_expression(
        self, interview_id: str, turn_id: Optional[str], text: str,
        organization_id: str, *, performance_id: str, assert_current,
    ) -> Optional[ApprovedSpeechOutput]:
        # Chrome MediaStream.currentTime includes sender underflow silence. The
        # real browser probe disproved sample-clock equivalence; keep this
        # transport opt-in until a content/sample playout mapping is validated.
        if os.getenv("INTERVIEWER_STREAMING_TTS_ENABLED", "false").lower() != "true":
            return None
        session = self.interviews.get_interview(interview_id, organization_id)
        current = self._current_turn(session)
        if (turn_id and current and current["id"] == turn_id and not current.get("is_followup")
                and text.strip() == str(current.get("question_spoken_text") or "").strip()):
            return None
        assert_current()
        try:
            with measure_interview_agent_stage("tts_stream_open_ms"):
                stream = await self.gateway.open_tts_stream(TTSSynthesizeRequest(
                    organization_id=organization_id, purpose="interview_agent_expression",
                    text=text, language=session.get("settings", {}).get("language", "zh-CN"),
                    voice_profile_id=session.get("settings", {}).get("voice_profile_id") or "voice_default_cn",
                    format="audio/wav",
                    metadata={"interview_id": interview_id, "turn_id": turn_id, "approved": True},
                ))
        except ProviderError as exc:
            assert_current()
            if exc.code in {"provider_streaming_not_supported", "provider_bad_request"}:
                # Transport-only unsupported cases use the existing managed
                # asset path before any PCM escapes. Never replay spoken bytes.
                return None
            raise
        try:
            assert_current()
            publisher = LiveKitApprovedAudioPublisher(
                self.media_plane, room_name=interview_room_name(organization_id, interview_id),
                performance_id=performance_id, assert_current=assert_current,
            )
            return ApprovedSpeechOutput(
                performance_id=performance_id, stream=stream, publisher=publisher,
                assert_current=assert_current,
                archive=lambda **audio: self.expression_audio.store_pcm(
                    organization_id=organization_id, interview_id=interview_id,
                    turn_id=turn_id, source_type="approved_streaming_tts", **audio,
                ),
            )
        except BaseException:
            await stream.abort()
            raise

    async def _expression_audio(
        self,
        interview_id: str,
        turn_id: Optional[str],
        text: str,
        actor_id: str,
        organization_id: str,
    ) -> Dict[str, Any]:
        session = self.interviews.get_interview(interview_id, organization_id)
        current = self._current_turn(session)
        if (turn_id and current and current["id"] == turn_id and not current.get("is_followup")
                and text.strip() == str(current.get("question_spoken_text") or "").strip()):
            response = await self.avatar.speak(
                interview_id,
                {
                    "turn_id": turn_id,
                    "language": session.get("settings", {}).get("language", "zh-CN"),
                    "voice": session.get("settings", {}).get("voice_profile_id") or "default",
                },
                actor_id=actor_id,
            )
            if response.get("mode") == "audio" and response.get("audio_uri"):
                return {
                    "audio_uri": str(response["audio_uri"]),
                    "duration_ms": response.get("duration_ms"),
                    "visemes": list(response.get("visemes") or []),
                    "delivery": "pre_generated",
                }
            # 题目预生成阶段可能仍返回开发占位结果。正式面试不能让浏览器
            # 自行朗读，但也不能因此直接暂停；统一回落到服务端受管 TTS。
        with measure_interview_agent_stage("tts_synthesis_ms"):
            response = await self.gateway.invoke(
                cap.TTS_SYNTHESIZE,
                TTSSynthesizeRequest(
                    organization_id=organization_id,
                    purpose="interview_agent_expression",
                    text=text,
                    language=session.get("settings", {}).get("language", "zh-CN"),
                    voice_profile_id=session.get("settings", {}).get("voice_profile_id") or "voice_default_cn",
                    format="audio/wav",
                    metadata={"interview_id": interview_id, "turn_id": turn_id, "approved": True},
                ),
            )
        with measure_interview_agent_stage("tts_asset_import_ms"):
            materialized = await self.expression_audio.import_tts(
                organization_id=organization_id,
                interview_id=interview_id,
                turn_id=turn_id,
                audio_uri=str(response.audio_uri),
                content_type=response.content_type,
                duration_ms=response.duration_ms,
                provider_id=response.provider.provider_id,
            )
        return {
            "audio_uri": str(materialized["audio_uri"]),
            "duration_ms": materialized.get("duration_ms"),
            "visemes": [cue.model_dump(mode="json") for cue in response.visemes],
            "delivery": "cascade",
        }

    def _pause_for_fatal_expression(self, interview_id: str, organization_id: str) -> None:
        session = self.interviews.get_interview(interview_id, organization_id)
        if session.get("status") == "in_progress":
            self.interviews.pause_interview(
                interview_id,
                reason="agent_expression_unavailable",
                organization_id=organization_id,
            )

    def _record_disconnect(
        self, interview_id: str, connection_id: str, reason: str, organization_id: str
    ) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            state = session.setdefault("agent_runtime", {})
            state["last_disconnect"] = {
                "connection_id": connection_id,
                "reason": str(reason)[:200],
                "occurred_at": utc_now(),
            }
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(session, expected_version=session["version"])

    def _record_problem(
        self,
        interview_id: str,
        payload: Dict[str, Any],
        organization_id: str,
    ) -> None:
        """Keep internal diagnostics in privileged domain state, not replay."""

        self.interviews.record_agent_problem(
            interview_id,
            payload,
            organization_id,
        )

    @staticmethod
    def _current_turn(session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        current_id = session.get("current_turn_id")
        return next((item for item in session.get("turns", []) if item.get("id") == current_id), None)

    @staticmethod
    def _takeover_projection(
        value: Optional[Dict[str, Any]], *, privileged: bool
    ) -> Optional[Dict[str, Any]]:
        if not value:
            return None
        projection = {
            "status": "active" if InterviewAgentRuntime._lease_active(value) else "lost",
            "expires_at": value.get("expires_at"),
            "version": value.get("version"),
        }
        if privileged:
            projection.update(
                {
                    "actor_id": value.get("actor_id"),
                    "reason": value.get("reason"),
                    "lease_id": value.get("lease_id"),
                    "media_permit_available": int(
                        value.get("media_permit_lease_version") or 0
                    )
                    != int(value.get("version") or 0),
                }
            )
        return projection

    @staticmethod
    def _lease_active(
        value: Dict[str, Any], *, now: Optional[datetime] = None
    ) -> bool:
        try:
            expires = datetime.fromisoformat(str(value.get("expires_at")).replace("Z", "+00:00"))
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            return expires > current.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _participant_role(principal: Principal) -> str:
        return "candidate" if "candidate" in principal.roles else "human"

    @staticmethod
    def _supplement_projection(session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        value = (session.get("agent_runtime") or {}).get("supplement_confirmation") or {}
        if (session.get("status") != "in_progress" or value.get("turn_id") != session.get("current_turn_id")
                or value.get("status") not in {"listening", "awaiting_reply"}):
            return None
        return {key: value.get(key) for key in ("status", "turn_id", "capture_id")}

    @staticmethod
    def _public_problem_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        recoverable = bool(payload.get("recoverable"))
        calibration = bool(payload.get("calibration"))
        result = {
            "code": str(payload.get("code") or "AGENT_RUNTIME_PROBLEM")[:128],
            "message": (
                "这段录音未得到有效转写，请继续说话或重新说一次。"
                if payload.get("code") == "STT_TRANSCRIPT_UNAVAILABLE" and recoverable
                else "试音暂时不可用，请重试或等待面试官接管。"
                if calibration
                else (
                    "实时面试暂时遇到问题，请重试当前操作。"
                    if recoverable
                    else "实时面试已暂停，请等待面试官处理。"
                )
            ),
            "recoverable": recoverable,
            "action": str(
                payload.get("action")
                or (
                    "retry_or_correct_signal"
                    if recoverable
                    else "pause_or_human_takeover"
                )
            )[:128],
        }
        messages = {
            "UNDERSTANDING_UNAVAILABLE": "语音已收到，暂时无法完成回答理解；正在重试，你也可以继续补充。",
            "UNDERSTANDING_RETRY_EXHAUSTED": "语音已保留，但回答理解暂时不可用。恢复后请说话或点击提前结束回答重试。",
            "TRANSCRIPT_UNAVAILABLE": "已收到音频，仍在等待本段最终转写；已有字幕和回答会保留。",
            "DETECTOR_UNAVAILABLE": "自动接话检测暂时不可用，仍在收音。",
            "ENDPOINT_UNCERTAIN": "我还在听，你可以继续补充。",
        }
        if recoverable and payload.get("code") in messages:
            result["message"] = messages[payload["code"]]
            if isinstance(payload.get("capture_id"), str):
                result["capture_id"] = payload["capture_id"][:128]
        if payload.get("code") in {"CAPTURE_RECOVERING", "CAPTURE_RETRY_REQUIRED"}:
            result["message"] = (
                "正在恢复语音识别，音频仍在保留。"
                if payload["code"] == "CAPTURE_RECOVERING" else
                "本题收音未能恢复，请重试本题；不会提交不完整回答。"
            )
            capture_id = payload.get("capture_id")
            if isinstance(capture_id, str) and 0 < len(capture_id) <= 128:
                result["capture_id"] = capture_id
        if calibration:
            result["calibration"] = True
        if payload.get("occurred_at"):
            result["occurred_at"] = str(payload["occurred_at"])[:64]
        return result

    @staticmethod
    def _require_candidate(principal: Principal) -> None:
        if "candidate" not in principal.roles:
            raise ApiError("CANDIDATE_SIGNAL_FORBIDDEN", "Signal is candidate-only.", status_code=403)

    @staticmethod
    def _require_human(principal: Principal) -> None:
        if not principal.roles.intersection({"admin", "interviewer"}):
            raise ApiError("HUMAN_TAKEOVER_FORBIDDEN", "Human takeover requires interviewer access.", status_code=403)

    @staticmethod
    def _completion_receipt(session: Dict[str, Any]) -> Dict[str, Any]:
        input_completed_at = session.get("candidate_input_completed_at")
        return {
            "interview_id": session["id"],
            "status": (
                session.get("status")
                if session.get("status") in {"completed", "report_generating", "report_ready"}
                else "submitted_for_human_review"
            ),
            "submitted_at": input_completed_at or session.get("completed_at") or utc_now(),
            "recording_retention_notice": "录音录像按邀请页同意范围和企业保留策略保存。",
            "human_review_required": True,
        }

    @staticmethod
    def _required(value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if value is None:
            raise ApiError("INTERVIEW_NOT_FOUND", "Interview does not exist.", status_code=404)
        return value


async def run_takeover_lease_watchdog(
    store: Any,
    *,
    organization_id: str = "org_default",
    interval_seconds: float = 1.0,
) -> None:
    """Continuously reconcile expired persisted leases on every API process.

    The transactional consume makes concurrent sweepers harmless. Running one
    on every process also closes the gap left by a process restart or an owner
    WebSocket disappearing before its in-channel timer fires.
    """

    runtime = InterviewAgentRuntime(store)
    while True:
        try:
            await runtime.sweep_expired_takeovers(organization_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A later pass retries authoritative state. Per-interview failures
            # are also captured by the runtime when the lease was consumed.
            pass
        await asyncio.sleep(max(0.1, float(interval_seconds)))
