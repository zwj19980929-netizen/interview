from contextlib import contextmanager
from copy import deepcopy
from threading import RLock
from typing import Dict, Iterator, List, Optional

from app.persistence.interface import Document, PersistenceTransaction, Predicate, TransactionBackend
from app.repositories.memory import InMemoryStore


DOCUMENT_COLLECTIONS = (
    "questions",
    "job_positions",
    "knowledge_bases",
    "question_speech_assets",
    "role_requirements",
    "interview_plans",
    "candidate_profiles",
    "resume_documents",
    "file_objects",
    "audit_events",
    "resume_reviews",
    "experience_questions",
    "interview_appointments",
    "candidate_intakes",
    "candidates",
    "interviews",
    "turns",
    "answers",
    "evaluations",
    "reports",
    "provider_connections",
    "model_configurations",
    "model_routes",
    "model_circuit_states",
)


class _MemoryTransactionBackend(TransactionBackend):
    def __init__(self, store: InMemoryStore) -> None:
        self.documents: Dict[str, Dict[str, Document]] = {
            collection: deepcopy(getattr(store, collection)) for collection in DOCUMENT_COLLECTIONS
        }
        self.work_items = deepcopy(store.outbox_work_items)
        self.secrets = deepcopy(store.provider_secrets)
        self.invocations = deepcopy(store.model_invocations)

    def get_document(self, collection: str, item_id: str) -> Optional[Document]:
        return deepcopy(self.documents[collection].get(item_id))

    def list_documents(self, collection: str) -> List[Document]:
        return [deepcopy(item) for item in self.documents[collection].values()]

    def insert_document(self, collection: str, item: Document) -> None:
        self.documents[collection][item["id"]] = deepcopy(item)

    def replace_document(self, collection: str, item: Document) -> None:
        self.documents[collection][item["id"]] = deepcopy(item)

    def delete_documents(self, collection: str, predicate: Predicate) -> None:
        ids = [item_id for item_id, item in self.documents[collection].items() if predicate(item)]
        for item_id in ids:
            self.documents[collection].pop(item_id, None)

    def search_question_catalog(
        self,
        *,
        organization_id: str,
        job_position_id: str,
        knowledge_base_ids: List[str],
        skills: List[str],
        difficulties: List[str],
        question_types: List[str],
    ) -> List[Document]:
        allowed_knowledge_bases = set(knowledge_base_ids)
        required_skills = set(skills)
        allowed_difficulties = set(difficulties)
        allowed_types = set(question_types)
        return [
            deepcopy(item)
            for item in self.documents["questions"].values()
            if item.get("organization_id") == organization_id
            and item.get("job_position_id") == job_position_id
            and item.get("knowledge_base_id") in allowed_knowledge_bases
            and item.get("status") == "active"
            and item.get("validation_status") == "valid"
            and item.get("speech_status") == "ready"
            and (not required_skills or required_skills.intersection(item.get("skills", [])))
            and (not allowed_difficulties or item.get("difficulty") in allowed_difficulties)
            and (not allowed_types or item.get("type") in allowed_types)
        ]

    def get_work_item(self, item_id: str) -> Optional[Document]:
        return deepcopy(self.work_items.get(item_id))

    def list_work_items(self) -> List[Document]:
        return [deepcopy(item) for item in self.work_items.values()]

    def insert_work_item(self, item: Document) -> None:
        self.work_items[item["id"]] = deepcopy(item)

    def replace_work_item(self, item: Document) -> None:
        self.work_items[item["id"]] = deepcopy(item)

    def get_secret(self, organization_id: str, item_id: str) -> Document:
        return deepcopy(self.secrets.get(item_id, {}))

    def replace_secret(self, organization_id: str, item_id: str, secret: Document) -> None:
        self.secrets[item_id] = deepcopy(secret)

    def list_invocations(self) -> List[Document]:
        return deepcopy(self.invocations)

    def insert_invocation(self, item: Document) -> None:
        self.invocations.append(deepcopy(item))


class MemoryPersistence:
    def __init__(self, store: InMemoryStore) -> None:
        self.store = store
        self._lock = RLock()

    @contextmanager
    def transaction(self, organization_id: str) -> Iterator[PersistenceTransaction]:
        with self._lock:
            backend = _MemoryTransactionBackend(self.store)
            transaction = PersistenceTransaction(backend, organization_id)
            try:
                yield transaction
            except Exception:
                raise
            else:
                for collection in DOCUMENT_COLLECTIONS:
                    target = getattr(self.store, collection)
                    target.clear()
                    target.update(deepcopy(backend.documents[collection]))
                self.store.outbox_work_items.clear()
                self.store.outbox_work_items.update(deepcopy(backend.work_items))
                self.store.provider_secrets.clear()
                self.store.provider_secrets.update(deepcopy(backend.secrets))
                self.store.model_invocations.clear()
                self.store.model_invocations.extend(deepcopy(backend.invocations))
