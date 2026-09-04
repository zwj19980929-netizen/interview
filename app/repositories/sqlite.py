import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from app.core.time import utc_now
from app.repositories.memory import InMemoryStore


DOCUMENT_COLLECTIONS = [
    "questions",
    "question_generation_batches",
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
    "interview_media_captures",
    "agent_tickets",
    "evidence_ownerships",
    "evidence_commands",
    "evidence_media_streams",
    "evidence_media_segments",
    "turns",
    "answers",
    "evaluations",
    "reports",
    "provider_connections",
    "model_configurations",
    "model_routes",
    "model_circuit_states",
]


class SQLiteStore(InMemoryStore):
    """SQLite-backed store for local development.

    The public shape intentionally matches InMemoryStore so services can move
    toward repository boundaries without a large rewrite. PostgreSQL uses the
    same document and structured catalog interfaces.
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._suspend_persistence = True
        self._init_db()
        super().__init__()
        self._suspend_persistence = False
        self._load_from_db()
        if not self.provider_connections:
            self.provider_connections = self._default_provider_connections()
            self.save_many("provider_connections", list(self.provider_connections.values()))

    def _default_provider_connections(self) -> Dict[str, Dict[str, Any]]:
        now = utc_now()
        return {
            "provider_conn_mock": {
                "id": "provider_conn_mock",
                "organization_id": "org_default",
                "provider_id": "mock",
                "display_name": "Mock Provider",
                "enabled": True,
                "connection_config": {},
                "credential_status": "valid",
                "credential_ref": "secret://mock",
                "version": 1,
                "created_at": now,
                "updated_at": now,
            }
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    collection TEXT NOT NULL,
                    id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    updated_at TEXT,
                    PRIMARY KEY (collection, id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS model_invocations (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_secrets (
                    provider_connection_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_work_items (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (organization_id, idempotency_key)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_documents_collection
                ON documents(collection)
                """
            )

    def _load_from_db(self) -> None:
        for collection in DOCUMENT_COLLECTIONS:
            getattr(self, collection).clear()
        self.model_invocations.clear()
        self.outbox_work_items.clear()
        with self._connect() as connection:
            for row in connection.execute("SELECT collection, id, data FROM documents"):
                collection = row["collection"]
                if collection not in DOCUMENT_COLLECTIONS:
                    continue
                getattr(self, collection)[row["id"]] = json.loads(row["data"])
            for row in connection.execute("SELECT data FROM model_invocations ORDER BY created_at ASC"):
                self.model_invocations.append(json.loads(row["data"]))
            for row in connection.execute("SELECT provider_connection_id, data FROM provider_secrets"):
                self.provider_secrets[row["provider_connection_id"]] = json.loads(row["data"])
            for row in connection.execute("SELECT id, data FROM outbox_work_items ORDER BY created_at, id"):
                self.outbox_work_items[row["id"]] = json.loads(row["data"])

    def reset(self) -> None:
        super().reset()
        if getattr(self, "_suspend_persistence", False):
            return
        with self._connect() as connection:
            connection.execute("DELETE FROM documents")
            connection.execute("DELETE FROM model_invocations")
            connection.execute("DELETE FROM provider_secrets")
            connection.execute("DELETE FROM outbox_work_items")
        self.save_many("provider_connections", list(self.provider_connections.values()))

    def save_item(self, collection: str, item_id: str, item: Dict[str, Any]) -> None:
        if getattr(self, "_suspend_persistence", False):
            return
        if collection not in DOCUMENT_COLLECTIONS:
            raise ValueError("Unknown collection: %s" % collection)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO documents(collection, id, data, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(collection, id)
                DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
                """,
                (
                    collection,
                    item_id,
                    json.dumps(item, ensure_ascii=False),
                    item.get("updated_at") or item.get("created_at"),
                ),
            )

    def save_many(self, collection: str, items: List[Dict[str, Any]]) -> None:
        for item in items:
            self.save_item(collection, item["id"], item)

    def add_model_invocation(self, item: Dict[str, Any]) -> None:
        self.model_invocations.append(item)
        if getattr(self, "_suspend_persistence", False):
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO model_invocations(id, data, created_at)
                VALUES (?, ?, ?)
                """,
                (item["id"], json.dumps(item, ensure_ascii=False), item.get("created_at")),
            )

    def save_provider_secret(self, connection_id: str, credentials: Dict[str, Any]) -> None:
        self.provider_secrets[connection_id] = credentials
        if getattr(self, "_suspend_persistence", False):
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO provider_secrets(provider_connection_id, data, updated_at)
                VALUES (?, ?, ?)
                """,
                (connection_id, json.dumps(credentials, ensure_ascii=False), utc_now()),
            )

    def get_provider_secret(self, connection_id: str) -> Dict[str, Any]:
        return self.provider_secrets.get(connection_id, {})
