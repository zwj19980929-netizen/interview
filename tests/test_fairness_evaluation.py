from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
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
