from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.persistence.errors import ConcurrencyConflict
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore


class InterviewPlanService:
    def __init__(self, store: InMemoryStore, *, persistence: Optional[Persistence] = None) -> None:
        self.persistence = persistence or persistence_for(store)

    def list_plans(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return transaction.interview_plans.list()

    def patch_plan(
        self,
        plan_id: str,
        payload: Dict[str, Any],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        expected_version = int(payload["expected_version"])
        with self.persistence.transaction(organization_id) as transaction:
            plan = transaction.interview_plans.get(plan_id)
            if not plan:
                raise ApiError("INTERVIEW_PLAN_NOT_FOUND", "Interview plan does not exist.", status_code=404)
            if plan["version"] != expected_version:
                raise ConcurrencyConflict(
                    "InterviewPlan %s expected version %s, found %s"
                    % (plan_id, expected_version, plan["version"])
                )

            new_status = payload.get("status")
            if new_status is not None:
                allowed = {
                    "draft": {"draft", "approved", "archived"},
                    "approved": {"approved", "archived"},
                    "archived": {"archived"},
                }
                if new_status not in allowed.get(plan["status"], set()):
                    raise ApiError(
                        "INTERVIEW_PLAN_STATUS_TRANSITION_INVALID",
                        "Interview plan status transition is not allowed.",
                        status_code=409,
                    )
                plan["status"] = new_status
                if new_status == "approved" and not plan.get("approved_at"):
                    plan["approved_at"] = utc_now()

            if payload.get("items") is not None:
                if plan["status"] != "draft":
                    raise ApiError(
                        "INTERVIEW_PLAN_IMMUTABLE",
                        "Only a draft interview plan can change its items.",
                        status_code=409,
                    )
                normalized_items: List[Dict[str, Any]] = []
                for index, raw in enumerate(payload["items"], start=1):
                    question = transaction.questions.get(raw["question_id"])
                    if question is None:
                        raise ApiError("QUESTION_NOT_FOUND", "A plan question does not exist.", status_code=404)
                    item = dict(raw)
                    item.setdefault("id", new_id("plan_item"))
                    item["order"] = index
                    normalized_items.append(item)
                if not normalized_items:
                    raise ApiError("INTERVIEW_PLAN_EMPTY", "Interview plan must contain a question.")
                plan["items"] = normalized_items
                if plan.get("assembly_summary"):
                    plan["assembly_summary"]["manually_edited"] = True
                    warning = (
                        "计划题目已在自动装配后被人工编辑；当前覆盖摘要描述的是原始草稿。"
                    )
                    warnings = plan["assembly_summary"].setdefault("warnings", [])
                    if warning not in warnings:
                        warnings.append(warning)

            plan["updated_at"] = utc_now()
            return transaction.interview_plans.update(plan, expected_version=expected_version)
