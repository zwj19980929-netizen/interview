"""Scoring declined answers uses persisted speech semantics, never input tags."""

from copy import deepcopy

import pytest

from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.repositories.sqlite import SQLiteStore
from app.services.evaluation import EvaluationService
from app.services.interviews import InterviewService
from app.services.reports import ReportService
from app.services.fairness import FairnessEvaluationService


NOW = "2026-09-08T12:00:00Z"
ORG = "org_declined"
TECHNICAL = "使用幂等键防止重复执行，Outbox确保事务提交后投递任务。"
DECLINED = "这一题我暂时不会，没有别的要补充了，请进入下一题。"


class ScoringGateway:
    def __init__(self):
        self.inputs = []

    async def invoke(self, capability, request):
        self.inputs.append(request.metadata["answer_text"])
        return ChatJSONResponse(data={
            "score": 85, "confidence": .95, "dimension_scores": {"semantic_correctness": 85},
            "covered_key_points": [{"key_point_id": "kp_outbox", "evidence": "Outbox"}],
            "missing_key_points": [], "incorrect_claims": [], "evidence": ["Outbox"],
            "review_flags": [], "summary": "有可核验技术回答。", "suggested_followup": None,
        }, usage=Usage(), provider=ProviderMeta(provider_id="synthetic", model="synthetic-score",
                                                request_id="synthetic-request", latency_ms=0))


def question(index):
    return {"id": "snapshot_%d" % index, "source_question_id": "question_%d" % index,
            "source_question_version": 1, "source_type": "position_bank",
            "question_text": "如何实现可靠任务处理？", "standard_answer": "幂等键和Outbox。",
            "key_points": [{"id": "kp_idempotency", "text": "幂等键"}, {"id": "kp_outbox", "text": "Outbox"}],
            "rubric": {"semantic_correctness": 1.0}}


def session_for(intents, *, followups=False):
    session = {"id": "iv_declined", "organization_id": ORG, "turns": [], "answers": [],
               "evaluation_revisions": [], "report_revisions": [], "status": "completed",
               "plan_snapshot": {"question_snapshots": []}}
    for index, intent in enumerate(intents):
        declined = intent == "answer_declined"
        text = DECLINED if declined else TECHNICAL
        snapshot = question(index)
        turn_id, utterance_id = "turn_%d" % index, "utterance_%d" % index
        understanding_id = "understanding_%d" % index
        audio_uri = "private-test://declined-%d.wav" % index
        is_followup = followups and index > 0
        root_turn_id = "turn_0" if is_followup else turn_id
        understanding = {
            "understanding_id": understanding_id, "revision": 1,
            "prompt_version": "interview_turn_understanding.v7", "utterance_id": utterance_id,
            "intent": intent, "answer_summary": "明确表示不再作答。" if declined else TECHNICAL,
            "claims": [] if declined else [{"claim": TECHNICAL, "evidence_quote": TECHNICAL}],
            "evidence_quotes": [text], "covered_capability_points": [] if declined else ["幂等键", "Outbox"],
            "missing_capability_points": ["幂等键", "Outbox"] if declined else [],
            "ambiguities": [], "contradictions": [], "confidence": .95,
            "suggested_action": "next", "provider": {"provider_id": "synthetic"}, "created_at": NOW,
        }
        turn = {"id": turn_id, "order": index + 1, "is_followup": is_followup,
                "root_turn_id": root_turn_id, "question_snapshot": snapshot,
                "question_snapshot_id": snapshot["id"], "status": "completed",
                "current_understanding": understanding, "utterances": [{
                    "utterance_id": utterance_id, "revision": 1, "speaker": "candidate",
                    "text": text, "is_final": True, "authoritative": True, "audio_uri": audio_uri,
                    "stt_confidence": .96, "source": "server_streaming", "created_at": NOW,
                }]}
        answer = {"id": "answer_%d" % index, "organization_id": ORG, "interview_id": session["id"],
                  "turn_id": turn_id, "question_id": snapshot["source_question_id"],
                  "question_snapshot_id": snapshot["id"], "utterance_id": utterance_id,
                  "understanding_id": understanding_id, "root_turn_id": root_turn_id,
                  "final_transcript": text, "audio_uri": audio_uri, "transcript_source": "server_streaming",
                  "stt_confidence": .96, "evaluation_status": "pending"}
        session["turns"].append(turn)
        session["answers"].append(answer)
        if not is_followup:
            session["plan_snapshot"]["question_snapshots"].append({
                "question_snapshot_id": snapshot["id"], "weight": 1.0, "dimension": "technical"})
    return session


def persist(store, session):
    persistence = persistence_for(store)
    with persistence.transaction(ORG) as transaction:
        current = transaction.interview_sessions.get(session["id"])
        if current:
            transaction.interview_sessions.update(session, expected_version=current["version"])
        else:
            transaction.interview_sessions.add(session)
    return persistence


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_declined_answer_scores_zero_without_llm_and_all_points_missing(backend, tmp_path):
    store = InMemoryStore() if backend == "memory" else SQLiteStore(str(tmp_path / "declined.sqlite3"))
    session = session_for(["answer_declined"])
    persistence = persist(store, session)
    # A separately opened SQLite process must observe the persisted contract.
    if backend == "sqlite":
        store = SQLiteStore(str(store.path))
        persistence = persistence_for(store)
    gateway = ScoringGateway()
    evaluation = await EvaluationService(store, gateway=gateway, persistence=persistence).evaluate_answer(
        session["answers"][0], question(0))
    assert gateway.inputs == []
    assert evaluation["score"] == 0 and all(value == 0 for value in evaluation["dimension_scores"].values())
    assert evaluation["covered_key_points"] == evaluation["incorrect_claims"] == evaluation["evidence"] == []
    assert {point["key_point_id"] for point in evaluation["missing_key_points"]} == {"kp_idempotency", "kp_outbox"}
    assert evaluation["suggested_followup"] is None
    assert evaluation["model_info"]["scoring_rule_version"] == "declined_answer.v1"
    assert evaluation["model_info"]["declined_answers"][0]["understanding_id"] == "understanding_0"
    assert evaluation["model_info"]["prompt_version"] is None
    assert evaluation["answer_id"] == "answer_0" and evaluation["created_by"] == "system"


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", ["client_tag", "foreign_tenant", "understanding_id", "utterance_id",
                                       "revised_text", "not_authoritative", "claims", "ambiguity", "missing_points"])
async def test_untrusted_or_unbound_declined_tags_cannot_trigger_zero_policy(invalid):
    store, gateway = InMemoryStore(), ScoringGateway()
    session = session_for(["answer" if invalid == "client_tag" else "answer_declined"])
    answer = deepcopy(session["answers"][0])
    answer["intent"] = "answer_declined"
    if invalid == "foreign_tenant":
        answer["organization_id"] = "another_tenant"
    elif invalid in {"understanding_id", "utterance_id"}:
        answer[invalid] = "another_id"
    elif invalid == "revised_text":
        answer["final_transcript"] = TECHNICAL
    elif invalid == "not_authoritative":
        session["turns"][0]["utterances"][0]["authoritative"] = False
    elif invalid == "claims":
        session["turns"][0]["current_understanding"]["claims"] = [{"claim": "claim", "evidence_quote": DECLINED}]
    elif invalid == "ambiguity":
        session["turns"][0]["current_understanding"]["ambiguities"] = ["未听清"]
    elif invalid == "missing_points":
        session["turns"][0]["current_understanding"]["missing_capability_points"] = []
    persistence = persist(store, session)
    evaluation = await EvaluationService(store, gateway=gateway, persistence=persistence).evaluate_answer(answer, question(0))
    assert gateway.inputs == [answer["final_transcript"]]
    assert evaluation["score"] == 85 and "scoring_rule_version" not in evaluation["model_info"]


@pytest.mark.anyio
@pytest.mark.parametrize("intents", [
    ["answer", "answer_declined"], ["answer_declined", "answer"],
    ["answer_declined", "answer_declined"],
])
async def test_merged_scoring_filters_only_declined_evidence_and_keeps_real_answers(intents):
    store, gateway = InMemoryStore(), ScoringGateway()
    session = session_for(intents, followups=True)
    persistence = persist(store, session)
    service = InterviewService(store, persistence=persistence)
    merged = service._merged_authoritative_answer(session, root_answer_id="answer_0",
                                                  evidence_answer_ids=["answer_0", "answer_1"])
    evaluation = await EvaluationService(store, gateway=gateway, persistence=persistence).evaluate_answer(merged, question(0))
    if "answer" not in intents:
        assert evaluation["score"] == 0 and gateway.inputs == []
    else:
        assert evaluation["score"] == 85 and len(gateway.inputs) == 1
        assert DECLINED not in gateway.inputs[0] and TECHNICAL in gateway.inputs[0]
        if intents[0] == "answer":
            assert gateway.inputs == [TECHNICAL]
        assert len(evaluation["model_info"]["excluded_declined_answers"]) == 1
    # Scoring projection must never rewrite any authoritative answer.
    assert [item["final_transcript"] for item in session["answers"]] == [
        DECLINED if intent == "answer_declined" else TECHNICAL for intent in intents]


@pytest.mark.anyio
async def test_all_declined_and_last_declined_scores_are_usable_by_reports_and_fairness():
    store, gateway = InMemoryStore(), ScoringGateway()
    session = session_for(["answer_declined", "answer_declined"])
    persistence = persist(store, session)
    evaluator = EvaluationService(store, gateway=gateway, persistence=persistence)
    for index, answer in enumerate(session["answers"]):
        evaluation = await evaluator.evaluate_answer(answer, question(index))
        evaluation["revision"] = 1
        answer["current_evaluation_id"] = evaluation["id"]
        answer["evaluation_status"] = "completed"
        session["evaluation_revisions"].append(evaluation)
    persist(store, session)
    report = ReportService(store, persistence=persistence).build_report(session, trigger_reason="final_answer_evaluated")
    assert report["overall_score"] == 0 and report["job_fit_level"] == "insufficient_evidence"
    assert len(report["question_evaluations"]) == 2
    assert all(item["evidence"] == [] and item["covered_key_points"] == [] for item in report["question_evaluations"])
    assert report["human_decision_required"] and not report["strengths"] and not gateway.inputs
    calibration = FairnessEvaluationService(store, persistence=persistence).score_calibration({
        "dataset_version": "synthetic-declined-v1", "labels": [
            {"evaluation_id": item["id"], "human_score": 0, "fairness_cohort": "synthetic_cohort"}
            for item in session["evaluation_revisions"]]}, actor_id="synthetic-auditor", organization_id=ORG)
    assert calibration["sample_size"] == 2 and calibration["metrics"]["mae"] == 0


@pytest.mark.anyio
async def test_failed_persisted_evidence_read_cannot_become_a_zero_score():
    class UnavailablePersistence:
        def transaction(self, organization_id):
            raise RuntimeError("synthetic persistence unavailable")

    gateway = ScoringGateway()
    evaluator = EvaluationService(InMemoryStore(), gateway=gateway, persistence=UnavailablePersistence())
    with pytest.raises(RuntimeError, match="synthetic persistence unavailable"):
        await evaluator.evaluate_answer(session_for(["answer_declined"])["answers"][0], question(0))
    assert gateway.inputs == []
