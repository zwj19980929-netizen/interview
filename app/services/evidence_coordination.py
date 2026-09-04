"""Database-backed owner lease and fencing for authoritative Evidence.

PostgreSQL is the production truth.  The same transaction contract is used by
the deterministic memory and SQLite adapters so concurrency invariants can be
tested without teaching AgentChannel about leases or database rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.domain.evidence_coordination import (
    EvidenceCommitFence,
    EvidenceOwnershipGrant,
)
from app.persistence.interface import Persistence, PersistenceTransaction
from app.persistence.errors import ConcurrencyConflict, RecordAlreadyExists
from app.persistence.provider import persistence_for


class EvidenceOwnershipLost(ApiError):
    def __init__(self) -> None:
        super().__init__(
            "EVIDENCE_OWNER_FENCED",
            "The authoritative Evidence owner lease is no longer current.",
            status_code=409,
        )


class EvidenceOwnershipCoordinator:
    """Small Interface over lease acquisition, renewal and commit fencing."""

    def __init__(
        self,
        store: Any,
        *,
        persistence: Optional[Persistence] = None,
        lease_seconds: float = 15.0,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.lease_seconds = max(0.05, min(float(lease_seconds), 120.0))

    @staticmethod
    def ownership_id(interview_id: str) -> str:
        return "evidence_ownership:%s" % interview_id

    def attach_control(
        self,
        *,
        interview_id: str,
        organization_id: str,
        connection_id: str,
        local_instance_id: str,
    ) -> EvidenceOwnershipGrant:
        last_conflict: Optional[Exception] = None
        for _attempt in range(3):
            try:
                return self._attach_control_once(
                    interview_id=interview_id,
                    organization_id=organization_id,
                    connection_id=connection_id,
                    local_instance_id=local_instance_id,
                )
            except (RecordAlreadyExists, ConcurrencyConflict) as exc:
                # PostgreSQL cannot predicate-lock an absent generic document
                # row. A simultaneous first claim may therefore race on INSERT;
                # re-reading the winner turns the loser into a remote proxy.
                last_conflict = exc
        raise ApiError(
            "EVIDENCE_COORDINATION_CONFLICT",
            "Authoritative Evidence ownership changed too quickly to attach safely.",
            status_code=503,
            details={"error_type": type(last_conflict).__name__},
        )

    def _attach_control_once(
        self,
        *,
        interview_id: str,
        organization_id: str,
        connection_id: str,
        local_instance_id: str,
    ) -> EvidenceOwnershipGrant:
        ownership_id = self.ownership_id(interview_id)
        with self.persistence.transaction(organization_id) as transaction:
            if transaction.interview_sessions.get(interview_id) is None:
                raise ApiError(
                    "INTERVIEW_NOT_FOUND",
                    "Interview session does not exist.",
                    status_code=404,
                )
            now = transaction.database_now()
            current = transaction.evidence_ownerships.get(ownership_id)
            if current is None:
                current = transaction.evidence_ownerships.add(
                    {
                        "id": ownership_id,
                        "organization_id": organization_id,
                        "interview_id": interview_id,
                        "owner_instance_id": local_instance_id,
                        "lease_id": new_id("evidence_lease"),
                        "ownership_epoch": 1,
                        "lease_expires_at": self._after(now),
                        "acquired_at": self._format(now),
                        "renewed_at": self._format(now),
                        "released_at": None,
                        "state": "active",
                        "control_connection_id": connection_id,
                        "control_generation": 1,
                        "control_attached_at": self._format(now),
                        "reconnect_grace_expires_at": None,
                        "created_at": self._format(now),
                        "updated_at": self._format(now),
                    }
                )
            else:
                expected_version = current["version"]
                active = self._is_active(current, now)
                if not active:
                    current["owner_instance_id"] = local_instance_id
                    current["lease_id"] = new_id("evidence_lease")
                    current["ownership_epoch"] = int(
                        current.get("ownership_epoch", 0)
                    ) + 1
                    current["acquired_at"] = self._format(now)
                    current["released_at"] = None
                    current["state"] = "active"
                elif current.get("owner_instance_id") == local_instance_id:
                    # Reattaching on the same process renews the existing epoch;
                    # a control reconnect must never look like owner failover.
                    current["state"] = "active"
                current["control_generation"] = int(
                    current.get("control_generation", 0)
                ) + 1
                current["control_connection_id"] = connection_id
                current["control_attached_at"] = self._format(now)
                current["reconnect_grace_expires_at"] = None
                if current.get("owner_instance_id") == local_instance_id:
                    current["lease_expires_at"] = self._after(now)
                    current["renewed_at"] = self._format(now)
                current["updated_at"] = self._format(now)
                current = transaction.evidence_ownerships.update(
                    current, expected_version=expected_version
                )
            return self._grant(current, local_instance_id)

    def renew(self, grant: EvidenceOwnershipGrant) -> EvidenceOwnershipGrant:
        with self.persistence.transaction(grant.organization_id) as transaction:
            now = transaction.database_now()
            current = transaction.evidence_ownerships.get(grant.ownership_id)
            self._require_fence(current, grant.commit_fence(), now)
            assert current is not None
            expected_version = current["version"]
            current["lease_expires_at"] = self._after(now)
            current["renewed_at"] = self._format(now)
            current["updated_at"] = self._format(now)
            current = transaction.evidence_ownerships.update(
                current, expected_version=expected_version
            )
            return self._grant(current, grant.local_instance_id)

    def claim_owner(
        self,
        *,
        interview_id: str,
        organization_id: str,
        local_instance_id: str,
    ) -> EvidenceOwnershipGrant:
        """Acquire an expired/released owner epoch without changing control.

        Background owner recovery must not impersonate a new candidate control
        connection. It therefore preserves control_connection_id/generation and
        only advances the ownership epoch when the previous lease is inactive.
        """

        last_conflict: Optional[Exception] = None
        for _attempt in range(3):
            try:
                return self._claim_owner_once(
                    interview_id=interview_id,
                    organization_id=organization_id,
                    local_instance_id=local_instance_id,
                )
            except ConcurrencyConflict as exc:
                last_conflict = exc
        raise ApiError(
            "EVIDENCE_COORDINATION_CONFLICT",
            "Authoritative Evidence ownership changed too quickly to claim safely.",
            status_code=503,
            details={"error_type": type(last_conflict).__name__},
        )

    def _claim_owner_once(
        self,
        *,
        interview_id: str,
        organization_id: str,
        local_instance_id: str,
    ) -> EvidenceOwnershipGrant:
        ownership_id = self.ownership_id(interview_id)
        with self.persistence.transaction(organization_id) as transaction:
            now = transaction.database_now()
            current = transaction.evidence_ownerships.get(ownership_id)
            if current is None:
                raise ApiError(
                    "EVIDENCE_CONTROL_UNAVAILABLE",
                    "Evidence ownership cannot start before a candidate control is attached.",
                    status_code=409,
                )
            if self._is_active(current, now):
                return self._grant(current, local_instance_id)
            expected_version = int(current["version"])
            current["owner_instance_id"] = local_instance_id
            current["lease_id"] = new_id("evidence_lease")
            current["ownership_epoch"] = int(
                current.get("ownership_epoch", 0)
            ) + 1
            current["lease_expires_at"] = self._after(now)
            current["acquired_at"] = self._format(now)
            current["renewed_at"] = self._format(now)
            current["released_at"] = None
            current["state"] = "active"
            current["updated_at"] = self._format(now)
            current = transaction.evidence_ownerships.update(
                current, expected_version=expected_version
            )
            return self._grant(current, local_instance_id)

    def detach_control(
        self,
        grant: EvidenceOwnershipGrant,
        *,
        grace_seconds: float,
    ) -> bool:
        with self.persistence.transaction(grant.organization_id) as transaction:
            current = transaction.evidence_ownerships.get(grant.ownership_id)
            if current is None or not self._same_control(current, grant):
                return False
            now = transaction.database_now()
            expected_version = current["version"]
            current["reconnect_grace_expires_at"] = self._format(
                now + timedelta(seconds=max(0.0, float(grace_seconds)))
            )
            current["updated_at"] = self._format(now)
            transaction.evidence_ownerships.update(
                current, expected_version=expected_version
            )
            return True

    def release(self, grant: EvidenceOwnershipGrant) -> bool:
        """Conditionally release only this epoch; never clobber its successor."""

        with self.persistence.transaction(grant.organization_id) as transaction:
            current = transaction.evidence_ownerships.get(grant.ownership_id)
            if current is None or not self._same_fence(current, grant.commit_fence()):
                return False
            now = transaction.database_now()
            expected_version = current["version"]
            current["state"] = "released"
            current["lease_expires_at"] = self._format(now)
            current["released_at"] = self._format(now)
            current["updated_at"] = self._format(now)
            transaction.evidence_ownerships.update(
                current, expected_version=expected_version
            )
            return True

    def assert_current(self, fence: EvidenceCommitFence, organization_id: str) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            assert_current_evidence_fence(
                transaction,
                fence,
                now=transaction.database_now(),
            )

    def assert_control_current(self, grant: EvidenceOwnershipGrant) -> None:
        with self.persistence.transaction(grant.organization_id) as transaction:
            assert_current_evidence_control(transaction, grant)

    def _grant(
        self, current: dict, local_instance_id: str
    ) -> EvidenceOwnershipGrant:
        owner_instance_id = str(current["owner_instance_id"])
        return EvidenceOwnershipGrant(
            ownership_id=str(current["id"]),
            interview_id=str(current["interview_id"]),
            organization_id=str(current["organization_id"]),
            local_instance_id=local_instance_id,
            owner_instance_id=owner_instance_id,
            lease_id=str(current["lease_id"]),
            ownership_epoch=int(current["ownership_epoch"]),
            lease_expires_at=str(current["lease_expires_at"]),
            control_connection_id=str(current["control_connection_id"]),
            control_generation=int(current["control_generation"]),
            disposition=(
                "local_owner"
                if owner_instance_id == local_instance_id
                else "remote_owner"
            ),
        )

    def _after(self, now: datetime) -> str:
        return self._format(now + timedelta(seconds=self.lease_seconds))

    @staticmethod
    def _same_fence(current: dict, fence: EvidenceCommitFence) -> bool:
        return bool(
            current.get("id") == fence.ownership_id
            and current.get("owner_instance_id") == fence.owner_instance_id
            and current.get("lease_id") == fence.lease_id
            and int(current.get("ownership_epoch", 0))
            == fence.ownership_epoch
        )

    @classmethod
    def _require_fence(
        cls,
        current: Optional[dict],
        fence: EvidenceCommitFence,
        now: datetime,
    ) -> None:
        if (
            current is None
            or not cls._same_fence(current, fence)
            or not cls._is_active(current, now)
        ):
            raise EvidenceOwnershipLost()

    @staticmethod
    def _same_control(current: dict, grant: EvidenceOwnershipGrant) -> bool:
        return bool(
            current.get("control_connection_id") == grant.control_connection_id
            and int(current.get("control_generation", 0))
            == grant.control_generation
        )

    @staticmethod
    def _is_active(current: dict, now: datetime) -> bool:
        if current.get("state") != "active":
            return False
        try:
            expires = datetime.fromisoformat(
                str(current.get("lease_expires_at") or "").replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > now.astimezone(timezone.utc)

    @staticmethod
    def _format(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def assert_current_evidence_fence(
    transaction: PersistenceTransaction,
    fence: EvidenceCommitFence,
    *,
    now: Optional[datetime] = None,
) -> None:
    """Lock and validate the ownership row inside a domain commit transaction."""

    current = transaction.evidence_ownerships.get(fence.ownership_id)
    EvidenceOwnershipCoordinator._require_fence(
        current,
        fence,
        now or transaction.database_now(),
    )


def assert_current_evidence_control(
    transaction: PersistenceTransaction,
    grant: EvidenceOwnershipGrant,
) -> None:
    """Validate the candidate controller in the caller's transaction."""

    current = transaction.evidence_ownerships.get(grant.ownership_id)
    if current is None or not EvidenceOwnershipCoordinator._same_control(
        current, grant
    ):
        raise ApiError(
            "EVIDENCE_CONTROL_STALE",
            "A newer candidate control connection owns Evidence commands.",
            status_code=409,
        )
