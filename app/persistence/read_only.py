from typing import Any

from app.persistence.errors import ReadOnlyViolation
from app.persistence.interface import TransactionBackend


class ReadOnlyTransactionBackend:
    """Keep the read-only contract identical across all storage adapters.

    Allow only explicit read operations. New backend methods cannot silently
    write through a read transaction, even on the in-memory adapter.
    """

    _READ_METHODS = frozenset({
        "database_now", "get_document", "list_documents", "search_question_catalog",
        "get_work_item", "list_work_items", "get_secret", "list_invocations",
        "list_unsettled_evidence_commands",
        "list_interview_watchdog_candidates",
    })

    def __init__(self, backend: TransactionBackend) -> None:
        self._backend = backend

    def __getattr__(self, name: str) -> Any:
        if name not in self._READ_METHODS:
            raise ReadOnlyViolation("Read-only transactions cannot change persisted state.")
        return getattr(self._backend, name)
