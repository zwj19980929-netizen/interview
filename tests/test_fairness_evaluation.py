import pytest
from pydantic import ValidationError

from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.schemas.api import ScoreCalibrationRun
from app.services.fairness import FairnessEvaluationService


def test_selection_distribution_flags_difficulty_spread_without_deciding() -> None:
    store = InMemoryStore()
    persistence = persistence_for(store)
    with persistence.transaction("org_a") as transaction:
        for interview_id, difficulty in (("iv_easy", "easy"), ("iv_hard", "hard")):
            transaction.interview_sessions.add(
                {
                    "id": interview_id,
                    "organization_id": "org_a",
                    "plan_snapshot": {"job_position_id": "position_1"},
                    "turns": [
                        {
                            "id": "turn_%s" % interview_id,
                            "phase": "position_bank",
                            "question_snapshot": {"difficulty": difficulty, "skills": ["python"]},
                        }
                    ],
                }
            )
    result = FairnessEvaluationService(store, persistence=persistence).selection_distribution(
        "position_1", actor_id="auditor_1", organization_id="org_a"
    )
    assert result["sample_size"] == 2
    assert result["metrics"]["difficulty_spread"] == 2.0
    assert result["human_review_required"] is True
    assert "selection_difficulty_spread_exceeds_threshold" in result["warnings"]
    assert "recommendation" not in result


def test_score_calibration_uses_only_current_evaluation_and_opaque_cohorts() -> None:
    store = InMemoryStore()
    persistence = persistence_for(store)
    with persistence.transaction("org_a") as transaction:
        for index, (ai_score, confidence, cohort) in enumerate(((82, 0.95, "cohort_a"), (60, 0.7, "cohort_b"))):
            transaction.interview_sessions.add({
                "id": "iv_%s" % index,
                "organization_id": "org_a",
                "turns": [{"id": "turn_%s" % index, "question_snapshot": {"type": "scenario"}}],
                "answers": [{
                    "id": "answer_%s" % index,
                    "turn_id": "turn_%s" % index,
                    "current_evaluation_id": "eval_%s" % index,
                    "language": "zh-CN",
                    "stt_confidence": confidence,
                }],
                "evaluation_revisions": [{"id": "eval_%s" % index, "answer_id": "answer_%s" % index, "score": ai_score}],
            })
    result = FairnessEvaluationService(store, persistence=persistence).score_calibration(
        {
            "dataset_version": "real-deidentified-v1",
            "labels": [
                {"evaluation_id": "eval_0", "human_score": 80, "fairness_cohort": "cohort_a"},
                {"evaluation_id": "eval_1", "human_score": 70, "fairness_cohort": "cohort_b"},
            ],
        },
        actor_id="auditor_1",
        organization_id="org_a",
    )

    assert result["sample_size"] == 2
    assert result["metrics"]["mae"] == 6.0
    assert result["calibration_candidate"]["apply_automatically"] is False
    assert result["human_review_required"] is True
    assert "calibration_sample_below_30" in result["warnings"]
    assert all("interview_id" not in item for item in result["strata"]["fairness_cohort"])


def test_score_calibration_schema_rejects_candidate_pii() -> None:
    with pytest.raises(ValidationError):
        ScoreCalibrationRun.model_validate({
            "dataset_version": "deidentified-v1",
            "labels": [
                {"evaluation_id": "eval_1", "human_score": 80, "candidate_name": "禁止提交"},
                {"evaluation_id": "eval_2", "human_score": 70},
            ],
        })
