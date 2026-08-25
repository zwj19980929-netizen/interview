from collections import Counter
from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore


class OperationsService:
    """Read/replay interface for durable jobs and audit events."""

    def __init__(self, store: InMemoryStore, *, persistence: Optional[Persistence] = None) -> None:
        self.persistence = persistence or persistence_for(store)

    def work_items(self, status: Optional[str] = None, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            items = transaction.outbox.list(status)
        counts = Counter(item["status"] for item in items if status is None)
        return {
            "items": [self._public(item) for item in items],
            "metrics": {
                "total": len(items),
                "by_status": dict(sorted(counts.items())),
                "dead_letter_count": sum(1 for item in items if item["status"] == "dead_letter"),
            },
        }

    def replay(
        self,
        work_item_id: str,
        *,
        reason: str,
        actor_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            if transaction.outbox.get(work_item_id) is None:
                raise ApiError("WORK_ITEM_NOT_FOUND", "Work item does not exist.", status_code=404)
            replayed = transaction.outbox.replay(work_item_id, reason=reason, actor_id=actor_id)
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "outbox.work.replayed",
                    "resource_type": "outbox_work_item",
                    "resource_id": work_item_id,
                    "metadata": {"reason": reason},
                    "created_at": utc_now(),
                }
            )
        return self._public(replayed)

    def audit_events(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return transaction.audit_events.list()

    def _public(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in item.items() if key not in {"lease_token", "payload"}}
