import json
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Set, Tuple

from app.persistence.interface import Document, PersistenceTransaction, Predicate, TransactionBackend
from app.repositories.sqlite import SQLiteStore


class _SQLiteTransactionBackend(TransactionBackend):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        # SQLite is authoritative inside the transaction.  These deltas only
        # refresh the legacy in-process read model after a successful commit;
        # reloading every collection here used to make one tiny realtime
        # command deserialize the entire local database on the event loop.
        self._document_updates: Dict[Tuple[str, str], Document] = {}
        self._document_deletes: Set[Tuple[str, str]] = set()
        self._work_item_updates: Dict[str, Document] = {}
        self._secret_updates: Dict[str, Document] = {}
        self._secret_deletes: Set[str] = set()
        self._invocation_inserts: List[Document] = []

    def database_now(self) -> datetime:
        row = self.connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS now"
        ).fetchone()
        return datetime.fromisoformat(str(row["now"]).replace("Z", "+00:00")).astimezone(
            timezone.utc
        )

    def get_document(self, collection: str, item_id: str) -> Optional[Document]:
        row = self.connection.execute(
            "SELECT data FROM documents WHERE collection = ? AND id = ?",
            (collection, item_id),
        ).fetchone()
        return json.loads(row["data"]) if row else None

    def list_documents(self, collection: str) -> List[Document]:
        rows = self.connection.execute(
            "SELECT data FROM documents WHERE collection = ? ORDER BY id",
            (collection,),
        ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def insert_document(self, collection: str, item: Document) -> None:
        self.connection.execute(
            "INSERT INTO documents(collection, id, data, updated_at) VALUES (?, ?, ?, ?)",
            (collection, item["id"], json.dumps(item, ensure_ascii=False), _item_time(item)),
        )
        self._remember_document(collection, item)

    def replace_document(self, collection: str, item: Document) -> None:
        cursor = self.connection.execute(
            "UPDATE documents SET data = ?, updated_at = ? WHERE collection = ? AND id = ?",
            (json.dumps(item, ensure_ascii=False), _item_time(item), collection, item["id"]),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Document disappeared during transaction: %s/%s" % (collection, item["id"]))
        self._remember_document(collection, item)

    def delete_documents(self, collection: str, predicate: Predicate) -> None:
        for item in self.list_documents(collection):
            if predicate(item):
                self.connection.execute(
                    "DELETE FROM documents WHERE collection = ? AND id = ?",
                    (collection, item["id"]),
                )
                key = (collection, str(item["id"]))
                self._document_updates.pop(key, None)
                self._document_deletes.add(key)

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
        clauses = [
            "collection = 'questions'",
            "json_extract(data, '$.organization_id') = ?",
            "json_extract(data, '$.status') = 'active'",
            "json_extract(data, '$.validation_status') = 'valid'",
            "json_extract(data, '$.speech_status') = 'ready'",
        ]
        parameters: List[str] = [organization_id]
        self._append_json_in_filter(clauses, parameters, "knowledge_base_id", knowledge_base_ids)
        self._append_json_in_filter(clauses, parameters, "difficulty", difficulties)
        self._append_json_in_filter(clauses, parameters, "type", question_types)
        if skills:
            placeholders = ", ".join("?" for _ in skills)
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(json_extract(data, '$.skills')) "
                "WHERE json_each.value IN (%s))" % placeholders
            )
            parameters.extend(skills)
        rows = self.connection.execute(
            "SELECT data FROM documents WHERE %s ORDER BY id" % " AND ".join(clauses),
            parameters,
        ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def _append_json_in_filter(
        self,
        clauses: List[str],
        parameters: List[str],
        field: str,
        values: List[str],
    ) -> None:
        if not values:
            return
        placeholders = ", ".join("?" for _ in values)
        clauses.append("json_extract(data, '$.%s') IN (%s)" % (field, placeholders))
        parameters.extend(values)

    def get_work_item(self, item_id: str) -> Optional[Document]:
        row = self.connection.execute(
            "SELECT data FROM outbox_work_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        return json.loads(row["data"]) if row else None

    def list_work_items(self) -> List[Document]:
        rows = self.connection.execute("SELECT data FROM outbox_work_items ORDER BY created_at, id").fetchall()
        return [json.loads(row["data"]) for row in rows]

    def insert_work_item(self, item: Document) -> None:
        self.connection.execute(
            """
            INSERT INTO outbox_work_items(
                id, organization_id, idempotency_key, status, data, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"],
                item["organization_id"],
                item["idempotency_key"],
                item["status"],
                json.dumps(item, ensure_ascii=False),
                item["created_at"],
                item["updated_at"],
            ),
        )
        self._work_item_updates[str(item["id"])] = deepcopy(item)

    def replace_work_item(self, item: Document) -> None:
        cursor = self.connection.execute(
            """
            UPDATE outbox_work_items
            SET status = ?, data = ?, updated_at = ?
            WHERE id = ?
            """,
            (item["status"], json.dumps(item, ensure_ascii=False), item["updated_at"], item["id"]),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Work item disappeared during transaction: %s" % item["id"])
        self._work_item_updates[str(item["id"])] = deepcopy(item)

    def get_secret(self, organization_id: str, item_id: str) -> Document:
        row = self.connection.execute(
            "SELECT data FROM provider_secrets WHERE provider_connection_id = ?",
            (item_id,),
        ).fetchone()
        return json.loads(row["data"]) if row else {}

    def replace_secret(self, organization_id: str, item_id: str, secret: Document) -> None:
        self.connection.execute(
            """
            INSERT INTO provider_secrets(provider_connection_id, data, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(provider_connection_id)
            DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
            """,
            (item_id, json.dumps(secret, ensure_ascii=False)),
        )
        self._secret_deletes.discard(item_id)
        self._secret_updates[item_id] = deepcopy(secret)

    def delete_secret(self, organization_id: str, item_id: str) -> None:
        self.connection.execute(
            "DELETE FROM provider_secrets WHERE provider_connection_id = ?",
            (item_id,),
        )
        self._secret_updates.pop(item_id, None)
        self._secret_deletes.add(item_id)

    def list_invocations(self) -> List[Document]:
        rows = self.connection.execute(
            "SELECT data FROM model_invocations ORDER BY created_at, id"
        ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def insert_invocation(self, item: Document) -> None:
        self.connection.execute(
            "INSERT INTO model_invocations(id, data, created_at) VALUES (?, ?, ?)",
            (item["id"], json.dumps(item, ensure_ascii=False), item.get("created_at")),
        )
        self._invocation_inserts.append(deepcopy(item))

    def _remember_document(self, collection: str, item: Document) -> None:
        key = (collection, str(item["id"]))
        self._document_deletes.discard(key)
        self._document_updates[key] = deepcopy(item)

    def sync_store_cache(self, store: SQLiteStore) -> None:
        """Apply only this committed transaction to the compatibility cache."""

        for collection, item_id in self._document_deletes:
            getattr(store, collection).pop(item_id, None)
        for (collection, item_id), item in self._document_updates.items():
            getattr(store, collection)[item_id] = deepcopy(item)
        for item_id, item in self._work_item_updates.items():
            store.outbox_work_items[item_id] = deepcopy(item)
        for item_id in self._secret_deletes:
            store.provider_secrets.pop(item_id, None)
        for item_id, secret in self._secret_updates.items():
            store.provider_secrets[item_id] = deepcopy(secret)
        store.model_invocations.extend(deepcopy(self._invocation_inserts))


class SQLitePersistence:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @contextmanager
    def transaction(self, organization_id: str) -> Iterator[PersistenceTransaction]:
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            backend = _SQLiteTransactionBackend(connection)
            yield PersistenceTransaction(backend, organization_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        backend.sync_store_cache(self.store)


def _item_time(item: Document) -> Optional[str]:
    return item.get("updated_at") or item.get("created_at")
