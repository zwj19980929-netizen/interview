import pytest

from app.domain.candidate_screening import (
    SCREENING_SCORE_POLICY_VERSION,
    apply_screening_score_policy,
    effective_screening_outcome,
    screening_recommendation_for_score,
)


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0, "unqualified"),
        (59, "unqualified"),
        (60, "manual_review"),
        (74, "manual_review"),
        (75, "qualified"),
        (100, "qualified"),
    ],
)
def test_screening_score_policy_has_unambiguous_boundaries(score: int, expected: str) -> None:
    assert screening_recommendation_for_score(score) == expected


@pytest.mark.parametrize("score", [-1, 101, 74.5, True, None])
def test_screening_score_policy_rejects_invalid_scores(score) -> None:
    with pytest.raises(ValueError):
        screening_recommendation_for_score(score)


def test_score_policy_overrides_a_conflicting_model_recommendation() -> None:
    model_output = {"score": 75, "recommendation": "manual_review", "summary": "模型建议冲突"}

    normalized = apply_screening_score_policy(model_output)

    assert normalized["recommendation"] == "qualified"
    assert normalized["policy_version"] == SCREENING_SCORE_POLICY_VERSION
    assert model_output["recommendation"] == "manual_review"


def test_human_review_overrides_the_score_derived_ai_recommendation() -> None:
    review = {
        "status": "ready_for_review",
        "screening_score": 42,
        "screening_recommendation": "manual_review",
        "human_decision": "qualified",
    }

    assert effective_screening_outcome(review) == "qualified"
