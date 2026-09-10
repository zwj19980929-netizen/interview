"""Regression for submitted interviews whose scoring and recording never settled."""
from copy import deepcopy
import hashlib
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.errors import ApiError
from app.core.prompt.contracts import prompt_contract
from app.core.prompt.validation import validate_structured_response, StructuredResponseValidationError
from app.file_storage.local import LocalPrivateFileAdapter
from app.model_gateway.errors import ProviderError
from app.persistence.errors import ConcurrencyConflict
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.evaluation import EvaluationService
from app.services.interviews import InterviewService
from app.services.media_capture import InterviewMediaCaptureService
from app.services.review import EnterpriseReviewService
from app.transport.http.media import private_media_response
from test_declined_answer_evaluation import ORG, NOW, ScoringGateway, session_for, question, persist


def pending_session(store):
    session = session_for(["answer", "answer"])
    session.update(status="in_progress", current_turn_id=None, candidate_input_completed_at=NOW,
                   candidate={"name": "Synthetic"}, lifecycle_events=[], created_at=NOW, updated_at=NOW)
    for turn in session["turns"]:
        turn.update(status="evaluating", started_at=NOW)
    return session, persist(store, session)


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_failed_scoring_replay_is_idempotent_and_generates_report(backend, tmp_path):
    store = InMemoryStore() if backend == "memory" else SQLiteStore(str(tmp_path / "isolated.sqlite3"))
    session, persistence = pending_session(store)
    service = InterviewService(store, persistence=persistence)
    original = deepcopy(session["answers"])
    service.retry_processing(session["id"], ORG)
    service.retry_processing(session["id"], ORG)
    with persistence.transaction(ORG) as tx:
        works = tx.outbox.list()
    assert len(works) == 2
    async def fail(*args):
        raise ProviderError("provider_output_truncated", "sensitive fake response must not be stored", retryable=False)
    service.evaluation.evaluate_answer = fail
    for work in works:
        with pytest.raises(ProviderError):
            await service.process_outbox_work(work["id"], ORG)
    with persistence.transaction(ORG) as tx:
        failed = tx.outbox.list()
    assert all(w["status"] == "dead_letter" and w["attempt_count"] == 1 for w in failed)
    assert all(w["last_error_code"] == "provider_output_truncated" for w in failed)
    assert all("sensitive" not in w["last_error"] for w in failed)
    projection = EnterpriseReviewService(store, persistence=persistence).get_review(session["id"], ORG)
    assert projection["processing"]["failed"] == 2
    assert projection["processing"]["can_retry"]
    service.retry_processing(session["id"], ORG)
    service.retry_processing(session["id"], ORG)
    service.evaluation = EvaluationService(store, persistence=persistence, gateway=ScoringGateway())
    for work in works:
        await service.process_outbox_work(work["id"], ORG)
    result = service.get_interview(session["id"], ORG)
    assert result["status"] == "report_ready" and result["current_report_id"]
    assert len(result["report_revisions"]) == 1 and len(result["evaluation_revisions"]) == 2
    assert [a["final_transcript"] for a in result["answers"]] == [a["final_transcript"] for a in original]
    assert [a["audio_uri"] for a in result["answers"]] == [a["audio_uri"] for a in original]
    with persistence.transaction(ORG) as tx:
        assert len([w for w in tx.outbox.list() if w["kind"] == "answer.evaluate"]) == 2
    with pytest.raises(ConcurrencyConflict):
        await service.process_outbox_work(works[0]["id"], ORG)


@pytest.mark.anyio
@pytest.mark.parametrize("recover", [True, False])
async def test_output_budget_grows_only_once_and_keypoints_reach_prompt(recover):
    store = InMemoryStore()
    session, persistence = pending_session(store)
    gateway = ScoringGateway()
    response = await gateway.invoke("llm.chat_json", type("Request", (), {"metadata": {"answer_text": "Outbox"}})())
    calls = []
    async def invoke(capability, request):
        calls.append(request)
        if len(calls) == 1 or not recover:
            raise ProviderError("provider_output_truncated", "truncated", retryable=False)
        return response
    gateway.invoke = invoke
    service = EvaluationService(store, persistence=persistence, gateway=gateway)
    if recover:
        assert (await service.evaluate_answer(session["answers"][0], question(0)))["score"] == 85
    else:
        with pytest.raises(ProviderError):
            await service.evaluate_answer(session["answers"][0], question(0))
    assert [item.max_output_tokens for item in calls] == [8000, 16000]
    assert "kp_outbox" in calls[0].messages[-1].content
    assert calls[0].metadata["prompt_version"] == "answer_evaluation.v5"


def capture_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERVIEWER_LOCAL_MEDIA", "true")
    store = InMemoryStore()
    session, persistence = pending_session(store)
    capture = {"id": "capture_synthetic", "organization_id": ORG, "interview_id": session["id"],
        "status": "stopping", "provider": "livekit", "egress_id": "egress_synthetic", "room_name": "room_synthetic",
        "participant_identity": "candidate_synthetic", "object_key": "synthetic.mp4", "started_at": NOW,
        "consented_scopes": ["audio_recording", "video_recording"], "requested_scopes": ["audio_recording", "video_recording"]}
    with persistence.transaction(ORG) as tx:
        tx.interview_media_captures.add(capture)
    storage = LocalPrivateFileAdapter(tmp_path)
    info = {"egress_id": capture["egress_id"], "room_name": capture["room_name"], "status": "EGRESS_ENDING",
            "participant": {"identity": capture["participant_identity"]}, "file_results": []}
    plane = type("Plane", (), {})()
    plane.list_egress = AsyncMock(return_value={"items": [info]})
    service = InterviewMediaCaptureService(store, persistence=persistence, media_plane=plane, storage=storage)
    return store, session, capture, info, service, storage


@pytest.mark.anyio
async def test_ending_cannot_verify_partial_file_and_worker_reconciles_without_webhook(tmp_path, monkeypatch):
    store, session, capture, info, service, storage = capture_fixture(tmp_path, monkeypatch)
    (tmp_path / capture["object_key"]).write_bytes(b"partial")
    pending = await service.finalize_provider_result(capture["id"], info, actor_id="synthetic", organization_id=ORG)
    assert pending["status"] == "hash_pending" and not pending["content_hash"]
    work = service.request_finalization(session["id"], actor_id="synthetic", organization_id=ORG)
    assert service.request_finalization(session["id"], actor_id="synthetic", organization_id=ORG)["id"] == work["id"]
    info["status"] = "EGRESS_COMPLETE"
    content = b"complete-video-with-audio" * 100
    (tmp_path / capture["object_key"]).write_bytes(content)
    done = await service.process_finalization_work(work["id"], ORG)
    assert done["content_hash"] == "sha256:" + hashlib.sha256(content).hexdigest()
    assert done["status"] == "completed" and done["byte_count"] == len(content)
    # Late stop response must not downgrade a successfully finalized object.
    late = await service.finalize_provider_result(capture["id"], {"status": "EGRESS_ENDING"}, actor_id="synthetic", organization_id=ORG)
    assert late["status"] == "completed"
    review = EnterpriseReviewService(store, persistence=service.persistence, storage=storage)
    grant = review.recording_url(session["id"], ORG)
    token = grant["url"].rsplit("/", 1)[1]
    opened = review.open_playback_grant(token)
    assert "content" not in opened  # Never buffer the entire video.
    app = FastAPI()
    @app.api_route("/media", methods=["GET", "HEAD"])
    async def media(request: Request):
        return private_media_response(opened, request)
    api = TestClient(app)
    response = api.get("/media", headers={"Range": "bytes=5-24"})
    assert response.status_code == 206 and response.content == content[5:25]
    assert response.headers["content-range"] == "bytes 5-24/%s" % len(content)
    head = api.head("/media", headers={"Range": "bytes=-4"})
    assert head.status_code == 206 and not head.content and head.headers["content-length"] == "4"
    assert api.get("/media", headers={"Range": "bytes=0-1,5-6"}).status_code == 416
    assert api.get("/media").content == content
    with service.persistence.transaction(ORG) as tx:
        current = tx.interview_media_captures.get(capture["id"])
        current.update(status="retention_purged")
        tx.interview_media_captures.update(current, expected_version=current["version"])
    with pytest.raises(ApiError):
        review.open_playback_grant(token)
    with pytest.raises(ApiError):
        review.recording_url(session["id"], "another_tenant")


@pytest.mark.parametrize("mutation", ["extra", "long", "wrong_type", "unknown_dimension"])
def test_evaluation_nested_response_contract(mutation):
    schema = prompt_contract("answer_evaluation", {"question_text": "Q", "standard_answer": "A", "answer_text": "Answer"}).response_schema
    data = {"score": 50, "confidence": .8, "dimension_scores": {"communication": 50}, "covered_key_points": [],
            "missing_key_points": [{"key_point_id": "kp", "reason": "Missing"}], "incorrect_claims": [],
            "evidence": [], "review_flags": [], "summary": "Summary"}
    validate_structured_response(data, schema)
    if mutation == "extra": data["missing_key_points"][0]["secret"] = "bad"
    if mutation == "long": data["summary"] = "x" * 601
    if mutation == "wrong_type": data["evidence"] = [99]
    if mutation == "unknown_dimension": data["dimension_scores"] = {"age": 50}
    with pytest.raises(StructuredResponseValidationError):
        validate_structured_response(data, schema)
