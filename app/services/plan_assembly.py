from dataclasses import dataclass, field
from copy import deepcopy
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.domain.question_selection import QuestionSelection, QuestionSelectionRequest
from app.persistence.errors import ConcurrencyConflict
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.catalog import CatalogService
from app.services.text import normalize_skill, tokenize


DIFFICULTY_RANK = {"junior": 1, "mid": 2, "senior": 3, "expert": 4}


@dataclass(frozen=True)
class PlanAssemblyPolicy:
    coverage: Tuple[str, ...] = ()
    allow_followups: bool = True
    max_same_skill_questions: int = 3
    difficulty_curve: bool = True
    deduplication_threshold: float = 0.72


@dataclass(frozen=True)
class PlanAssemblyRequest:
    role_requirement_id: str
    knowledge_base_ids: Tuple[str, ...] = ()
    question_count: int = 8
    policy: PlanAssemblyPolicy = field(default_factory=PlanAssemblyPolicy)
    job_position_id: Optional[str] = None
    candidate_profile_id: Optional[str] = None
    resume_review_id: Optional[str] = None


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

    async def assemble(
        self,
        request: PlanAssemblyRequest,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        self._validate_request(request)
        with self.persistence.transaction(organization_id) as transaction:
            role = transaction.role_requirements.get(request.role_requirement_id)
        if role is None:
            raise ApiError(
                "ROLE_REQUIREMENT_NOT_FOUND",
                "Role requirement does not exist.",
                status_code=404,
            )
        self._validate_target_scope(request, role, organization_id)
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
                "No indexed questions matched this role requirement and plan policy.",
            )

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
        experience_question_snapshots = self._approved_experience_question_snapshots(
            request.resume_review_id, organization_id
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
            "status": "draft",
            "estimated_minutes": interview_duration,
            "assembly_policy": self._policy_document(request.policy),
            "selection_policy": self._policy_document(request.policy),
            "assembly_summary": summary,
            "bank_slots": bank_slots,
            "experience_question_ids": [item["id"] for item in experience_question_snapshots],
            "experience_question_snapshots": experience_question_snapshots,
            "execution_schema_version": 2,
            "created_at": now,
            "updated_at": now,
        }
        with self.persistence.transaction(organization_id) as transaction:
            current_role = transaction.role_requirements.get(role["id"])
            if current_role is None or current_role["version"] != role["version"]:
                raise ConcurrencyConflict("RoleRequirement changed while the plan was assembled.")
            return transaction.interview_plans.add(plan)

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
            if any(item["job_position_id"] != request.job_position_id for item in knowledge_bases if item):
                raise ApiError("KNOWLEDGE_BASE_POSITION_MISMATCH", "Knowledge base belongs to another position.", status_code=409)
            if any(item["status"] != "ready" for item in knowledge_bases if item):
                raise ApiError("KNOWLEDGE_BASE_NOT_READY", "Every selected knowledge base must be ready.", status_code=409)
            if request.candidate_profile_id and transaction.candidate_profiles.get(request.candidate_profile_id) is None:
                raise ApiError("CANDIDATE_PROFILE_NOT_FOUND", "Candidate profile does not exist.", status_code=404)
            if request.resume_review_id:
                review = transaction.resume_reviews.get(request.resume_review_id)
                if review is None:
                    raise ApiError("RESUME_REVIEW_NOT_FOUND", "Resume review does not exist.", status_code=404)
                if review["job_position_id"] != request.job_position_id or review["candidate_profile_id"] != request.candidate_profile_id:
                    raise ApiError("RESUME_REVIEW_SCOPE_MISMATCH", "Resume review does not match the plan scope.", status_code=409)
                if review["status"] != "ready_for_review":
                    raise ApiError("RESUME_REVIEW_NOT_READY", "Resume review is not ready.", status_code=409)

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
            self.require_execution_v2(plan)

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
                    if not plan.get("approved_at"):
                        plan["approved_at"] = utc_now()
                plan["status"] = new_status

            plan["updated_at"] = utc_now()
            return transaction.interview_plans.update(plan, expected_version=expected_version)

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
        snapshots: List[Dict[str, Any]] = []
        for order, question_id in enumerate(question_ids, start=1):
            question = transaction.experience_questions.get(question_id)
            if question is None:
                raise ApiError("EXPERIENCE_QUESTION_NOT_FOUND", "Experience question does not exist.", status_code=404)
            if question.get("resume_review_id") != plan.get("resume_review_id"):
                raise ApiError("EXPERIENCE_QUESTION_SCOPE_MISMATCH", "Experience question belongs to another review.", status_code=409)
            if question.get("status") != "approved" or question.get("speech_status") != "ready":
                raise ApiError("EXPERIENCE_QUESTION_NOT_READY", "Experience question is not approved and ready.", status_code=409)
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

    def _validate_question_scope(self, plan: Dict[str, Any], question: Dict[str, Any]) -> None:
        if question.get("status") != "active":
            raise ApiError("INTERVIEW_PLAN_QUESTION_INACTIVE", "Plan questions must be active.", status_code=409)
        if plan.get("job_position_id") and question.get("job_position_id") != plan.get("job_position_id"):
            raise ApiError("INTERVIEW_PLAN_QUESTION_SCOPE_MISMATCH", "Plan question belongs to another position.", status_code=409)
        knowledge_base_ids = set(plan.get("knowledge_base_ids", []))
        if knowledge_base_ids and question.get("knowledge_base_id") not in knowledge_base_ids:
            raise ApiError("INTERVIEW_PLAN_QUESTION_SCOPE_MISMATCH", "Plan question belongs to another knowledge base.", status_code=409)

    def _experience_snapshot(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: deepcopy(item.get(key))
            for key in (
                "id",
                "version",
                "question_text",
                "standard_answer",
                "key_points",
                "rubric",
                "speech_asset_id",
                "speech_status",
            )
        }

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
                and item["speech_status"] == "ready"
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
                        transaction.experience_questions.list(), key=lambda value: value["order"]
                    )
                    if item["resume_review_id"] == resume_review_id
                    and item["status"] == "approved"
                    and item["speech_status"] == "ready"
                ),
                start=1,
            ):
                snapshot = self._experience_snapshot(item)
                snapshot["order"] = order
                result.append(snapshot)
            return result

    def _validate_request(self, request: PlanAssemblyRequest) -> None:
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
