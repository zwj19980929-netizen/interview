"""Connection-independent authoritative audio Evidence chain.

The chain owns the only formal streaming STT instance and its private audio
recording. It deliberately has no WebSocket, LiveKit or conversation policy
knowledge, which lets a control channel reconnect without finalizing or
duplicating the candidate's in-flight answer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from app.core.errors import ApiError
from app.domain.evidence_coordination import EvidenceCommitFence
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.services.streaming_stt import StreamingInterviewSTT
from app.services.warmup_calibration import WarmupCalibrationStream


@dataclass(frozen=True)
class EvidenceOpenResult:
    kind: str
    turn_id: Optional[str]
    events: List[Dict[str, Any]]


@dataclass(frozen=True)
class EvidenceAudioResult:
    kind: str
    turn_id: Optional[str]
    events: List[Dict[str, Any]]
    first_server_audio: bool


@dataclass(frozen=True)
class EvidenceFinishResult:
    kind: str
    turn_id: Optional[str]
    events: List[Dict[str, Any]]
    final: Optional[Dict[str, Any]]
    interview_result: Optional[Dict[str, Any]]


class InterviewEvidenceChain:
    """One interview's warm-up/formal evidence stream, independent of sockets."""

    def __init__(
        self,
        store: Any,
        interview_id: str,
        *,
        organization_id: str = "org_default",
        persistence: Optional[Persistence] = None,
        commit_fence: Optional[EvidenceCommitFence] = None,
        commit_guard: Optional[Callable[[], None]] = None,
    ) -> None:
        self.store = store
        self.interview_id = interview_id
        self.organization_id = organization_id
        self.persistence = persistence or persistence_for(store)
        self.commit_fence = commit_fence
        self.commit_guard = commit_guard
        self._stt: Optional[StreamingInterviewSTT] = None
        self._warmup: Optional[WarmupCalibrationStream] = None
        self._server_audio_seen = False
        self._finishing = False
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        return self._stt is not None or self._warmup is not None

    @property
    def kind(self) -> Optional[str]:
        if self._warmup is not None:
            return "warmup"
        if self._stt is not None:
            return "formal"
        return None

    @property
    def turn_id(self) -> Optional[str]:
        return self._stt.turn_id if self._stt is not None else None

    async def open(
        self,
        payload: Dict[str, Any],
        *,
        turn_id: Optional[str],
        calibration_status: str,
    ) -> EvidenceOpenResult:
        async with self._lock:
            if self._finishing:
                raise ApiError(
                    "AGENT_EVIDENCE_FINISHING",
                    "The previous Evidence stream is still finalizing.",
                    status_code=409,
                )
            if self.is_open:
                raise ApiError(
                    "AGENT_EVIDENCE_ALREADY_OPEN",
                    "Evidence stream is already open.",
                    status_code=409,
                )
            self._server_audio_seen = False
            if calibration_status != "completed":
                if calibration_status not in {"listening", "retrying"}:
                    raise ApiError(
                        "WARMUP_NOT_LISTENING",
                        "Finish the opening before starting microphone calibration.",
                        status_code=409,
                    )
                stream = WarmupCalibrationStream(
                    self.store,
                    self.interview_id,
                    organization_id=self.organization_id,
                    persistence=self.persistence,
                )
                events = await stream.open(payload)
                self._warmup = stream
                return EvidenceOpenResult(
                    kind="warmup", turn_id=None, events=events
                )
            stream = StreamingInterviewSTT(
                self.store,
                self.interview_id,
                organization_id=self.organization_id,
                persistence=self.persistence,
                commit_fence=self.commit_fence,
                commit_guard=self.commit_guard,
            )
            events = await stream.open({**payload, "turn_id": turn_id})
            self._stt = stream
            return EvidenceOpenResult(
                kind="formal", turn_id=stream.turn_id, events=events
            )

    async def send_audio(self, pcm_s16le: bytes) -> Optional[EvidenceAudioResult]:
        # LiveKit 会在 ASR WebSocket 尚处于握手阶段时持续送入静音帧。
        # 此时证据门仍是关闭的，必须无锁快速丢弃；如果先等待 _lock，
        # 网络握手会占住锁并让两秒媒体缓冲被无意义的静音填满。
        if not self.is_open:
            return None
        async with self._lock:
            if not self.is_open:
                # LiveKit is continuously subscribed while the Evidence gate is
                # closed. Agent speech and room ambience never enter STT.
                return None
            if not pcm_s16le or len(pcm_s16le) > 64 * 1024:
                raise ApiError(
                    "AGENT_AUDIO_FRAME_INVALID",
                    "Authoritative audio frames must contain 1 to 65536 bytes.",
                    status_code=422,
                )
            first = not self._server_audio_seen
            self._server_audio_seen = True
            if self._warmup is not None:
                events = await self._warmup.send_audio(pcm_s16le)
                return EvidenceAudioResult(
                    kind="warmup",
                    turn_id=None,
                    events=events,
                    first_server_audio=first,
                )
            assert self._stt is not None
            events = await self._stt.send_audio(pcm_s16le)
            return EvidenceAudioResult(
                kind="formal",
                turn_id=self._stt.turn_id,
                events=events,
                first_server_audio=first,
            )

    async def finish(self, payload: Dict[str, Any]) -> EvidenceFinishResult:
        async with self._lock:
            if self._warmup is not None:
                stream = self._warmup
                # 端点一旦确认就先关闭证据门；Provider final 等待期间到达的
                # LiveKit 静音不应继续排队争抢同一把锁。
                self._warmup = None
                self._finishing = True
                kind = "warmup"
                turn_id = None
            elif self._stt is not None:
                stream = self._stt
                turn_id = stream.turn_id
                self._stt = None
                self._finishing = True
                kind = "formal"
            else:
                raise ApiError(
                    "AGENT_EVIDENCE_NOT_OPEN",
                    "Evidence stream is not open.",
                    status_code=409,
                )

        try:
            if kind == "warmup":
                events = await stream.finish()
                final = next(
                    (
                        item
                        for item in events
                        if item.get("type") == "transcript.final"
                    ),
                    None,
                )
                if final is None:
                    raise ApiError(
                        "WARMUP_FINAL_INVALID",
                        "Warm-up calibration did not produce one server final.",
                        status_code=502,
                    )
                return EvidenceFinishResult(
                    kind="warmup",
                    turn_id=None,
                    events=events,
                    final=final,
                    interview_result=None,
                )
            try:
                events = await stream.finish(payload)
            except BaseException:
                try:
                    await stream.close(repair_disconnect=True)
                except BaseException:
                    pass
                raise
            return EvidenceFinishResult(
                kind="formal",
                turn_id=turn_id,
                events=events,
                final=next(
                    (
                        item
                        for item in events
                        if item.get("type") == "transcript.final"
                    ),
                    None,
                ),
                interview_result=stream.last_result,
            )
        finally:
            async with self._lock:
                self._finishing = False

    async def abort_warmup(self) -> None:
        async with self._lock:
            stream = self._warmup
            self._warmup = None
            if stream is not None:
                await stream.abort()

    async def abort(self) -> None:
        """Drop process-local streams without repairing or committing an answer."""

        async with self._lock:
            warmup = self._warmup
            formal = self._stt
            self._warmup = None
            self._stt = None
            if warmup is not None:
                await warmup.abort()
            if formal is not None:
                await formal.close(repair_disconnect=False)

    async def close_for_disconnect(self) -> Optional[EvidenceFinishResult]:
        """Repair persisted formal audio after the reconnect grace expires."""

        async with self._lock:
            if self._warmup is not None:
                stream = self._warmup
                self._warmup = None
                await stream.abort()
                return None
            if self._stt is None:
                return None
            stream = self._stt
            self._stt = None
            turn_id = stream.turn_id
            events = await stream.close(repair_disconnect=True)
            return EvidenceFinishResult(
                kind="formal",
                turn_id=turn_id,
                events=events,
                final=next(
                    (
                        item
                        for item in events
                        if item.get("type") == "transcript.final"
                    ),
                    None,
                ),
                interview_result=stream.last_result,
            )

    async def recover_persisted_turn(
        self,
        turn_id: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> EvidenceFinishResult:
        """Batch-repair a prior owner's durably complete media checkpoint."""

        async with self._lock:
            if self.is_open:
                raise ApiError(
                    "AGENT_EVIDENCE_ALREADY_OPEN",
                    "Cannot repair persisted Evidence while a stream is open.",
                    status_code=409,
                )
            stream = StreamingInterviewSTT(
                self.store,
                self.interview_id,
                organization_id=self.organization_id,
                persistence=self.persistence,
                commit_fence=self.commit_fence,
                commit_guard=self.commit_guard,
            )
            values = payload or {}
            events = await stream.recover_persisted(
                turn_id=turn_id,
                language=str(values.get("language") or "zh-CN"),
                development_transcript=values.get("development_transcript"),
                development_confidence=float(
                    values.get("development_confidence", 0.9)
                ),
            )
            return EvidenceFinishResult(
                kind="formal",
                turn_id=turn_id,
                events=events,
                final=None,
                interview_result=stream.last_result,
            )
