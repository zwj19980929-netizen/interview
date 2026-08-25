from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.persistence.errors import ConcurrencyConflict
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.questions import QuestionService
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
        questions: Optional[QuestionService] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.questions = questions or QuestionService(store, persistence=self.persistence)

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
        items = self._items(ordered, weights, minutes, targets, request.policy)
        summary = self._summary(
            request=request,
            candidates=candidates,
            selections=ordered,
            dimensions=dimensions,
            targets=targets,
            warnings=warnings,
        )
        now = utc_now()
        plan = {
            "id": new_id("plan"),
            "organization_id": organization_id,
            "role_requirement_id": role["id"],
            "status": "draft",
            "estimated_minutes": sum(item["expected_minutes"] for item in items),
            "assembly_policy": self._policy_document(request.policy),
            "assembly_summary": summary,
            "items": items,
            "created_at": now,
            "updated_at": now,
        }
        with self.persistence.transaction(organization_id) as transaction:
            current_role = transaction.role_requirements.get(role["id"])
            if current_role is None or current_role["version"] != role["version"]:
                raise ConcurrencyConflict("RoleRequirement changed while the plan was assembled.")
            return transaction.interview_plans.add(plan)

    def _validate_request(self, request: PlanAssemblyRequest) -> None:
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
        search = await self.questions.search_questions(
            {
                "query": role.get("description", ""),
                "filters": {
                    "skills": filter_skills,
                    "knowledge_base_ids": list(request.knowledge_base_ids),
                },
                "limit": min(200, max(20, request.question_count * 6)),
                "include_answer": True,
            },
            organization_id,
        )
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

    def _items(
        self,
        selections: Sequence[_Selection],
        weight_units: Sequence[int],
        minutes: Sequence[int],
        targets: Dict[str, int],
        policy: PlanAssemblyPolicy,
    ) -> List[Dict[str, Any]]:
        items = []
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
            items.append(
                {
                    "id": new_id("plan_item"),
                    "order": index,
                    "question_id": candidate.question_id,
                    "dimension": candidate.dimension,
                    "weight": round(units / 10_000, 4),
                    "expected_minutes": expected_minutes,
                    "allow_followup": policy.allow_followups,
                    "selection_reason": "; ".join(reason_parts),
                }
            )
        return items

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
