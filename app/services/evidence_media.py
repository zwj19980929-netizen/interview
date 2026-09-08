"""Durable sealed-segment Evidence media module.

The external interface is intentionally small: an owner opens a writer, the
writer appends frames and seals a complete checkpoint, and a current owner can
materialize that checkpoint for batch STT. Storage objects and persistence rows are
implementation details. An in-process buffer is never represented as durable.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any, Optional

from app.adapters.local_media import PCM_MIME_TYPES, pcm_wav_header
from app.core.errors import ApiError
from app.core.ids import new_id
from app.domain.evidence_coordination import EvidenceCommitFence
from app.domain.evidence_media import (
    EvidenceMediaCheckpoint,
    RecoveredEvidenceRecording,
)
from app.file_storage.interface import PrivateFileStorage
from app.file_storage.provider import private_file_storage
from app.persistence.interface import Persistence, PersistenceTransaction
from app.persistence.provider import persistence_for
from app.services.evidence_coordination import EvidenceOwnershipCoordinator, assert_current_evidence_fence


def _stream_id(interview_id: str, turn_id: str) -> str:
    digest = hashlib.sha256(
        ("%s\0%s" % (interview_id, turn_id)).encode("utf-8")
    ).hexdigest()[:32]
    return "evidence_media_%s" % digest


def _checksum(content: bytes) -> str:
    return "sha256:%s" % hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class _MediaSpec:
    interview_id: str
    turn_id: str
    content_type: str
    sample_rate_hz: int
    channels: int

    @property
    def normalized_content_type(self) -> str:
        return self.content_type.split(";", 1)[0].strip().lower()


class DurableEvidenceMediaWriter:
    """Process-local buffer whose sealed prefixes are database authoritative."""

    def __init__(
        self,
        module: "DurableEvidenceMedia",
        spec: _MediaSpec,
        fence: EvidenceCommitFence,
        checkpoint: EvidenceMediaCheckpoint,
    ) -> None:
        self._module = module
        self._spec = spec
        self._fence = fence
        self._checkpoint = checkpoint
        self._buffer = bytearray()
        self._first_buffered_sequence: Optional[int] = None
        self._next_frame_sequence = checkpoint.last_sealed_frame_sequence + 1
        self._closed = False

    @property
    def checkpoint(self) -> EvidenceMediaCheckpoint:
        return self._checkpoint

    @property
    def unsealed_byte_count(self) -> int:
        return len(self._buffer)

    def append(self, chunk: bytes) -> EvidenceMediaCheckpoint:
        if self._closed:
            raise ApiError(
                "EVIDENCE_MEDIA_WRITER_CLOSED",
                "Evidence media writer is already closed.",
                status_code=409,
            )
        if not chunk:
            return self._checkpoint
        if len(chunk) > self._module.max_frame_bytes:
            raise ApiError(
                "EVIDENCE_MEDIA_FRAME_TOO_LARGE",
                "Evidence audio frame exceeds the durable segment limit.",
                status_code=413,
            )
        if self._first_buffered_sequence is None:
            self._first_buffered_sequence = self._next_frame_sequence
        self._buffer.extend(chunk)
        self._next_frame_sequence += 1
        if len(self._buffer) >= self._module.segment_target_bytes:
            self._seal_buffer(complete=False)
        return self._checkpoint

    def seal(self, *, complete: bool = True) -> EvidenceMediaCheckpoint:
        if self._closed:
            raise ApiError(
                "EVIDENCE_MEDIA_WRITER_CLOSED",
                "Evidence media writer is already closed.",
                status_code=409,
            )
        if self._buffer:
            self._seal_buffer(complete=complete)
        elif complete:
            self._checkpoint = self._module._mark_complete(
                self._spec, self._fence, self._checkpoint.capture_revision
            )
        self._closed = complete
        return self._checkpoint

    def abort(self) -> int:
        """Discard only the process-local suffix; sealed prefixes remain."""

        discarded = len(self._buffer)
        self._buffer.clear()
        self._first_buffered_sequence = None
        self._closed = True
        return discarded

    def _seal_buffer(self, *, complete: bool) -> None:
        assert self._first_buffered_sequence is not None
        content = bytes(self._buffer)
        last_sequence = self._next_frame_sequence - 1
        self._checkpoint = self._module._seal_segment(
            self._spec,
            self._fence,
            capture_revision=self._checkpoint.capture_revision,
            content=content,
            first_frame_sequence=self._first_buffered_sequence,
            last_frame_sequence=last_sequence,
            complete=complete,
        )
        self._buffer.clear()
        self._first_buffered_sequence = None


class DurableEvidenceMedia:
    """Deep module for fenced segment sealing and verified reconstruction."""

    def __init__(
        self,
        store: Any,
        *,
        organization_id: str = "org_default",
        persistence: Optional[Persistence] = None,
        storage: Optional[PrivateFileStorage] = None,
        segment_target_bytes: Optional[int] = None,
    ) -> None:
        self.organization_id = organization_id
        self.persistence = persistence or persistence_for(store)
        self.storage = storage or private_file_storage()
        configured = segment_target_bytes or int(
            os.getenv("INTERVIEWER_EVIDENCE_SEGMENT_BYTES", "96000")
        )
        self.segment_target_bytes = max(4096, min(int(configured), 4 * 1024 * 1024))
        self.max_frame_bytes = min(self.segment_target_bytes, 1024 * 1024)

    def open_writer(
        self,
        *,
        interview_id: str,
        turn_id: str,
        content_type: str,
        sample_rate_hz: int,
        channels: int,
        fence: EvidenceCommitFence,
    ) -> DurableEvidenceMediaWriter:
        spec = self._validate_spec(
            interview_id, turn_id, content_type, sample_rate_hz, channels
        )
        stream_id = _stream_id(interview_id, turn_id)
        with self.persistence.transaction(self.organization_id) as transaction:
            assert_current_evidence_fence(transaction, fence)
            now = transaction.database_now().isoformat()
            current = transaction.evidence_media_streams.get(stream_id)
            if current is None:
                current = transaction.evidence_media_streams.add(
                    {
                        "id": stream_id,
                        "organization_id": self.organization_id,
                        "interview_id": interview_id,
                        "turn_id": turn_id,
                        "content_type": spec.normalized_content_type,
                        "sample_rate_hz": sample_rate_hz,
                        "channels": channels,
                        "status": "open",
                        "capture_revision": 1,
                        "last_sealed_ordinal": 0,
                        "last_sealed_frame_sequence": 0,
                        "sealed_byte_count": 0,
                        "ownership_epoch": fence.ownership_epoch,
                        "complete": False,
                        "recovered_audio_uri": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
            else:
                self._require_matching_spec(current, spec)
                if current.get("complete") or current.get("recovered_audio_uri"):
                    raise ApiError(
                        "EVIDENCE_MEDIA_ALREADY_COMPLETE",
                        "Evidence media for this turn is already complete.",
                        status_code=409,
                    )
                if (
                    int(current.get("ownership_epoch", 0))
                    != fence.ownership_epoch
                    and int(current.get("last_sealed_ordinal", 0)) > 0
                ):
                    raise ApiError(
                        "EVIDENCE_MEDIA_RESET_REQUIRED",
                        "A successor owner must explicitly reset an incomplete Evidence capture before recording again.",
                        status_code=409,
                    )
                expected_version = int(current["version"])
                current["ownership_epoch"] = fence.ownership_epoch
                current["updated_at"] = now
                current = transaction.evidence_media_streams.update(
                    current, expected_version=expected_version
                )
            checkpoint = self._checkpoint(current)
        return DurableEvidenceMediaWriter(self, spec, fence, checkpoint)

    def recover(
        self,
        *,
        interview_id: str,
        turn_id: str,
        fence: EvidenceCommitFence,
    ) -> RecoveredEvidenceRecording:
        """Materialize only a complete contiguous hash-verified capture.

        The method never reads another process's unsealed buffer. Empty streams,
        incomplete prefixes and ordinal/frame gaps fail closed instead of
        yielding a fake recording.
        """

        stream_id = _stream_id(interview_id, turn_id)
        with self.persistence.transaction(self.organization_id) as transaction:
            assert_current_evidence_fence(transaction, fence)
            stream = transaction.evidence_media_streams.get(stream_id)
            if stream is None or int(stream.get("last_sealed_ordinal", 0)) <= 0:
                raise ApiError(
                    "EVIDENCE_MEDIA_NOT_RECOVERABLE",
                    "No sealed Evidence media is available for repair.",
                    status_code=409,
                )
            if not stream.get("complete"):
                raise ApiError(
                    "EVIDENCE_MEDIA_INCOMPLETE",
                    "The sealed Evidence prefix is incomplete and cannot form a CandidateAnswer.",
                    status_code=409,
                )
            if stream.get("recovered_audio_uri"):
                return self._existing_recovery(transaction, stream, fence)
            normalized = str(stream.get("content_type") or "").lower()
            if normalized not in PCM_MIME_TYPES:
                raise ApiError(
                    "EVIDENCE_MEDIA_FORMAT_NOT_RECOVERABLE",
                    "Durable owner-loss repair currently requires PCM16 Evidence segments.",
                    status_code=409,
                )
            segments = sorted(
                (
                    item
                    for item in transaction.evidence_media_segments.list()
                    if item.get("stream_id") == stream_id
                    and int(item.get("capture_revision", 1))
                    == int(stream.get("capture_revision", 1))
                ),
                key=lambda item: int(item.get("ordinal", 0)),
            )
            expected_count = int(stream["last_sealed_ordinal"])
            expected_last_frame = int(stream["last_sealed_frame_sequence"])
            expected_byte_count = int(stream["sealed_byte_count"])
            if len(segments) != expected_count:
                raise ApiError(
                    "EVIDENCE_MEDIA_SEGMENT_GAP",
                    "Sealed Evidence segment count does not match its checkpoint.",
                    status_code=409,
                )
            objects = []
            expected_frame = 1
            for ordinal, segment in enumerate(segments, start=1):
                if (
                    int(segment.get("ordinal", 0)) != ordinal
                    or int(segment.get("first_frame_sequence", 0)) != expected_frame
                    or int(segment.get("last_frame_sequence", 0))
                    < int(segment.get("first_frame_sequence", 0))
                ):
                    raise ApiError(
                        "EVIDENCE_MEDIA_SEGMENT_GAP",
                        "Sealed Evidence segments are not a contiguous prefix.",
                        status_code=409,
                    )
                file_object = transaction.file_objects.get(str(segment["file_id"]))
                if (
                    file_object is None
                    or file_object.get("status") != "ready"
                    or file_object.get("purpose") != "candidate_evidence_segment"
                    or file_object.get("interview_id") != interview_id
                    or file_object.get("turn_id") != turn_id
                ):
                    raise ApiError(
                        "EVIDENCE_MEDIA_SEGMENT_INVALID",
                        "A sealed Evidence segment is unavailable or out of scope.",
                        status_code=409,
                    )
                objects.append((segment, file_object))
                expected_frame = int(segment["last_frame_sequence"]) + 1

        chunks = []
        for segment, file_object in objects:
            try:
                content = self.storage.open(str(file_object["object_key"]))
            except Exception as exc:
                raise ApiError(
                    "EVIDENCE_MEDIA_SEGMENT_UNAVAILABLE",
                    "A sealed Evidence segment could not be read.",
                    status_code=503,
                    details={"error_type": type(exc).__name__},
                ) from exc
            if (
                len(content) != int(segment["byte_count"])
                or _checksum(content) != segment["checksum"]
                or segment["checksum"] != file_object.get("checksum")
            ):
                raise ApiError(
                    "EVIDENCE_MEDIA_CHECKSUM_MISMATCH",
                    "A sealed Evidence segment failed integrity verification.",
                    status_code=409,
                )
            chunks.append(content)
        pcm = b"".join(chunks)
        if not pcm:
            raise ApiError(
                "EVIDENCE_MEDIA_NOT_RECOVERABLE",
                "No sealed Evidence bytes are available for repair.",
                status_code=409,
            )
        wav = pcm_wav_header(
            len(pcm),
            sample_rate_hz=int(stream["sample_rate_hz"]),
            channels=int(stream["channels"]),
        ) + pcm
        file_id = new_id("file")
        stored = self.storage.store(
            organization_id=self.organization_id,
            object_id=file_id,
            content=wav,
            content_type="audio/wav",
            checksum=_checksum(wav),
        )
        try:
            with self.persistence.transaction(self.organization_id) as transaction:
                assert_current_evidence_fence(transaction, fence)
                current = transaction.evidence_media_streams.get(stream_id)
                if current is None:
                    raise ApiError(
                        "EVIDENCE_MEDIA_NOT_RECOVERABLE",
                        "Evidence media checkpoint disappeared during repair.",
                        status_code=409,
                    )
                if (
                    int(current.get("capture_revision", 1))
                    != int(stream.get("capture_revision", 1))
                    or not current.get("complete")
                    or int(current.get("last_sealed_ordinal", 0)) != expected_count
                    or int(current.get("last_sealed_frame_sequence", 0))
                    != expected_last_frame
                    or int(current.get("sealed_byte_count", 0))
                    != expected_byte_count
                ):
                    raise ApiError(
                        "EVIDENCE_MEDIA_CHECKPOINT_CHANGED",
                        "Evidence media checkpoint changed during repair; retry from the new prefix.",
                        status_code=409,
                    )
                if current.get("recovered_audio_uri"):
                    # Another retry completed first. Do not publish a second
                    # answer recording; delete this attempt below.
                    existing = self._existing_recovery(transaction, current, fence)
                    self.storage.delete(stored.object_key)
                    return existing
                now = transaction.database_now().isoformat()
                transaction.file_objects.add(
                    {
                        "id": file_id,
                        "organization_id": self.organization_id,
                        "purpose": "candidate_answer_audio",
                        "status": "ready",
                        "storage_backend": stored.storage_backend,
                        "object_key": stored.object_key,
                        "content_type": stored.content_type,
                        "checksum": stored.checksum,
                        "byte_count": stored.byte_count,
                        "scan_status": "not_applicable",
                        "source_type": "evidence_segment_repair",
                        "interview_id": interview_id,
                        "turn_id": turn_id,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                expected_version = int(current["version"])
                current["recovered_audio_uri"] = "private-file://%s" % file_id
                current["recovered_byte_count"] = stored.byte_count
                current["recovered_source_pcm_byte_count"] = len(pcm)
                current["recovered_at"] = now
                current["recovered_by_epoch"] = fence.ownership_epoch
                current["status"] = "recovered"
                current["updated_at"] = now
                current = transaction.evidence_media_streams.update(
                    current, expected_version=expected_version
                )
                return self._recovery(current, fence)
        except BaseException:
            self.storage.delete(stored.object_key)
            raise

    def reset_incomplete(
        self,
        *,
        interview_id: str,
        turn_id: str,
        fence: EvidenceCommitFence,
        reason: str,
    ) -> EvidenceMediaCheckpoint:
        """Abandon an incomplete capture before the candidate repeats it."""

        stream_id = _stream_id(interview_id, turn_id)
        with self.persistence.transaction(self.organization_id) as transaction:
            assert_current_evidence_fence(transaction, fence)
            stream = transaction.evidence_media_streams.get(stream_id)
            if stream is None:
                raise ApiError(
                    "EVIDENCE_MEDIA_STREAM_NOT_OPEN",
                    "Evidence media stream is not open.",
                    status_code=409,
                )
            if stream.get("complete") or stream.get("recovered_audio_uri"):
                raise ApiError(
                    "EVIDENCE_MEDIA_ALREADY_COMPLETE",
                    "Completed Evidence media cannot be reset.",
                    status_code=409,
                )
            return self._advance_capture(
                transaction, stream, fence,
                reason=str(reason or "owner_loss")[:128],
            )

    def prepare_candidate_retry(
        self, *, interview_id: str, turn_id: str, capture_id: str,
        fence: EvidenceCommitFence,
    ) -> bool:
        """Advance only the durable failed, unanswered capture, at most once.

        A failed open may be retried without archiving another empty revision.
        The candidate cannot name a recording or request a general reset.
        """
        if fence.ownership_id != EvidenceOwnershipCoordinator.ownership_id(interview_id):
            raise ApiError("EVIDENCE_OWNER_FENCED", "Capture retry belongs to another interview.", status_code=409)
        with self.persistence.transaction(self.organization_id) as transaction:
            assert_current_evidence_fence(transaction, fence)
            session = transaction.interview_sessions.get(interview_id)
            state = (session or {}).get("agent_runtime") or {}
            recovery = state.get("capture_recovery") or {}
            if (not session or session.get("status") != "in_progress"
                    or session.get("current_turn_id") != turn_id
                    or any(answer.get("turn_id") == turn_id for answer in session.get("answers", []))
                    or (state.get("takeover") or {}).get("status") == "active"
                    or recovery.get("status") != "retry_required"
                    or recovery.get("turn_id") != turn_id
                    or recovery.get("capture_id") != capture_id):
                return False
            stream = transaction.evidence_media_streams.get(_stream_id(interview_id, turn_id))
            if (not stream or stream.get("complete") or stream.get("recovered_audio_uri")):
                raise ApiError("EVIDENCE_MEDIA_ALREADY_COMPLETE", "Incomplete capture required for retry.", status_code=409)
            revision = int(stream.get("capture_revision", 1))
            if recovery.get("retry_capture_revision") == revision:
                return True
            if recovery.get("capture_revision") != revision:
                raise ApiError("EVIDENCE_MEDIA_CHECKPOINT_CHANGED", "Failed capture changed.", status_code=409)
            checkpoint = self._advance_capture(transaction, stream, fence, reason="candidate_capture_retry")
            recovery["retry_capture_revision"] = checkpoint.capture_revision
            state["capture_recovery"] = recovery
            session["agent_runtime"] = state
            transaction.interview_sessions.update(session, expected_version=session["version"])
            return True

    @staticmethod
    def assert_complete_capture(
        transaction: PersistenceTransaction,
        *,
        interview_id: str,
        turn_id: str,
        media_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Fence a semantic result to the exact sealed capture, not just its owner."""

        stream_id = _stream_id(interview_id, turn_id)
        stream = transaction.evidence_media_streams.get(stream_id)
        if (
            stream is None
            or not stream.get("complete")
            or media_evidence.get("mode") != "sealed_segments"
            or media_evidence.get("complete") is not True
            or media_evidence.get("stream_id") != stream_id
            or media_evidence.get("capture_revision") != stream.get("capture_revision", 1)
            or media_evidence.get("sealed_segment_count") != stream.get("last_sealed_ordinal")
            or media_evidence.get("last_sealed_frame_sequence") != stream.get("last_sealed_frame_sequence")
        ):
            raise ApiError(
                "EVIDENCE_MEDIA_CHECKPOINT_CHANGED",
                "The transcript no longer matches the complete Evidence capture.",
                status_code=409,
            )
        return stream

    @classmethod
    def release_rejected_capture(
        cls,
        transaction: PersistenceTransaction,
        *,
        interview_id: str,
        turn_id: str,
        media_evidence: dict[str, Any],
        utterance_id: str,
        fence: EvidenceCommitFence,
    ) -> EvidenceMediaCheckpoint:
        """Advance capture in the same transaction that rejects an utterance.

        This is not a general reset: the authoritative lifecycle must have
        committed its non-answer decision in this transaction. Old sealed
        segments and the rejected utterance remain immutable evidence.
        """

        assert_current_evidence_fence(transaction, fence)
        stream = cls.assert_complete_capture(
            transaction, interview_id=interview_id, turn_id=turn_id,
            media_evidence=media_evidence,
        )
        session = transaction.interview_sessions.get(interview_id) or {}
        turn = next((item for item in session.get("turns", []) if item["id"] == turn_id), {})
        rejected = any(
            event.get("type") == "utterance.not_accepted"
            and event.get("payload", {}).get("turn_id") == turn_id
            and event.get("payload", {}).get("utterance_id") == utterance_id
            for event in session.get("lifecycle_events", [])
        )
        if (
            turn.get("status") != "asking"
            or (turn.get("current_understanding") or {}).get("utterance_id") != utterance_id
            or not rejected
            or any(answer.get("turn_id") == turn_id for answer in session.get("answers", []))
        ):
            raise ApiError(
                "EVIDENCE_MEDIA_REJECTION_REQUIRED",
                "Only a committed non-answer may release a complete Evidence capture.",
                status_code=409,
            )
        return cls._advance_capture(
            transaction, stream, fence,
            reason="utterance_not_accepted", rejected_utterance_id=utterance_id,
        )

    @classmethod
    def release_untranscribed_capture(
        cls, transaction: PersistenceTransaction, *, interview_id: str, turn_id: str,
        media_evidence: dict[str, Any], audio_uri: str, fence: EvidenceCommitFence,
    ) -> EvidenceMediaCheckpoint:
        """Release only the exact recording marked transcription-unavailable in this transaction."""
        assert_current_evidence_fence(transaction, fence)
        stream = cls.assert_complete_capture(
            transaction, interview_id=interview_id, turn_id=turn_id, media_evidence=media_evidence,
        )
        session = transaction.interview_sessions.get(interview_id) or {}
        turn = next((item for item in session.get("turns", []) if item["id"] == turn_id), {})
        if (
            turn.get("status") != "asking"
            or turn.get("transcription_error") != "STT_TRANSCRIPT_UNAVAILABLE"
            or (turn.get("recording") or {}).get("audio_uri") != audio_uri
            or any(item.get("turn_id") == turn_id for item in session.get("answers", []))
        ):
            raise ApiError("EVIDENCE_MEDIA_REJECTION_REQUIRED", "The recording has not been marked untranscribed.", status_code=409)
        return cls._advance_capture(
            transaction, stream, fence, reason="transcript_unavailable",
            untranscribed_audio_uri=audio_uri,
        )

    @classmethod
    def _advance_capture(
        cls, transaction: PersistenceTransaction, stream: dict[str, Any],
        fence: EvidenceCommitFence, *, reason: str,
        rejected_utterance_id: Optional[str] = None,
        untranscribed_audio_uri: Optional[str] = None,
    ) -> EvidenceMediaCheckpoint:
        now = transaction.database_now().isoformat()
        expected_version = int(stream["version"])
        archived = {
            "capture_revision": int(stream.get("capture_revision", 1)),
            "sealed_segment_count": int(stream.get("last_sealed_ordinal", 0)),
            "last_sealed_frame_sequence": int(stream.get("last_sealed_frame_sequence", 0)),
            "sealed_byte_count": int(stream.get("sealed_byte_count", 0)),
            "abandoned_by_epoch": fence.ownership_epoch,
            "reason": reason,
            "abandoned_at": now,
        }
        if rejected_utterance_id is not None:
            archived["rejected_utterance_id"] = rejected_utterance_id
            archived["recovered_audio_uri"] = stream.get("recovered_audio_uri")
        if untranscribed_audio_uri is not None:
            archived["untranscribed_audio_uri"] = untranscribed_audio_uri
        stream.setdefault("abandoned_captures", []).append(archived)
        stream.update(
            capture_revision=int(stream.get("capture_revision", 1)) + 1,
            last_sealed_ordinal=0, last_sealed_frame_sequence=0, sealed_byte_count=0,
            ownership_epoch=fence.ownership_epoch, complete=False, status="open",
            updated_at=now, recovered_audio_uri=None,
        )
        for key in ("recovered_byte_count", "recovered_source_pcm_byte_count", "recovered_at", "recovered_by_epoch"):
            stream.pop(key, None)
        stream = transaction.evidence_media_streams.update(stream, expected_version=expected_version)
        return cls._checkpoint(stream)

    @staticmethod
    def _assert_writable_capture(stream: dict[str, Any], capture_revision: int) -> None:
        if int(stream.get("capture_revision", 1)) != capture_revision:
            raise ApiError(
                "EVIDENCE_MEDIA_CHECKPOINT_CHANGED",
                "This writer belongs to an earlier Evidence capture.",
                status_code=409,
            )
        if stream.get("complete") or stream.get("recovered_audio_uri"):
            raise ApiError(
                "EVIDENCE_MEDIA_ALREADY_COMPLETE",
                "Completed Evidence media cannot receive more audio.",
                status_code=409,
            )

    def _seal_segment(
        self,
        spec: _MediaSpec,
        fence: EvidenceCommitFence,
        *,
        capture_revision: int,
        content: bytes,
        first_frame_sequence: int,
        last_frame_sequence: int,
        complete: bool,
    ) -> EvidenceMediaCheckpoint:
        file_id = new_id("file")
        checksum = _checksum(content)
        stored = self.storage.store(
            organization_id=self.organization_id,
            object_id=file_id,
            content=content,
            content_type="application/octet-stream",
            checksum=checksum,
        )
        stream_id = _stream_id(spec.interview_id, spec.turn_id)
        try:
            with self.persistence.transaction(self.organization_id) as transaction:
                assert_current_evidence_fence(transaction, fence)
                stream = transaction.evidence_media_streams.get(stream_id)
                if stream is None:
                    raise ApiError(
                        "EVIDENCE_MEDIA_STREAM_NOT_OPEN",
                        "Evidence media stream is not open.",
                        status_code=409,
                    )
                self._require_matching_spec(stream, spec)
                self._assert_writable_capture(stream, capture_revision)
                ordinal = int(stream.get("last_sealed_ordinal", 0)) + 1
                if first_frame_sequence != int(
                    stream.get("last_sealed_frame_sequence", 0)
                ) + 1:
                    raise ApiError(
                        "EVIDENCE_MEDIA_SEQUENCE_CONFLICT",
                        "Evidence frame sequence is not contiguous with the durable checkpoint.",
                        status_code=409,
                    )
                now = transaction.database_now().isoformat()
                transaction.file_objects.add(
                    {
                        "id": file_id,
                        "organization_id": self.organization_id,
                        "purpose": "candidate_evidence_segment",
                        "status": "ready",
                        "storage_backend": stored.storage_backend,
                        "object_key": stored.object_key,
                        "content_type": "application/octet-stream",
                        "checksum": checksum,
                        "byte_count": len(content),
                        "scan_status": "not_applicable",
                        "source_type": "authoritative_evidence_ingress",
                        "interview_id": spec.interview_id,
                        "turn_id": spec.turn_id,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                transaction.evidence_media_segments.add(
                    {
                        "id": "%s_capture_%04d_segment_%08d"
                        % (
                            stream_id,
                            int(stream.get("capture_revision", 1)),
                            ordinal,
                        ),
                        "organization_id": self.organization_id,
                        "stream_id": stream_id,
                        "capture_revision": int(
                            stream.get("capture_revision", 1)
                        ),
                        "interview_id": spec.interview_id,
                        "turn_id": spec.turn_id,
                        "ordinal": ordinal,
                        "first_frame_sequence": first_frame_sequence,
                        "last_frame_sequence": last_frame_sequence,
                        "byte_count": len(content),
                        "checksum": checksum,
                        "file_id": file_id,
                        "ownership_epoch": fence.ownership_epoch,
                        "sealed_at": now,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                expected_version = int(stream["version"])
                stream["last_sealed_ordinal"] = ordinal
                stream["last_sealed_frame_sequence"] = last_frame_sequence
                stream["sealed_byte_count"] = int(
                    stream.get("sealed_byte_count", 0)
                ) + len(content)
                stream["ownership_epoch"] = fence.ownership_epoch
                stream["complete"] = bool(complete)
                stream["status"] = "complete" if complete else "open"
                stream["updated_at"] = now
                stream = transaction.evidence_media_streams.update(
                    stream, expected_version=expected_version
                )
                return self._checkpoint(stream)
        except BaseException:
            self.storage.delete(stored.object_key)
            raise

    def _mark_complete(
        self, spec: _MediaSpec, fence: EvidenceCommitFence, capture_revision: int
    ) -> EvidenceMediaCheckpoint:
        stream_id = _stream_id(spec.interview_id, spec.turn_id)
        with self.persistence.transaction(self.organization_id) as transaction:
            assert_current_evidence_fence(transaction, fence)
            stream = transaction.evidence_media_streams.get(stream_id)
            if stream is None:
                raise ApiError(
                    "EVIDENCE_MEDIA_STREAM_NOT_OPEN",
                    "Evidence media stream is not open.",
                    status_code=409,
                )
            self._assert_writable_capture(stream, capture_revision)
            if int(stream.get("last_sealed_ordinal", 0)) <= 0:
                raise ApiError(
                    "EVIDENCE_MEDIA_NOT_RECOVERABLE",
                    "Empty Evidence media cannot be marked complete.",
                    status_code=409,
                )
            expected_version = int(stream["version"])
            stream["complete"] = True
            stream["status"] = "complete"
            stream["ownership_epoch"] = fence.ownership_epoch
            stream["updated_at"] = transaction.database_now().isoformat()
            stream = transaction.evidence_media_streams.update(
                stream, expected_version=expected_version
            )
            return self._checkpoint(stream)

    @staticmethod
    def _validate_spec(
        interview_id: str,
        turn_id: str,
        content_type: str,
        sample_rate_hz: int,
        channels: int,
    ) -> _MediaSpec:
        if not interview_id or not turn_id:
            raise ApiError(
                "EVIDENCE_MEDIA_SCOPE_INVALID",
                "Interview and turn are required for Evidence media.",
                status_code=422,
            )
        normalized = content_type.split(";", 1)[0].strip().lower()
        if normalized not in PCM_MIME_TYPES:
            raise ApiError(
                "EVIDENCE_MEDIA_FORMAT_NOT_RECOVERABLE",
                "Durable Evidence media requires PCM16 input.",
                status_code=415,
            )
        pcm_wav_header(0, sample_rate_hz=sample_rate_hz, channels=channels)
        return _MediaSpec(
            interview_id=interview_id,
            turn_id=turn_id,
            content_type=content_type,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
        )

    @staticmethod
    def _require_matching_spec(stream: dict, spec: _MediaSpec) -> None:
        if (
            stream.get("interview_id") != spec.interview_id
            or stream.get("turn_id") != spec.turn_id
            or stream.get("content_type") != spec.normalized_content_type
            or int(stream.get("sample_rate_hz", 0)) != spec.sample_rate_hz
            or int(stream.get("channels", 0)) != spec.channels
        ):
            raise ApiError(
                "EVIDENCE_MEDIA_SPEC_CONFLICT",
                "Evidence media format changed within one interview turn.",
                status_code=409,
            )

    @staticmethod
    def _checkpoint(stream: dict) -> EvidenceMediaCheckpoint:
        count = int(stream.get("last_sealed_ordinal", 0))
        return EvidenceMediaCheckpoint(
            stream_id=str(stream["id"]),
            interview_id=str(stream["interview_id"]),
            turn_id=str(stream["turn_id"]),
            capture_revision=int(stream.get("capture_revision", 1)),
            sealed_segment_count=count,
            last_sealed_frame_sequence=int(
                stream.get("last_sealed_frame_sequence", 0)
            ),
            sealed_byte_count=int(stream.get("sealed_byte_count", 0)),
            ownership_epoch=int(stream.get("ownership_epoch", 1)),
            recoverability=(
                "complete"
                if stream.get("complete") and count > 0
                else "sealed_prefix" if count > 0 else "none"
            ),
        )

    def _existing_recovery(
        self, transaction: Any, stream: dict, fence: EvidenceCommitFence
    ) -> RecoveredEvidenceRecording:
        file_id = str(stream["recovered_audio_uri"]).removeprefix(
            "private-file://"
        )
        file_object = transaction.file_objects.get(file_id)
        if (
            file_object is None
            or file_object.get("status") != "ready"
            or file_object.get("purpose") != "candidate_answer_audio"
            or file_object.get("interview_id") != stream.get("interview_id")
            or file_object.get("turn_id") != stream.get("turn_id")
        ):
            raise ApiError(
                "EVIDENCE_MEDIA_RECOVERY_INVALID",
                "Recovered Evidence recording is missing or out of scope.",
                status_code=409,
            )
        try:
            content = self.storage.open(str(file_object["object_key"]))
        except Exception as exc:
            raise ApiError(
                "EVIDENCE_MEDIA_RECOVERY_UNAVAILABLE",
                "Recovered Evidence recording could not be read.",
                status_code=503,
                details={"error_type": type(exc).__name__},
            ) from exc
        if (
            len(content) != int(file_object.get("byte_count", 0))
            or _checksum(content) != file_object.get("checksum")
        ):
            raise ApiError(
                "EVIDENCE_MEDIA_CHECKSUM_MISMATCH",
                "Recovered Evidence recording failed integrity verification.",
                status_code=409,
            )
        return self._recovery(stream, fence)

    @staticmethod
    def _recovery(
        stream: dict, fence: EvidenceCommitFence
    ) -> RecoveredEvidenceRecording:
        return RecoveredEvidenceRecording(
            stream_id=str(stream["id"]),
            interview_id=str(stream["interview_id"]),
            turn_id=str(stream["turn_id"]),
            capture_revision=int(stream.get("capture_revision", 1)),
            audio_uri=str(stream["recovered_audio_uri"]),
            content_type="audio/wav",
            byte_count=int(stream["recovered_byte_count"]),
            source_pcm_byte_count=int(stream["recovered_source_pcm_byte_count"]),
            sample_rate_hz=int(stream["sample_rate_hz"]),
            channels=int(stream["channels"]),
            last_sealed_frame_sequence=int(
                stream["last_sealed_frame_sequence"]
            ),
            sealed_segment_count=int(stream["last_sealed_ordinal"]),
            ownership_epoch=fence.ownership_epoch,
            complete=bool(stream.get("complete")),
        )
