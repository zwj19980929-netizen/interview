import os

from app.persistence.provider import persistence_for
from app.repositories.provider import get_store
from app.workers.celery_app import celery_app
from app.workers.knowledge_base_speech import execute_work_item


@celery_app.task(name="interviewer.dispatch_due_work")
def dispatch_due_work() -> dict:
    """Publish database-backed due work without claiming or executing it in the scheduler."""
    organization_id = os.getenv("INTERVIEWER_ORGANIZATION_ID", "org_default")
    limit = max(1, int(os.getenv("INTERVIEWER_CELERY_DISPATCH_LIMIT", "100")))
    persistence = persistence_for(get_store())
    with persistence.transaction(organization_id) as transaction:
        items = transaction.outbox.claimable(limit)
    for item in items:
        execute_work_item.delay(organization_id, item["id"])
    return {"organization_id": organization_id, "dispatched": len(items)}
