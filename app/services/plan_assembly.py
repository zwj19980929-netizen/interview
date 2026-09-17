from dataclasses import dataclass, field, replace
from copy import deepcopy
import asyncio
import logging
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.core.prompt.inquiry_units import VERSION as INQUIRY_PROMPT_VERSION, InquiryUnitValidationError, inquiry_units_contract, validate_inquiry_units
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ChatJSONRequest, InvocationExecutionBudget
from app.domain.candidate_screening import effective_screening_outcome
from app.domain.adaptive_interview import (
    AdaptiveInterviewPolicy, freeze_assessment_contract, frozen_candidate,
    validate_assessment_contract,
)
from app.domain.question_selection import QuestionSelection, QuestionSelectionRequest
from app.domain.speech_profile import freeze_interview_speech_profile, speech_profile_fingerprint
from app.persistence.errors import ConcurrencyConflict
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.catalog import CatalogService
from app.services.text import normalize_skill, tokenize
from app.services.interview_skills import InterviewSkillService


DIFFICULTY_RANK = {"junior": 1, "mid": 2, "senior": 3, "expert": 4}
logger = logging.getLogger(__name__)
INQUIRY_BATCH_POINT_LIMIT = 6
INQUIRY_TOTAL_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class PlanAssemblyPolicy:
    coverage: Tuple[str, ...] = ()
    allow_followups: bool = True
    max_same_skill_questions: int = 3
    difficulty_curve: bool = True
    deduplication_threshold: float = 0.72


@dataclass(frozen=True)
class PlanAssemblyRequest:
    role_requirement_id: Optional[str]
    knowledge_base_ids: Tuple[str, ...] = ()
    question_count: int = 8
    policy: PlanAssemblyPolicy = field(default_factory=PlanAssemblyPolicy)
    job_position_id: Optional[str] = None
    candidate_profile_id: Optional[str] = None
    resume_review_id: Optional[str] = None
    approve: bool = False
    execution_schema_version: int = 2
    adaptive_policy: Optional[Dict[str, Any]] = None
    enterprise_skill_id: Optional[str] = None
    skill_id: Optional[str] = None
    company_context: Optional[str] = None
    use_customization_defaults: bool = True
    preparation_mode: Optional[str] = None
    duration_minutes: int = 45


@dataclass(frozen=True)
class _Candidate:
    question_id: str
    title: str
    skills: Tuple[str, ...]
    difficulty: str
    score: float
    match_reasons: Tuple[str, ...]
    concepts: FrozenSet[str]
    dimension: str


@dataclass(frozen=True)
class _Selection:
    candidate: _Candidate
    target_dimension: str
    overlap: float
    relaxed_constraints: Tuple[str, ...] = ()


class InterviewPlanAssembly:
    """Deep in-process module for producing one explainable InterviewPlan draft."""

    def __init__(
        self,
        store: InMemoryStore,
        *,
        persistence: Optional[Persistence] = None,
        catalog: Optional[CatalogService] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.catalog = catalog or CatalogService(store, persistence=self.persistence)
        self.skills = InterviewSkillService(store, persistence=self.persistence)

    async def prepare(self, *, job_position_id: str, candidate_profile_id: str,
                      knowledge_base_ids: Sequence[str], duration_minutes: int = 45,
                      organization_id: str = "org_default",
                      on_progress: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        """Prepare one usable interview from existing materials, without a role draft."""
        if (not job_position_id or not candidate_profile_id or not knowledge_base_ids
                or len(knowledge_base_ids) > 10 or len(set(knowledge_base_ids)) != len(knowledge_base_ids)
                or any(not isinstance(item, str) or not item.strip() for item in knowledge_base_ids)
                or type(duration_minutes) is not int or not 5 <= duration_minutes <= 120):
            raise ApiError("INTERVIEW_PREPARATION_INVALID", "请选择岗位、候选人与题库，面试时长为5至120分钟。", status_code=422)
        return await self.assemble(PlanAssemblyRequest(
            role_requirement_id=None, job_position_id=job_position_id, candidate_profile_id=candidate_profile_id,
            knowledge_base_ids=tuple(knowledge_base_ids), duration_minutes=duration_minutes,
            question_count=min(12, max(1, duration_minutes // 5)), approve=True,
            execution_schema_version=3, preparation_mode="question_bank",
            adaptive_policy={"min_root_questions": 1, "min_evidence_units_per_competency": 1},
        ), organization_id, on_progress=on_progress)

    async def assemble(
        self,
        request: PlanAssemblyRequest,
        organization_id: str = "org_default",
        *, on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        self._validate_request(request)
        self._notify_progress(on_progress, {"stage": "reading"})
        bank_preparation = request.preparation_mode == "question_bank"
        basis = None
        with self.persistence.transaction(organization_id) as transaction:
            if bank_preparation:
                position = transaction.job_positions.get(request.job_position_id)
                if position is None or position.get("status") in {"archived", "deleting"}:
                    raise ApiError("JOB_POSITION_NOT_FOUND", "请选择有效的招聘岗位。", status_code=404)
                basis = {"kind": "question_bank", "job_position_id": position["id"],
                         "position_version": position["version"], "position_name": position["name"],
                         "position_description": position.get("description", ""),
                         "knowledge_base_ids": list(request.knowledge_base_ids)}
                # An assembly context, not a persisted or invented RoleRequirement.
                role = {"id": None, "version": None, "job_position_id": position["id"],
                        "interview_duration_minutes": request.duration_minutes}
            else:
                role = transaction.role_requirements.get(request.role_requirement_id)
        if role is None:
            raise ApiError(
                "ROLE_REQUIREMENT_NOT_FOUND",
                "Role requirement does not exist.",
                status_code=404,
            )
        request = self._resolve_resume_review(request, organization_id)
        self._validate_target_scope(request, role, organization_id)
        default_skill_snapshot = None
        customization_version = None
        if request.use_customization_defaults:
            from app.services.interview_customization import InterviewCustomizationService

            supplied_skill = request.skill_id or request.enterprise_skill_id
            supplied_company = isinstance(request.company_context, str) and request.company_context.strip()
            if not supplied_skill or not supplied_company:
                defaults = InterviewCustomizationService(None, persistence=self.persistence).resolve_defaults(
                    organization_id, include_skill=not supplied_skill, include_company=not supplied_company,
                )
                customization_version = defaults["customization_version"]
                if not supplied_skill:
                    request = replace(request, skill_id=defaults["skill_id"])
                    default_skill_snapshot = defaults["skill_snapshot"]
                if not supplied_company and (request.company_context is None or isinstance(request.company_context, str)):
                    request = replace(request, company_context=defaults["company_context"])
        interview_duration = int(role.get("interview_duration_minutes", 45))
        if request.question_count > interview_duration:
            raise ApiError(
                "INTERVIEW_PLAN_POLICY_INVALID",
                "question_count cannot exceed interview_duration_minutes.",
                status_code=400,
            )

        dimensions, targets, dimension_weights = self._coverage_targets(role, request)
        candidates = await self._candidate_pool(
            role,
            request,
            dimensions,
            dimension_weights,
            organization_id,
        )
        if not candidates:
            raise ApiError(
                "NO_QUESTIONS_MATCHED",
                "所选题库没有可用问题，请先完成题目入库与语音准备。",
            )

        if bank_preparation:
            skills = self._unique_skills([skill for candidate in candidates for skill in candidate.skills]) or ["general"]
            role["parsed_profile"] = {"skill_weights": {skill: 1.0 for skill in skills}}
            dimensions, targets, dimension_weights = self._coverage_targets(role, request)
            candidates = [replace(item, dimension=self._candidate_dimension(item.skills, dimensions, dimension_weights))
                          for item in candidates]

        selections, warnings = self._select(
            candidates,
            request.question_count,
            dimensions,
            targets,
            dimension_weights,
            request.policy,
            role.get("parsed_profile", {}).get("target_difficulty")
            or role.get("seniority", "mid"),
        )
        ordered = self._order(selections, role, request.policy)
        weights = self._allocate_units(
            self._item_weight_inputs(ordered, dimension_weights),
            10_000,
        )
        minutes = self._allocate_minutes(
            weights,
            int(role.get("interview_duration_minutes", 45)),
        )
        slot_drafts = self._slot_drafts(ordered, weights, minutes, targets, request.policy)
        summary = self._summary(
            request=request,
            candidates=candidates,
            selections=ordered,
            dimensions=dimensions,
            targets=targets,
            warnings=warnings,
        )
        now = utc_now()
        bank_slots = self._bank_slots(slot_drafts, candidates, organization_id)
        speech_profile_snapshot = self._interview_speech_profile(
            request.knowledge_base_ids, organization_id
        )
        experience_question_snapshots = self._approved_experience_question_snapshots(
            request.resume_review_id, organization_id
        )
        summary["experience_question_count"] = len(experience_question_snapshots)
        summary["experience_question_target"] = 3
        if len(experience_question_snapshots) < 2 and not bank_preparation:
            summary.setdefault("warnings", []).append(
                "简历题不足2道：请确认该候选人在此岗位的合格简历审核中已有批准的问题。"
            )
        self._rebalance_execution_budget(
            bank_slots,
            experience_question_snapshots,
            interview_duration,
        )
        plan = {
            "id": new_id("plan"),
            "organization_id": organization_id,
            "role_requirement_id": role["id"],
            "job_position_id": request.job_position_id or role.get("job_position_id"),
            "candidate_profile_id": request.candidate_profile_id,
            "resume_review_id": request.resume_review_id,
            "knowledge_base_ids": list(request.knowledge_base_ids),
            "knowledge_base_snapshots": self._knowledge_base_snapshots(
                request.knowledge_base_ids, organization_id
            ),
            "speech_profile_snapshot": speech_profile_snapshot,
            "status": "draft",
            "estimated_minutes": interview_duration,
            "assembly_policy": self._policy_document(request.policy),
            "selection_policy": self._policy_document(request.policy),
            "assembly_summary": summary,
            "bank_slots": bank_slots,
            "experience_question_ids": [item["id"] for item in experience_question_snapshots],
            "experience_question_snapshots": experience_question_snapshots,
            "execution_schema_version": request.execution_schema_version,
            "created_at": now,
            "updated_at": now,
        }
        if bank_preparation:
            plan.update(preparation_mode="question_bank", assessment_basis=basis)
            # Selection relaxations are internal decisions, not missing user inputs.
            summary["selection_notes"] = summary.pop("warnings", [])
            summary["warnings"] = []
        if request.skill_id and request.enterprise_skill_id and request.skill_id != request.enterprise_skill_id:
            raise ApiError("INTERVIEW_SKILL_REFERENCE_CONFLICT", "Only one optional Skill can be selected.", status_code=422)
        selected_skill_id = request.skill_id or request.enterprise_skill_id
        if selected_skill_id:
            # Existing snapshots retain their storage identity for compatibility.
            plan["enterprise_skill_id"] = selected_skill_id
            if default_skill_snapshot:
                plan["enterprise_skill_snapshot"] = deepcopy(default_skill_snapshot)
        if customization_version is not None:
            plan["customization_version"] = customization_version
        if request.company_context is not None:
            if not isinstance(request.company_context, str) or len(request.company_context) > 12000:
                raise ApiError("COMPANY_CONTEXT_INVALID", "Optional company context must be text within 12000 characters.", status_code=422)
            if request.company_context.strip():
                plan["company_context"] = request.company_context.strip()
        if request.execution_schema_version == 3:
            plan["adaptive_policy"] = AdaptiveInterviewPolicy.model_validate(request.adaptive_policy or {}).model_dump()
            self._bound_adaptive_pool(plan, request.question_count)
            source_plan = deepcopy(plan)
            # Before units exist, the original-question count cannot be used
            # to reject a valid unit budget. Final freezing below validates it.
            source_plan["adaptive_policy"] = {}
            with self.persistence.transaction(organization_id) as transaction:
                self._freeze_adaptive_contract(transaction, source_plan, role, dimension_weights, request.question_count)
            reusable = self._reusable_inquiry_units(source_plan, organization_id) if bank_preparation else {}
            plan["inquiry_unit_snapshots"] = await self._prepare_inquiry_units(
                source_plan["assessment_contract"]["candidate_questions"], organization_id,
                reusable=reusable, on_progress=on_progress)
        self._notify_progress(on_progress, {"stage": "saving"})
        # Give request cancellation a checkpoint before the atomic save, even
        # when every question was reused and no model call yielded control.
        await asyncio.sleep(0)
        with self.persistence.transaction(organization_id) as transaction:
            if bank_preparation:
                self._validate_preparation_basis(transaction, plan)
            else:
                current_role = transaction.role_requirements.get(role["id"])
                if current_role is None or current_role["version"] != role["version"]:
                    raise ConcurrencyConflict("RoleRequirement changed while the plan was assembled.")
            if request.execution_schema_version == 3:
                self._freeze_adaptive_contract(transaction, plan, role, dimension_weights, request.question_count)
                contract = plan["assessment_contract"]
                plan["assembly_summary"]["inquiry_unit_count"] = sum(len(item["inquiry_units"]) for item in contract["candidate_questions"])
                plan["assembly_summary"]["minimum_required_root_questions"] = contract["budget"]["min_root_questions"]
                if bank_preparation:
                    summary["coverage_dimensions"] = [item["id"] for item in contract["competencies"]]
                    summary["uncovered_dimensions"] = []
                plan["assembly_summary"]["warnings"] = [warning for warning in plan["assembly_summary"].get("warnings", [])
                    if not (warning.startswith("请求 ") and "候选池只有" in warning)]
            if request.resume_review_id:
                review = transaction.resume_reviews.get(request.resume_review_id)
                if not review or effective_screening_outcome(review) != "qualified":
                    raise ConcurrencyConflict("Resume review changed while the plan was assembled.")
                for snapshot in experience_question_snapshots:
                    current = transaction.experience_questions.get(snapshot["id"])
                    if not current or current["version"] != snapshot["version"] or current.get("status") != "approved":
                        raise ConcurrencyConflict("Experience question changed while the plan was assembled.")
            # Freeze the exact approved Skill on the reviewable draft. Approval
            # revalidates this revision instead of silently selecting a newer one.
            self._freeze_enterprise_skill(transaction, plan)
            if request.approve:
                self._validate_canonical_plan(transaction, plan)
                plan["status"] = "approved"
                plan["approved_at"] = utc_now()
                if bank_preparation:
                    plan["approval_source"] = "automatic_preparation"
            return transaction.interview_plans.add(plan)

    @staticmethod
    def _validate_preparation_basis(transaction: Any, plan: Dict[str, Any]) -> None:
        basis = plan["assessment_basis"]
        position = transaction.job_positions.get(basis["job_position_id"])
        if (not position or position["version"] != basis["position_version"]
                or position.get("status") in {"archived", "deleting"}):
            raise ConcurrencyConflict("岗位资料发生变化，请重新准备面试。")
        candidate = transaction.candidate_profiles.get(plan["candidate_profile_id"])
        if (not candidate or candidate.get("status") in {"archived", "deleted", "purged"}
                or candidate.get("job_position_id") not in {None, position["id"]}):
            raise ConcurrencyConflict("候选人资料或所属岗位发生变化，请重新准备面试。")
        for snapshot in plan["knowledge_base_snapshots"]:
            bank = transaction.knowledge_bases.get(snapshot["knowledge_base_id"])
            if (not bank or bank["version"] != snapshot["knowledge_base_version"] or bank["status"] != "ready"
                    or not (bank.get("job_position_id") == position["id"]
                            or bank["id"] in position.get("knowledge_base_ids", []))):
                raise ConcurrencyConflict("题库内容或关联发生变化，请重新准备面试。")

    def _resolve_resume_review(self, request: PlanAssemblyRequest, organization_id: str) -> PlanAssemblyRequest:
        if request.resume_review_id:
            return request
        with self.persistence.transaction(organization_id) as transaction:
            reviews = [item for item in transaction.resume_reviews.list()
                       if item.get("candidate_profile_id") == request.candidate_profile_id
                       and item.get("job_position_id") == request.job_position_id
                       and effective_screening_outcome(item) == "qualified"]
        if not reviews:
            return request
        latest = max(reviews, key=lambda item: (item.get("created_at", ""), item["id"]))
        return replace(request, resume_review_id=latest["id"])

    def _validate_target_scope(
        self,
        request: PlanAssemblyRequest,
        role: Dict[str, Any],
        organization_id: str,
    ) -> None:
        if not request.job_position_id:
            return
        with self.persistence.transaction(organization_id) as transaction:
            position = transaction.job_positions.get(request.job_position_id)
            if position is None:
                raise ApiError("JOB_POSITION_NOT_FOUND", "Job position does not exist.", status_code=404)
            if role.get("job_position_id") != request.job_position_id:
                raise ApiError("ROLE_POSITION_MISMATCH", "Role requirement belongs to another position.", status_code=409)
            knowledge_bases = [transaction.knowledge_bases.get(item) for item in request.knowledge_base_ids]
            if not knowledge_bases or any(item is None for item in knowledge_bases):
                raise ApiError("KNOWLEDGE_BASE_NOT_FOUND", "Every selected knowledge base must exist.", status_code=404)
            assigned_ids = set(position.get("knowledge_base_ids", []))
            assigned_ids.update(
                item["id"]
                for item in transaction.knowledge_bases.list()
                if item.get("job_position_id") == request.job_position_id
            )
            if any(item["id"] not in assigned_ids for item in knowledge_bases if item):
                raise ApiError("KNOWLEDGE_BASE_POSITION_MISMATCH", "Knowledge base is not assigned to this position.", status_code=409)
            if any(item["status"] != "ready" for item in knowledge_bases if item):
                raise ApiError("KNOWLEDGE_BASE_NOT_READY", "Every selected knowledge base must be ready.", status_code=409)
            if request.candidate_profile_id and transaction.candidate_profiles.get(request.candidate_profile_id) is None:
                raise ApiError("CANDIDATE_PROFILE_NOT_FOUND", "Candidate profile does not exist.", status_code=404)
            if request.preparation_mode == "question_bank":
                candidate = transaction.candidate_profiles.get(request.candidate_profile_id)
                if (not candidate or candidate.get("job_position_id") not in {None, request.job_position_id}
                        or candidate.get("status") in {"archived", "deleted", "purged"}):
                    raise ApiError("CANDIDATE_POSITION_MISMATCH", "请选择该岗位的有效候选人。", status_code=409)
            if request.resume_review_id:
                review = transaction.resume_reviews.get(request.resume_review_id)
                if review is None:
                    raise ApiError("RESUME_REVIEW_NOT_FOUND", "Resume review does not exist.", status_code=404)
                if review["job_position_id"] != request.job_position_id or review["candidate_profile_id"] != request.candidate_profile_id:
                    raise ApiError("RESUME_REVIEW_SCOPE_MISMATCH", "Resume review does not match the plan scope.", status_code=409)
                if review["status"] != "ready_for_review":
                    raise ApiError("RESUME_REVIEW_NOT_READY", "Resume review is not ready.", status_code=409)
                if effective_screening_outcome(review) != "qualified":
                    raise ApiError(
                        "CANDIDATE_QUESTION_BANK_NOT_ELIGIBLE",
                        "Resume questions require a qualified effective screening outcome.",
                        status_code=409,
                    )

    def _bank_slots(
        self,
        slot_drafts: Sequence[Dict[str, Any]],
        candidates: Sequence[_Candidate],
        organization_id: str,
    ) -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            questions = {item["id"]: item for item in transaction.questions.list()}
        slots = []
        for draft in slot_drafts:
            eligible = [candidate for candidate in candidates if candidate.dimension == draft["dimension"]]
            if not eligible:
                eligible = list(candidates)
            pool = []
            for candidate in eligible:
                question = questions.get(candidate.question_id)
                if not question or question.get("validation_status") not in {None, "valid"}:
                    continue
                if question.get("speech_status") not in {None, "ready"}:
                    continue
                pool.append(
                    {
                        "question_id": question["id"],
                        "question_version": question["version"],
                        "question_hash": question.get("content_hash"),
                    }
                )
            pool.sort(key=lambda candidate: candidate["question_id"] != draft["question_id"])
            slots.append(
                {
                    "id": new_id("slot"),
                    "order": draft["order"],
                    "dimension": draft["dimension"],
                    "difficulty": questions.get(draft["question_id"], {}).get("difficulty", "mid"),
                    "weight": draft["weight"],
                    "expected_minutes": draft["expected_minutes"],
                    "candidate_pool": pool,
                    "candidate_pool_count": len(pool),
                    "candidate_pool_hash": QuestionSelection().pool_hash(pool),
                    "display_question_id": draft["question_id"],
                    "allow_followup": draft.get("allow_followup", True),
                    "selection_reason": draft.get("selection_reason", ""),
                }
            )
        return slots

    def patch_plan(
        self,
        plan_id: str,
        payload: Dict[str, Any],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        """Edit and approve a plan through its canonical execution representation."""
        expected_version = int(payload["expected_version"])
        with self.persistence.transaction(organization_id) as transaction:
            plan = transaction.interview_plans.get(plan_id)
            if plan is None:
                raise ApiError("INTERVIEW_PLAN_NOT_FOUND", "Interview plan does not exist.", status_code=404)
            if plan["version"] != expected_version:
                raise ConcurrencyConflict(
                    "InterviewPlan %s expected version %s, found %s"
                    % (plan_id, expected_version, plan["version"])
                )
            self.require_execution_plan(plan)

            editable_fields = {"bank_slots", "experience_question_ids", "selection_policy", "assembly_policy"}
            editing = any(field in payload and payload[field] is not None for field in editable_fields)
            if editing and plan["status"] != "draft":
                raise ApiError(
                    "INTERVIEW_PLAN_IMMUTABLE",
                    "Only a draft interview plan can change its execution definition.",
                    status_code=409,
                )

            if payload.get("bank_slots") is not None:
                plan["bank_slots"] = self._normalize_slots(transaction, plan, payload["bank_slots"])

            if payload.get("experience_question_ids") is not None:
                snapshots = self._experience_snapshots_by_ids(
                    transaction,
                    plan,
                    payload["experience_question_ids"],
                )
                plan["experience_question_ids"] = [item["id"] for item in snapshots]
                plan["experience_question_snapshots"] = snapshots

            if payload.get("selection_policy") is not None:
                plan["selection_policy"] = deepcopy(payload["selection_policy"])
            if payload.get("assembly_policy") is not None:
                plan["assembly_policy"] = deepcopy(payload["assembly_policy"])

            if editing:
                self._rebalance_execution_budget(
                    plan.get("bank_slots", []),
                    plan.get("experience_question_snapshots", []),
                    int(plan.get("estimated_minutes", 0)),
                )
                summary = plan.setdefault("assembly_summary", {})
                summary["manually_edited"] = True
                summary["selected_question_count"] = len(plan["bank_slots"])
                summary["requested_question_count"] = len(plan["bank_slots"])
                warning = "计划执行槽位已由面试官人工编辑，覆盖摘要已按当前槽位重新标记。"
                warnings = summary.setdefault("warnings", [])
                if warning not in warnings:
                    warnings.append(warning)
                if plan.get("execution_schema_version") == 3:
                    contract = plan["assessment_contract"]
                    self._freeze_adaptive_contract(transaction, plan, transaction.role_requirements.get(plan["role_requirement_id"]),
                        {item["id"]: item["weight"] for item in contract["competencies"]},
                        contract["budget"]["max_root_questions"])

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
                if new_status == "approved":
                    self._validate_canonical_plan(transaction, plan)
                    self._freeze_enterprise_skill(transaction, plan)
                    if not plan.get("approved_at"):
                        plan["approved_at"] = utc_now()
                plan["status"] = new_status

            plan["updated_at"] = utc_now()
            return transaction.interview_plans.update(plan, expected_version=expected_version)

    def _freeze_enterprise_skill(self, transaction: Any, plan: Dict[str, Any]) -> None:
        if not plan.get("enterprise_skill_id"):
            return
        if plan.get("enterprise_skill_snapshot"):
            self.skills.verify_current_authorization(transaction, plan["enterprise_skill_snapshot"], for_new_plan=True)
        else:
            plan["enterprise_skill_snapshot"] = self.skills.freeze_snapshot(transaction, plan["enterprise_skill_id"])

    def materialize_execution(
        self,
        transaction: Any,
        plan: Dict[str, Any],
        session_seed: str,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Resolve the canonical plan into immutable selections and turn blueprints."""
        self.require_execution_v2(plan)
        slots = plan.get("bank_slots", [])
        selected_ids: List[str] = []
        selections: List[Dict[str, Any]] = []
        turn_blueprints: List[Dict[str, Any]] = []
        for slot in sorted(slots, key=lambda item: item["order"]):
            selection = QuestionSelection().select(
                QuestionSelectionRequest(
                    session_seed=session_seed,
                    slot_id=slot["id"],
                    candidates=tuple(slot["candidate_pool"]),
                    excluded_question_ids=tuple(selected_ids),
                )
            )
            selected_ids.append(selection["question_id"])
            selections.append(selection)
            turn_blueprints.append(
                {
                    "id": new_id("turn_blueprint"),
                    "order": len(turn_blueprints) + 1,
                    "source_type": "position_bank",
                    "question_id": selection["question_id"],
                    "slot_id": slot["id"],
                    "dimension": slot.get("dimension", "general"),
                    "weight": float(slot.get("weight", 0.0)),
                    "expected_minutes": int(slot.get("expected_minutes", 0)),
                    "allow_followup": bool(slot.get("allow_followup", True)),
                    "selection": selection,
                }
            )
        for question in sorted(
            plan.get("experience_question_snapshots", []),
            key=lambda item: item.get("order", 0),
        ):
            turn_blueprints.append(
                {
                    "id": new_id("turn_blueprint"),
                    "order": len(turn_blueprints) + 1,
                    "source_type": "resume_experience",
                    "question_id": question["id"],
                    "frozen_question": deepcopy(question),
                    "dimension": "resume_experience",
                    "weight": float(question.get("weight", 0.0)),
                    "expected_minutes": int(question.get("expected_minutes", 0)),
                    "allow_followup": bool(question.get("allow_followup", True)),
                    "selection_reason": "使用计划批准的简历经历核验问题。",
                }
            )
        if not turn_blueprints:
            raise ApiError("INTERVIEW_PLAN_EMPTY", "Approved plan has no selectable question.", status_code=409)
        self._normalize_turn_blueprint_weights(turn_blueprints)
        return selections, turn_blueprints

    def _normalize_slots(
        self,
        transaction: Any,
        plan: Dict[str, Any],
        raw_slots: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not raw_slots:
            raise ApiError("INTERVIEW_PLAN_EMPTY", "Interview plan must contain a slot.")
        result: List[Dict[str, Any]] = []
        for order, raw in enumerate(raw_slots, start=1):
            pool: List[Dict[str, Any]] = []
            raw_pool = raw.get("candidate_pool") or []
            for raw_candidate in raw_pool:
                question_id = raw_candidate.get("question_id") if isinstance(raw_candidate, dict) else raw_candidate
                question = transaction.questions.get(question_id)
                if question is None:
                    raise ApiError("QUESTION_NOT_FOUND", "A slot candidate does not exist.", status_code=404)
                self._validate_question_scope(plan, question)
                pool.append(
                    {
                        "question_id": question["id"],
                        "question_version": question["version"],
                        "question_hash": question.get("content_hash"),
                    }
                )
            if not pool:
                raise ApiError("INTERVIEW_PLAN_CANDIDATE_POOL_EMPTY", "Every plan slot needs candidates.", status_code=409)
            display_question_id = raw.get("display_question_id") or pool[0]["question_id"]
            if display_question_id not in {item["question_id"] for item in pool}:
                display_question_id = pool[0]["question_id"]
            result.append(
                {
                    "id": raw.get("id") or new_id("slot"),
                    "order": order,
                    "dimension": raw.get("dimension", "general"),
                    "difficulty": raw.get("difficulty", "mid"),
                    "weight": float(raw.get("weight", 1.0)),
                    "expected_minutes": int(raw.get("expected_minutes", 1)),
                    "candidate_pool": pool,
                    "candidate_pool_count": len(pool),
                    "candidate_pool_hash": QuestionSelection().pool_hash(pool),
                    "display_question_id": display_question_id,
                    "allow_followup": bool(raw.get("allow_followup", True)),
                    "selection_reason": raw.get("selection_reason", "人工编辑候选槽位。"),
                }
            )
        return result

    def _experience_snapshots_by_ids(
        self,
        transaction: Any,
        plan: Dict[str, Any],
        question_ids: Sequence[str],
    ) -> List[Dict[str, Any]]:
        if len(question_ids) > 3 or len(set(question_ids)) != len(question_ids):
            raise ApiError("EXPERIENCE_QUESTION_COUNT_INVALID", "Select at most three distinct resume questions.", status_code=422)
        snapshots: List[Dict[str, Any]] = []
        for order, question_id in enumerate(question_ids, start=1):
            question = transaction.experience_questions.get(question_id)
            if question is None:
                raise ApiError("EXPERIENCE_QUESTION_NOT_FOUND", "Experience question does not exist.", status_code=404)
            if question.get("resume_review_id") != plan.get("resume_review_id"):
                raise ApiError("EXPERIENCE_QUESTION_SCOPE_MISMATCH", "Experience question belongs to another review.", status_code=409)
            if question.get("status") != "approved":
                raise ApiError(
                    "EXPERIENCE_QUESTION_NOT_APPROVED",
                    "Experience question must be approved before it can enter a plan.",
                    status_code=409,
                )
            if not self._experience_question_grounded(question):
                raise ApiError(
                    "EXPERIENCE_QUESTION_NOT_GROUNDED",
                    "Experience question is not bound to an immutable resume evidence snapshot.",
                    status_code=409,
                )
            snapshot = self._experience_snapshot(question)
            snapshot["order"] = order
            snapshots.append(snapshot)
        return snapshots

    def _rebalance_execution_budget(
        self,
        slots: List[Dict[str, Any]],
        experience_snapshots: List[Dict[str, Any]],
        duration: int,
    ) -> None:
        entries: List[Dict[str, Any]] = [*slots, *experience_snapshots]
        if not entries:
            return
        if duration < len(entries):
            raise ApiError(
                "INTERVIEW_PLAN_DURATION_INVALID",
                "Interview duration must allocate at least one minute to every executable question.",
                status_code=409,
            )
        fallback_weight = 1.0 / len(entries)
        raw_weights = [max(0.0001, float(item.get("weight", fallback_weight))) for item in entries]
        weight_units = self._allocate_units(raw_weights, 10_000)
        minutes = self._allocate_minutes(weight_units, duration)
        for entry, units, expected_minutes in zip(entries, weight_units, minutes):
            entry["weight"] = round(units / 10_000, 4)
            entry["expected_minutes"] = expected_minutes

    def _normalize_turn_blueprint_weights(self, blueprints: List[Dict[str, Any]]) -> None:
        total = sum(float(blueprint.get("weight", 0.0)) for blueprint in blueprints)
        if total <= 0:
            units = self._allocate_units([1.0] * len(blueprints), 10_000)
        else:
            units = self._allocate_units([float(blueprint.get("weight", 0.0)) for blueprint in blueprints], 10_000)
        for blueprint, value in zip(blueprints, units):
            blueprint["weight"] = round(value / 10_000, 4)

    def _validate_canonical_plan(self, transaction: Any, plan: Dict[str, Any]) -> None:
        if plan.get("execution_schema_version") == 3:
            contract = plan.get("assessment_contract") or {}
            validate_assessment_contract(contract)
            if plan.get("preparation_mode") == "question_bank":
                if contract.get("assessment_basis") != plan.get("assessment_basis"):
                    raise ConcurrencyConflict("The frozen preparation basis changed before approval.")
                self._validate_preparation_basis(transaction, plan)
            else:
                role = transaction.role_requirements.get(contract["role_requirement_id"])
                if role is None or role["version"] != contract["role_requirement_version"]:
                    raise ConcurrencyConflict("The frozen role requirement changed before plan approval.")
            for candidate in contract["candidate_questions"]:
                if candidate["source_type"] != "position_bank":
                    continue
                current = transaction.questions.get(candidate["question_id"])
                if current is None or current["version"] != candidate["question_version"]:
                    raise ConcurrencyConflict("A frozen candidate question changed before plan approval.")
        self._validate_speech_profile_snapshot(transaction, plan)
        slots = plan.get("bank_slots", [])
        experiences = plan.get("experience_question_snapshots", [])
        if not slots and not experiences:
            raise ApiError("INTERVIEW_PLAN_EMPTY", "Interview plan has no executable question.", status_code=409)
        entries = [*slots, *experiences]
        if round(sum(float(item.get("weight", 0.0)) for item in entries), 4) != 1.0:
            raise ApiError("INTERVIEW_PLAN_WEIGHT_INVALID", "Interview plan weights must total 1.", status_code=409)
        if sum(int(item.get("expected_minutes", 0)) for item in entries) != int(plan.get("estimated_minutes", 0)):
            raise ApiError("INTERVIEW_PLAN_DURATION_INVALID", "Interview plan duration must be conserved.", status_code=409)
        for slot in slots:
            if not slot.get("candidate_pool"):
                raise ApiError("INTERVIEW_PLAN_CANDIDATE_POOL_EMPTY", "Every plan slot needs candidates.", status_code=409)
            for candidate in slot["candidate_pool"]:
                question = transaction.questions.get(candidate["question_id"])
                if question is None:
                    raise ApiError("QUESTION_NOT_FOUND", "A slot candidate does not exist.", status_code=404)
                self._validate_question_scope(plan, question)
                if plan.get("job_position_id") and (
                    question.get("validation_status") != "valid"
                    or question.get("speech_status") != "ready"
                ):
                    raise ApiError("INTERVIEW_PLAN_QUESTION_NOT_READY", "Every slot candidate must be valid and speech-ready.", status_code=409)

    def require_execution_v2(self, plan: Dict[str, Any]) -> None:
        if int(plan.get("execution_schema_version", 0)) != 2 or "items" in plan:
            raise ApiError(
                "INTERVIEW_PLAN_MIGRATION_REQUIRED",
                "Interview plan must be migrated to execution schema version 2 before use.",
                status_code=409,
            )

    def require_execution_plan(self, plan: Dict[str, Any]) -> None:
        if plan.get("execution_schema_version") == 3 and "items" not in plan:
            validate_assessment_contract(plan.get("assessment_contract") or {})
            return
        self.require_execution_v2(plan)

    def _freeze_adaptive_contract(self, transaction: Any, plan: Dict[str, Any], role: Dict[str, Any],
                                  dimension_weights: Dict[str, float], question_count: int) -> None:
        weights = dict(dimension_weights)
        experiences = plan.get("experience_question_snapshots", [])
        if experiences and "resume_experience" not in weights:
            weights["resume_experience"] = 0.2
        candidates: Dict[str, Dict[str, Any]] = {}
        for slot in plan["bank_slots"]:
            for reference in slot["candidate_pool"]:
                question = transaction.questions.get(reference["question_id"])
                if question is None:
                    raise ApiError("QUESTION_NOT_FOUND", "An approved candidate question does not exist.", status_code=404)
                self._validate_question_scope(plan, question)
                if question["version"] != reference["question_version"]:
                    raise ConcurrencyConflict("An approved candidate question changed during plan assembly.")
                mapped = [name for name in weights if name in self._unique_skills(question.get("skills", []))]
                if not mapped and "general" in weights:
                    mapped = ["general"]
                if not mapped:
                    continue
                candidates[question["id"]] = frozen_candidate(question, competency_ids=mapped,
                    source_type="position_bank", expected_minutes=slot["expected_minutes"],
                    allow_followup=slot.get("allow_followup", True))
        for question in experiences:
            candidates[question["id"]] = frozen_candidate(question, competency_ids=["resume_experience"],
                source_type="resume_experience", expected_minutes=question.get("expected_minutes", 3),
                allow_followup=question.get("allow_followup", True))
        bank_preparation = plan.get("preparation_mode") == "question_bank"
        if bank_preparation:
            supported = {name for candidate in candidates.values() for name in candidate["competency_ids"]}
            weights = {name: 1.0 for name in weights if name in supported}
        if "inquiry_unit_snapshots" not in plan and not bank_preparation:
            self._check_adaptive_source_coverage(plan, role, weights, list(candidates.values()))
        if "inquiry_unit_snapshots" in plan:
            for candidate in candidates.values():
                units = plan["inquiry_unit_snapshots"].get(candidate["question_id"])
                if not units:
                    raise ApiError("INQUIRY_UNITS_REQUIRED", "新增题目需要重新生成并审核该自主考察计划。", status_code=409)
                candidate["inquiry_units"] = deepcopy(units)
            if bank_preparation:
                supported = set()
                for candidate in candidates.values():
                    mapped = {name for unit in candidate["inquiry_units"] for name in unit["competency_ids"]}
                    candidate["competency_ids"] = sorted(mapped)
                    supported.update(mapped)
                weights = {name: 1.0 for name in weights if name in supported}
        plan["assessment_contract"] = freeze_assessment_contract(
            role=role, candidates=list(candidates.values()), dimension_weights=weights,
            question_count=question_count, duration_minutes=int(plan["estimated_minutes"]),
            policy=plan.get("adaptive_policy"),
            assessment_basis=plan.get("assessment_basis"),
            allow_followups=bool(plan.get("selection_policy", {}).get("allow_followups", True)))

    def _check_adaptive_source_coverage(self, plan: Dict[str, Any], role: Dict[str, Any],
                                      weights: Dict[str, float], candidates: Sequence[Dict[str, Any]]) -> None:
        """Explain shortlist gaps before building a contract or generating units.

        This is the actual scoped, ready shortlist, not every question in a
        knowledge base. Inquiry units can only narrow their source mappings.
        """
        required = {name for name, weight in weights.items() if weight > 0}
        available = {name for candidate in candidates for name in candidate["competency_ids"]}
        missing = required - available
        if not missing:
            return
        role_required = {normalize_skill(str(name)) for name, weight in
                         (role.get("parsed_profile", {}).get("skill_weights") or {}).items() if float(weight) > 0}
        requested = set(self._unique_skills(plan.get("selection_policy", {}).get("coverage", [])))
        raise ApiError("ASSESSMENT_SOURCE_COVERAGE_MISSING",
            "当前可用题池尚未覆盖以下能力：%s。请补齐题库中可用题目，或调整对应岗位要求及本次考察重点后重新生成。"
            % "、".join(sorted(missing)), status_code=422,
            details={"missing_competency_ids": sorted(missing),
                     "missing_role_competency_ids": sorted(missing & role_required),
                     "missing_requested_competency_ids": sorted(missing & requested)})

    @staticmethod
    def _bound_adaptive_pool(plan: Dict[str, Any], question_count: int) -> None:
        """Freeze an explicit, reviewable shortlist before paid adaptation."""
        slots = plan["bank_slots"]
        limit = min(60, max(12, question_count * 2, len({slot["dimension"] for slot in slots})))
        chosen = set()
        # Start with one approved representative per slot/dimension, then fill
        # in fair rounds so large skill pools do not crowd out smaller ones.
        for slot in slots:
            if slot["candidate_pool"]:
                chosen.add(slot["candidate_pool"][0]["question_id"])
        maximum = max((len(slot["candidate_pool"]) for slot in slots), default=0)
        for index in range(maximum):
            for slot in slots:
                if len(chosen) >= limit:
                    break
                if index < len(slot["candidate_pool"]):
                    chosen.add(slot["candidate_pool"][index]["question_id"])
        for slot in slots:
            slot["candidate_pool"] = [item for item in slot["candidate_pool"] if item["question_id"] in chosen]
            slot["candidate_pool_count"] = len(slot["candidate_pool"])
            slot["candidate_pool_hash"] = QuestionSelection().pool_hash(slot["candidate_pool"])
        plan["assembly_summary"]["adaptive_source_question_count"] = len(chosen)

    @staticmethod
    def _notify_progress(callback, progress):
        if callback is not None:
            try:
                callback(progress)
            except Exception:
                # Observability must not change the assembly outcome.
                logger.warning("inquiry_progress_observer_failed")

    def _reusable_inquiry_units(self, source_plan: Dict[str, Any], organization_id: str) -> Dict[str, Any]:
        """Reuse validated immutable sources; never another candidate's evidence."""
        wanted = {row["question_id"]: row for row in source_plan["assessment_contract"]["candidate_questions"]}
        result = {}
        with self.persistence.transaction(organization_id) as transaction:
            plans = transaction.interview_plans.list()
        for previous in sorted(plans, key=lambda row: row.get("created_at", ""), reverse=True):
            if previous.get("status") != "approved" or previous.get("preparation_mode") != "question_bank":
                continue
            contract = previous.get("assessment_contract") or {}
            if contract.get("presentation_policy") != "approved_inquiry_units.v1":
                continue
            try:
                validate_assessment_contract(contract)
            except (ApiError, ValueError, TypeError, KeyError, AttributeError):
                continue
            for cached in contract["candidate_questions"]:
                current = wanted.get(cached["question_id"])
                if not current or current["question_id"] in result:
                    continue
                if (cached["question_hash"] != current["question_hash"]
                        or cached["question_version"] != current["question_version"]
                        or cached["source_type"] != current["source_type"]):
                    continue
                if current["source_type"] == "resume_experience":
                    if any(previous.get(key) != source_plan.get(key) for key in ("candidate_profile_id", "resume_review_id")):
                        continue
                    original_scope = ["resume_experience"]
                else:
                    # Bank preparation originally offered all source labels to
                    # the model, then narrowed the final contract to actual units.
                    original_scope = self._unique_skills(cached["frozen_question"].get("skills", [])) or ["general"]
                units = cached.get("inquiry_units") or []
                if (set(original_scope) != set(current["competency_ids"]) or not units
                        or any(unit.get("prompt_version") != INQUIRY_PROMPT_VERSION for unit in units)
                        or any(not set(unit["competency_ids"]).issubset(current["competency_ids"]) for unit in units)):
                    continue
                result[current["question_id"]] = deepcopy(units)
            if len(result) == len(wanted):
                break
        return result

    async def _prepare_inquiry_units(self, candidates: List[Dict[str, Any]], organization_id: str,
                                     *, reusable=None, on_progress=None) -> Dict[str, Any]:
        reusable = reusable or {}
        sources = []
        for candidate in candidates:
            question = candidate["frozen_question"]
            points = [{"id": point["id"], "text": point["text"]} if isinstance(point, dict)
                      else {"id": str(point), "text": str(point)} for point in question["key_points"]]
            if len(points) > 20:
                raise ApiError("INQUIRY_SOURCE_TOO_COMPLEX", "单道原题最多支持20个关键点，请先在题库拆分并审核。", status_code=422)
            sources.append({"id": question["id"], "question_text": question["question_text"],
                            "standard_answer": question["standard_answer"], "key_points": points,
                            "competency_ids": deepcopy(candidate["competency_ids"])})
        total_points = sum(len(source["key_points"]) for source in sources)
        reused_points = sum(len(units) for units in reusable.values())
        completed_points = reused_points

        def report():
            self._notify_progress(on_progress, {"stage": "preparing", "completed_points": completed_points,
                "total_points": total_points, "reused_points": reused_points})

        report()
        semaphore = asyncio.Semaphore(3)
        # Cost is determined by the number of generated units, not source count.
        # One source per call makes the Schema's IDs and exact unit count local
        # to that source. Retain its full approved answer when splitting points.
        batches = [[{**source, "key_points": source["key_points"][index:index + INQUIRY_BATCH_POINT_LIMIT]}]
                   for source in sources if source["id"] not in reusable
                   for index in range(0, len(source["key_points"]), INQUIRY_BATCH_POINT_LIMIT)]

        async def generate(batch):
            nonlocal completed_points
            for attempt in range(2):
                try:
                    async with semaphore:
                        contract = inquiry_units_contract(batch, repair=bool(attempt))
                        response = await self.catalog.gateway.invoke(cap.LLM_CHAT_JSON, ChatJSONRequest(
                            organization_id=organization_id, purpose="question_generation", messages=contract.messages,
                            json_schema=contract.response_schema, max_output_tokens=3500, temperature=0,
                            execution_budget=InvocationExecutionBudget(timeout_s=60, max_provider_retries=0),
                            metadata={"prompt_version": contract.version}))
                        validated = validate_inquiry_units(response.data, batch)
                        completed_points += sum(len(units) for units in validated.values())
                        report()
                        return validated
                except (InquiryUnitValidationError, ProviderError) as exc:
                    invalid = isinstance(exc, InquiryUnitValidationError) or exc.code == "provider_schema_invalid"
                    if attempt or not invalid:
                        raise
                    # Repair only this invalid small batch. Successful batches
                    # remain intact; retries share the same total deadline.
                    logger.warning("inquiry_preparation_repair points=%d", sum(len(source["key_points"]) for source in batch))

        tasks = [asyncio.create_task(generate(batch)) for batch in batches]
        try:
            groups = await asyncio.wait_for(asyncio.gather(*tasks), timeout=INQUIRY_TOTAL_TIMEOUT_SECONDS)
            merged = {source["id"]: deepcopy(reusable.get(source["id"], [])) for source in sources}
            for group in groups:
                for source_id, units in group.items():
                    merged[source_id].extend(units)
            for source in sources:
                points = [unit["assessed_rubric_point_ids"][0] for unit in merged[source["id"]]]
                if points != [point["id"] for point in source["key_points"]]:
                    # Batch completion and model row order must not affect scope.
                    if len(points) != len(set(points)) or set(points) != {point["id"] for point in source["key_points"]}:
                        raise InquiryUnitValidationError("Inquiry batches must cover every source point exactly once.", "point_partition")
                    order = {point["id"]: index for index, point in enumerate(source["key_points"])}
                    merged[source["id"]].sort(key=lambda unit: order[unit["assessed_rubric_point_ids"][0]])
            return merged
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, asyncio.TimeoutError) or (isinstance(exc, ProviderError) and exc.code == "provider_timeout"):
                code, message, reason = "INQUIRY_UNIT_GENERATION_TIMEOUT", "模型整理问题超时，计划尚未创建。已保留你的选择，请稍后重试。", "timeout"
            elif isinstance(exc, ValueError) or (isinstance(exc, ProviderError) and exc.code == "provider_schema_invalid"):
                code, message, reason = "INQUIRY_UNIT_RESPONSE_INVALID", "模型返回的问题未通过内容校验，计划尚未创建。请重试，无需重新生成简历问题。", "invalid_response"
            elif isinstance(exc, ProviderError):
                code, message, reason = "INQUIRY_UNIT_PROVIDER_UNAVAILABLE", "问题整理服务暂时不可用，计划尚未创建。请检查模型服务后重试。", "provider_unavailable"
            else:
                code, message, reason = "INQUIRY_UNIT_GENERATION_FAILED", "创建计划时发生异常，计划尚未创建。已保留你的选择，请稍后重试。", "unexpected"
            detail = getattr(exc, "reason_code", None)
            if detail not in {"source_partition", "point_partition", "spoken_question", "reference_range", "reference_content", "competency_scope"}:
                detail = "unspecified"
            logger.warning("inquiry_preparation_failed reason=%s detail=%s sources=%d points=%d batches=%d", reason, detail,
                           len(sources), sum(len(source["key_points"]) for source in sources), len(batches))
            raise ApiError(code, message, status_code=503) from None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _validate_question_scope(self, plan: Dict[str, Any], question: Dict[str, Any]) -> None:
        if question.get("status") != "active":
            raise ApiError("INTERVIEW_PLAN_QUESTION_INACTIVE", "Plan questions must be active.", status_code=409)
        if (plan.get("preparation_mode") != "question_bank" and plan.get("job_position_id")
                and question.get("job_position_id") != plan.get("job_position_id")):
            raise ApiError("INTERVIEW_PLAN_QUESTION_SCOPE_MISMATCH", "Plan question belongs to another position.", status_code=409)
        knowledge_base_ids = set(plan.get("knowledge_base_ids", []))
        if knowledge_base_ids and question.get("knowledge_base_id") not in knowledge_base_ids:
            raise ApiError("INTERVIEW_PLAN_QUESTION_SCOPE_MISMATCH", "Plan question belongs to another knowledge base.", status_code=409)

    def _experience_snapshot(self, item: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = {
            key: deepcopy(item.get(key))
            for key in (
                "id",
                "version",
                "question_text",
                "standard_answer",
                "key_points",
                "rubric",
                "evidence_refs",
                "status",
                "speech_asset_id",
                "speech_status",
            )
        }
        snapshot["speech_asset_id"] = None
        snapshot["speech_status"] = "deferred"
        return snapshot

    def _approved_experience_questions(
        self, resume_review_id: Optional[str], organization_id: str
    ) -> List[str]:
        if not resume_review_id:
            return []
        with self.persistence.transaction(organization_id) as transaction:
            return [
                item["id"]
                for item in sorted(transaction.experience_questions.list(), key=lambda value: value["order"])
                if item["resume_review_id"] == resume_review_id
                and item["status"] == "approved"
                and self._experience_question_grounded(item)
            ]

    def _knowledge_base_snapshots(
        self, knowledge_base_ids: Sequence[str], organization_id: str
    ) -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            result = []
            for knowledge_base_id in knowledge_base_ids:
                item = transaction.knowledge_bases.get(knowledge_base_id)
                if item is not None:
                    result.append(
                        {
                            "knowledge_base_id": item["id"],
                            "knowledge_base_version": item["version"],
                            "status": item["status"],
                            "speech_profile": deepcopy(item.get("speech_profile")),
                        }
                    )
            return result

    def _approved_experience_question_snapshots(
        self, resume_review_id: Optional[str], organization_id: str
    ) -> List[Dict[str, Any]]:
        if not resume_review_id:
            return []
        with self.persistence.transaction(organization_id) as transaction:
            result: List[Dict[str, Any]] = []
            for order, item in enumerate(
                (
                    item
                    for item in sorted(
                        transaction.experience_questions.list(), key=lambda value: (value["order"], value["id"])
                    )
                    if item["resume_review_id"] == resume_review_id
                    and item["status"] == "approved"
                    and self._experience_question_grounded(item)
                ),
                start=1,
            ):
                snapshot = self._experience_snapshot(item)
                snapshot["order"] = order
                result.append(snapshot)
                if len(result) == 3:
                    break
            return result

    def _interview_speech_profile(
        self,
        knowledge_base_ids: Sequence[str],
        organization_id: str,
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            knowledge_bases = [transaction.knowledge_bases.get(item) for item in knowledge_base_ids]
        if not knowledge_bases or any(item is None for item in knowledge_bases):
            raise ApiError(
                "KNOWLEDGE_BASE_NOT_FOUND",
                "Every selected knowledge base must exist.",
                status_code=404,
            )
        if any(not item.get("speech_profile") for item in knowledge_bases if item):
            raise ApiError(
                "KNOWLEDGE_BASE_SPEECH_PROFILE_REQUIRED",
                "Every selected knowledge base must have a speech profile.",
                status_code=409,
            )
        profile = freeze_interview_speech_profile(item for item in knowledge_bases if item)
        if profile is None:
            raise ApiError(
                "INTERVIEW_PLAN_SPEECH_PROFILE_CONFLICT",
                "All knowledge bases in one interview plan must use the same speech profile.",
                status_code=409,
            )
        return profile

    def _validate_speech_profile_snapshot(self, transaction: Any, plan: Dict[str, Any]) -> None:
        frozen = plan.get("speech_profile_snapshot")
        if not frozen:
            raise ApiError(
                "INTERVIEW_PLAN_SPEECH_PROFILE_REQUIRED",
                "Interview plan must freeze one speech profile before approval.",
                status_code=409,
            )
        knowledge_bases = [
            transaction.knowledge_bases.get(item) for item in plan.get("knowledge_base_ids", [])
        ]
        current = freeze_interview_speech_profile(item for item in knowledge_bases if item)
        if current is None:
            raise ApiError(
                "INTERVIEW_PLAN_SPEECH_PROFILE_CONFLICT",
                "All knowledge bases in one interview plan must use the same speech profile.",
                status_code=409,
            )
        if current["fingerprint"] != frozen.get("fingerprint"):
            raise ApiError(
                "INTERVIEW_PLAN_SPEECH_PROFILE_STALE",
                "Knowledge base speech profile changed after this plan was assembled.",
                status_code=409,
                details={
                    "expected_fingerprint": frozen.get("fingerprint"),
                    "current_fingerprint": speech_profile_fingerprint(current),
                },
            )

    @staticmethod
    def _experience_question_grounded(item: Dict[str, Any]) -> bool:
        refs = item.get("evidence_refs") or []
        question_text = str(item.get("question_text") or "")
        return bool(refs) and all(
            isinstance(ref, dict)
            and bool(str(ref.get("label") or "").strip())
            and bool(str(ref.get("evidence") or "").strip())
            and str(ref.get("label") or "").strip().casefold() in question_text.casefold()
            for ref in refs
        )

    def _validate_request(self, request: PlanAssemblyRequest) -> None:
        if request.preparation_mode is not None and (
                request.preparation_mode != "question_bank" or request.role_requirement_id is not None
                or request.execution_schema_version != 3 or not request.approve
                or not request.job_position_id or not request.candidate_profile_id or not request.knowledge_base_ids):
            raise ApiError("INTERVIEW_PREPARATION_INVALID", "Bank preparation requires a complete scoped v3 request.", status_code=422)
        if not isinstance(request.use_customization_defaults, bool):
            raise ApiError(
                "INTERVIEW_CUSTOMIZATION_POLICY_INVALID",
                "use_customization_defaults must be a boolean.",
                status_code=422,
            )
        if request.execution_schema_version not in {2, 3}:
            raise ApiError("INTERVIEW_PLAN_VERSION_INVALID", "execution_schema_version must be 2 or 3.", status_code=422)
        if request.adaptive_policy is not None:
            if request.execution_schema_version != 3:
                raise ApiError("INTERVIEW_PLAN_POLICY_INVALID", "adaptive_policy requires execution schema version 3.", status_code=422)
            try:
                AdaptiveInterviewPolicy.model_validate(request.adaptive_policy)
            except ValueError as error:
                raise ApiError("ASSESSMENT_POLICY_INVALID", "Adaptive interview budget is invalid.", status_code=422) from error
        if not request.job_position_id or not request.candidate_profile_id or not request.knowledge_base_ids:
            raise ApiError(
                "INTERVIEW_PLAN_SCOPE_REQUIRED",
                "job_position_id, candidate_profile_id and knowledge_base_ids are required.",
                status_code=422,
            )
        if request.question_count < 1 or request.question_count > 50:
            raise ApiError(
                "INTERVIEW_PLAN_POLICY_INVALID",
                "question_count must be between 1 and 50.",
                status_code=400,
            )
        policy = request.policy
        if policy.max_same_skill_questions < 1 or policy.max_same_skill_questions > 50:
            raise ApiError(
                "INTERVIEW_PLAN_POLICY_INVALID",
                "max_same_skill_questions must be between 1 and 50.",
                status_code=400,
            )
        if policy.deduplication_threshold < 0.0 or policy.deduplication_threshold > 1.0:
            raise ApiError(
                "INTERVIEW_PLAN_POLICY_INVALID",
                "deduplication_threshold must be between 0 and 1.",
                status_code=400,
            )

    def _coverage_targets(
        self,
        role: Dict[str, Any],
        request: PlanAssemblyRequest,
    ) -> Tuple[List[str], Dict[str, int], Dict[str, float]]:
        role_weights = {
            normalize_skill(str(key)): max(0.0, float(value))
            for key, value in (role.get("parsed_profile", {}).get("skill_weights") or {}).items()
        }
        explicit = self._unique_skills(request.policy.coverage)
        ranked_role = sorted(role_weights, key=lambda item: (-role_weights[item], item))
        dimensions = self._unique_skills([*explicit, *ranked_role]) or ["general"]
        weights = {
            dimension: role_weights.get(dimension, 1.0 if dimension in explicit else 0.1)
            for dimension in dimensions
        }
        total_weight = sum(weights.values()) or float(len(weights))
        weights = {key: value / total_weight for key, value in weights.items()}

        targets = {dimension: 0 for dimension in dimensions}
        remaining = request.question_count
        for dimension in dimensions:
            if remaining <= 0:
                break
            targets[dimension] += 1
            remaining -= 1
        if remaining:
            extra = self._allocate_units([weights[item] for item in dimensions], remaining)
            for dimension, count in zip(dimensions, extra):
                targets[dimension] += count
        return dimensions, targets, weights

    async def _candidate_pool(
        self,
        role: Dict[str, Any],
        request: PlanAssemblyRequest,
        dimensions: Sequence[str],
        dimension_weights: Dict[str, float],
        organization_id: str,
    ) -> List[_Candidate]:
        filter_skills = [item for item in dimensions if item != "general"]
        search_payload = {
            "query": "" if request.job_position_id else role.get("description", ""),
            "job_position_id": request.job_position_id,
            "knowledge_base_ids": list(request.knowledge_base_ids),
            "filters": {
                "skills": filter_skills,
            },
            "limit": min(200, max(20, request.question_count * 6)),
            "include_answer": True,
        }
        search = self.catalog.search_questions(search_payload, organization_id)
        candidates: List[_Candidate] = []
        seen = set()
        for item in search["items"]:
            question_id = item["question_id"]
            if question_id in seen:
                continue
            seen.add(question_id)
            skills = tuple(self._unique_skills(item.get("skills", [])))
            dimension = self._candidate_dimension(skills, dimensions, dimension_weights)
            concepts = self._concepts(item)
            candidates.append(
                _Candidate(
                    question_id=question_id,
                    title=item.get("title", ""),
                    skills=skills,
                    difficulty=item.get("difficulty", "mid"),
                    score=float(item.get("score", 0.0)),
                    match_reasons=tuple(item.get("match_reasons", [])),
                    concepts=frozenset(concepts),
                    dimension=dimension,
                )
            )
        return candidates

    def _select(
        self,
        candidates: Sequence[_Candidate],
        question_count: int,
        dimensions: Sequence[str],
        targets: Dict[str, int],
        dimension_weights: Dict[str, float],
        policy: PlanAssemblyPolicy,
        target_difficulty: str,
    ) -> Tuple[List[_Selection], List[str]]:
        available = list(candidates)
        selected: List[_Selection] = []
        selected_counts: Dict[str, int] = {item: 0 for item in dimensions}
        warnings: List[str] = []

        while available and len(selected) < question_count:
            available_dimensions = [
                dimension
                for dimension in dimensions
                if any(
                    dimension == candidate.dimension or dimension in candidate.skills
                    for candidate in available
                )
            ] or list(dimensions)
            target = max(
                available_dimensions,
                key=lambda item: (
                    (targets[item] - selected_counts.get(item, 0)) / max(1, targets[item]),
                    dimension_weights.get(item, 0.0),
                    -dimensions.index(item),
                ),
            )
            ranked = []
            for candidate in available:
                overlap = self._maximum_overlap(candidate, selected)
                same_skill_limit = selected_counts.get(candidate.dimension, 0) >= policy.max_same_skill_questions
                redundant = bool(selected) and overlap > 0.0 and overlap >= policy.deduplication_threshold
                target_match = target == candidate.dimension or target in candidate.skills
                score = (
                    candidate.score
                    + (0.45 if target_match else 0.0)
                    + 0.15 * self._difficulty_fit(candidate.difficulty, target_difficulty)
                    - 0.50 * overlap
                )
                ranked.append((same_skill_limit or redundant, -score, candidate.question_id, candidate, overlap, same_skill_limit, redundant))
            ranked.sort(key=lambda item: (item[0], item[1], item[2]))
            _, _, _, chosen, overlap, same_skill_limit, redundant = ranked[0]
            relaxed = []
            if same_skill_limit:
                relaxed.append("same_skill_limit")
            if redundant:
                relaxed.append("deduplication")
            if relaxed:
                labels = {
                    "same_skill_limit": "单技能题数上限",
                    "deduplication": "关键点去重阈值",
                }
                warning = "为补足请求题量，已放宽：%s。" % "、".join(
                    labels[item] for item in relaxed
                )
                if warning not in warnings:
                    warnings.append(warning)
            selected.append(
                _Selection(
                    candidate=chosen,
                    target_dimension=target,
                    overlap=overlap,
                    relaxed_constraints=tuple(relaxed),
                )
            )
            selected_counts[chosen.dimension] = selected_counts.get(chosen.dimension, 0) + 1
            available = [item for item in available if item.question_id != chosen.question_id]

        if len(selected) < question_count:
            warnings.append(
                "请求 %s 道题，但候选池只有 %s 道唯一且已索引的题目。"
                % (question_count, len(selected))
            )
        return selected, warnings

    def _order(
        self,
        selections: Sequence[_Selection],
        role: Dict[str, Any],
        policy: PlanAssemblyPolicy,
    ) -> List[_Selection]:
        if not policy.difficulty_curve:
            return list(selections)
        target = role.get("parsed_profile", {}).get("target_difficulty") or role.get("seniority", "mid")
        target_rank = DIFFICULTY_RANK.get(target, 2)
        if target_rank <= 1:
            curve = [1, 2, 3, 4]
        elif target_rank == 2:
            curve = [2, 3, 1, 4]
        elif target_rank == 3:
            curve = [2, 3, 4, 1]
        else:
            curve = [3, 4, 2, 1]
        curve_position = {rank: index for index, rank in enumerate(curve)}
        remaining = list(selections)
        ordered: List[_Selection] = []
        last_dimension = ""
        while remaining:
            remaining.sort(
                key=lambda item: (
                    curve_position.get(DIFFICULTY_RANK.get(item.candidate.difficulty, 2), 9),
                    item.candidate.dimension == last_dimension,
                    -item.candidate.score,
                    item.candidate.question_id,
                )
            )
            chosen = remaining.pop(0)
            ordered.append(chosen)
            last_dimension = chosen.candidate.dimension
        return ordered

    def _slot_drafts(
        self,
        selections: Sequence[_Selection],
        weight_units: Sequence[int],
        minutes: Sequence[int],
        targets: Dict[str, int],
        policy: PlanAssemblyPolicy,
    ) -> List[Dict[str, Any]]:
        slots = []
        covered: Dict[str, int] = {}
        for index, (selection, units, expected_minutes) in enumerate(
            zip(selections, weight_units, minutes),
            start=1,
        ):
            candidate = selection.candidate
            covered[candidate.dimension] = covered.get(candidate.dimension, 0) + 1
            reason_parts = [
                "覆盖 %s（%s/%s）"
                % (
                    candidate.dimension,
                    covered[candidate.dimension],
                    max(1, targets.get(candidate.dimension, 0)),
                ),
                *candidate.match_reasons,
                "难度曲线: %s" % candidate.difficulty,
            ]
            if selection.overlap:
                reason_parts.append("与已选题关键点最大重叠 %.2f" % selection.overlap)
            if selection.relaxed_constraints:
                labels = {
                    "same_skill_limit": "单技能题数上限",
                    "deduplication": "关键点去重阈值",
                }
                reason_parts.append(
                    "为补足题量放宽: %s"
                    % "、".join(labels[item] for item in selection.relaxed_constraints)
                )
            slots.append(
                {
                    "id": new_id("slot_draft"),
                    "order": index,
                    "question_id": candidate.question_id,
                    "dimension": candidate.dimension,
                    "weight": round(units / 10_000, 4),
                    "expected_minutes": expected_minutes,
                    "allow_followup": policy.allow_followups,
                    "selection_reason": "; ".join(reason_parts),
                }
            )
        return slots

    def _summary(
        self,
        *,
        request: PlanAssemblyRequest,
        candidates: Sequence[_Candidate],
        selections: Sequence[_Selection],
        dimensions: Sequence[str],
        targets: Dict[str, int],
        warnings: Sequence[str],
    ) -> Dict[str, Any]:
        selected_counts = {dimension: 0 for dimension in dimensions}
        for selection in selections:
            selected_counts[selection.candidate.dimension] = selected_counts.get(selection.candidate.dimension, 0) + 1
        uncovered = [
            dimension
            for dimension in dimensions
            if selected_counts.get(dimension, 0) < targets.get(dimension, 0)
        ]
        return {
            "requested_question_count": request.question_count,
            "candidate_count": len(candidates),
            "selected_question_count": len(selections),
            "coverage": [
                {
                    "dimension": dimension,
                    "target_count": targets.get(dimension, 0),
                    "selected_count": selected_counts.get(dimension, 0),
                }
                for dimension in dimensions
            ],
            "uncovered_dimensions": uncovered,
            "difficulty_curve": [item.candidate.difficulty for item in selections],
            "warnings": list(warnings),
        }

    def _item_weight_inputs(
        self,
        selections: Sequence[_Selection],
        dimension_weights: Dict[str, float],
    ) -> List[float]:
        counts: Dict[str, int] = {}
        for item in selections:
            counts[item.candidate.dimension] = counts.get(item.candidate.dimension, 0) + 1
        return [
            max(0.0001, dimension_weights.get(item.candidate.dimension, 0.1))
            / counts[item.candidate.dimension]
            * (0.85 + 0.15 * item.candidate.score)
            for item in selections
        ]

    def _allocate_minutes(self, weights: Sequence[int], duration: int) -> List[int]:
        if not weights:
            return []
        total_minutes = max(len(weights), duration)
        base = [1] * len(weights)
        remaining = total_minutes - len(weights)
        extras = self._allocate_units([float(item) for item in weights], remaining)
        return [minimum + extra for minimum, extra in zip(base, extras)]

    def _allocate_units(self, values: Sequence[float], total_units: int) -> List[int]:
        if not values:
            return []
        if total_units <= 0:
            return [0] * len(values)
        total = sum(max(0.0, value) for value in values)
        normalized = [1.0 / len(values)] * len(values) if total <= 0 else [max(0.0, value) / total for value in values]
        exact = [value * total_units for value in normalized]
        units = [int(value) for value in exact]
        remainder = total_units - sum(units)
        order = sorted(range(len(values)), key=lambda index: (-(exact[index] - units[index]), index))
        for index in order[:remainder]:
            units[index] += 1
        return units

    def _maximum_overlap(self, candidate: _Candidate, selected: Sequence[_Selection]) -> float:
        maximum = 0.0
        for item in selected:
            left = candidate.concepts
            right = item.candidate.concepts
            if not left or not right:
                continue
            maximum = max(maximum, len(left.intersection(right)) / len(left.union(right)))
        return maximum

    def _concepts(self, item: Dict[str, Any]) -> FrozenSet[str]:
        key_point_text = " ".join(
            str(key_point.get("text", "")) if isinstance(key_point, dict) else str(key_point)
            for key_point in item.get("key_points", [])
        )
        source = key_point_text or "%s %s" % (item.get("title", ""), item.get("question_text", ""))
        return frozenset(tokenize(source))

    def _candidate_dimension(
        self,
        skills: Sequence[str],
        dimensions: Sequence[str],
        weights: Dict[str, float],
    ) -> str:
        matching = [item for item in skills if item in dimensions]
        if matching:
            return max(matching, key=lambda item: (weights.get(item, 0.0), -dimensions.index(item)))
        return skills[0] if skills else "general"

    def _difficulty_fit(
        self,
        difficulty: str,
        target_difficulty: str,
    ) -> float:
        rank = DIFFICULTY_RANK.get(difficulty, 2)
        target_rank = DIFFICULTY_RANK.get(target_difficulty, 2)
        return 1.0 - abs(rank - target_rank) / 3.0

    def _policy_document(self, policy: PlanAssemblyPolicy) -> Dict[str, Any]:
        return {
            "coverage": list(self._unique_skills(policy.coverage)),
            "allow_followups": policy.allow_followups,
            "max_same_skill_questions": policy.max_same_skill_questions,
            "difficulty_curve": policy.difficulty_curve,
            "deduplication_threshold": policy.deduplication_threshold,
        }

    def _unique_skills(self, values: Sequence[str]) -> List[str]:
        result = []
        for value in values:
            normalized = normalize_skill(str(value))
            if normalized and normalized not in result:
                result.append(normalized)
        return result
