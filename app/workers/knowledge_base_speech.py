import asyncio

from app.repositories.provider import get_store
from app.persistence.errors import ConcurrencyConflict
from app.workers.celery_app import celery_app
from app.workers.outbox import OutboxWorker


@celery_app.task(
    name="interviewer.execute_work_item",
    bind=True,
    autoretry_for=(),
)
def execute_work_item(self, organization_id: str, work_item_id: str) -> dict:
    """Execute one durable item; business retry state remains in the database."""
    try:
        result = asyncio.run(OutboxWorker(get_store()).run_item(work_item_id, organization_id))
    except ConcurrencyConflict:
        return {"id": work_item_id, "status": "already_claimed"}
    return {"id": result["id"], "kind": result.get("kind"), "status": result.get("status")}
