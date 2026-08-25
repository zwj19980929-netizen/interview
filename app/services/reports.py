from copy import deepcopy
from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore


class ReportService:
    """Builds reports from frozen plan data and current evaluation pointers."""

    def __init__(self, store: InMemoryStore, *, persistence: Optional[Persistence] = None) -> None:
        self.persistence = persistence or persistence_for(store)

    def _recommendation(self, score: int) -> str:
        if score >= 90:
            return "strong_advance"
        if score >= 75:
            return "advance"
        if score >= 60:
            return "hold"
        return "reject"

    def build_report(self, interview: Dict[str, Any], *, trigger_reason: str) -> Dict[str, Any]:
        evaluations_by_id = {item["id"]: item for item in interview.get("evaluation_revisions", [])}
        item_by_snapshot = {
            item["question_snapshot_id"]: item for item in interview["plan_snapshot"]["items"]
        }
        question_evaluations: List[Dict[str, Any]] = []
        weighted_score = 0.0
        completed_weight = 0.0
        dimension_totals: Dict[str, Dict[str, float]] = {}

        for answer in interview.get("answers", []):
            evaluation_id = answer.get("current_evaluation_id")
            evaluation = evaluations_by_id.get(evaluation_id)
            if evaluation is None:
                continue
            plan_item = item_by_snapshot.get(
                evaluation["question_snapshot_id"],
                {"weight": 0.0, "dimension": "general"},
            )
            weight = float(plan_item.get("weight", 0.0))
            weighted_score += float(evaluation["score"]) * weight
            completed_weight += weight
            dimension = plan_item.get("dimension", "general")
            totals = dimension_totals.setdefault(dimension, {"weighted": 0.0, "weight": 0.0})
            totals["weighted"] += float(evaluation["score"]) * weight
            totals["weight"] += weight
            question_evaluations.append(
                {
                    "answer_id": answer["id"],
                    "evaluation_id": evaluation["id"],
                    "evaluation_revision": evaluation["revision"],
                    "question_id": evaluation["question_id"],
                    "question_snapshot_id": evaluation["question_snapshot_id"],
                    "score": evaluation["score"],
                    "confidence": evaluation["confidence"],
                    "covered_key_points": deepcopy(evaluation["covered_key_points"]),
                    "missing_key_points": deepcopy(evaluation["missing_key_points"]),
                    "evidence": [item["evidence"] for item in evaluation["covered_key_points"]],
                    "summary": evaluation["feedback"],
                }
            )

        overall_score = int(round(weighted_score / completed_weight)) if completed_weight else 0
        dimension_scores = [
            {
                "dimension": dimension,
                "score": int(round(value["weighted"] / value["weight"])) if value["weight"] else 0,
            }
            for dimension, value in sorted(dimension_totals.items())
        ]
        return {
            "id": new_id("report"),
            "organization_id": interview["organization_id"],
            "interview_id": interview["id"],
            "overall_score": overall_score,
            "recommendation": self._recommendation(overall_score),
            "dimension_scores": dimension_scores,
            "strengths": ["关键点覆盖较好"] if overall_score >= 75 else [],
            "risks": ["存在未覆盖关键点，建议人工复核"] if overall_score < 75 else [],
            "followup_suggestions": ["针对缺失关键点安排人工追问"] if overall_score < 75 else [],
            "question_evaluations": question_evaluations,
            "evaluation_ids": [item["evaluation_id"] for item in question_evaluations],
            "trigger_reason": trigger_reason,
            "human_decision_required": True,
            "generated_by": "system",
            "generated_at": utc_now(),
        }

    def get_report(self, interview_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        interview = self._get_interview(interview_id, organization_id)
        report_id = interview.get("current_report_id")
        report = next(
            (item for item in interview.get("report_revisions", []) if item["id"] == report_id),
            None,
        )
        if report is None:
            raise ApiError("REPORT_NOT_FOUND", "Interview report is not ready.", status_code=404)
        return report

    def list_report_revisions(
        self,
        interview_id: str,
        organization_id: str = "org_default",
    ) -> List[Dict[str, Any]]:
        interview = self._get_interview(interview_id, organization_id)
        return deepcopy(interview.get("report_revisions", []))

    def _get_interview(self, interview_id: str, organization_id: str) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            interview = transaction.interview_sessions.get(interview_id)
        if interview is None:
            raise ApiError("INTERVIEW_NOT_FOUND", "Interview does not exist.", status_code=404)
        return interview
