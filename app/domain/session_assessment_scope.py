"""Freeze a feasible assessment before the interview; never infer it from answers."""
from collections import Counter
from datetime import datetime
from math import ceil
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import ApiError

VERSION = "session_assessment_scope.v1"
POLICY = "budgeted_core_competencies.v1"
Pair = Tuple[str, str]


class ScopedUnit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question_id: str = Field(min_length=1)
    inquiry_unit_id: str = Field(min_length=1)


class AssessmentScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: str
    policy: str
    selected_units: List[ScopedUnit] = Field(min_length=1, max_length=10)
    source_question_counts: Dict[str, int]
    included_competency_ids: List[str] = Field(min_length=1)
    excluded_competency_ids: List[str]
    available_competency_ids: List[str] = Field(min_length=1)
    estimated_required_seconds: int = Field(ge=1)


class ScopedCompetency(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1)
    weight: float = Field(gt=0, le=1, allow_inf_nan=False)
    required: bool
    min_evidence_roots: int = Field(ge=1, le=5)


class ScopedBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    min_root_questions: int = Field(ge=1, le=10)
    max_root_questions: int = Field(ge=1, le=10)
    max_followups_per_root: int = Field(ge=0, le=3)
    max_total_followups: int = Field(ge=0, le=30)
    max_duration_seconds: int = Field(gt=0)
    closing_reserve_seconds: int = Field(ge=0, le=300)


class ScopedBasis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["question_bank"]
    job_position_id: str = Field(min_length=1)
    position_version: int = Field(ge=1)
    knowledge_base_ids: List[str] = Field(min_length=1)
    position_name: str = ""
    position_description: str = ""


class ScopedInquiryUnit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1)
    question_text: str = Field(min_length=5, max_length=160)
    assessed_rubric_point_ids: List[str] = Field(min_length=1, max_length=1)
    competency_ids: List[str] = Field(min_length=1)
    standard_answer_quote: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)


class ScopedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question_id: str = Field(min_length=1)
    question_version: int = Field(ge=1)
    question_hash: str = Field(min_length=1)
    source_type: Literal["position_bank", "resume_experience"]
    competency_ids: List[str] = Field(min_length=1)
    frozen_question: Dict[str, Any]
    expected_minutes: int = Field(ge=1)
    allow_followup: bool
    inquiry_units: List[ScopedInquiryUnit] = Field(min_length=1)


class SessionAssessmentContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["interview_assessment.v2"]
    role_requirement_id: None
    role_requirement_version: None
    competencies: List[ScopedCompetency] = Field(min_length=1)
    candidate_questions: List[ScopedCandidate] = Field(min_length=1)
    budget: ScopedBudget
    coverage_policy: Literal["session_scope_answered_roots.v1"]
    scoring_policy: Literal["fixed_session_competencies.v1"]
    presentation_policy: Literal["approved_inquiry_units.v1"]
    stop_policy: Literal["bounded_evidence.v1"]
    assessment_basis: ScopedBasis
    assessment_scope: AssessmentScope
    contract_hash: str = Field(min_length=1)


def scope_pairs(contract: Dict[str, Any]) -> Optional[Set[Pair]]:
    scope = contract.get("assessment_scope")
    if not scope:
        return None
    return {(item["question_id"], item["inquiry_unit_id"]) for item in scope["selected_units"]}


def scope_window_check(contract, start, end):
    """The same frozen duration applies at scheduling, readiness and commit."""
    if not contract or contract.get('schema_version') != 'interview_assessment.v2':
        return None
    from app.domain.adaptive_interview import validate_assessment_contract
    validate_assessment_contract(contract)
    def instant(value):
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ApiError('DATETIME_INVALID', 'Datetime must include a timezone.')
        return result
    required = contract['assessment_scope']['estimated_required_seconds'] + contract['budget']['closing_reserve_seconds']
    available = max(0, int((instant(end) - instant(start)).total_seconds()))
    return {'required_seconds': required, 'available_seconds': available, 'ready': available >= required}


def require_scope_window(contract, start, end, *, starting=False):
    check = scope_window_check(contract, start, end)
    if check is not None and not check['ready']:
        raise ApiError('ASSESSMENT_START_TIME_INSUFFICIENT' if starting else 'ASSESSMENT_APPOINTMENT_WINDOW_TOO_SHORT',
            '剩余预约时间不足以完成本场已确定的考察范围，请联系面试官改期。' if starting
            else '预约时长不足以完成本场已确定的考察范围，请延长预约或重新准备更短的计划。',
            status_code=422, details={key: check[key] for key in ('required_seconds', 'available_seconds')})


def scope_allows_followup(session, available_seconds, minimum_followup_seconds=90):
    """Optional depth cannot consume the time reserved for unasked core units."""
    contract = session.get('plan_snapshot', {}).get('assessment_contract') or {}
    if contract.get('schema_version') != 'interview_assessment.v2':
        return True
    from app.domain.adaptive_interview import validate_assessment_contract
    validate_assessment_contract(contract)
    selected = {(turn.get('question_id'), turn.get('inquiry_unit_id'))
                for turn in session.get('turns', []) if not turn.get('is_followup')}
    pending = scope_pairs(contract) - selected
    reserved = sum(row['seconds'] for row in unit_rows(contract['candidate_questions'])
                   if (row['question_id'], row['inquiry_unit_id']) in pending)
    return available_seconds >= reserved + minimum_followup_seconds


def unit_rows(candidates):
    return sorted([{"question_id": candidate["question_id"], "inquiry_unit_id": unit["id"],
                    "source_type": candidate["source_type"], "competency_ids": unit["competency_ids"],
                    # Includes a planning allowance for delivery, confirmation
                    # and candidate thinking, rather than only answer playback.
                    "seconds": max(180, ceil(candidate["expected_minutes"] / len(candidate["inquiry_units"])) * 60)}
                   for candidate in candidates for unit in candidate.get("inquiry_units", [])],
                  key=lambda row: (row["question_id"], row["inquiry_unit_id"]))


def freeze_session_scope(candidates, weights, maximum, available_seconds, evidence_minimum):
    rows = unit_rows(candidates)
    available_ids = sorted(weights)
    supply = Counter(name for row in rows for name in row["competency_ids"])
    # A short appointment may narrow the skill scope; it cannot weaken the
    # evidence threshold of a skill that has sufficient approved material.
    needs = {name: min(evidence_minimum, supply[name]) for name in weights if supply[name]}
    source_supply = Counter(row["source_type"] for row in rows)
    resume_needs = max((needs[name] for row in rows if row['source_type'] == 'resume_experience'
                        for name in row['competency_ids'] if name in needs), default=0)
    if source_supply['position_bank'] and resume_needs > 3:
        raise ApiError('ASSESSMENT_COVERAGE_BUDGET_INVALID', '简历能力的最低证据要求超过本场简历主题名额，请调整准备策略。', status_code=422)
    resume_target = min(3, max(2, resume_needs, maximum // 4), source_supply["resume_experience"]) if maximum >= 4 else 0
    limits = {"resume_experience": resume_target, "position_bank": maximum - resume_target}
    if not source_supply["position_bank"]:
        limits = {"resume_experience": maximum, "position_bank": 0}
    elif not source_supply["resume_experience"]:
        limits = {"resume_experience": 0, "position_bank": maximum}
    # Short source supply may transfer slots, but never increases total size.
    for source in ("position_bank", "resume_experience"):
        other = "resume_experience" if source == "position_bank" else "position_bank"
        excess = max(0, limits[source] - source_supply[source])
        limits[source] -= excess
        limits[other] += excess
    selected = []
    selected_ids = set()

    def counts(items):
        return Counter(name for row in items for name in row["competency_ids"])

    def fits(items):
        sources = Counter(row["source_type"] for row in items)
        return (len(items) <= maximum and sum(row["seconds"] for row in items) <= available_seconds
                and all(count <= limits.get(source, 0) for source, count in sources.items()))

    while len(selected) < maximum:
        observed = counts(selected)
        core = {name for name, minimum in needs.items() if observed[name] >= minimum}
        options = []
        for name in available_ids:
            if name in core:
                continue
            missing = needs.get(name, 0) - observed[name]
            if missing <= 0:
                continue
            eligible = [row for row in rows if name in row['competency_ids']
                        and (row['question_id'], row['inquiry_unit_id']) not in selected_ids]
            # Prefer missing resume slots, then diverse source questions and
            # shorter delivery. Every resulting bundle still passes quotas.
            used_sources = Counter(row['source_type'] for row in selected)
            eligible.sort(key=lambda row: (
                not (row['source_type'] == 'resume_experience' and used_sources['resume_experience'] < resume_target),
                row['question_id'] in {item['question_id'] for item in selected}, row['seconds'],
                row['question_id'], row['inquiry_unit_id']))
            bundle = []
            for row in eligible:
                if fits(selected + bundle + [row]):
                    bundle.append(row)
                if len(bundle) == missing:
                    break
            if len(bundle) != missing:
                continue
            projected = counts(selected + bundle)
            gained = {key for key, minimum in needs.items() if projected[key] >= minimum} - core
            gain = sum(weights[key] for key in gained)
            resume_gain = min(resume_target - used_sources['resume_experience'],
                              sum(row['source_type'] == 'resume_experience' for row in bundle))
            options.append(((-bool(resume_gain), -gain / len(bundle), sum(row['seconds'] for row in bundle), name), bundle))
        if not options:
            break
        _, bundle = min(options, key=lambda option: option[0])
        selected += bundle
        selected_ids.update((row['question_id'], row['inquiry_unit_id']) for row in bundle)

    observed = counts(selected)
    core = {name for name, minimum in needs.items() if observed[name] >= minimum}
    # Extra questions may reinforce the frozen core; do not introduce a new
    # compulsory label whose minimum evidence cannot fit in the last slot.
    for row in sorted(rows, key=lambda row: (-sum(weights[name] for name in row['competency_ids'] if name in core),
                                             row['seconds'], row['question_id'], row['inquiry_unit_id'])):
        identity = (row['question_id'], row['inquiry_unit_id'])
        if identity not in selected_ids and core.intersection(row['competency_ids']) and fits(selected + [row]):
            selected.append(row)
            selected_ids.add(identity)
    if not selected or any(not core.intersection(row['competency_ids']) for row in selected):
        raise ApiError('ASSESSMENT_COVERAGE_BUDGET_INVALID', '题数或时长不足以完成核心能力的最低证据要求，请调整准备范围。', status_code=422)
    observed = counts(selected)
    core = {name for name, minimum in needs.items() if observed[name] >= minimum}
    total_weight = sum(weights[name] for name in core)
    competencies = [{'id': name, 'weight': weights[name] / total_weight, 'required': True,
                     'min_evidence_roots': needs[name]} for name in sorted(core)]
    selected.sort(key=lambda row: (row['source_type'], row['question_id'], row['inquiry_unit_id']))
    sources = Counter(row['source_type'] for row in selected)
    scope = {'schema_version': VERSION, 'policy': POLICY,
             'selected_units': [{'question_id': row['question_id'], 'inquiry_unit_id': row['inquiry_unit_id']} for row in selected],
             'source_question_counts': dict(sorted(sources.items())),
             'included_competency_ids': sorted(core), 'excluded_competency_ids': sorted(set(available_ids) - core),
             'available_competency_ids': available_ids,
             'estimated_required_seconds': sum(row['seconds'] for row in selected)}
    return competencies, scope


def validate_session_scope(contract):
    try:
        SessionAssessmentContract.model_validate(contract)
        scope = AssessmentScope.model_validate(contract.get('assessment_scope')).model_dump()
        if scope['schema_version'] != VERSION or scope['policy'] != POLICY:
            raise ValueError('unsupported scope')
        if scope['source_question_counts'].keys() - {'position_bank', 'resume_experience'}:
            raise ValueError('unknown source')
        if any(isinstance(value, bool) or value < 1 for value in scope['source_question_counts'].values()):
            raise ValueError('invalid source count')
        core = scope['included_competency_ids']
        available = scope['available_competency_ids']
        excluded = scope['excluded_competency_ids']
        for items in (core, available, excluded):
            if items != sorted(set(items)) or any(not item.strip() for item in items):
                raise ValueError('invalid scope labels')
        if set(core) & set(excluded) or set(core) | set(excluded) != set(available):
            raise ValueError('scope partition changed')
        if core != sorted(item['id'] for item in contract['competencies']):
            raise ValueError('core mismatch')
        approved = {(row['question_id'], row['inquiry_unit_id']): row for row in unit_rows(contract['candidate_questions'])}
        if set(available) != {name for row in approved.values() for name in row['competency_ids']}:
            raise ValueError('available skills differ from approved pool')
        identities = [(item['question_id'], item['inquiry_unit_id']) for item in scope['selected_units']]
        if len(identities) != len(set(identities)) or any(identity not in approved for identity in identities):
            raise ValueError('unapproved or duplicate selection')
        selected = [approved[identity] for identity in identities]
        if any(not set(core).intersection(row['competency_ids']) for row in selected):
            raise ValueError('unscored selected question')
        if dict(Counter(row['source_type'] for row in selected)) != scope['source_question_counts']:
            raise ValueError('source quota mismatch')
        budget = contract['budget']
        if not (budget['min_root_questions'] == budget['max_root_questions'] == len(selected)):
            raise ValueError('scope and root budget differ')
        cost = sum(row['seconds'] for row in selected)
        if cost != scope['estimated_required_seconds'] or cost + budget['closing_reserve_seconds'] > budget['max_duration_seconds']:
            raise ValueError('scope cannot fit time budget')
        evidence = Counter(name for row in selected for name in row['competency_ids'])
        if any(not item['required'] or evidence[item['id']] < item['min_evidence_roots'] for item in contract['competencies']):
            raise ValueError('scope lacks required evidence')
    except (ValueError, TypeError, KeyError) as error:
        raise ApiError('ASSESSMENT_CONTRACT_INVALID', 'The frozen session assessment scope is invalid.', status_code=409) from error
