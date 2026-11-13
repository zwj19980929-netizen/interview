"""Admin explanations preserve interview history and playback trust checks."""
from copy import deepcopy

import pytest

from app.domain.interview_completion import completion_projection, presented_question_counts
from app.domain.interview_lifecycle import LifecycleCommand, LifecycleCommandType as Kind
from app.services.review import EnterpriseReviewService
from tests.test_adaptive_interview import LIFECYCLE, NOW, make_session, select, submit, end
from test_interview_processing_recovery import capture_fixture, ORG


def test_time_limit_is_recorded_in_ending_transaction_and_replays_unchanged():
    session = make_session()
    command = LifecycleCommand(Kind.END_CANDIDATE_INPUT, {
        "decision_id": "deadline", "expected_decision_revision": session["decision_revision"],
        "reason": "budget_exhausted"})
    ended = LIFECYCLE.execute(session, command, now="2026-09-10T10:10:51Z").session
    assert completion_projection(ended) == {"trigger": "time_limit", "limit_minutes": 10, "elapsed_seconds": 651}
    assert "candidate_input_completion_detail" not in session
    replay = LIFECYCLE.execute(ended, command, now="2026-09-10T11:00:00Z").session
    assert replay["candidate_input_completion_detail"] == ended["candidate_input_completion_detail"]


def test_last_question_records_question_limit_without_counting_followups():
    session = make_session(maximum=2, minimum=2)
    for question in ["q1", "q2"]:
        session = submit(select(session, question).session).session
    session["turns"].append({**deepcopy(session["turns"][0]), "id": "followup", "is_followup": True})
    ended = end(session, "budget_exhausted").session
    assert completion_projection(ended) == {"trigger": "primary_question_limit", "primary_question_limit": 2}
    assert presented_question_counts(ended) == {"primary": 2, "followup": 1}


def test_legacy_end_uses_end_timestamp_never_now_and_does_not_modify_history():
    session = make_session()
    session.update(status="report_ready", candidate_input_completion_reason="budget_exhausted",
                   candidate_input_completed_at="2026-09-10T10:10:51Z")
    before = deepcopy(session)
    assert completion_projection(session)["trigger"] == "time_limit"
    assert session == before
    session["candidate_input_completed_at"] = NOW
    assert completion_projection(session) is None
    session["candidate_input_completion_reason"] = "candidate_requested"
    assert completion_projection(session) is None


def test_unpresented_pending_and_budget_skips_are_not_questions():
    session = {"turns": [{"id": "p", "status": "pending", "started_at": NOW},
                         {"id": "s", "status": "skipped", "skip_reason": "primary_question_budget_exhausted"},
                         {"id": "a", "status": "skipped", "started_at": NOW}]}
    assert presented_question_counts(session) == {"primary": 1, "followup": 0}


def test_earlier_appointment_does_not_claim_full_contract_duration_elapsed():
    session = make_session()
    session.update(scheduled_end_at="2026-09-10T10:05:00Z", candidate_input_completed_at="2026-09-10T10:04:50Z",
                   candidate_input_completion_reason="budget_exhausted")
    assert completion_projection(session) == {"trigger": "time_limit", "limit_minutes": 5, "elapsed_seconds": 290}


@pytest.mark.anyio
async def test_review_shows_config_failure_instead_of_stopping_and_keeps_playback_closed(tmp_path, monkeypatch):
    fixture = capture_fixture(tmp_path, monkeypatch)
    store, session, capture, _, service, _ = fixture
    work = service.request_finalization(session["id"], actor_id="test", organization_id=ORG)
    from app.core.errors import ApiError
    service.media_plane.list_egress.side_effect = ApiError("LIVEKIT_RECORDING_NOT_READY", "secret")
    with pytest.raises(ApiError):
        await service.process_finalization_work(work["id"], ORG)
    review_service = EnterpriseReviewService(store, persistence=service.persistence)
    result = review_service.get_review(session["id"], ORG)
    recording = result["recording"]
    assert recording["status"] == "stopping"
    assert recording["processing_status"] == "failed"
    assert recording["failure_reason"] == "configuration"
    assert not recording["available"] and not recording["can_retry"]
    with pytest.raises(ApiError):
        review_service.recording_url(session["id"], ORG)
    assert service.interviews.get_interview(session["id"], ORG)["answers"] == session["answers"]


def test_historical_dead_letter_does_not_look_like_normal_finalizing():
    capture = {"id": "capture", "status": "stopping"}
    work = {"kind": "interview.media.finalize", "payload": {"capture_id": "capture"},
            "status": "dead_letter", "last_error_code": "recording_finalization_pending",
            "attempt_count": 8, "max_attempts": 8}
    result = EnterpriseReviewService._recording_projection(capture, [work])
    assert result["failure_reason"] == "retry_exhausted"
    assert result["processing_status"] == "failed" and result["can_retry"]
    work["status"] = "pending"
    assert EnterpriseReviewService._recording_projection(capture, [work])["processing_status"] == "processing"
    work["payload"]["capture_id"] = "foreign"
    assert EnterpriseReviewService._recording_projection(capture, [work])["processing_status"] == "processing"


def test_automatic_backoff_and_permanent_failure_are_not_exhausted_retries():
    capture = {"id": "capture", "status": "stopping",
               "finalization_error": {"code": "recording_finalization_pending", "category": "pending", "retryable": True}}
    work = {"kind": "interview.media.finalize", "payload": {"capture_id": "capture"}, "status": "failed",
            "error_retryable": True, "attempt_count": 1, "max_attempts": 8}
    result = EnterpriseReviewService._recording_projection(capture, [work])
    assert result["processing_status"] == "processing" and not result["can_retry"]
    work.update(status="dead_letter", error_retryable=False)
    capture["finalization_error"] = {"code": "recording_finalization_failed", "category": "unknown", "retryable": False}
    result = EnterpriseReviewService._recording_projection(capture, [work])
    assert result["processing_status"] == "failed" and result["failure_reason"] == "unknown"


def test_stale_failure_cannot_override_verified_recording_or_expose_unknown_error():
    capture = {"id": "capture", "status": "completed", "content_hash": "sha256:actual",
               "byte_count": 100, "storage_protection": "local_private_development",
               "finalization_error": {"code": "SECRET /path", "category": "unknown", "retryable": False}}
    result = EnterpriseReviewService._recording_projection(capture)
    assert result["available"] and result["processing_status"] == "completed"
    assert result["failure_reason"] is None and not result["can_retry"]
    assert "SECRET" not in str(result)
    capture["storage_protection"] = None
    result = EnterpriseReviewService._recording_projection(capture)
    assert not result["available"] and result["processing_status"] == "failed"


def test_report_export_preserves_frozen_scope_without_changing_saved_report():
    import csv
    import json
    from io import StringIO
    from app.repositories.memory import InMemoryStore
    from app.services.reports import ReportService
    from tests.test_session_assessment_scope_090 import contract_for_pool, planned_pairs, session_with, choose
    from tests.test_inquiry_unit_assessment import submit_unit
    from tests.test_adaptive_interview import scored
    contract = contract_for_pool()
    session = session_with(contract)
    for candidate, unit in planned_pairs(contract):
        session = scored(submit_unit(choose(session, candidate, unit).session), 80).session
    session = end(session).session
    service = ReportService(InMemoryStore())
    report = service.build_report(session, trigger_reason="synthetic")
    session.update(report_revisions=[report], current_report_id=report["id"], status="report_ready")
    with service.persistence.transaction("org_default") as tx:
        tx.interview_sessions.add(session)
    for format in ("json", "csv"):
        content = service.export_report(session["id"], export_format=format, actor_id="synthetic")["content"]
        row = json.loads(content) if format == "json" else next(csv.DictReader(StringIO(content)))
        assert row["score_scope"] == "session_assessment"
        scope = row["assessment_scope"] if format == "json" else json.loads(row["assessment_scope"])
        assert scope == contract["assessment_scope"]
        assert int(row["overall_score"]) == 80
    with service.persistence.transaction("org_default") as tx:
        assert tx.interview_sessions.get(session["id"])["report_revisions"] == [report]
