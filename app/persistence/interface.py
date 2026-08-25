from contextlib import AbstractContextManager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from typing import Any, Callable, Dict, List, Optional, Protocol

from app.core.ids import new_id
from app.core.sensitive_data import ProviderSecretVault
from app.core.time import utc_now
from app.persistence.errors import ConcurrencyConflict, RecordAlreadyExists, RecordNotFound


Document = Dict[str, Any]
Predicate = Callable[[Document], bool]


class TransactionBackend(Protocol):
    def get_document(self, collection: str, item_id: str) -> Optional[Document]: ...

    def list_documents(self, collection: str) -> List[Document]: ...

    def insert_document(self, collection: str, item: Document) -> None: ...

    def replace_document(self, collection: str, item: Document) -> None: ...

    def delete_documents(self, collection: str, predicate: Predicate) -> None: ...

    def search_question_catalog(
        self,
        *,
        organization_id: str,
        job_position_id: str,
        knowledge_base_ids: List[str],
        skills: List[str],
        difficulties: List[str],
        question_types: List[str],
    ) -> List[Document]: ...

    def get_work_item(self, item_id: str) -> Optional[Document]: ...

    def list_work_items(self) -> List[Document]: ...

    def insert_work_item(self, item: Document) -> None: ...

    def replace_work_item(self, item: Document) -> None: ...

    def get_secret(self, organization_id: str, item_id: str) -> Document: ...

    def replace_secret(self, organization_id: str, item_id: str, secret: Document) -> None: ...

    def list_invocations(self) -> List[Document]: ...

    def insert_invocation(self, item: Document) -> None: ...


class VersionedDocumentRepository:
    def __init__(
        self,
        backend: TransactionBackend,
        organization_id: str,
        *,
        collection: str,
        entity_name: str,
    ) -> None:
        self._backend = backend
        self._organization_id = organization_id
        self._collection = collection
        self._entity_name = entity_name

    def add(self, document: Document) -> Document:
        item = deepcopy(document)
        self._require_tenant(item)
        if self._backend.get_document(self._collection, item["id"]):
            raise RecordAlreadyExists("%s already exists: %s" % (self._entity_name, item["id"]))
        item["version"] = 1
        self._backend.insert_document(self._collection, item)
        return deepcopy(item)

    def get(self, item_id: str) -> Optional[Document]:
        item = self._backend.get_document(self._collection, item_id)
        if not item or item.get("organization_id") != self._organization_id:
            return None
        item.setdefault("version", 1)
        return deepcopy(item)

    def list(self) -> List[Document]:
        items: List[Document] = []
        for stored in self._backend.list_documents(self._collection):
            if stored.get("organization_id") != self._organization_id:
                continue
            item = deepcopy(stored)
            item.setdefault("version", 1)
            items.append(item)
        return items

    def update(self, document: Document, *, expected_version: int) -> Document:
        item = deepcopy(document)
        self._require_tenant(item)
        current = self._backend.get_document(self._collection, item["id"])
        if not current or current.get("organization_id") != self._organization_id:
            raise RecordNotFound("%s does not exist: %s" % (self._entity_name, item["id"]))
        current_version = int(current.get("version", 1))
        if current_version != expected_version:
            raise ConcurrencyConflict(
                "%s %s expected version %s, found %s"
                % (self._entity_name, item["id"], expected_version, current_version)
            )
        item["version"] = expected_version + 1
        self._backend.replace_document(self._collection, item)
        return deepcopy(item)

    def _require_tenant(self, item: Document) -> None:
        if item.get("organization_id") != self._organization_id:
            raise ValueError("%s organization_id does not match the transaction tenant." % self._entity_name)


class QuestionRepository(VersionedDocumentRepository):
    def __init__(self, backend: TransactionBackend, organization_id: str) -> None:
        super().__init__(
            backend,
            organization_id,
            collection="questions",
            entity_name="Question",
        )

    def search_catalog(
        self,
        *,
        job_position_id: str,
        knowledge_base_ids: List[str],
        skills: List[str],
        difficulties: List[str],
        question_types: List[str],
    ) -> List[Document]:
        """Apply tenant and structured candidate filters at the repository seam."""
        items = self._backend.search_question_catalog(
            organization_id=self._organization_id,
            job_position_id=job_position_id,
            knowledge_base_ids=knowledge_base_ids,
            skills=skills,
            difficulties=difficulties,
            question_types=question_types,
        )
        result: List[Document] = []
        for stored in items:
            item = deepcopy(stored)
            item.setdefault("version", 1)
            result.append(item)
        return result


class OutboxRepository:
    def __init__(self, backend: TransactionBackend, organization_id: str) -> None:
        self._backend = backend
        self._organization_id = organization_id

    def enqueue(self, work_item: Document) -> Document:
        item = deepcopy(work_item)
        self._require_tenant(item)
        for existing in self._backend.list_work_items():
            if (
                existing.get("organization_id") == self._organization_id
                and existing.get("idempotency_key") == item.get("idempotency_key")
            ):
                return deepcopy(existing)
        self._backend.insert_work_item(item)
        return deepcopy(item)

    def get(self, item_id: str) -> Optional[Document]:
        item = self._backend.get_work_item(item_id)
        if not item or item.get("organization_id") != self._organization_id:
            return None
        return deepcopy(item)

    def list(self, status: Optional[str] = None) -> List[Document]:
        return [
            deepcopy(item)
            for item in self._backend.list_work_items()
            if item.get("organization_id") == self._organization_id
            and (status is None or item.get("status") == status)
        ]

    def claimable(self, limit: int = 20) -> List[Document]:
        items = [
            item
            for item in self.list()
            if (
                item.get("status") in {"pending", "failed"}
                and _utc_is_due(item.get("available_at"))
            )
            or (
                item.get("status") == "running"
                and _utc_is_due(item.get("lease_expires_at"))
            )
        ]
        items.sort(key=lambda item: (item.get("available_at", ""), item.get("created_at", ""), item["id"]))
        return items[: max(0, limit)]

    def start(self, item_id: str, *, lease_seconds: int = 60) -> Document:
        item = self._required(item_id)
        claimable_status = item["status"] in {"pending", "failed"}
        expired_lease = item["status"] == "running" and _utc_is_due(item.get("lease_expires_at"))
        if not _utc_is_due(item.get("available_at")) or not (claimable_status or expired_lease):
            raise ConcurrencyConflict("Work item is not claimable: %s" % item_id)
        item["status"] = "running"
        item["attempt_count"] = int(item.get("attempt_count", 0)) + 1
        item["lease_token"] = new_id("lease")
        item["lease_expires_at"] = _utc_after(lease_seconds)
        item["updated_at"] = utc_now()
        self._backend.replace_work_item(item)
        return deepcopy(item)

    def complete(self, item_id: str, *, lease_token: str) -> Document:
        item = self._required(item_id)
        self._require_lease(item, lease_token)
        item["status"] = "completed"
        item["lease_token"] = None
        item["lease_expires_at"] = None
        item["last_error"] = None
        item["updated_at"] = utc_now()
        self._backend.replace_work_item(item)
        return deepcopy(item)

    def fail(self, item_id: str, error: str, *, lease_token: str) -> Document:
        item = self._required(item_id)
        self._require_lease(item, lease_token)
        max_attempts = max(1, int(item.get("max_attempts", 5)))
        exhausted = int(item.get("attempt_count", 0)) >= max_attempts
        item["status"] = "dead_letter" if exhausted else "failed"
        item["lease_token"] = None
        item["lease_expires_at"] = None
        item["last_error"] = error[:1000]
        if exhausted:
            item["dead_lettered_at"] = utc_now()
        else:
            base = max(0, int(item.get("retry_base_seconds", 0)))
            delay = min(int(item.get("retry_max_seconds", 300)), base * (2 ** max(0, int(item["attempt_count"]) - 1)))
            item["available_at"] = _utc_after(delay) if delay else utc_now()
        item["updated_at"] = utc_now()
        self._backend.replace_work_item(item)
        return deepcopy(item)

    def replay(self, item_id: str, *, reason: str, actor_id: str) -> Document:
        item = self._required(item_id)
        if item.get("status") not in {"failed", "dead_letter"}:
            raise ConcurrencyConflict("Only failed or dead-letter work can be replayed: %s" % item_id)
        item["status"] = "pending"
        item["attempt_count"] = 0
        item["available_at"] = utc_now()
        item["lease_token"] = None
        item["lease_expires_at"] = None
        item["dead_lettered_at"] = None
        item["replay_count"] = int(item.get("replay_count", 0)) + 1
        item["last_replay"] = {"reason": reason, "actor_id": actor_id, "replayed_at": utc_now()}
        item["updated_at"] = utc_now()
        self._backend.replace_work_item(item)
        return deepcopy(item)

    def _required(self, item_id: str) -> Document:
        item = self._backend.get_work_item(item_id)
        if not item or item.get("organization_id") != self._organization_id:
            raise RecordNotFound("Work item does not exist: %s" % item_id)
        return item

    def _require_tenant(self, item: Document) -> None:
        if item.get("organization_id") != self._organization_id:
            raise ValueError("Work item organization_id does not match the transaction tenant.")

    def _require_lease(self, item: Document, lease_token: str) -> None:
        if (
            item.get("status") != "running"
            or item.get("lease_token") != lease_token
            or _utc_is_due(item.get("lease_expires_at"))
        ):
            raise ConcurrencyConflict("Work item lease is no longer owned: %s" % item["id"])


class ProviderSecretRepository:
    def __init__(self, backend: TransactionBackend, organization_id: str) -> None:
        self._backend = backend
        self._organization_id = organization_id
        self._vault = ProviderSecretVault()

    def get(self, provider_connection_id: str) -> Document:
        sealed = self._backend.get_secret(self._organization_id, provider_connection_id)
        return deepcopy(self._vault.open(sealed))

    def replace(self, provider_connection_id: str, credentials: Document) -> None:
        self._backend.replace_secret(
            self._organization_id,
            provider_connection_id,
            self._vault.seal(deepcopy(credentials)),
        )


class ModelInvocationRepository:
    def __init__(self, backend: TransactionBackend, organization_id: str) -> None:
        self._backend = backend
        self._organization_id = organization_id

    def append(self, invocation: Document) -> Document:
        item = deepcopy(invocation)
        if item.get("organization_id") != self._organization_id:
            raise ValueError("ModelInvocation organization_id does not match the transaction tenant.")
        self._backend.insert_invocation(item)
        return deepcopy(item)

    def list(self) -> List[Document]:
        return [
            deepcopy(item)
            for item in self._backend.list_invocations()
            if item.get("organization_id") == self._organization_id
        ]


class PersistenceTransaction:
    def __init__(self, backend: TransactionBackend, organization_id: str) -> None:
        self.job_positions = VersionedDocumentRepository(
            backend, organization_id, collection="job_positions", entity_name="JobPosition"
        )
        self.knowledge_bases = VersionedDocumentRepository(
            backend, organization_id, collection="knowledge_bases", entity_name="KnowledgeBase"
        )
        self.questions = QuestionRepository(backend, organization_id)
        self.question_speech_assets = VersionedDocumentRepository(
            backend,
            organization_id,
            collection="question_speech_assets",
            entity_name="QuestionSpeechAsset",
        )
        self.role_requirements = VersionedDocumentRepository(
            backend, organization_id, collection="role_requirements", entity_name="RoleRequirement"
        )
        self.interview_plans = VersionedDocumentRepository(
            backend, organization_id, collection="interview_plans", entity_name="InterviewPlan"
        )
        self.candidate_profiles = VersionedDocumentRepository(
            backend, organization_id, collection="candidate_profiles", entity_name="CandidateProfile"
        )
        self.resume_documents = VersionedDocumentRepository(
            backend, organization_id, collection="resume_documents", entity_name="ResumeDocument"
        )
        self.file_objects = VersionedDocumentRepository(
            backend, organization_id, collection="file_objects", entity_name="FileObject"
        )
        self.audit_events = VersionedDocumentRepository(
            backend, organization_id, collection="audit_events", entity_name="AuditEvent"
        )
        self.resume_reviews = VersionedDocumentRepository(
            backend, organization_id, collection="resume_reviews", entity_name="ResumeReview"
        )
        self.experience_questions = VersionedDocumentRepository(
            backend, organization_id, collection="experience_questions", entity_name="ExperienceQuestion"
        )
        self.interview_appointments = VersionedDocumentRepository(
            backend,
            organization_id,
            collection="interview_appointments",
            entity_name="InterviewAppointment",
        )
        self.candidate_intakes = VersionedDocumentRepository(
            backend, organization_id, collection="candidate_intakes", entity_name="CandidateIntake"
        )
        self.interview_sessions = VersionedDocumentRepository(
            backend, organization_id, collection="interviews", entity_name="InterviewSession"
        )
        self.provider_connections = VersionedDocumentRepository(
            backend, organization_id, collection="provider_connections", entity_name="ProviderConnection"
        )
        self.model_configurations = VersionedDocumentRepository(
            backend, organization_id, collection="model_configurations", entity_name="ModelConfiguration"
        )
        self.model_routes = VersionedDocumentRepository(
            backend, organization_id, collection="model_routes", entity_name="ModelRoute"
        )
        self.model_circuit_states = VersionedDocumentRepository(
            backend, organization_id, collection="model_circuit_states", entity_name="ModelCircuitState"
        )
        self.outbox = OutboxRepository(backend, organization_id)
        self.provider_secrets = ProviderSecretRepository(backend, organization_id)
        self.model_invocations = ModelInvocationRepository(backend, organization_id)


class Persistence(Protocol):
    def transaction(self, organization_id: str) -> AbstractContextManager[PersistenceTransaction]: ...


def new_work_item(
    *,
    organization_id: str,
    kind: str,
    aggregate_id: str,
    idempotency_key: str,
    payload: Optional[Document] = None,
) -> Document:
    now = utc_now()
    runtime = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower()
    return {
        "id": new_id("work"),
        "organization_id": organization_id,
        "kind": kind,
        "aggregate_id": aggregate_id,
        "idempotency_key": idempotency_key,
        "payload": deepcopy(payload or {}),
        "status": "pending",
        "attempt_count": 0,
        "max_attempts": max(1, int(os.getenv("INTERVIEWER_OUTBOX_MAX_ATTEMPTS", "5"))),
        "retry_base_seconds": max(
            0,
            int(os.getenv("INTERVIEWER_OUTBOX_RETRY_BASE_SECONDS", "2" if runtime == "production" else "0")),
        ),
        "retry_max_seconds": max(1, int(os.getenv("INTERVIEWER_OUTBOX_RETRY_MAX_SECONDS", "300"))),
        "dead_lettered_at": None,
        "replay_count": 0,
        "available_at": now,
        "lease_token": None,
        "lease_expires_at": None,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
    }


def _utc_after(seconds: int) -> str:
    value = datetime.now(timezone.utc) + timedelta(seconds=max(1, seconds))
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_is_due(value: Optional[str]) -> bool:
    if not value:
        return False
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed <= datetime.now(timezone.utc)
