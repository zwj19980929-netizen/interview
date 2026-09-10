import asyncio
import os
from typing import Any, Dict, List, Optional

from app.persistence.errors import ConcurrencyConflict
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.repositories.provider import get_store
from app.services.media_capture import InterviewMediaCaptureService
from app.services.interviews import InterviewService
from app.services.catalog import CatalogService
from app.services.knowledge_base_speech import KnowledgeBaseSpeechService
from app.services.question_generation import QuestionGenerationService
from app.services.talent import TalentService
from app.services.resume_ingestion import ResumeIngestionService
from app.services.appointment_reminders import AppointmentReminderService
from app.services.interview_agent import InterviewAgentRuntime


class OutboxWorker:
    """Dispatches durable work; the database remains the source of truth."""

    def __init__(self, store: InMemoryStore, *, persistence: Optional[Persistence] = None) -> None:
        self.store = store
        self.persistence = persistence or persistence_for(store)
        self.catalog = CatalogService(store, persistence=self.persistence)
        self.knowledge_base_speech = KnowledgeBaseSpeechService(store, persistence=self.persistence)
        self.question_generation = QuestionGenerationService(store, persistence=self.persistence)
        self.talent = TalentService(store, persistence=self.persistence, catalog=self.catalog)
        self.resume_ingestion = ResumeIngestionService(store, persistence=self.persistence)
        self.interviews = InterviewService(store, persistence=self.persistence)
        self.agent_runtime = InterviewAgentRuntime(store)
        self.appointment_reminders = AppointmentReminderService(store, persistence=self.persistence)

    async def run_once(
        self,
        organization_id: str = "org_default",
        *,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            items = transaction.outbox.claimable(limit)

        results: List[Dict[str, Any]] = []
        for item in items:
            try:
                await self.run_item(item["id"], organization_id)
            except ConcurrencyConflict:
                continue
            except Exception as exc:
                results.append({"id": item["id"], "kind": item["kind"], "status": "failed", "error": str(exc)})
                continue

            with self.persistence.transaction(organization_id) as transaction:
                current = transaction.outbox.get(item["id"])
            results.append(
                {
                    "id": item["id"],
                    "kind": item["kind"],
                    "status": current["status"] if current else "missing",
                }
            )
        return results

    async def run_item(
        self, work_item_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.outbox.get(work_item_id)
        if item is None:
            raise KeyError("Work item does not exist: %s" % work_item_id)
        if item["kind"] == "question.speech.generate":
            await self.catalog.process_speech_work(item["id"], organization_id)
        elif item["kind"] in {"question_generation.plan", "question_generation.generate"}:
            await self.question_generation.process_plan_work(item["id"], organization_id)
        elif item["kind"] == "question_generation.generate_chunk":
            await self.question_generation.process_chunk_work(item["id"], organization_id)
        elif item["kind"] == "question_generation.merge":
            self.question_generation.process_merge_work(item["id"], organization_id)
        elif item["kind"] == "knowledge_base.speech.rebuild":
            self.knowledge_base_speech.process_build_work(item["id"], organization_id)
        elif item["kind"] == "resume.review":
            await self.talent.process_review_work(item["id"], organization_id)
        elif item["kind"] == "resume.experience_questions.generate":
            await self.talent.process_experience_question_generation_work(item["id"], organization_id)
        elif item["kind"] == "resume.ingest":
            await self.resume_ingestion.process(item["id"], organization_id)
        elif item["kind"] in {"knowledge_base.import", "knowledge_base.rebuild"}:
            await self.catalog.process_build_work(item["id"], organization_id)
        elif item["kind"] == "interview.media.finalize":
            await InterviewMediaCaptureService(self.store, persistence=self.persistence).process_finalization_work(item["id"], organization_id)
        elif item["kind"] in {"answer.evaluate", "interview.report.generate"}:
            result = await self.interviews.process_outbox_work(item["id"], organization_id)
            await self._publish_interview_work(item, result)
        elif item["kind"] == "appointment.reminder.email":
            self.appointment_reminders.process_work_item(item["id"], organization_id)
        else:
            self._fail_unsupported(item, organization_id)
        with self.persistence.transaction(organization_id) as transaction:
            return transaction.outbox.get(work_item_id) or {"id": work_item_id, "status": "missing"}

    async def _publish_interview_work(
        self,
        item: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        interview_id = str((item.get("payload") or {}).get("interview_id") or item.get("aggregate_id") or "")
        if not interview_id:
            return
        # Evaluation details remain an enterprise REST projection. Realtime
        # clients receive only the stable, role-projected Agent snapshot.
        await self.agent_runtime.publish_snapshot(
            interview_id,
            item.get("organization_id", "org_default"),
        )

    async def run_forever(
        self,
        organization_id: str = "org_default",
        *,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        while True:
            await self.run_once(organization_id)
            await asyncio.sleep(max(0.1, poll_interval_seconds))

    def _fail_unsupported(self, item: Dict[str, Any], organization_id: str) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            running = transaction.outbox.start(item["id"])
            transaction.outbox.fail(
                item["id"],
                "Unsupported outbox work kind: %s" % item["kind"],
                lease_token=running["lease_token"],
            )


def main() -> None:
    organization_id = os.getenv("INTERVIEWER_ORGANIZATION_ID", "org_default")
    poll_interval = float(os.getenv("INTERVIEWER_OUTBOX_POLL_SECONDS", "1"))
    asyncio.run(
        OutboxWorker(get_store()).run_forever(
            organization_id,
            poll_interval_seconds=poll_interval,
        )
    )


if __name__ == "__main__":
    main()
