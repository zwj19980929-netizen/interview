from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.persistence.errors import ConcurrencyConflict, RecordAlreadyExists
from app.persistence.interface import Document, PersistenceTransaction, Predicate, TransactionBackend
from app.repositories.postgresql import PostgreSQLStore


MIGRATIONS = [
    Path(__file__).resolve().parents[2] / "migrations" / "001_postgresql_persistence.sql",
    Path(__file__).resolve().parents[2] / "migrations" / "002_model_configuration_v2.sql",
]


class _PostgreSQLTransactionBackend(TransactionBackend):
    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    def get_document(self, collection: str, item_id: str) -> Optional[Document]:
        row = self.connection.execute(
            "SELECT data FROM documents WHERE collection = %s AND id = %s FOR UPDATE",
            (collection, item_id),
        ).fetchone()
        return dict(row["data"]) if row else None

    def list_documents(self, collection: str) -> List[Document]:
        rows = self.connection.execute(
            "SELECT data FROM documents WHERE collection = %s ORDER BY id", (collection,)
        ).fetchall()
        return [dict(row["data"]) for row in rows]

    def insert_document(self, collection: str, item: Document) -> None:
        self.connection.execute(
            "INSERT INTO documents(collection, id, organization_id, data, updated_at) VALUES (%s, %s, %s, %s, %s)",
            (collection, item["id"], item["organization_id"], Jsonb(item), _item_time(item)),
        )

    def replace_document(self, collection: str, item: Document) -> None:
        previous_version = max(1, int(item.get("version", 1)) - 1)
        cursor = self.connection.execute(
            """
            UPDATE documents SET data = %s, updated_at = %s
            WHERE collection = %s AND id = %s
              AND COALESCE((data->>'version')::integer, 1) = %s
            """,
            (Jsonb(item), _item_time(item), collection, item["id"], previous_version),
        )
        if cursor.rowcount != 1:
            raise ConcurrencyConflict("PostgreSQL document version changed: %s/%s" % (collection, item["id"]))

    def delete_documents(self, collection: str, predicate: Predicate) -> None:
        for item in self.list_documents(collection):
            if predicate(item):
                self.connection.execute(
                    "DELETE FROM documents WHERE collection = %s AND id = %s",
                    (collection, item["id"]),
                )

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
            "organization_id = %s",
            "data->>'status' = 'active'",
            "data->>'validation_status' = 'valid'",
            "data->>'speech_status' = 'ready'",
            "data->>'knowledge_base_id' = ANY(%s)",
        ]
        parameters: List[object] = [organization_id, knowledge_base_ids]
        if skills:
            clauses.append("data->'skills' ?| %s")
            parameters.append(skills)
        if difficulties:
            clauses.append("data->>'difficulty' = ANY(%s)")
            parameters.append(difficulties)
        if question_types:
            clauses.append("data->>'type' = ANY(%s)")
            parameters.append(question_types)
        rows = self.connection.execute(
            "SELECT data FROM documents WHERE %s ORDER BY id" % " AND ".join(clauses), parameters
        ).fetchall()
        return [dict(row["data"]) for row in rows]

    def get_work_item(self, item_id: str) -> Optional[Document]:
        row = self.connection.execute(
            "SELECT data FROM outbox_work_items WHERE id = %s FOR UPDATE", (item_id,)
        ).fetchone()
        return dict(row["data"]) if row else None

    def list_work_items(self) -> List[Document]:
        rows = self.connection.execute(
            "SELECT data FROM outbox_work_items ORDER BY created_at, id"
        ).fetchall()
        return [dict(row["data"]) for row in rows]

    def insert_work_item(self, item: Document) -> None:
        self.connection.execute(
            """
            INSERT INTO outbox_work_items(id, organization_id, idempotency_key, status, data, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                item["id"], item["organization_id"], item["idempotency_key"], item["status"],
                Jsonb(item), item["created_at"], item["updated_at"],
            ),
        )

    def replace_work_item(self, item: Document) -> None:
        cursor = self.connection.execute(
            "UPDATE outbox_work_items SET status = %s, data = %s, updated_at = %s WHERE id = %s",
            (item["status"], Jsonb(item), item["updated_at"], item["id"]),
        )
        if cursor.rowcount != 1:
            raise ConcurrencyConflict("PostgreSQL work item changed: %s" % item["id"])

    def get_secret(self, organization_id: str, item_id: str) -> Document:
        row = self.connection.execute(
            "SELECT data FROM provider_secrets WHERE organization_id = %s AND provider_connection_id = %s",
            (organization_id, item_id),
        ).fetchone()
        return dict(row["data"]) if row else {}

    def replace_secret(self, organization_id: str, item_id: str, secret: Document) -> None:
        self.connection.execute(
            """
            INSERT INTO provider_secrets(provider_connection_id, organization_id, data, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT(organization_id, provider_connection_id) DO UPDATE
            SET data = excluded.data, updated_at = excluded.updated_at
            """,
            (item_id, organization_id, Jsonb(secret)),
        )

    def delete_secret(self, organization_id: str, item_id: str) -> None:
        self.connection.execute(
            "DELETE FROM provider_secrets WHERE organization_id = %s AND provider_connection_id = %s",
            (organization_id, item_id),
        )

    def list_invocations(self) -> List[Document]:
        rows = self.connection.execute(
            "SELECT data FROM model_invocations ORDER BY created_at, id"
        ).fetchall()
        return [dict(row["data"]) for row in rows]

    def insert_invocation(self, item: Document) -> None:
        self.connection.execute(
            "INSERT INTO model_invocations(id, organization_id, data, created_at) VALUES (%s, %s, %s, %s)",
            (item["id"], item["organization_id"], Jsonb(item), item["created_at"]),
        )


class PostgreSQLPersistence:
    def __init__(self, store: PostgreSQLStore) -> None:
        self.store = store
        self._verify_schema()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.store.dsn, row_factory=dict_row)

    def _verify_schema(self) -> None:
        """Fail fast without granting the runtime role any DDL responsibility."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    to_regclass('public.documents') AS documents,
                    to_regclass('public.outbox_work_items') AS outbox_work_items,
                    to_regclass('public.provider_secrets') AS provider_secrets,
                    to_regclass('public.model_invocations') AS model_invocations
                """
            ).fetchone()
            missing = [name for name, value in dict(row).items() if value is None]
            if missing:
                raise RuntimeError(
                    "PostgreSQL schema is not migrated (%s missing). Run "
                    "`python -m app.migrations.postgresql` with a migration-owner DSN before startup."
                    % ", ".join(sorted(missing))
                )

    @contextmanager
    def transaction(self, organization_id: str) -> Iterator[PersistenceTransaction]:
        try:
            with self._connect() as connection:
                connection.execute("SELECT set_config('app.organization_id', %s, true)", (organization_id,))
                yield PersistenceTransaction(_PostgreSQLTransactionBackend(connection), organization_id)
        except psycopg.errors.UniqueViolation as exc:
            raise RecordAlreadyExists("PostgreSQL unique constraint rejected the transaction.") from exc
        except psycopg.errors.SerializationFailure as exc:
            raise ConcurrencyConflict("PostgreSQL serialization conflict.") from exc


def _item_time(item: Document) -> Optional[str]:
    return item.get("updated_at") or item.get("created_at")
