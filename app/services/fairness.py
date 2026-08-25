from collections import Counter
from statistics import mean
from typing import Any, Dict, List, Optional

from app.core.ids import new_id
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
