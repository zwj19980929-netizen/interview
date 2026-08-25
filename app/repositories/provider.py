import os
from typing import Optional

from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.repositories.postgresql import PostgreSQLStore


Store = InMemoryStore

_store: Optional[InMemoryStore] = None


def _create_store() -> InMemoryStore:
    backend = os.getenv("INTERVIEWER_DB_BACKEND", "sqlite").lower()
    if backend == "memory":
        return InMemoryStore()
    if backend == "sqlite":
        path = os.getenv("INTERVIEWER_SQLITE_PATH", "data/interviewer.sqlite3")
        return SQLiteStore(path)
    if backend in {"postgres", "postgresql"}:
        dsn = os.getenv("INTERVIEWER_POSTGRES_DSN", "")
        if not dsn:
            raise RuntimeError("INTERVIEWER_POSTGRES_DSN is required for the PostgreSQL backend.")
        return PostgreSQLStore(dsn)
    raise RuntimeError("Unsupported INTERVIEWER_DB_BACKEND: %s" % backend)


def get_store() -> InMemoryStore:
    global _store
    if _store is None:
        _store = _create_store()
    return _store


def reset_store_for_tests() -> InMemoryStore:
    global _store
    _store = InMemoryStore()
    return _store
