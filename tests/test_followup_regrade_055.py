from copy import deepcopy

import pytest

from app.domain.evaluation_evidence import evidence_fingerprint
from app.domain.interview_lifecycle import InterviewSessionLifecycle, LifecycleCommand, LifecycleCommandType
from test_followup_review_report_055 import _scored_chain


def test_corrected_completed_child_requeues_root_once_with_new_evidence_identity():
    session = _scored_chain()
    root, child = session["answers"][:2]
    original_fingerprint = evidence_fingerprint(session["answers"])
    root["current_evidence_fingerprint"] = original_fingerprint
    original_root_eval = root["current_evaluation_id"]
    child.update(final_transcript="更正后的独立子回答", current_transcript_revision=2)
    lifecycle = InterviewSessionLifecycle()
    requested = lifecycle.execute(session, LifecycleCommand(LifecycleCommandType.REGRADE_REQUESTED,
                                   {"answer_id": child["id"]}))
    evaluation = deepcopy(session["evaluation_revisions"][1])
    evaluation["id"] = "child_regraded"
    completed = lifecycle.execute(requested.session, LifecycleCommand(LifecycleCommandType.EVALUATION_SUCCEEDED,
                                   {"answer_id": child["id"], "evaluation": evaluation, "revision": 2}))
    assert len(completed.effects) == 1
    effect = completed.effects[0]
    assert effect["type"] == "evaluation.requested"
    assert effect["payload"]["answer_id"] == root["id"]
    assert effect["payload"]["evidence_answer_ids"] == [item["id"] for item in session["answers"]]
    assert effect["payload"]["evidence_fingerprint"] != original_fingerprint
    updated_root = completed.session["answers"][0]
    assert updated_root["evaluation_status"] == "pending"
    assert updated_root["current_evaluation_id"] == original_root_eval
    events, effects = [], []
    lifecycle._maybe_request_evidence_group_evaluation(completed.session, "turn_root", "2026-09-17T00:00:00Z", events, effects)
    assert events == effects == []
    root_eval = deepcopy(session["evaluation_revisions"][0])
    root_eval.update(id="root_regraded", evidence_fingerprint=effect["payload"]["evidence_fingerprint"])
    settled = lifecycle.execute(completed.session, LifecycleCommand(LifecycleCommandType.EVALUATION_SUCCEEDED,
                               {"answer_id": root["id"], "evaluation": root_eval, "revision": 2}))
    assert settled.session["answers"][0]["current_evidence_fingerprint"] == effect["payload"]["evidence_fingerprint"]


def test_root_merge_waits_for_pending_child_regrade():
    session = _scored_chain()
    session["answers"][1]["evaluation_status"] = "pending"
    events, effects = [], []
    InterviewSessionLifecycle()._maybe_request_evidence_group_evaluation(
        session, "turn_root", "2026-09-17T00:00:00Z", events, effects)
    assert events == effects == []


def test_evidence_identity_excludes_score_but_includes_order_and_text_revision():
    answers = _scored_chain()["answers"]
    before = evidence_fingerprint(answers)
    changed = deepcopy(answers)
    changed[1]["current_evaluation_id"] = "another_score"
    assert evidence_fingerprint(changed) == before
    changed[1]["current_transcript_revision"] = 2
    assert evidence_fingerprint(changed) != before
    assert evidence_fingerprint(list(reversed(answers))) != before


@pytest.mark.anyio
async def test_actual_child_correction_regrades_child_then_parent_with_latest_revision():
    from app.services.review import EnterpriseReviewService
    from app.services.interviews import InterviewService
    from app.services.evaluation import EvaluationService
    from test_followup_evaluation_055 import fixture, Gateway
    session, store, _ = fixture()
    service = InterviewService(store)
    gateway = Gateway()
    service.evaluation = EvaluationService(store, gateway=gateway)
    review = EnterpriseReviewService(store)
    review.interviews = service
    outcome = await review.correct_transcript(session["id"], "answer_child_one",
        {"final_transcript": "Outbox 与业务数据原子提交。", "reviewer_id": "synthetic_reviewer", "reason": "合成回听更正"})
    assert outcome["transcript_revision"] == 2
    with service.persistence.transaction("org_default") as tx:
        saved = tx.interview_sessions.get(session["id"])
    child = next(a for a in saved["answers"] if a["id"] == "answer_child_one")
    root = saved["answers"][0]
    assert child["evaluation_status"] == root["evaluation_status"] == "completed"
    assert len(saved["evaluation_revisions"]) == 5
    assert [r.metadata["prompt_version"] for r in gateway.requests] == ["answer_evaluation.v5", "answer_evaluation.v8"]
    root_eval = next(e for e in saved["evaluation_revisions"] if e["id"] == root["current_evaluation_id"])
    assert root_eval["evidence_fingerprint"] == evidence_fingerprint(saved["answers"])
    assert any(s["answer_id"] == child["id"] and s["transcript_revision"] == 2 for s in root_eval["evidence_sources"])
