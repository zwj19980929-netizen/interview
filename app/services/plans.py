from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.plan_assembly import InterviewPlanAssembly


class InterviewPlanService:
    def __init__(
        self,
        store: InMemoryStore,
        *,
        persistence: Optional[Persistence] = None,
        assembly: Optional[InterviewPlanAssembly] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.assembly = assembly or InterviewPlanAssembly(store, persistence=self.persistence)

    def list_plans(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            plans = transaction.interview_plans.list()
        for plan in plans:
            self.assembly.require_execution_v2(plan)
        return plans

    def get_plan(self, plan_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            plan = transaction.interview_plans.get(plan_id)
        if plan is None:
            raise ApiError("INTERVIEW_PLAN_NOT_FOUND", "Interview plan does not exist.", status_code=404)
        self.assembly.require_execution_v2(plan)
        return plan

    def patch_plan(
        self,
        plan_id: str,
        payload: Dict[str, Any],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        return self.assembly.patch_plan(plan_id, payload, organization_id)
