"""Unintelligible technical speech yields review evidence, never invented scores."""
from copy import deepcopy

import pytest

from app.core.errors import ApiError
from app.domain.scoring_quality import project_evaluation
from app.domain.speech_quality import transcript_identity, transcript_is_verified
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.evaluation import EvaluationService
from app.services.interviews import InterviewService
from app.services.reports import ReportService
from test_declined_answer_evaluation import (
    ORG, NOW, TECHNICAL, ScoringGateway, persist, question, session_for,
)


def unresolved_session(*, technical=False, followup=False):
    session = session_for(["answer", "answer"] if followup else ["answer"], followups=followup)
    index = 1 if followup else 0
    turn, answer = session["turns"][index], session["answers"][index]
    fragment = "然后要做蓝桥。"
    finish = "我的介绍完毕。我已经说完了。"
    text = (TECHNICAL if technical else "") + fragment + finish
    raw = turn["current_understanding"]
    raw.update(intent="answer_unresolved", prompt_version="interview_turn_understanding.v50",
               answer_content="partial" if technical else "none", turn_intent="finish_answer",
               completion_basis={"source": "explicit_server_intent", "evidence_quotes": ["我已经说完了。"]},
               answer_summary="候选人结束本题，部分技术转写待核实。", followup_allowed=False,
               ambiguities=["蓝桥含义无法核实。"], clarification_target={"evidence_quote": fragment, "focus_quote": "蓝桥"},
               evidence_quotes=([TECHNICAL] if technical else []) + [fragment, "我已经说完了。"],
               claims=[{"claim": TECHNICAL, "evidence_quote": TECHNICAL}] if technical else [],
               covered_capability_points=["幂等键", "Outbox"] if technical else [],
               missing_capability_points=[] if technical else ["幂等键", "Outbox"])
    turn["utterances"][0]["text"] = text
    answer.update(final_transcript=text, raw_transcript=text, current_transcript_revision=1)
    return session


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("technical", [False, True])
async def test_persisted_unresolved_answer_is_unscored_without_llm_and_retains_original_evidence(backend, technical, tmp_path):
    store = InMemoryStore() if backend == "memory" else SQLiteStore(str(tmp_path / "unresolved.sqlite3"))
    session = unresolved_session(technical=technical)
    before = deepcopy(session)
    persistence = persist(store, session)
    gateway = ScoringGateway()
    evaluation = await EvaluationService(store, persistence=persistence, gateway=gateway).evaluate_answer(session["answers"][0], question(0))
    assert evaluation["score"] is None and evaluation["dimension_scores"] == {}
    assert evaluation["score_status"] == "unavailable" and evaluation["manual_review_required"]
    assert evaluation["covered_key_points"] == evaluation["missing_key_points"] == []
    assert evaluation["model_info"]["scoring_rule_version"] == "unresolved_answer.v1"
    assert evaluation["model_info"]["unresolved_answers"][0]["answer_id"] == "answer_0"
    assert not gateway.inputs and session == before
    assert bool(session["turns"][0]["current_understanding"]["claims"]) is technical


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", ["client_tag", "foreign_tenant", "understanding_id", "utterance_id", "revised_text", "not_authoritative", "audio_mismatch", "old_understanding"])
async def test_untrusted_or_unbound_labels_cannot_defer_an_ordinary_score(invalid):
    session = session_for(["answer"]) if invalid == "client_tag" else unresolved_session()
    answer = deepcopy(session["answers"][0])
    answer["intent"] = "answer_unresolved"
    if invalid == "foreign_tenant":
        answer["organization_id"] = "another_tenant"
    elif invalid in {"understanding_id", "utterance_id"}:
        answer[invalid] = "foreign_id"
    elif invalid == "revised_text":
        answer["final_transcript"] = TECHNICAL
    elif invalid == "not_authoritative":
        session["turns"][0]["utterances"][0]["authoritative"] = False
    elif invalid == "audio_mismatch":
        session["turns"][0]["utterances"][0]["audio_uri"] = "private-test://other.wav"
    elif invalid == "old_understanding":
        session["turns"][0]["current_understanding"]["understanding_id"] = "old_understanding"
    store, gateway = InMemoryStore(), ScoringGateway()
    persistence = persist(store, session)
    result = await EvaluationService(store, persistence=persistence, gateway=gateway).evaluate_answer(answer, question(0))
    assert result["score"] == 85 and gateway.inputs == [answer["final_transcript"]]
    assert "scoring_rule_version" not in result["model_info"]


@pytest.mark.anyio
async def test_unresolved_followup_prevents_combined_score_from_guessing_missing_speech():
    session = unresolved_session(followup=True)
    store, gateway = InMemoryStore(), ScoringGateway()
    persistence = persist(store, session)
    merged = InterviewService(store, persistence=persistence)._merged_authoritative_answer(
        session, root_answer_id="answer_0", evidence_answer_ids=["answer_0", "answer_1"])
    result = await EvaluationService(store, persistence=persistence, gateway=gateway).evaluate_answer(merged, question(0))
    assert result["score"] is None and not gateway.inputs
    assert result["model_info"]["unresolved_answers"][0]["answer_id"] == "answer_1"
    assert TECHNICAL in merged["final_transcript"] and "蓝桥" in merged["final_transcript"]


@pytest.mark.anyio
async def test_unresolved_evaluation_generates_manual_review_report_with_null_total_and_export():
    session = unresolved_session(technical=True)
    store, gateway = InMemoryStore(), ScoringGateway()
    persistence = persist(store, session)
    result = await EvaluationService(store, persistence=persistence, gateway=gateway).evaluate_answer(session["answers"][0], question(0))
    result["revision"] = 1
    session["evaluation_revisions"] = [result]
    session["answers"][0].update(current_evaluation_id=result["id"], evaluation_status="completed")
    reports = ReportService(store, persistence=persistence)
    report = reports.build_report(session, trigger_reason="final_answer_evaluated")
    assert report["overall_score"] is None and report["dimension_scores"] == []
    assert report["job_fit_level"] == report["recommendation"] == "manual_review"
    assert report["pending_verification_answer_ids"] == ["answer_0"]
    assert report["question_evaluations"][0]["score"] is None
    assert report["human_decision_required"] and "暂不计分" in report["recognition_notice"]
    report["revision"] = 1
    session.update(report_revisions=[report], current_report_id=report["id"])
    persist(store, session)
    for kind in ("csv", "json"):
        exported = reports.export_report(session["id"], export_format=kind, actor_id="reviewer", organization_id=ORG)
        assert "manual_review" in exported["content"]


@pytest.mark.anyio
async def test_human_verified_revision_can_score_and_old_revision_stays_unscored():
    session = unresolved_session()
    store, gateway = InMemoryStore(), ScoringGateway()
    persistence = persist(store, session)
    service = EvaluationService(store, persistence=persistence, gateway=gateway)
    first = await service.evaluate_answer(session["answers"][0], question(0))
    answer = session["answers"][0]
    answer.update(final_transcript=TECHNICAL, current_transcript_revision=2, transcript_source="human_correction")
    answer["transcript_revisions"] = [{"revision": 2, "source": "human_correction", "reviewer_id": "reviewer", "text": TECHNICAL}]
    answer["transcription_verification"] = {"audio_reviewed": True, "reviewer_id": "reviewer", "verified_at": NOW,
                                             "transcript_identity": transcript_identity(answer)}
    assert transcript_is_verified(answer)
    persist(store, session)
    result = await service.evaluate_answer(answer, question(0))
    assert result["score"] == 85 and gateway.inputs == [TECHNICAL]
    old = project_evaluation({**first, "provisional_score": 90}, [answer])
    assert old["score"] is None and old["manual_review_required"]
    assert result["transcription_verifications"]["answer_0"] == transcript_identity(answer)


@pytest.mark.anyio
async def test_verification_for_another_revision_does_not_remove_the_review_gate():
    session = unresolved_session()
    session["answers"][0]["transcription_verification"] = {"audio_reviewed": True, "reviewer_id": "reviewer", "verified_at": NOW,
                                                           "transcript_identity": "stale"}
    store, gateway = InMemoryStore(), ScoringGateway()
    persist(store, session)
    result = await EvaluationService(store, gateway=gateway).evaluate_answer(session["answers"][0], question(0))
    assert result["score"] is None and not gateway.inputs


def test_unrelated_broken_evaluation_still_cannot_build_report():
    session = session_for(["answer"])
    session["answers"][0].update(current_evaluation_id="invalid_eval", evaluation_status="completed")
    session["evaluation_revisions"] = [{"id": "invalid_eval", "answer_id": "answer_0", "score": None,
        "confidence": .8, "dimension_scores": {}, "model_info": {}, "review_flags": []}]
    with pytest.raises(ApiError) as caught:
        ReportService(InMemoryStore()).build_report(session, trigger_reason="synthetic_invalid")
    assert caught.value.code == "REPORT_EVALUATION_UNAVAILABLE"


@pytest.mark.anyio
async def test_worker_finishes_unresolved_evaluation_and_report_then_real_review_requeues_scoring():
    store, gateway = InMemoryStore(), ScoringGateway()
    session = unresolved_session()
    session.update(status="in_progress", current_turn_id=None, candidate_input_completed_at=NOW,
                   candidate={"name": "Synthetic"}, lifecycle_events=[], created_at=NOW, updated_at=NOW)
    session["turns"][0].update(status="evaluating", started_at=NOW)
    persistence = persist(store, session)
    service = InterviewService(store, persistence=persistence)
    service.evaluation = EvaluationService(store, persistence=persistence, gateway=gateway)
    service.retry_processing(session["id"], ORG)
    with persistence.transaction(ORG) as transaction:
        works = transaction.outbox.list()
    for work in works:
        await service.process_outbox_work(work["id"], ORG)
    current = service.get_interview(session["id"], ORG)
    assert current["status"] == "report_ready" and current["answers"][0]["evaluation_status"] == "completed"
    first_report = service.reports.get_report(session["id"], ORG)
    assert first_report["overall_score"] is None and first_report["recommendation"] == "manual_review"
    assert not gateway.inputs
    from app.services.review import EnterpriseReviewService
    review = EnterpriseReviewService(store, persistence=persistence)
    with pytest.raises(ApiError) as caught:
        review.complete_review(session["id"], {}, ORG)
    assert caught.value.code == "REVIEW_EVIDENCE_UNRESOLVED"
    assert "回听录音" in caught.value.message and "核验转写" in caught.value.message and "重评" in caught.value.message
    answer = current["answers"][0]
    old_evaluation_id = answer["current_evaluation_id"]
    service.verify_transcription(session["id"], answer["id"], {
        "expected_evaluation_id": old_evaluation_id, "expected_transcript_revision": 1,
        "audio_reviewed": True, "reason": "已回听并核实实际术语", "final_transcript": TECHNICAL,
    }, ORG, actor_id="reviewer")
    with persistence.transaction(ORG) as transaction:
        queued = [work for work in transaction.outbox.list() if work["kind"] == "answer.evaluate" and work["status"] == "pending"]
    assert len(queued) == 1
    await service.process_outbox_work(queued[0]["id"], ORG)
    final = service.get_interview(session["id"], ORG)
    assert service.reports.get_report(session["id"], ORG)["overall_score"] == 85
    assert gateway.inputs == [TECHNICAL] and len(final["report_revisions"]) == 2
    old = next(item for item in final["evaluation_revisions"] if item["id"] == old_evaluation_id)
    assert old["score"] is None and old["manual_review_required"]
    assert final["answers"][0]["audio_uri"] == answer["audio_uri"]
    assert review.complete_review(session["id"], {}, ORG)["ai_decision_used"] is False


def _unresolved_exit_session(*, technical=False):
    from app.core.prompt.candidate_exit import EXIT_CONFIRMATION_SPEECH_VERSION
    from app.services.candidate_exit_confirmation import confirmed_exit_receipt, text_hash
    session = unresolved_session(technical=technical)
    turn, answer = session["turns"][0], session["answers"][0]
    request = (TECHNICAL if technical else "") + "然后要做蓝桥。我不想面了。"
    reply = "是的，确定结束。"
    text = request + reply
    understanding = turn["current_understanding"]
    understanding.update(turn_intent="stop_interview", completion_basis={"source": "explicit_server_intent", "evidence_quotes": [reply]},
                         evidence_quotes=([TECHNICAL] if technical else []) + ["然后要做蓝桥。", reply])
    turn["utterances"][0]["text"] = text
    answer.update(final_transcript=text, raw_transcript=text)
    turn["candidate_exit_confirmation"] = confirmed_exit_receipt({
        "version": EXIT_CONFIRMATION_SPEECH_VERSION, "decision": "confirm", "request_text": request, "reply_text": reply,
        "capture_id": "capture_synthetic", "question_act_id": "act_exit_synthetic", "transcript_sha256": text_hash(text),
    }, text, confirmed_at=NOW)
    return session


@pytest.mark.anyio
@pytest.mark.parametrize("technical", [False, True])
async def test_confirmed_exit_with_unresolved_content_stays_unscored_and_preserves_claims(technical):
    session = _unresolved_exit_session(technical=technical)
    store, gateway = InMemoryStore(), ScoringGateway()
    persist(store, session)
    result = await EvaluationService(store, gateway=gateway).evaluate_answer(session["answers"][0], question(0))
    assert result["score"] is None and result["manual_review_required"] and not gateway.inputs
    assert result["model_info"]["scoring_rule_version"] == "unresolved_answer.v1"
    assert bool(session["turns"][0]["current_understanding"]["claims"]) is technical


@pytest.mark.anyio
@pytest.mark.parametrize("change", [
    None, {"state": "pending"}, {"version": "forged"}, {"confirmed_at": ""},
    {"question_act_id": ""}, {"capture_id": ""}, {"transcript_sha256": "0" * 64},
    {"request_sha256": "forged"}, {"reply_sha256": ""},
])
async def test_unresolved_exit_without_a_bound_confirmation_receipt_cannot_score_or_guess(change):
    from app.model_gateway.errors import ProviderError
    session = _unresolved_exit_session()
    turn = session["turns"][0]
    if change is None:
        turn.pop("candidate_exit_confirmation")
    else:
        turn["candidate_exit_confirmation"].update(change)
    store, gateway = InMemoryStore(), ScoringGateway()
    persist(store, session)
    with pytest.raises(ProviderError) as caught:
        await EvaluationService(store, gateway=gateway).evaluate_answer(session["answers"][0], question(0))
    assert caught.value.code == "evaluation_evidence_invalid" and not gateway.inputs
