"""Bounded browser audio-gap recovery for the authoritative Evidence module.

This is deliberately not a second audio transport. LiveKit remains the formal
continuous media path. During a detected media gap, a candidate may replay at
most thirty seconds from the browser's AES-GCM ring over *JSON* control
messages. Every clear PCM frame is first persisted in private storage; the
database-fenced Evidence owner then reads that object and feeds the existing
``InterviewEvidenceChain``. Raw audio never enters the command journal.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Optional

from app.core.errors import ApiError
from app.core.time import utc_now
from app.domain.evidence_coordination import EvidenceCommitFence
from app.file_storage.interface import PrivateFileStorage
from app.file_storage.provider import private_file_storage
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.services.evidence_coordination import assert_current_evidence_fence


_PROTOCOL = "agent-json-backfill.v1"
_MAX_BATCHES = 16


def _sha256(content: bytes) -> str:
    return "sha256:%s" % hashlib.sha256(content).hexdigest()


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256(
        "\0".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()[:40]
    return "%s_%s" % (prefix, digest)


@dataclass(frozen=True)
class BackfillBatch:
    batch_id: str
    audio_epoch: str
    source_connection_id: str
    turn_id: str
    first_sequence: int
    last_sequence: int
    total_bytes: int
    ack_through: int
    status: str


@dataclass(frozen=True)
class BackfillFrame:
    batch_id: str
    file_id: str
    audio_epoch: str
    client_sequence: int
    checksum: str
    byte_count: int
    duplicate: bool = False

    def command_payload(self) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "file_id": self.file_id,
            "audio_epoch": self.audio_epoch,
            "client_sequence": self.client_sequence,
            "checksum": self.checksum,
            "byte_count": self.byte_count,
        }


class BrowserAudioBackfill:
    """Authorize, stage, apply and retire one bounded recovery batch."""

    def __init__(
        self,
        store: Any,
        *,
        persistence: Optional[Persistence] = None,
        storage: Optional[PrivateFileStorage] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.storage = storage or private_file_storage()

    def authorize_gap(
        self,
        *,
        interview_id: str,
        organization_id: str,
        turn_id: Optional[str],
        fence: Optional[EvidenceCommitFence],
        reason: str,
    ) -> None:
        """Open a server-observed thirty-second recovery admission window."""

        if not turn_id:
            return
        with self.persistence.transaction(organization_id) as transaction:
            if fence is not None:
                assert_current_evidence_fence(transaction, fence)
            interview = transaction.interview_sessions.get(interview_id)
            if interview is None:
                return
            now = transaction.database_now()
            runtime = interview.setdefault("agent_runtime", {})
            previous = runtime.get("browser_backfill_gap") or {}
            runtime["browser_backfill_gap"] = {
                "generation": int(previous.get("generation", 0)) + 1,
                "status": "authorized",
                "turn_id": turn_id,
                "reason": str(reason or "media_gap")[:64],
                "opened_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=30)).isoformat(),
                "ownership_epoch": fence.ownership_epoch if fence else None,
            }
            interview["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                interview, expected_version=interview["version"]
            )

    def begin(
        self,
        *,
        interview_id: str,
        organization_id: str,
        actor_id: str,
        current_connection_id: str,
        turn_id: Optional[str],
        payload: Dict[str, Any],
    ) -> BackfillBatch:
        effective_turn = str(turn_id or "")
        source_connection = str(payload.get("source_connection_id") or "")
        audio_epoch = str(payload.get("audio_epoch") or "")
        if not effective_turn or effective_turn == "__warmup__":
            raise ApiError(
                "BROWSER_BACKFILL_FORMAL_TURN_REQUIRED",
                "Browser backfill is restricted to a formal frozen interview turn.",
                status_code=409,
            )
        try:
            first_sequence = int(payload.get("first_sequence") or 0)
            last_sequence = int(payload.get("last_sequence") or 0)
            total_bytes = int(payload.get("total_bytes") or 0)
            captured_from_ms = int(payload.get("captured_from_ms") or 0)
            captured_to_ms = int(payload.get("captured_to_ms") or 0)
        except (TypeError, ValueError) as exc:
            raise ApiError(
                "BROWSER_BACKFILL_CONTRACT_INVALID",
                "Browser backfill boundaries must be integers.",
                status_code=422,
            ) from exc
        with self.persistence.transaction(organization_id) as transaction:
            current_ticket, source_ticket, capability = self._authorize_locked(
                transaction,
                interview_id=interview_id,
                actor_id=actor_id,
                current_connection_id=current_connection_id,
                source_connection_id=source_connection,
                audio_epoch=audio_epoch,
            )
            del current_ticket, source_ticket
            retention_ms = int(capability.get("retention_ms") or 0)
            max_bytes = int(capability.get("max_bytes") or 0)
            if (
                retention_ms != 30_000
                or not (1 <= first_sequence <= last_sequence <= 2**53 - 1)
                or last_sequence - first_sequence + 1 > 4096
                or not (1 <= total_bytes <= max_bytes <= 2 * 1024 * 1024)
                or captured_from_ms <= 0
                or captured_to_ms < captured_from_ms
                or captured_to_ms - captured_from_ms > retention_ms
            ):
                raise ApiError(
                    "BROWSER_BACKFILL_LIMIT_EXCEEDED",
                    "Browser backfill exceeds the frozen 30-second or byte limit.",
                    status_code=413,
                )
            interview = transaction.interview_sessions.get(interview_id)
            if interview is None:
                raise ApiError(
                    "INTERVIEW_NOT_FOUND", "Interview does not exist.", status_code=404
                )
            if str(interview.get("current_turn_id") or "") != effective_turn:
                raise ApiError(
                    "BROWSER_BACKFILL_TURN_STALE",
                    "Browser backfill belongs to a stale interview turn.",
                    status_code=409,
                )
            batch_id = _stable_id(
                "browser_backfill",
                interview_id,
                effective_turn,
                audio_epoch,
                first_sequence,
                last_sequence,
            )
            runtime = interview.setdefault("agent_runtime", {})
            batches = runtime.setdefault("browser_backfill_batches", {})
            existing = batches.get(batch_id)
            fingerprint = {
                "audio_epoch": audio_epoch,
                "source_connection_id": source_connection,
                "turn_id": effective_turn,
                "first_sequence": first_sequence,
                "last_sequence": last_sequence,
                "total_bytes": total_bytes,
                "captured_from_ms": captured_from_ms,
                "captured_to_ms": captured_to_ms,
            }
            if existing is not None:
                if any(existing.get(key) != value for key, value in fingerprint.items()):
                    raise ApiError(
                        "BROWSER_BACKFILL_BATCH_CONFLICT",
                        "The recovery batch identity was reused with different bounds.",
                        status_code=409,
                    )
                return self._batch(batch_id, existing)
            gap = runtime.get("browser_backfill_gap") or {}
            if (
                gap.get("status") != "authorized"
                or gap.get("turn_id") != effective_turn
                or transaction.database_now().isoformat()
                > str(gap.get("expires_at") or "")
            ):
                raise ApiError(
                    "BROWSER_BACKFILL_GAP_NOT_AUTHORIZED",
                    "The server did not observe an eligible media gap for this turn.",
                    status_code=409,
                )
            active = [
                item
                for item in batches.values()
                if item.get("status") == "open"
                and item.get("turn_id") == effective_turn
            ]
            if active:
                raise ApiError(
                    "BROWSER_BACKFILL_ALREADY_OPEN",
                    "Finish the current browser recovery batch before opening another.",
                    status_code=409,
                )
            now = transaction.database_now()
            item = {
                **fingerprint,
                "status": "open",
                "ack_through": first_sequence - 1,
                "received_bytes": 0,
                "pending_sequence": None,
                "opened_at": now.isoformat(),
                "upload_expires_at": (now + timedelta(seconds=30)).isoformat(),
                "completed_at": None,
                "gap_generation": int(gap.get("generation", 0)),
            }
            batches[batch_id] = item
            if len(batches) > _MAX_BATCHES:
                completed = sorted(
                    (
                        key
                        for key, value in batches.items()
                        if value.get("status") == "complete" and key != batch_id
                    ),
                    key=lambda key: str(batches[key].get("completed_at") or ""),
                )
                for key in completed[: len(batches) - _MAX_BATCHES]:
                    batches.pop(key, None)
            interview["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                interview, expected_version=interview["version"]
            )
            return self._batch(batch_id, item)

    def stage(
        self,
        *,
        interview_id: str,
        organization_id: str,
        actor_id: str,
        current_connection_id: str,
        turn_id: Optional[str],
        payload: Dict[str, Any],
    ) -> BackfillFrame:
        batch_id = str(payload.get("batch_id") or "")
        source_connection = str(payload.get("source_connection_id") or "")
        audio_epoch = str(payload.get("audio_epoch") or "")
        try:
            sequence = int(payload.get("client_sequence") or 0)
        except (TypeError, ValueError) as exc:
            raise ApiError(
                "BROWSER_BACKFILL_CONTRACT_INVALID",
                "Browser backfill sequence must be an integer.",
                status_code=422,
            ) from exc
        encoded = payload.get("audio_base64")
        if not isinstance(encoded, str) or not encoded:
            raise ApiError(
                "BROWSER_BACKFILL_AUDIO_REQUIRED",
                "Browser backfill chunk requires base64 PCM audio.",
                status_code=422,
            )
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ApiError(
                "BROWSER_BACKFILL_AUDIO_INVALID",
                "Browser backfill audio is not valid base64.",
                status_code=422,
            ) from exc
        checksum = _sha256(content)
        with self.persistence.transaction(organization_id) as transaction:
            _current, _source, capability = self._authorize_locked(
                transaction,
                interview_id=interview_id,
                actor_id=actor_id,
                current_connection_id=current_connection_id,
                source_connection_id=source_connection,
                audio_epoch=audio_epoch,
            )
            max_frame = int(capability.get("max_frame_bytes") or 0)
            if not (1 <= len(content) <= max_frame <= 32 * 1024):
                raise ApiError(
                    "BROWSER_BACKFILL_FRAME_TOO_LARGE",
                    "One browser backfill frame exceeds its frozen byte limit.",
                    status_code=413,
                )
            interview, item = self._batch_locked(
                transaction, interview_id, batch_id
            )
            self._require_batch_scope(
                item,
                turn_id=turn_id,
                source_connection_id=source_connection,
                audio_epoch=audio_epoch,
            )
            now = transaction.database_now()
            if str(item.get("status")) != "open":
                raise ApiError(
                    "BROWSER_BACKFILL_BATCH_CLOSED",
                    "Browser recovery batch is already closed.",
                    status_code=409,
                )
            if now.isoformat() > str(item.get("upload_expires_at") or ""):
                raise ApiError(
                    "BROWSER_BACKFILL_EXPIRED",
                    "Browser recovery upload window expired.",
                    status_code=410,
                )
            ack_through = int(item.get("ack_through", 0))
            if sequence <= ack_through:
                file_id = _stable_id("backfill_frame", batch_id, sequence)
                existing = transaction.file_objects.get(file_id)
                if (
                    existing is None
                    or existing.get("checksum") != checksum
                    or int(existing.get("byte_count", 0)) != len(content)
                ):
                    raise ApiError(
                        "BROWSER_BACKFILL_SEQUENCE_CONFLICT",
                        "An acknowledged recovery sequence was replayed with different audio.",
                        status_code=409,
                    )
                return BackfillFrame(
                    batch_id=batch_id,
                    file_id=file_id,
                    audio_epoch=audio_epoch,
                    client_sequence=sequence,
                    checksum=checksum,
                    byte_count=len(content),
                    duplicate=True,
                )
            if sequence != ack_through + 1:
                raise ApiError(
                    "BROWSER_BACKFILL_SEQUENCE_GAP",
                    "Browser recovery frames must be acknowledged in strict order.",
                    status_code=409,
                    details={"ack_through": ack_through},
                )
            if sequence > int(item.get("last_sequence", 0)):
                raise ApiError(
                    "BROWSER_BACKFILL_SEQUENCE_OUT_OF_RANGE",
                    "Browser recovery sequence exceeds the declared batch.",
                    status_code=409,
                )
            pending = item.get("pending_sequence")
            file_id = _stable_id("backfill_frame", batch_id, sequence)
            if pending is not None:
                existing = transaction.file_objects.get(file_id)
                if (
                    int(pending) != sequence
                    or existing is None
                    or existing.get("checksum") != checksum
                    or int(existing.get("byte_count", 0)) != len(content)
                ):
                    raise ApiError(
                        "BROWSER_BACKFILL_FRAME_PENDING",
                        "Another recovery frame is awaiting the Evidence owner.",
                        status_code=409,
                    )
                return BackfillFrame(
                    batch_id=batch_id,
                    file_id=file_id,
                    audio_epoch=audio_epoch,
                    client_sequence=sequence,
                    checksum=checksum,
                    byte_count=len(content),
                    duplicate=True,
                )
            if int(item.get("received_bytes", 0)) + len(content) > int(
                item.get("total_bytes", 0)
            ):
                raise ApiError(
                    "BROWSER_BACKFILL_BYTE_COUNT_EXCEEDED",
                    "Browser recovery bytes exceed the declared complete batch.",
                    status_code=413,
                )

        stored = self.storage.store(
            organization_id=organization_id,
            object_id=file_id,
            content=content,
            content_type="application/octet-stream",
            checksum=checksum,
        )
        try:
            with self.persistence.transaction(organization_id) as transaction:
                interview, item = self._batch_locked(
                    transaction, interview_id, batch_id
                )
                self._require_batch_scope(
                    item,
                    turn_id=turn_id,
                    source_connection_id=source_connection,
                    audio_epoch=audio_epoch,
                )
                existing = transaction.file_objects.get(file_id)
                if existing is None:
                    transaction.file_objects.add(
                        {
                            "id": file_id,
                            "organization_id": organization_id,
                            "purpose": "candidate_evidence_backfill_frame",
                            "status": "ready",
                            "storage_backend": stored.storage_backend,
                            "object_key": stored.object_key,
                            "content_type": stored.content_type,
                            "checksum": stored.checksum,
                            "byte_count": stored.byte_count,
                            "scan_status": "not_applicable",
                            "source_type": "browser_audio_gap_backfill",
                            "interview_id": interview_id,
                            "turn_id": str(turn_id or ""),
                            "backfill_batch_id": batch_id,
                            "audio_epoch": audio_epoch,
                            "client_sequence": sequence,
                            "created_at": utc_now(),
                            "updated_at": utc_now(),
                        }
                    )
                elif (
                    existing.get("checksum") != checksum
                    or int(existing.get("byte_count", 0)) != len(content)
                ):
                    raise ApiError(
                        "BROWSER_BACKFILL_SEQUENCE_CONFLICT",
                        "Recovery sequence already contains different audio.",
                        status_code=409,
                    )
                batches = interview.setdefault("agent_runtime", {}).setdefault(
                    "browser_backfill_batches", {}
                )
                current = batches[batch_id]
                if int(current.get("ack_through", 0)) < sequence:
                    pending = current.get("pending_sequence")
                    if pending is not None and int(pending) != sequence:
                        raise ApiError(
                            "BROWSER_BACKFILL_FRAME_PENDING",
                            "Another recovery frame is awaiting the Evidence owner.",
                            status_code=409,
                        )
                    current["pending_sequence"] = sequence
                    interview["updated_at"] = utc_now()
                    transaction.interview_sessions.update(
                        interview, expected_version=interview["version"]
                    )
        except BaseException:
            # Deterministic object ids make a same-content retry safe, but an
            # unreferenced failed attempt must not linger in private storage.
            self.storage.delete(stored.object_key)
            raise
        return BackfillFrame(
            batch_id=batch_id,
            file_id=file_id,
            audio_epoch=audio_epoch,
            client_sequence=sequence,
            checksum=checksum,
            byte_count=len(content),
        )

    def read_for_owner(
        self,
        *,
        organization_id: str,
        interview_id: str,
        turn_id: str,
        payload: Dict[str, Any],
        fence: EvidenceCommitFence,
    ) -> Optional[bytes]:
        file_id = str(payload.get("file_id") or "")
        with self.persistence.transaction(organization_id) as transaction:
            assert_current_evidence_fence(transaction, fence)
            _interview, batch = self._batch_locked(
                transaction, interview_id, str(payload.get("batch_id") or "")
            )
            file_object = transaction.file_objects.get(file_id)
            sequence = int(payload.get("client_sequence") or 0)
            if sequence <= int(batch.get("ack_through", 0)):
                return None
            if (
                batch.get("turn_id") != turn_id
                or batch.get("audio_epoch") != payload.get("audio_epoch")
                or int(batch.get("pending_sequence") or 0) != sequence
                or file_object is None
                or file_object.get("purpose")
                != "candidate_evidence_backfill_frame"
                or file_object.get("status") != "ready"
                or file_object.get("interview_id") != interview_id
                or file_object.get("turn_id") != turn_id
                or file_object.get("backfill_batch_id")
                != payload.get("batch_id")
                or file_object.get("checksum") != payload.get("checksum")
                or int(file_object.get("byte_count", 0))
                != int(payload.get("byte_count") or 0)
            ):
                raise ApiError(
                    "BROWSER_BACKFILL_FRAME_INVALID",
                    "Persisted browser recovery frame failed scope validation.",
                    status_code=409,
                )
            object_key = str(file_object.get("object_key") or "")
        try:
            content = self.storage.open(object_key)
        except Exception as exc:
            raise ApiError(
                "BROWSER_BACKFILL_FRAME_UNAVAILABLE",
                "Persisted browser recovery frame is unavailable.",
                status_code=503,
                details={"error_type": type(exc).__name__},
            ) from exc
        if (
            len(content) != int(payload.get("byte_count") or 0)
            or _sha256(content) != payload.get("checksum")
        ):
            raise ApiError(
                "BROWSER_BACKFILL_CHECKSUM_MISMATCH",
                "Persisted browser recovery frame failed integrity validation.",
                status_code=409,
            )
        return content

    def mark_applied(
        self,
        *,
        organization_id: str,
        interview_id: str,
        payload: Dict[str, Any],
        fence: EvidenceCommitFence,
    ) -> int:
        file_id = str(payload.get("file_id") or "")
        sequence = int(payload.get("client_sequence") or 0)
        with self.persistence.transaction(organization_id) as transaction:
            assert_current_evidence_fence(transaction, fence)
            interview, batch = self._batch_locked(
                transaction, interview_id, str(payload.get("batch_id") or "")
            )
            ack_through = int(batch.get("ack_through", 0))
            if sequence <= ack_through:
                return ack_through
            if sequence != ack_through + 1 or int(
                batch.get("pending_sequence") or 0
            ) != sequence:
                raise ApiError(
                    "BROWSER_BACKFILL_SEQUENCE_GAP",
                    "Evidence owner cannot acknowledge a non-contiguous recovery frame.",
                    status_code=409,
                )
            file_object = transaction.file_objects.get(file_id)
            if file_object is None or file_object.get("status") != "ready":
                raise ApiError(
                    "BROWSER_BACKFILL_FRAME_INVALID",
                    "Evidence owner cannot acknowledge an unavailable recovery frame.",
                    status_code=409,
                )
            expected_file_version = int(file_object["version"])
            file_object["status"] = "applied"
            file_object["applied_by_ownership_epoch"] = fence.ownership_epoch
            file_object["updated_at"] = utc_now()
            transaction.file_objects.update(
                file_object, expected_version=expected_file_version
            )
            batch["ack_through"] = sequence
            batch["received_bytes"] = int(batch.get("received_bytes", 0)) + int(
                payload.get("byte_count") or 0
            )
            batch["pending_sequence"] = None
            interview["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                interview, expected_version=interview["version"]
            )
            return sequence

    def complete(
        self,
        *,
        interview_id: str,
        organization_id: str,
        actor_id: str,
        current_connection_id: str,
        turn_id: Optional[str],
        payload: Dict[str, Any],
    ) -> int:
        batch_id = str(payload.get("batch_id") or "")
        source_connection = str(payload.get("source_connection_id") or "")
        audio_epoch = str(payload.get("audio_epoch") or "")
        objects = []
        with self.persistence.transaction(organization_id) as transaction:
            self._authorize_locked(
                transaction,
                interview_id=interview_id,
                actor_id=actor_id,
                current_connection_id=current_connection_id,
                source_connection_id=source_connection,
                audio_epoch=audio_epoch,
            )
            interview, batch = self._batch_locked(
                transaction, interview_id, batch_id
            )
            self._require_batch_scope(
                batch,
                turn_id=turn_id,
                source_connection_id=source_connection,
                audio_epoch=audio_epoch,
            )
            if batch.get("status") == "complete":
                return int(batch.get("ack_through", 0))
            ack_through = int(batch.get("ack_through", 0))
            if (
                batch.get("pending_sequence") is not None
                or ack_through != int(batch.get("last_sequence", 0))
                or int(batch.get("received_bytes", 0))
                != int(batch.get("total_bytes", 0))
            ):
                raise ApiError(
                    "BROWSER_BACKFILL_INCOMPLETE",
                    "Every declared recovery frame must be durably acknowledged before completion.",
                    status_code=409,
                    details={"ack_through": ack_through},
                )
            batch["status"] = "complete"
            batch["completed_at"] = transaction.database_now().isoformat()
            gap = interview.setdefault("agent_runtime", {}).get(
                "browser_backfill_gap"
            ) or {}
            if (
                gap.get("status") == "authorized"
                and gap.get("turn_id") == batch.get("turn_id")
                and int(gap.get("generation", 0))
                == int(batch.get("gap_generation", 0))
            ):
                gap["status"] = "consumed"
                gap["consumed_at"] = transaction.database_now().isoformat()
            interview["updated_at"] = utc_now()
            transaction.interview_sessions.update(
                interview, expected_version=interview["version"]
            )
            objects = [
                item
                for item in transaction.file_objects.list()
                if item.get("backfill_batch_id") == batch_id
                and item.get("purpose") == "candidate_evidence_backfill_frame"
            ]
            for item in objects:
                expected = int(item["version"])
                item["status"] = "deleted"
                item["deleted_at"] = utc_now()
                item["updated_at"] = item["deleted_at"]
                transaction.file_objects.update(item, expected_version=expected)
        for item in objects:
            object_key = str(item.get("object_key") or "")
            if object_key:
                self.storage.delete(object_key)
        return ack_through

    @staticmethod
    def _ticket_locked(transaction: Any, interview_id: str, connection_id: str) -> Dict[str, Any]:
        ticket = next(
            (
                item
                for item in transaction.agent_tickets.list()
                if item.get("interview_id") == interview_id
                and item.get("connection_id") == connection_id
                and item.get("participant_role") == "candidate"
                and item.get("status") == "consumed"
            ),
            None,
        )
        if ticket is None:
            raise ApiError(
                "BROWSER_BACKFILL_CONNECTION_INVALID",
                "Browser recovery requires a consumed candidate connection.",
                status_code=403,
            )
        return ticket

    def _authorize_locked(
        self,
        transaction: Any,
        *,
        interview_id: str,
        actor_id: str,
        current_connection_id: str,
        source_connection_id: str,
        audio_epoch: str,
    ) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        current = self._ticket_locked(
            transaction, interview_id, current_connection_id
        )
        source = self._ticket_locked(transaction, interview_id, source_connection_id)
        if (
            current.get("actor_id") != actor_id
            or source.get("actor_id") != actor_id
            or current.get("organization_id") != source.get("organization_id")
        ):
            raise ApiError(
                "BROWSER_BACKFILL_CONNECTION_INVALID",
                "Browser recovery connections do not belong to this candidate session.",
                status_code=403,
            )
        recovery = (source.get("media") or {}).get("recovery") or {}
        capability = recovery.get("browser_backfill") or {}
        if (
            capability.get("enabled") is not True
            or capability.get("protocol") != _PROTOCOL
            or capability.get("connection_id") != source_connection_id
            or capability.get("audio_epoch") != audio_epoch
        ):
            raise ApiError(
                "BROWSER_BACKFILL_EPOCH_INVALID",
                "Browser recovery epoch is not authorized by the consumed source ticket.",
                status_code=403,
            )
        return current, source, capability

    @staticmethod
    def _batch_locked(
        transaction: Any, interview_id: str, batch_id: str
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        interview = transaction.interview_sessions.get(interview_id)
        if interview is None:
            raise ApiError(
                "INTERVIEW_NOT_FOUND", "Interview does not exist.", status_code=404
            )
        batches = (interview.get("agent_runtime") or {}).get(
            "browser_backfill_batches"
        ) or {}
        batch = batches.get(batch_id)
        if batch is None:
            raise ApiError(
                "BROWSER_BACKFILL_BATCH_NOT_FOUND",
                "Browser recovery batch does not exist.",
                status_code=404,
            )
        return interview, batch

    @staticmethod
    def _require_batch_scope(
        item: Dict[str, Any],
        *,
        turn_id: Optional[str],
        source_connection_id: str,
        audio_epoch: str,
    ) -> None:
        if (
            item.get("turn_id") != str(turn_id or "")
            or item.get("source_connection_id") != source_connection_id
            or item.get("audio_epoch") != audio_epoch
        ):
            raise ApiError(
                "BROWSER_BACKFILL_SCOPE_INVALID",
                "Browser recovery batch does not belong to this turn, connection or epoch.",
                status_code=403,
            )

    @staticmethod
    def _batch(batch_id: str, item: Dict[str, Any]) -> BackfillBatch:
        return BackfillBatch(
            batch_id=batch_id,
            audio_epoch=str(item["audio_epoch"]),
            source_connection_id=str(item["source_connection_id"]),
            turn_id=str(item["turn_id"]),
            first_sequence=int(item["first_sequence"]),
            last_sequence=int(item["last_sequence"]),
            total_bytes=int(item["total_bytes"]),
            ack_through=int(item.get("ack_through", 0)),
            status=str(item.get("status") or "open"),
        )
