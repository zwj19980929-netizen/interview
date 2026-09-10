from collections import Counter
from math import sqrt
from statistics import mean
from typing import Any, Dict, List, Optional

from app.core.ids import new_id
from app.domain.scoring_quality import evaluation_answers, project_evaluation
from app.core.errors import ApiError
from app.core.time import utc_now
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore


DIFFICULTY_POINTS = {"junior": 1.0, "easy": 1.0, "mid": 2.0, "medium": 2.0, "senior": 3.0, "hard": 3.0}


class FairnessEvaluationService:
    """Compares selection difficulty and coverage without making hiring decisions."""

    def __init__(self, store: InMemoryStore, *, persistence: Optional[Persistence] = None) -> None:
        self.persistence = persistence or persistence_for(store)

    def selection_distribution(
        self,
        job_position_id: str,
        *,
        actor_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            sessions = [
                item
                for item in transaction.interview_sessions.list()
                if item.get("plan_snapshot", {}).get("job_position_id") == job_position_id
            ]
        rows: List[Dict[str, Any]] = []
        all_skills: Counter[str] = Counter()
        for session in sessions:
            turns = [item for item in session.get("turns", []) if item.get("phase", "position_bank") == "position_bank"]
            difficulties = [
                DIFFICULTY_POINTS.get(str(item.get("question_snapshot", {}).get("difficulty", "mid")).lower(), 2.0)
                for item in turns
            ]
            skills = sorted(
                {
                    str(skill)
                    for item in turns
                    for skill in item.get("question_snapshot", {}).get("skills", [])
                }
            )
            all_skills.update(skills)
            rows.append(
                {
                    "interview_id": session["id"],
                    "selected_question_count": len(turns),
                    "average_difficulty": round(mean(difficulties), 4) if difficulties else 0.0,
                    "skills": skills,
                }
            )
        difficulty_values = [item["average_difficulty"] for item in rows if item["selected_question_count"]]
        spread = max(difficulty_values) - min(difficulty_values) if difficulty_values else 0.0
        count_values = [item["selected_question_count"] for item in rows]
        count_spread = max(count_values) - min(count_values) if count_values else 0
        warnings = []
        if spread > 0.75:
            warnings.append("selection_difficulty_spread_exceeds_threshold")
        if count_spread > 1:
            warnings.append("selection_count_spread_exceeds_threshold")
        result = {
            "job_position_id": job_position_id,
            "sample_size": len(rows),
            "sessions": rows,
            "metrics": {
                "mean_difficulty": round(mean(difficulty_values), 4) if difficulty_values else 0.0,
                "difficulty_spread": round(spread, 4),
                "question_count_spread": count_spread,
                "skill_selection_counts": dict(sorted(all_skills.items())),
            },
            "warnings": warnings,
            "human_review_required": bool(warnings),
            "generated_at": utc_now(),
        }
        with self.persistence.transaction(organization_id) as transaction:
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "fairness.selection_distribution.generated",
                    "resource_type": "job_position",
                    "resource_id": job_position_id,
                    "metadata": {"sample_size": len(rows), "warnings": warnings},
                    "created_at": utc_now(),
                }
            )
        return result

    def score_calibration(
        self,
        payload: Dict[str, Any],
        *,
        actor_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        """Compare current AI revisions with de-identified human gold labels.

        Callers can submit only evaluation ids, scores and opaque cohort names;
        transcripts and candidate identifiers are deliberately outside this seam.
        The returned fit is advisory and is never applied to hiring decisions.
        """
        labels = payload.get("labels") or []
        label_ids = [str(item["evaluation_id"]) for item in labels]
        if len(label_ids) != len(set(label_ids)):
            raise ApiError("CALIBRATION_LABEL_DUPLICATE", "Each evaluation may be labeled only once.", status_code=409)
        with self.persistence.transaction(organization_id) as transaction:
            sessions = transaction.interview_sessions.list()
        current: Dict[str, Dict[str, Any]] = {}
        for session in sessions:
            answers = {item["id"]: item for item in session.get("answers", [])}
            turns = {item["id"]: item for item in session.get("turns", [])}
            for evaluation in session.get("evaluation_revisions", []):
                answer = answers.get(evaluation.get("answer_id"))
                if not answer or answer.get("current_evaluation_id") != evaluation.get("id"):
                    continue
                evaluation = project_evaluation(evaluation, evaluation_answers(session, evaluation))
                if evaluation["score"] is None:
                    continue
                turn = turns.get(answer.get("turn_id"), {})
                question = turn.get("question_snapshot", {})
                current[evaluation["id"]] = {
                    "ai_score": float(evaluation["score"]),
                    "question_type": str(question.get("source_type") or question.get("type") or "unknown"),
                    "language": str(answer.get("language") or "unknown"),
                    "stt_quality": _stt_quality(answer.get("stt_confidence")),
                }
        missing = sorted(set(label_ids).difference(current))
        if missing:
            raise ApiError(
                "CALIBRATION_EVALUATION_NOT_CURRENT",
                "Calibration labels must reference current evaluation revisions in this organization.",
                status_code=409,
                details={"missing_or_historical_count": len(missing)},
            )
        rows = []
        for label in labels:
            source = current[str(label["evaluation_id"])]
            rows.append({
                **source,
                "human_score": float(label["human_score"]),
                "fairness_cohort": label.get("fairness_cohort") or "unassigned",
            })
        errors = [item["ai_score"] - item["human_score"] for item in rows]
        metrics = {
            "mae": round(mean(abs(value) for value in errors), 4),
            "rmse": round(sqrt(mean(value * value for value in errors)), 4),
            "mean_signed_error": round(mean(errors), 4),
            "within_5_points_rate": round(mean(abs(value) <= 5 for value in errors), 4),
            "within_10_points_rate": round(mean(abs(value) <= 10 for value in errors), 4),
        }
        fit = _linear_fit(rows)
        strata = {
            "question_type": _strata(rows, "question_type"),
            "language": _strata(rows, "language"),
            "stt_quality": _strata(rows, "stt_quality"),
            "fairness_cohort": _strata(rows, "fairness_cohort"),
        }
        cohort_maes = [item["mae"] for item in strata["fairness_cohort"] if item["group"] != "unassigned"]
        cohort_gap = round(max(cohort_maes) - min(cohort_maes), 4) if len(cohort_maes) >= 2 else None
        warnings = []
        if len(rows) < 30:
            warnings.append("calibration_sample_below_30")
        if metrics["mae"] > 8:
            warnings.append("calibration_mae_exceeds_8")
        if abs(metrics["mean_signed_error"]) > 5:
            warnings.append("calibration_bias_exceeds_5")
        if cohort_gap is not None and cohort_gap > 5:
            warnings.append("fairness_cohort_mae_gap_exceeds_5")
        if any(item["sample_size"] < 10 for item in strata["fairness_cohort"] if item["group"] != "unassigned"):
            warnings.append("fairness_cohort_underpowered")
        result = {
            "dataset_version": payload["dataset_version"],
            "sample_size": len(rows),
            "metrics": metrics,
            "strata": strata,
            "fairness_cohort_mae_gap": cohort_gap,
            "calibration_candidate": {**fit, "apply_automatically": False},
            "warnings": warnings,
            "human_review_required": True,
            "generated_at": utc_now(),
        }
        with self.persistence.transaction(organization_id) as transaction:
            transaction.audit_events.add({
                "id": new_id("audit"),
                "organization_id": organization_id,
                "actor_id": actor_id,
                "action": "fairness.score_calibration.generated",
                "resource_type": "calibration_dataset",
                "resource_id": payload["dataset_version"],
                "metadata": {"sample_size": len(rows), "metrics": metrics, "warnings": warnings},
                "created_at": utc_now(),
            })
        return result


def _stt_quality(confidence: Optional[float]) -> str:
    if confidence is None:
        return "unknown"
    if confidence >= 0.9:
        return "high"
    if confidence >= 0.75:
        return "medium"
    return "low"


def _strata(rows: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    result = []
    for group in sorted({str(item[key]) for item in rows}):
        grouped = [item for item in rows if str(item[key]) == group]
        errors = [item["ai_score"] - item["human_score"] for item in grouped]
        result.append({
            "group": group,
            "sample_size": len(grouped),
            "mae": round(mean(abs(value) for value in errors), 4),
            "mean_signed_error": round(mean(errors), 4),
            "statistically_supported": len(grouped) >= 10,
        })
    return result


def _linear_fit(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    ai = [item["ai_score"] for item in rows]
    human = [item["human_score"] for item in rows]
    ai_mean = mean(ai)
    human_mean = mean(human)
    denominator = sum((value - ai_mean) ** 2 for value in ai)
    slope = sum((left - ai_mean) * (right - human_mean) for left, right in zip(ai, human)) / denominator if denominator else 1.0
    intercept = human_mean - slope * ai_mean
    return {"slope": round(slope, 6), "intercept": round(intercept, 6)}
