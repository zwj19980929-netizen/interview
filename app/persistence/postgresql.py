from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.persistence.errors import ConcurrencyConflict, RecordAlreadyExists
from app.persistence.interface import Document, InterviewWatchdogKind, PersistenceTransaction, Predicate, TransactionBackend
from app.persistence.read_only import ReadOnlyTransactionBackend
from app.repositories.postgresql import PostgreSQLStore


MIGRATIONS = [
    Path(__file__).resolve().parents[2] / "migrations" / "001_postgresql_persistence.sql",
    Path(__file__).resolve().parents[2] / "migrations" / "002_model_configuration_v2.sql",
    Path(__file__).resolve().parents[2] / "migrations" / "003_interview_skills.sql",
    Path(__file__).resolve().parents[2] / "migrations" / "004_optional_interview_skills.sql",
    Path(__file__).resolve().parents[2] / "migrations" / "005_interview_customization.sql",
    Path(__file__).resolve().parents[2] / "migrations" / "006_evidence_command_poll.sql",
    Path(__file__).resolve().parents[2] / "migrations" / "007_interview_watchdog_candidates.sql",
]


def _json_falsy(field: str) -> str:
    # Match existing Python truthiness, including legacy empty containers, but
    # not nonempty strings that happen to spell "false", "0" or "{}".
    return "COALESCE(data->'%s', 'null'::jsonb) IN " % field + (
        "('null'::jsonb, 'false'::jsonb, '0'::jsonb, '\"\"'::jsonb, '[]'::jsonb, '{}'::jsonb)"
    )


_INTERVIEW_WATCHDOG_PREDICATES = {
    "takeover": "jsonb_typeof(data#>'{agent_runtime,takeover}') = 'object' "
                "AND data#>'{agent_runtime,takeover}' <> '{}'::jsonb AND " + _json_falsy("list_removed_at"),
    "deadline": "data->>'status' IN ('scheduled', 'waiting', 'in_progress', 'paused') AND "
                + _json_falsy("candidate_input_completed_at"),
}


class _PostgreSQLTransactionBackend(TransactionBackend):
    def __init__(self, connection: psycopg.Connection, *, read_only: bool = False) -> None:
        self.connection = connection
        self.read_only = read_only

    def database_now(self) -> datetime:
        row = self.connection.execute(
            "SELECT clock_timestamp() AS now"
        ).fetchone()
        value = row["now"]
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def get_document(self, collection: str, item_id: str) -> Optional[Document]:
        row = self.connection.execute(
            "SELECT data FROM documents WHERE collection = %s AND id = %s"
            + ("" if self.read_only else " FOR UPDATE"),
            (collection, item_id),
        ).fetchone()
        return dict(row["data"]) if row else None

    def list_documents(self, collection: str) -> List[Document]:
        rows = self.connection.execute(
            "SELECT data FROM documents WHERE collection = %s ORDER BY id", (collection,)
        ).fetchall()
        return [dict(row["data"]) for row in rows]

    def list_unsettled_evidence_commands(
        self, *, organization_id: str, interview_id: str,
    ) -> List[Document]:
        # Candidate discovery takes no additional row locks. claim_next has
        # already locked ownership, then re-gets each command FOR UPDATE.
        rows = self.connection.execute(
            "SELECT data FROM documents WHERE collection = 'evidence_commands' "
            "AND organization_id = %s AND data->>'interview_id' = %s "
            "AND data->>'status' IN ('pending', 'running')",
            (organization_id, interview_id),
        ).fetchall()
        return [dict(row["data"]) for row in rows]

    def list_interview_watchdog_candidates(
        self, *, organization_id: str, kind: InterviewWatchdogKind,
    ) -> List[Document]:
        if kind not in ("takeover", "deadline"):
            raise ValueError("Unknown interview watchdog kind.")
        rows = self.connection.execute(
            "SELECT data FROM documents WHERE collection = 'interviews' "
            "AND organization_id = %s AND " + _INTERVIEW_WATCHDOG_PREDICATES[kind],
            (organization_id,),
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
            "SELECT data FROM outbox_work_items WHERE id = %s"
            + ("" if self.read_only else " FOR UPDATE"), (item_id,)
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
                    to_regclass('public.model_invocations') AS model_invocations,
                    to_regclass('public.uq_interview_skill_revision') AS interview_skill_revision_index,
                    (SELECT oid FROM pg_constraint
                     WHERE conrelid = to_regclass('public.documents')
                       AND conname = 'ck_interview_skill_active_version') AS interview_skill_active_state,
                    to_regclass('public.uq_interview_customization_organization') AS interview_customization_index,
                    to_regclass('public.idx_evidence_commands_unsettled') AS evidence_command_poll_index,
                    to_regclass('public.idx_interviews_watchdog_takeover') AS interview_takeover_watchdog_index,
                    to_regclass('public.idx_interviews_watchdog_deadline') AS interview_deadline_watchdog_index,
                    (SELECT oid FROM pg_constraint
                     WHERE conrelid = to_regclass('public.documents')
                       AND conname = 'ck_interview_customization') AS interview_customization_contract
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
    def transaction(
        self, organization_id: str, *, read_only: bool = False,
    ) -> Iterator[PersistenceTransaction]:
        try:
            with self._connect() as connection:
                if read_only:
                    connection.execute("SET TRANSACTION READ ONLY")
                connection.execute("SELECT set_config('app.organization_id', %s, true)", (organization_id,))
                backend = _PostgreSQLTransactionBackend(connection, read_only=read_only)
                yield PersistenceTransaction(
                    ReadOnlyTransactionBackend(backend) if read_only else backend, organization_id,
                )
        except psycopg.errors.UniqueViolation as exc:
            raise RecordAlreadyExists("PostgreSQL unique constraint rejected the transaction.") from exc
        except (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected) as exc:
            raise ConcurrencyConflict("PostgreSQL serialization conflict.") from exc


def _item_time(item: Document) -> Optional[str]:
    return item.get("updated_at") or item.get("created_at")
