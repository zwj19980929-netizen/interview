from app.repositories.memory import InMemoryStore


class PostgreSQLStore(InMemoryStore):
    """Configuration holder; durable state lives only in PostgreSQL."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        super().__init__()

    def _after_reset(self) -> None:
        return None
