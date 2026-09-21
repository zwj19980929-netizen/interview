"""A provider response cannot score text that changed while it was computing."""
import pytest

from app.repositories.memory import InMemoryStore
from app.services.interviews import InterviewService
from test_interview_processing_recovery import pending_session, ORG


@pytest.mark.anyio
async def test_changed_answer_during_scoring_is_observable_retry_not_stale_commit():
    store = InMemoryStore()
    session, persistence = pending_session(store)
    service = InterviewService(store, persistence=persistence)
    service.retry_processing(session["id"], ORG)
    with persistence.transaction(ORG) as tx:
        work = tx.outbox.list()[0]
        initial = tx.interview_sessions.get(session["id"])
        initial_ids = [item["id"] for item in initial.get("evaluation_revisions", [])]

    async def changed_during_inference(answer, *_):
        # Deliberately simulate a repair/concurrent version change outside the
        # ordinary review API, which itself already rejects in-flight edits.
        with persistence.transaction(ORG) as tx:
            current = tx.interview_sessions.get(session["id"])
            stored = next(item for item in current["answers"] if item["id"] == answer["id"])
            stored["final_transcript"] += " 新的合成版本。"
            stored["current_transcript_revision"] = int(stored.get("current_transcript_revision", 1)) + 1
            tx.interview_sessions.update(current, expected_version=current["version"])
        return {"id": "must_not_commit", "answer_id": answer["id"]}

    service.evaluation.evaluate_answer = changed_during_inference
    result = await service.process_outbox_work(work["id"], ORG)
    assert result["status"] == "superseded"
    with persistence.transaction(ORG) as tx:
        current = tx.interview_sessions.get(session["id"])
        retry = tx.outbox.get(work["id"])
    assert [item["id"] for item in current.get("evaluation_revisions", [])] == initial_ids
    assert retry["status"] == "failed"
    assert retry["last_error_code"] == "evaluation_evidence_changed"
    assert retry["error_retryable"] is True
    assert retry.get("available_at")
