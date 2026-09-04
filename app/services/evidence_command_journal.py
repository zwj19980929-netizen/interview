"""Durable, privacy-minimized command journal for authoritative Evidence.

The journal is PostgreSQL-backed through the common persistence seam. Redis may
later wake an owner, but command truth, claim recovery and idempotent results
remain here. Audio, transcripts, tickets, participant identities and provider
objects are intentionally outside this Interface.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Dict, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.domain.evidence_coordination import (
    ClaimedEvidenceCommand,
    EvidenceCommandOutcome,
    EvidenceCommandReceipt,
    EvidenceCommandSubmission,
    EvidenceCommitFence,
    EvidenceOwnershipGrant,
)
from app.persistence.errors import ConcurrencyConflict, RecordAlreadyExists
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.services.evidence_coordination import (
    assert_current_evidence_control,
    assert_current_evidence_fence,
)


_OPEN_PAYLOAD_FIELDS = {
    "content_type",
    "sample_rate_hz",
    "channels",
    "language",
    "enable_partial",
}
_SEAL_PAYLOAD_FIELDS = {"endpoint"}
_BACKFILL_PAYLOAD_FIELDS = {
    "batch_id",
    "file_id",
    "checksum",
    "byte_count",
    "audio_epoch",
    "client_sequence",
}
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?$")
_ENDPOINTS = {"explicit", "semantic_timeout", "candidate_requested"}
_SAFE_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class EvidenceCommandJournal:
    """Small durable Interface for submit/claim/complete/retry/result."""

    def __init__(
        self,
        store: Any,
        *,
        persistence: Optional[Persistence] = None,
        claim_seconds: float = 10.0,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.claim_seconds = max(0.1, min(float(claim_seconds), 60.0))

    def submit(
        self,
        grant: EvidenceOwnershipGrant,
        submission: EvidenceCommandSubmission,
    ) -> EvidenceCommandReceipt:
        """Persist before acknowledging; a same-key retry returns one result."""

        safe_payload = self._sanitize_payload(submission)
        command_id = self._command_id(
            grant.organization_id,
            grant.interview_id,
            submission.idempotency_key,
        )
        fingerprint = self._fingerprint(submission, safe_payload)
        last_conflict: Optional[Exception] = None
        for _attempt in range(3):
            try:
                return self._submit_once(
                    grant,
                    submission,
                    safe_payload=safe_payload,
                    command_id=command_id,
                    fingerprint=fingerprint,
                )
            except (RecordAlreadyExists, ConcurrencyConflict) as exc:
                # Simultaneous same-key inserts converge on the deterministic
                # command id; a retry reads and replays the winner.
                last_conflict = exc
        raise ApiError(
            "EVIDENCE_COMMAND_CONFLICT",
            "Evidence command changed too quickly to submit safely.",
            status_code=503,
            details={"error_type": type(last_conflict).__name__},
        )

    def _submit_once(
        self,
        grant: EvidenceOwnershipGrant,
        submission: EvidenceCommandSubmission,
        *,
        safe_payload: Dict[str, Any],
        command_id: str,
        fingerprint: str,
    ) -> EvidenceCommandReceipt:
        with self.persistence.transaction(grant.organization_id) as transaction:
            now = transaction.database_now()
            assert_current_evidence_fence(
                transaction,
                grant.commit_fence(),
                now=now,
            )
            assert_current_evidence_control(transaction, grant)
            existing = transaction.evidence_commands.get(command_id)
            if existing is not None:
                if existing.get("request_fingerprint") != fingerprint:
                    raise ApiError(
                        "EVIDENCE_COMMAND_IDEMPOTENCY_CONFLICT",
                        "The idempotency key already belongs to another Evidence command.",
                        status_code=409,
                    )
                existing_generation = int(
                    existing.get("control_generation", 0)
                )
                status = str(existing.get("status") or "")
                outcome = existing.get("outcome") or {}
                stale_rejection = bool(
                    status == "rejected"
                    and outcome.get("error_code") == "EVIDENCE_CONTROL_STALE"
                    and not self._is_due(existing.get("deadline_at"), now)
                )
                if (
                    existing_generation != grant.control_generation
                    and (status in {"pending", "running"} or stale_rejection)
                ):
                    # A browser may reconnect after the owner process dies and
                    # retry the same unknown command. The current authenticated
                    # controller can rebind that exact fingerprint; new keys or
                    # changed payloads cannot revive stale work. An in-flight
                    # claim is retained so its old owner may still acknowledge
                    # a completed effect before the lease changes.
                    expected_version = int(existing["version"])
                    existing["control_generation"] = grant.control_generation
                    existing["control_rebind_count"] = int(
                        existing.get("control_rebind_count", 0)
                    ) + 1
                    existing["updated_at"] = self._format(now)
                    if stale_rejection:
                        existing["status"] = "pending"
                        existing["outcome"] = None
                        existing["last_error_code"] = None
                        existing["available_at"] = self._format(now)
                        existing["claim_id"] = None
                        existing["claim_expires_at"] = None
                        existing["completed_at"] = None
                    existing = transaction.evidence_commands.update(
                        existing, expected_version=expected_version
                    )
                return self._receipt(existing, disposition="duplicate")

            now_text = self._format(now)
            item = transaction.evidence_commands.add(
                {
                    "id": command_id,
                    "organization_id": grant.organization_id,
                    "interview_id": grant.interview_id,
                    "idempotency_hash": self._idempotency_hash(
                        submission.idempotency_key
                    ),
                    "request_fingerprint": fingerprint,
                    "command_type": submission.command_type,
                    "turn_id": submission.turn_id,
                    "causation_id": submission.causation_id,
                    "payload": safe_payload,
                    "status": "pending",
                    "control_generation": grant.control_generation,
                    "target_owner_instance_id": grant.owner_instance_id,
                    "target_lease_id": grant.lease_id,
                    "target_ownership_epoch": grant.ownership_epoch,
                    "route_revision": 1,
                    "attempt_count": 0,
                    "available_at": now_text,
                    "deadline_at": self._format(
                        now + timedelta(seconds=submission.deadline_seconds)
                    ),
                    "claim_id": None,
                    "claim_expires_at": None,
                    "claimed_owner_instance_id": None,
                    "claimed_lease_id": None,
                    "claimed_ownership_epoch": None,
                    "outcome": None,
                    "last_error_code": None,
                    "created_at": now_text,
                    "updated_at": now_text,
                    "completed_at": None,
                }
            )
            return self._receipt(item, disposition="accepted")

    def get_receipt(
        self,
        grant: EvidenceOwnershipGrant,
        command_id: str,
    ) -> EvidenceCommandReceipt:
        """Return a result only to the currently attached controller."""

        with self.persistence.transaction(grant.organization_id) as transaction:
            assert_current_evidence_control(transaction, grant)
            item = transaction.evidence_commands.get(command_id)
            if item is None or item.get("interview_id") != grant.interview_id:
                raise ApiError(
                    "EVIDENCE_COMMAND_NOT_FOUND",
                    "Evidence command does not exist.",
                    status_code=404,
                )
            return self._receipt(item, disposition="duplicate")

    def has_unsettled(self, interview_id: str, organization_id: str) -> bool:
        """Tell a failover worker whether terminal-effect receipts remain."""

        with self.persistence.transaction(organization_id) as transaction:
            return any(
                item.get("interview_id") == interview_id
                and item.get("status") in {"pending", "running"}
                for item in transaction.evidence_commands.list()
            )

    def claim_next(
        self,
        fence: EvidenceCommitFence,
        organization_id: str,
    ) -> Optional[ClaimedEvidenceCommand]:
        """Claim one due command; an expired claim is safe to deliver again."""

        with self.persistence.transaction(organization_id) as transaction:
            now = transaction.database_now()
            assert_current_evidence_fence(transaction, fence, now=now)
            ownership = transaction.evidence_ownerships.get(fence.ownership_id)
            assert ownership is not None
            current_generation = int(ownership.get("control_generation", 0))
            summaries = sorted(
                (
                    item
                    for item in transaction.evidence_commands.list()
                    if item.get("interview_id") == ownership.get("interview_id")
                    and item.get("status")
                    in {"pending", "running"}
                ),
                key=lambda item: (
                    str(item.get("available_at") or ""),
                    str(item.get("created_at") or ""),
                    str(item.get("id") or ""),
                ),
            )
            for summary in summaries:
                item = transaction.evidence_commands.get(str(summary["id"]))
                if item is None or item.get("status") not in {"pending", "running"}:
                    continue
                expected_version = int(item["version"])
                if self._is_due(item.get("deadline_at"), now):
                    self._reject_locked(
                        transaction,
                        item,
                        expected_version=expected_version,
                        now=now,
                        status="expired",
                        error_code="EVIDENCE_COMMAND_EXPIRED",
                    )
                    continue
                if int(item.get("control_generation", 0)) != current_generation:
                    self._reject_locked(
                        transaction,
                        item,
                        expected_version=expected_version,
                        now=now,
                        status="rejected",
                        error_code="EVIDENCE_CONTROL_STALE",
                    )
                    continue
                status = str(item.get("status"))
                if status == "pending" and not self._is_due(
                    item.get("available_at"), now
                ):
                    continue
                if status == "running" and not self._is_due(
                    item.get("claim_expires_at"), now
                ):
                    continue

                route_changed = bool(
                    item.get("target_owner_instance_id")
                    != fence.owner_instance_id
                    or item.get("target_lease_id") != fence.lease_id
                    or int(item.get("target_ownership_epoch", 0))
                    != fence.ownership_epoch
                )
                if route_changed:
                    item["route_revision"] = int(
                        item.get("route_revision", 1)
                    ) + 1
                claim_id = new_id("evidence_claim")
                item.update(
                    {
                        "status": "running",
                        "target_owner_instance_id": fence.owner_instance_id,
                        "target_lease_id": fence.lease_id,
                        "target_ownership_epoch": fence.ownership_epoch,
                        "attempt_count": int(item.get("attempt_count", 0)) + 1,
                        "claim_id": claim_id,
                        "claim_expires_at": self._format(
                            now + timedelta(seconds=self.claim_seconds)
                        ),
                        "claimed_owner_instance_id": fence.owner_instance_id,
                        "claimed_lease_id": fence.lease_id,
                        "claimed_ownership_epoch": fence.ownership_epoch,
                        "updated_at": self._format(now),
                    }
                )
                item = transaction.evidence_commands.update(
                    item, expected_version=expected_version
                )
                return self._claimed(item)
            return None

    def complete(
        self,
        fence: EvidenceCommitFence,
        organization_id: str,
        command_id: str,
        claim_id: str,
        outcome: EvidenceCommandOutcome,
    ) -> EvidenceCommandReceipt:
        """Commit one safe result only while the executing owner is current."""

        with self.persistence.transaction(organization_id) as transaction:
            now = transaction.database_now()
            assert_current_evidence_fence(transaction, fence, now=now)
            item = transaction.evidence_commands.get(command_id)
            if item is None:
                raise ApiError(
                    "EVIDENCE_COMMAND_NOT_FOUND",
                    "Evidence command does not exist.",
                    status_code=404,
                )
            if item.get("status") in {"completed", "rejected", "expired"}:
                return self._receipt(item, disposition="duplicate")
            self._require_claim(item, fence, claim_id)
            expected_version = int(item["version"])
            item["status"] = (
                "completed" if outcome.disposition == "applied" else "rejected"
            )
            item["outcome"] = outcome.model_dump(mode="json")
            item["claim_id"] = None
            item["claim_expires_at"] = None
            item["completed_at"] = self._format(now)
            item["updated_at"] = self._format(now)
            item = transaction.evidence_commands.update(
                item, expected_version=expected_version
            )
            return self._receipt(item, disposition="duplicate")

    def fail(
        self,
        fence: EvidenceCommitFence,
        organization_id: str,
        command_id: str,
        claim_id: str,
        *,
        error_code: str,
        retryable: bool,
        retry_after_seconds: float = 0.1,
    ) -> EvidenceCommandReceipt:
        """Release a retryable claim or store a non-retryable safe rejection."""

        safe_error = EvidenceCommandOutcome(
            disposition="rejected",
            error_code=error_code,
            retryable=retryable,
        )
        with self.persistence.transaction(organization_id) as transaction:
            now = transaction.database_now()
            assert_current_evidence_fence(transaction, fence, now=now)
            item = transaction.evidence_commands.get(command_id)
            if item is None:
                raise ApiError(
                    "EVIDENCE_COMMAND_NOT_FOUND",
                    "Evidence command does not exist.",
                    status_code=404,
                )
            if item.get("status") in {"completed", "rejected", "expired"}:
                return self._receipt(item, disposition="duplicate")
            self._require_claim(item, fence, claim_id)
            expected_version = int(item["version"])
            item["last_error_code"] = error_code
            item["claim_id"] = None
            item["claim_expires_at"] = None
            item["updated_at"] = self._format(now)
            if retryable and not self._is_due(item.get("deadline_at"), now):
                item["status"] = "pending"
                item["available_at"] = self._format(
                    now
                    + timedelta(
                        seconds=max(0.0, min(float(retry_after_seconds), 30.0))
                    )
                )
                item["outcome"] = None
            else:
                item["status"] = "rejected"
                item["outcome"] = safe_error.model_dump(mode="json")
                item["completed_at"] = self._format(now)
            item = transaction.evidence_commands.update(
                item, expected_version=expected_version
            )
            return self._receipt(item, disposition="duplicate")

    @staticmethod
    def _sanitize_payload(
        submission: EvidenceCommandSubmission,
    ) -> Dict[str, Any]:
        payload = dict(submission.payload)
        if submission.command_type == "evidence.open":
            allowed = _OPEN_PAYLOAD_FIELDS
        elif submission.command_type == "evidence.seal":
            allowed = _SEAL_PAYLOAD_FIELDS
        elif submission.command_type == "evidence.backfill":
            allowed = _BACKFILL_PAYLOAD_FIELDS
        else:
            allowed = set()
        forbidden = sorted(set(payload) - allowed)
        if forbidden:
            raise ApiError(
                "EVIDENCE_COMMAND_PAYLOAD_FORBIDDEN",
                "Evidence command payload contains fields outside its allow-list.",
                status_code=422,
                details={"fields": forbidden},
            )
        if submission.command_type == "evidence.open":
            content_type = str(payload.get("content_type") or "audio/pcm")
            if content_type != "audio/pcm":
                raise ApiError(
                    "EVIDENCE_COMMAND_PAYLOAD_INVALID",
                    "Evidence open requires 16 kHz mono PCM.",
                    status_code=422,
                )
            try:
                sample_rate = int(payload.get("sample_rate_hz") or 16_000)
                channels = int(payload.get("channels") or 1)
            except (TypeError, ValueError) as exc:
                raise ApiError(
                    "EVIDENCE_COMMAND_PAYLOAD_INVALID",
                    "Evidence open requires numeric PCM settings.",
                    status_code=422,
                ) from exc
            language = str(payload.get("language") or "zh-CN")
            enable_partial = payload.get("enable_partial", True)
            if (
                sample_rate != 16_000
                or channels != 1
                or not _LANGUAGE_RE.fullmatch(language)
                or not isinstance(enable_partial, bool)
            ):
                raise ApiError(
                    "EVIDENCE_COMMAND_PAYLOAD_INVALID",
                    "Evidence open requires valid language and 16 kHz mono PCM settings.",
                    status_code=422,
                )
            return {
                "content_type": content_type,
                "sample_rate_hz": sample_rate,
                "channels": channels,
                "language": language,
                "enable_partial": enable_partial,
            }
        if submission.command_type == "evidence.seal":
            endpoint = str(payload.get("endpoint") or "explicit")
            if endpoint not in _ENDPOINTS:
                raise ApiError(
                    "EVIDENCE_COMMAND_PAYLOAD_INVALID",
                    "Evidence seal endpoint is not supported.",
                    status_code=422,
                )
            return {"endpoint": endpoint}
        if submission.command_type == "evidence.backfill":
            batch_id = str(payload.get("batch_id") or "")
            file_id = str(payload.get("file_id") or "")
            checksum = str(payload.get("checksum") or "")
            audio_epoch = str(payload.get("audio_epoch") or "")
            try:
                byte_count = int(payload.get("byte_count") or 0)
                client_sequence = int(payload.get("client_sequence") or 0)
            except (TypeError, ValueError) as exc:
                raise ApiError(
                    "EVIDENCE_COMMAND_PAYLOAD_INVALID",
                    "Evidence backfill metadata must use numeric bounds.",
                    status_code=422,
                ) from exc
            if (
                not _SAFE_REFERENCE_RE.fullmatch(batch_id)
                or not _SAFE_REFERENCE_RE.fullmatch(file_id)
                or not _SAFE_REFERENCE_RE.fullmatch(audio_epoch)
                or not _CHECKSUM_RE.fullmatch(checksum)
                or not (1 <= byte_count <= 32 * 1024)
                or not (1 <= client_sequence <= 2**53 - 1)
            ):
                raise ApiError(
                    "EVIDENCE_COMMAND_PAYLOAD_INVALID",
                    "Evidence backfill metadata is outside its frozen limits.",
                    status_code=422,
                )
            return {
                "batch_id": batch_id,
                "file_id": file_id,
                "checksum": checksum,
                "byte_count": byte_count,
                "audio_epoch": audio_epoch,
                "client_sequence": client_sequence,
            }
        return {}

    @classmethod
    def _reject_locked(
        cls,
        transaction,
        item: Dict[str, Any],
        *,
        expected_version: int,
        now: datetime,
        status: str,
        error_code: str,
    ) -> None:
        item["status"] = status
        item["outcome"] = EvidenceCommandOutcome(
            disposition="rejected",
            error_code=error_code,
            retryable=False,
        ).model_dump(mode="json")
        item["claim_id"] = None
        item["claim_expires_at"] = None
        item["completed_at"] = cls._format(now)
        item["updated_at"] = cls._format(now)
        transaction.evidence_commands.update(
            item, expected_version=expected_version
        )

    @staticmethod
    def _require_claim(
        item: Dict[str, Any],
        fence: EvidenceCommitFence,
        claim_id: str,
    ) -> None:
        if (
            item.get("status") != "running"
            or item.get("claim_id") != claim_id
            or item.get("claimed_owner_instance_id") != fence.owner_instance_id
            or item.get("claimed_lease_id") != fence.lease_id
            or int(item.get("claimed_ownership_epoch", 0))
            != fence.ownership_epoch
        ):
            raise ApiError(
                "EVIDENCE_COMMAND_CLAIM_STALE",
                "Evidence command claim is no longer current.",
                status_code=409,
            )

    @staticmethod
    def _receipt(
        item: Dict[str, Any],
        *,
        disposition: str,
    ) -> EvidenceCommandReceipt:
        outcome = item.get("outcome")
        return EvidenceCommandReceipt(
            command_id=str(item["id"]),
            disposition=disposition,
            status=str(item["status"]),
            outcome=(
                EvidenceCommandOutcome.model_validate(outcome)
                if isinstance(outcome, dict)
                else None
            ),
        )

    @staticmethod
    def _claimed(item: Dict[str, Any]) -> ClaimedEvidenceCommand:
        return ClaimedEvidenceCommand(
            command_id=str(item["id"]),
            claim_id=str(item["claim_id"]),
            command_type=str(item["command_type"]),
            turn_id=item.get("turn_id"),
            causation_id=item.get("causation_id"),
            payload=dict(item.get("payload") or {}),
            control_generation=int(item["control_generation"]),
            ownership_epoch=int(item["claimed_ownership_epoch"]),
            attempt_count=int(item["attempt_count"]),
            deadline_at=str(item["deadline_at"]),
        )

    @staticmethod
    def _command_id(
        organization_id: str,
        interview_id: str,
        idempotency_key: str,
    ) -> str:
        digest = hashlib.sha256(
            (organization_id + "\0" + interview_id + "\0" + idempotency_key).encode(
                "utf-8"
            )
        ).hexdigest()
        return "evidence_command:%s" % digest[:48]

    @staticmethod
    def _idempotency_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _fingerprint(
        submission: EvidenceCommandSubmission,
        payload: Dict[str, Any],
    ) -> str:
        material = json.dumps(
            {
                "command_type": submission.command_type,
                "turn_id": submission.turn_id,
                "payload": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_due(value: Any, now: datetime) -> bool:
        parsed = EvidenceCommandJournal._parse_time(value)
        return parsed is None or parsed <= now.astimezone(timezone.utc)

    @staticmethod
    def _parse_time(value: Any) -> Optional[datetime]:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _format(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
