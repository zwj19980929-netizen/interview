import os

from celery import Celery


BROKER_URL = os.getenv("INTERVIEWER_CELERY_BROKER_URL", "redis://127.0.0.1:6379/2")
QUEUE = os.getenv("INTERVIEWER_CELERY_QUEUE", "interviewer")

celery_app = Celery(
    "interviewer",
    broker=BROKER_URL,
    include=["app.workers.dispatcher", "app.workers.knowledge_base_speech", "app.workers.retention"],
)
celery_app.conf.update(
    task_default_queue=QUEUE,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=int(os.getenv("INTERVIEWER_CELERY_SOFT_TIME_LIMIT", "270")),
    task_time_limit=int(os.getenv("INTERVIEWER_CELERY_HARD_TIME_LIMIT", "300")),
    result_backend=None,
    beat_schedule_filename=os.getenv("INTERVIEWER_CELERY_BEAT_SCHEDULE", "/tmp/interviewer-celerybeat-schedule"),
    beat_schedule={
        "dispatch-durable-work": {
            "task": "interviewer.dispatch_due_work",
            "schedule": float(os.getenv("INTERVIEWER_CELERY_DISPATCH_SECONDS", "2")),
        },
        "purge-unqualified-candidates": {
            "task": "interviewer.run_screening_retention",
            "schedule": float(os.getenv("INTERVIEWER_SCREENING_RETENTION_SECONDS", "3600")),
        },
    },
)


def main() -> None:
    celery_app.worker_main(["worker", "--beat", "--loglevel=INFO"])
