from typing import Any, Dict, List, Optional

from app.core.ids import new_id
from app.core.time import utc_now
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.text import normalize_skill, tokenize


ROLE_PROFILE_STOPWORDS = {
    "and",
    "engineer",
    "engineering",
    "for",
    "role",
    "the",
    "with",
}


class RoleRequirementService:
    def __init__(self, store: InMemoryStore, *, persistence: Optional[Persistence] = None) -> None:
        self.persistence = persistence or persistence_for(store)

    def list_role_requirements(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return transaction.role_requirements.list()

    def create_role_requirement(self, payload: Dict[str, Any], organization_id: str = "org_default") -> Dict[str, Any]:
        must_have = [normalize_skill(skill) for skill in payload.get("must_have_skills", [])]
        nice_to_have = [normalize_skill(skill) for skill in payload.get("nice_to_have_skills", [])]
        skill_priorities: Dict[str, float] = {}
        for skill in must_have:
            if skill:
                skill_priorities[skill] = 3.0
        for skill in nice_to_have:
            if skill and skill not in skill_priorities:
                skill_priorities[skill] = 1.0
        if not skill_priorities:
            for token in tokenize(payload.get("description", "")):
                normalized = normalize_skill(token)
                if (
                    normalized.isascii()
                    and len(normalized) > 1
                    and normalized not in ROLE_PROFILE_STOPWORDS
                ):
                    skill_priorities.setdefault(normalized, 1.0)
                if len(skill_priorities) >= 8:
                    break
        if not skill_priorities:
            skill_priorities = {"general": 1.0}
        total_priority = sum(skill_priorities.values())
        skill_weights = {
            skill: round(priority / total_priority, 4)
            for skill, priority in skill_priorities.items()
        }
        last_skill = next(reversed(skill_weights))
        skill_weights[last_skill] = round(
            skill_weights[last_skill] + (1.0 - sum(skill_weights.values())),
            4,
        )
        parsed_profile = {
            "skill_weights": skill_weights,
            "target_difficulty": payload.get("seniority", "mid"),
            "business_scenarios": [],
            "avoid_topics": [],
        }
        role_id = new_id("role")
        now = utc_now()
        item = {
            "id": role_id,
            "organization_id": organization_id,
            "title": payload["title"],
            "description": payload["description"],
            "must_have_skills": must_have,
            "nice_to_have_skills": nice_to_have,
            "seniority": payload.get("seniority", "mid"),
            "interview_duration_minutes": payload.get("interview_duration_minutes", 45),
            "parsed_profile": parsed_profile,
            "created_at": now,
            "updated_at": now,
        }
        with self.persistence.transaction(organization_id) as transaction:
            return transaction.role_requirements.add(item)
