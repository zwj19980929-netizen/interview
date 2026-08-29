from copy import deepcopy
from typing import Any, Dict


SCREENING_SCORE_POLICY_VERSION = "candidate_screening_score.v1"
SCREENING_MANUAL_REVIEW_MIN = 60
SCREENING_QUALIFIED_MIN = 75


def screening_recommendation_for_score(score: int) -> str:
    """Map the validated 0-100 screening score to the canonical AI recommendation."""
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise ValueError("Candidate screening score must be an integer between 0 and 100.")
    if score < SCREENING_MANUAL_REVIEW_MIN:
        return "unqualified"
    if score < SCREENING_QUALIFIED_MIN:
        return "manual_review"
    return "qualified"


def apply_screening_score_policy(screening: Dict[str, Any]) -> Dict[str, Any]:
    """Return a domain-safe screening result whose recommendation cannot contradict its score."""
    normalized = deepcopy(screening)
    normalized["recommendation"] = screening_recommendation_for_score(normalized.get("score"))
    normalized["policy_version"] = SCREENING_SCORE_POLICY_VERSION
    return normalized


def screening_ai_recommendation(review: Dict[str, Any]) -> Any:
    """Derive the canonical AI recommendation while remaining compatible with pre-policy reviews."""
    score = review.get("screening_score")
    if isinstance(score, int) and not isinstance(score, bool) and 0 <= score <= 100:
        return screening_recommendation_for_score(score)
    return review.get("screening_recommendation")


def effective_screening_outcome(review: Dict[str, Any]) -> Any:
    """Return the human-overridable outcome used by projections and retention."""
    if review.get("status") != "ready_for_review":
        return None
    return review.get("human_decision") or screening_ai_recommendation(review)
