from contextlib import AbstractContextManager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol

from app.core.ids import new_id
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

    def get_work_item(self, item_id: str) -> Optional[Document]: ...

    def list_work_items(self) -> List[Document]: ...

    def insert_work_item(self, item: Document) -> None: ...

    def replace_work_item(self, item: Document) -> None: ...

    def get_secret(self, item_id: str) -> Document: ...

    def replace_secret(self, item_id: str, secret: Document) -> None: ...

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


class VectorDocumentRepository:
    def __init__(self, backend: TransactionBackend, organization_id: str) -> None:
        self._backend = backend
        self._organization_id = organization_id

    def list(self) -> List[Document]:
        return [
            deepcopy(item)
            for item in self._backend.list_documents("vector_documents")
            if item.get("organization_id") == self._organization_id
        ]

    def replace_for_question(self, question_id: str, documents: List[Document]) -> None:
        for item in documents:
            if item.get("organization_id") != self._organization_id or item.get("question_id") != question_id:
                raise ValueError("Vector document does not match the transaction tenant and question.")
        self._backend.delete_documents(
            "vector_documents",
            lambda item: item.get("organization_id") == self._organization_id
            and item.get("question_id") == question_id,
        )
        for item in documents:
            self._backend.insert_document("vector_documents", deepcopy(item))


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
        item["status"] = "failed"
        item["lease_token"] = None
        item["lease_expires_at"] = None
        item["last_error"] = error[:1000]
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
    def __init__(self, backend: TransactionBackend) -> None:
        self._backend = backend

    def get(self, provider_config_id: str) -> Document:
        return deepcopy(self._backend.get_secret(provider_config_id))

    def replace(self, provider_config_id: str, credentials: Document) -> None:
        self._backend.replace_secret(provider_config_id, deepcopy(credentials))


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
        self.questions = QuestionRepository(backend, organization_id)
        self.role_requirements = VersionedDocumentRepository(
            backend, organization_id, collection="role_requirements", entity_name="RoleRequirement"
        )
        self.interview_plans = VersionedDocumentRepository(
            backend, organization_id, collection="interview_plans", entity_name="InterviewPlan"
        )
        self.interview_sessions = VersionedDocumentRepository(
            backend, organization_id, collection="interviews", entity_name="InterviewSession"
        )
        self.provider_configs = VersionedDocumentRepository(
            backend, organization_id, collection="provider_configs", entity_name="ModelProviderConfig"
        )
        self.model_routes = VersionedDocumentRepository(
            backend, organization_id, collection="model_routes", entity_name="ModelRoute"
        )
        self.vector_documents = VectorDocumentRepository(backend, organization_id)
        self.outbox = OutboxRepository(backend, organization_id)
        self.provider_secrets = ProviderSecretRepository(backend)
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
    return {
        "id": new_id("work"),
        "organization_id": organization_id,
        "kind": kind,
        "aggregate_id": aggregate_id,
        "idempotency_key": idempotency_key,
        "payload": deepcopy(payload or {}),
        "status": "pending",
        "attempt_count": 0,
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
