from typing import cast

from app.persistence.interface import Persistence
from app.persistence.memory import MemoryPersistence
from app.persistence.sqlite import SQLitePersistence
from app.persistence.postgresql import PostgreSQLPersistence
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.repositories.postgresql import PostgreSQLStore


def persistence_for(store: InMemoryStore) -> Persistence:
    existing = getattr(store, "_persistence_adapter", None)
    if existing is not None:
        return cast(Persistence, existing)
    if isinstance(store, PostgreSQLStore):
        adapter = PostgreSQLPersistence(store)
    elif isinstance(store, SQLiteStore):
        adapter: Persistence = SQLitePersistence(store)
    else:
        adapter = MemoryPersistence(store)
    setattr(store, "_persistence_adapter", adapter)
    return adapter
