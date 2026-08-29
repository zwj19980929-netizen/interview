import os

from app.repositories.provider import get_store
from app.services.retention import RetentionService
from app.workers.celery_app import celery_app


@celery_app.task(name="interviewer.run_screening_retention")
def run_screening_retention() -> dict:
    organization_id = os.getenv("INTERVIEWER_ORGANIZATION_ID", "org_default")
    return RetentionService(get_store()).run_screening_retention(organization_id=organization_id)
