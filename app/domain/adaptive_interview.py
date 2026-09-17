"""Frozen assessment contract, bounded question selection and coverage accounting.

The module consumes trusted approved documents and has no model, persistence or
media dependency. A supervisor proposes IDs; the lifecycle commits these facts.
"""
from copy import deepcopy
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from math import ceil, isfinite
from time import monotonic
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.errors import ApiError
from app.core.ids import new_id


Document = Dict[str, Any]
_COVERAGE_MAX_SEARCH_NODES = 10000
_COVERAGE_MAX_SEARCH_SECONDS = 0.15


class AdaptiveInterviewPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    min_root_questions: Optional[int] = Field(default=None, ge=1, le=50)
    max_root_questions: Optional[int] = Field(default=None, ge=1, le=50)
    max_followups_per_root: int = Field(default=1, ge=0, le=3)
    max_total_followups: int = Field(default=8, ge=0, le=30)
    closing_reserve_seconds: int = Field(default=30, ge=0, le=300)
    min_evidence_units_per_competency: int = Field(default=2, ge=1, le=5)

    @model_validator(mode="after")
    def ordered_budget(self):
        if (self.min_root_questions is not None and self.max_root_questions is not None
                and self.min_root_questions > self.max_root_questions):
            raise ValueError("min_root_questions must not exceed max_root_questions")
        return self


def _invalid(message: str, code: str = "ASSESSMENT_CONTRACT_INVALID") -> None:
    raise ApiError(code, message, status_code=409)


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def is_adaptive(session: Document) -> bool:
    return (session.get("execution_schema_version") == 3
            or session.get("plan_snapshot", {}).get("execution_schema_version") == 3)


def assessment_contract(session: Document) -> Document:
    contract = session.get("plan_snapshot", {}).get("assessment_contract") or session.get("assessment_contract")
    if not isinstance(contract, dict):
        _invalid("Adaptive interview requires a frozen assessment contract.")
    validate_assessment_contract(contract)
    return contract


def _minimum_evidence_units(competencies: List[Document], candidates: List[Document]) -> int:
    """Exact bounded multicover; never present a greedy upper bound as a minimum."""
    indexes = {item["id"]: index for index, item in enumerate(competencies)}
    needs = tuple(item["min_evidence_roots"] for item in competencies)
    counts = Counter(tuple(sorted(indexes[name] for name in unit["competency_ids"] if name in indexes))
                     for candidate in candidates for unit in candidate.get("inquiry_units", []))
    counts.pop((), None)
    groups = sorted(counts)
    available = tuple(min(counts[group], max(needs)) for group in groups)
    greedy_needs, greedy_available = list(needs), list(available)
    upper = 0
    while any(greedy_needs):
        best = max(range(len(groups)), key=lambda index: sum(greedy_needs[name] > 0 for name in groups[index])
                   if greedy_available[index] else 0)
        if not greedy_available[best] or not any(greedy_needs[name] for name in groups[best]):
            _invalid("The approved units cannot satisfy the frozen competency evidence requirements.")
        for name in groups[best]:
            greedy_needs[name] = max(0, greedy_needs[name] - 1)
        greedy_available[best] -= 1
        upper += 1
    lower = max(max(needs), ceil(sum(needs) / max(len(group) for group in groups)))
    started, explored = monotonic(), 0

    def feasible(remaining, supply, slots, failed):
        nonlocal explored
        explored += 1
        if explored > _COVERAGE_MAX_SEARCH_NODES or monotonic() - started > _COVERAGE_MAX_SEARCH_SECONDS:
            raise ApiError("ASSESSMENT_COVERAGE_SEARCH_LIMIT",
                "能力映射组合较复杂，未能在规划计算预算内确认最低考察数量；请缩小候选范围或简化单元能力映射后重新生成。",
                status_code=422, details={"search_node_limit": _COVERAGE_MAX_SEARCH_NODES,
                                         "search_time_limit_seconds": _COVERAGE_MAX_SEARCH_SECONDS})
        if not any(remaining):
            return True
        if max(remaining) > slots:
            return False
        state = (remaining, supply, slots)
        if state in failed:
            return False
        useful = [index for index, count in enumerate(supply) if count
                  and any(remaining[name] for name in groups[index])]
        gain = max((sum(remaining[name] > 0 for name in groups[index]) for index in useful), default=0)
        if not gain or sum(remaining) > slots * gain:
            return False
        choices = {name: [index for index in useful if name in groups[index]]
                   for name, count in enumerate(remaining) if count}
        if any(sum(supply[index] for index in options) < remaining[name] for name, options in choices.items()):
            return False
        # Every feasible solution must choose a unit containing this least
        # flexible outstanding competency. Equal mappings share one supply slot.
        constrained = min(choices, key=lambda name: len(choices[name]))
        for index in sorted(choices[constrained],
                            key=lambda item: sum(remaining[name] > 0 for name in groups[item]), reverse=True):
            next_needs, next_supply = list(remaining), list(supply)
            for name in groups[index]:
                next_needs[name] = max(0, next_needs[name] - 1)
            next_supply[index] -= 1
            if feasible(tuple(next_needs), tuple(next_supply), slots - 1, failed):
                return True
        failed.add(state)
        return False

    for target in range(lower, upper):
        if feasible(needs, available, target, set()):
            return target
    return upper


def freeze_assessment_contract(
    *, role: Document, candidates: List[Document], dimension_weights: Dict[str, float],
    question_count: int, duration_minutes: int, policy: Optional[Document] = None,
    allow_followups: bool = True,
    assessment_basis: Optional[Document] = None,
) -> Document:
    try:
        settings = AdaptiveInterviewPolicy.model_validate(policy or {})
    except ValueError as error:
        raise ApiError("ASSESSMENT_POLICY_INVALID", "Adaptive interview budget is invalid.", status_code=422) from error
    if not candidates:
        _invalid("The approved candidate pool cannot be empty.")
    has_units = any(candidate.get("inquiry_units") for candidate in candidates)
    available_count = sum(len(candidate.get("inquiry_units") or [None]) for candidate in candidates)
    maximum = settings.max_root_questions or min(question_count, available_count)
    minimum = settings.min_root_questions or min(maximum, max(1, len(dimension_weights)))
    if maximum > available_count or minimum > maximum:
        _invalid("The root question budget exceeds the available approved pool.")
    if duration_minutes * 60 <= settings.closing_reserve_seconds:
        _invalid("The assessment must leave positive time after the closing reserve.")
    weights = {name: float(weight) for name, weight in dimension_weights.items() if float(weight) > 0}
    if not weights or any(not isfinite(value) for value in weights.values()):
        _invalid("Competencies need finite positive weights.")
    total = sum(weights.values())
    competency_unit_counts = {name: sum(1 for candidate in candidates for unit in candidate.get("inquiry_units", [])
                                        if name in unit.get("competency_ids", [])) for name in weights}
    missing_unit_coverage = [name for name, count in competency_unit_counts.items() if count == 0]
    if has_units and missing_unit_coverage:
        raise ApiError("INQUIRY_COMPETENCY_COVERAGE_MISSING", "批准单元未覆盖所有必要能力，请调整题池并重新生成计划。",
                       status_code=422, details={"missing_competency_ids": sorted(missing_unit_coverage)})
    competencies = [{"id": name, "weight": weight / total, "required": True,
                     "min_evidence_roots": min(settings.min_evidence_units_per_competency,
                        competency_unit_counts[name]) if has_units else 1}
                    for name, weight in sorted(weights.items())]
    if has_units:
        needed = _minimum_evidence_units(competencies, candidates)
        if assessment_basis is not None:
            # Preparation derives a feasible budget; users do not have to solve
            # the rubric multicover or guess how many source questions split.
            maximum = min(50, available_count, max(needed, duration_minutes // 3))
        if needed > maximum:
            raise ApiError("ASSESSMENT_COVERAGE_BUDGET_INVALID",
                "当前能力范围至少需要 %s 个考察单元，题数上限为 %s；请增加上限或调整每能力最低证据单元数。" % (needed, maximum),
                status_code=422, details={"minimum_required_root_questions": needed, "max_root_questions": maximum,
                                         "min_evidence_units_per_competency": settings.min_evidence_units_per_competency})
        minimum = max(minimum, needed)
    contract = {
        "schema_version": "interview_assessment.v1", "role_requirement_id": role["id"],
        "role_requirement_version": role["version"], "competencies": competencies,
        "candidate_questions": deepcopy(candidates),
        "budget": {"min_root_questions": minimum, "max_root_questions": maximum,
                   "max_followups_per_root": settings.max_followups_per_root if allow_followups else 0,
                   "max_total_followups": settings.max_total_followups if allow_followups else 0,
                   "max_duration_seconds": duration_minutes * 60,
                   "closing_reserve_seconds": settings.closing_reserve_seconds},
        "coverage_policy": "answered_roots.v1", "scoring_policy": "fixed_competencies.v1",
        "presentation_policy": "approved_inquiry_units.v1" if has_units else "complete_approved_question.v1",
        "stop_policy": "bounded_evidence.v1",
    }
    if assessment_basis is not None:
        contract["assessment_basis"] = deepcopy(assessment_basis)
    contract["contract_hash"] = _hash(contract)
    validate_assessment_contract(contract)
    return contract


def validate_assessment_contract(contract: Document) -> None:
    if contract.get("schema_version") != "interview_assessment.v1":
        _invalid("Unsupported assessment contract version.")
    body = {key: value for key, value in contract.items() if key != "contract_hash"}
    if contract.get("contract_hash") != _hash(body):
        _invalid("Frozen assessment contract content has changed.")
    basis = contract.get("assessment_basis")
    if basis is not None:
        if (basis.get("kind") != "question_bank" or not basis.get("job_position_id")
                or not isinstance(basis.get("position_version"), int)
                or not basis.get("knowledge_base_ids") or contract.get("role_requirement_id") is not None):
            _invalid("Question bank preparation needs a frozen position and bank basis.")
    competencies = contract.get("competencies") or []
    ids = [item.get("id") for item in competencies]
    if not ids or len(set(ids)) != len(ids) or not all(isinstance(item, str) and item.strip() for item in ids):
        _invalid("Competency identities must be nonempty and unique.")
    if abs(sum(float(item["weight"]) for item in competencies) - 1) > 0.00001:
        _invalid("Frozen competency weights must total one.")
    candidates = contract.get("candidate_questions") or []
    question_ids = [item.get("question_id") for item in candidates]
    if not candidates or len(set(question_ids)) != len(question_ids):
        _invalid("Approved candidate question identities must be unique.")
    available = set()
    for candidate in candidates:
        mapped = candidate.get("competency_ids") or []
        if not mapped or not set(mapped).issubset(ids):
            _invalid("Every approved question must map to frozen competencies.")
        question = candidate.get("frozen_question") or {}
        if (question.get("id") != candidate.get("question_id") or not question.get("version")
                or not str(question.get("question_text") or "").strip()
                or not str(question.get("standard_answer") or "").strip() or not question.get("key_points")):
            _invalid("Approved question snapshots must contain a complete scoring contract.")
        if candidate.get("question_hash") != _hash(question):
            _invalid("Approved question content hash does not match.")
        if contract.get("presentation_policy") == "approved_inquiry_units.v1":
            units = candidate.get("inquiry_units") or []
            point_ids = [point["id"] if isinstance(point, dict) else str(point) for point in question["key_points"]]
            covered = []
            unit_ids = []
            for unit in units:
                if (not isinstance(unit.get("question_text"), str) or not 5 <= len(unit["question_text"].strip()) <= 160
                        or unit["question_text"].count("?") + unit["question_text"].count("？") > 1
                        or "\n" in unit["question_text"] or "\r" in unit["question_text"]
                        or len(unit.get("assessed_rubric_point_ids", [])) != 1
                        or not set(unit["assessed_rubric_point_ids"]).issubset(point_ids)
                        or not unit.get("competency_ids") or not set(unit["competency_ids"]).issubset(mapped)
                        or len(unit["competency_ids"]) != len(set(unit["competency_ids"]))
                        or not str(unit.get("standard_answer_quote") or "").strip()
                        or unit["standard_answer_quote"] not in question["standard_answer"]
                        or unit.get("content_hash") != _hash({key: value for key, value in unit.items() if key != "content_hash"})):
                    _invalid("Approved inquiry unit text, evidence scope or answer reference is invalid.")
                covered.extend(unit["assessed_rubric_point_ids"])
                unit_ids.append(unit.get("id"))
                available.update(unit["competency_ids"])
            if sorted(covered) != sorted(point_ids) or len(unit_ids) != len(set(unit_ids)) or not all(unit_ids):
                _invalid("Approved inquiry units must partition each original rubric point exactly once.")
        else:
            available.update(mapped)
    if any(item.get("required") and item["id"] not in available for item in competencies):
        _invalid("Every required competency needs an approved candidate question.")


def frozen_candidate(question: Document, *, competency_ids: List[str], source_type: str,
                     expected_minutes: int = 3, allow_followup: bool = True) -> Document:
    allowed_fields = ("id", "version", "title", "question_text", "standard_answer", "key_points", "rubric",
                      "followup_probes", "difficulty", "type", "skills", "speech_asset_id", "speech_status",
                      "evidence_refs")
    frozen = {key: deepcopy(question[key]) for key in allowed_fields if key in question}
    return {"question_id": question["id"], "question_version": question["version"],
            "question_hash": _hash(frozen), "source_type": source_type,
            "competency_ids": list(dict.fromkeys(competency_ids)), "frozen_question": frozen,
            "expected_minutes": max(1, int(expected_minutes)), "allow_followup": allow_followup}


def initialize_adaptive_snapshot(plan: Document) -> Document:
    contract = deepcopy(plan.get("assessment_contract") or {})
    validate_assessment_contract(contract)
    return {"execution_schema_version": 3, "assessment_contract": contract,
            "question_snapshots": [], "question_selections": []}


def root_turns(session: Document) -> List[Document]:
    return [item for item in session.get("turns", []) if not item.get("is_followup")]


def coverage_summary(session: Document) -> Document:
    contract = assessment_contract(session)
    answer_turn_ids = {answer["turn_id"] for answer in session.get("answers", [])}
    answered = [turn for turn in root_turns(session) if turn["id"] in answer_turn_ids]
    dimensions = []
    for competency in contract["competencies"]:
        evidence = [turn["id"] for turn in answered if competency["id"] in turn.get("competency_ids", [])]
        sufficient = len(evidence) >= competency["min_evidence_roots"]
        dimensions.append({"competency_id": competency["id"], "evidence_root_turn_ids": evidence,
                           "evidence_status": "sufficient" if sufficient else "insufficient_evidence"})
    missing = [row["competency_id"] for row, competency in zip(dimensions, contract["competencies"])
               if competency["required"] and row["evidence_status"] != "sufficient"]
    minimum = contract["budget"]["min_root_questions"]
    reasons = (["required_competencies"] if missing else []) + (["minimum_root_count"] if len(answered) < minimum else [])
    return {"answered_root_count": len(answered), "selected_root_count": len(root_turns(session)),
            "required_root_count": minimum, "insufficient_reasons": reasons,
            "competencies": dimensions, "missing_required_competencies": missing,
            "sufficient": not reasons}


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def budget_summary(session: Document, now: str) -> Document:
    contract = assessment_contract(session)
    budget = contract["budget"]
    elapsed = max(0, (_instant(now) - _instant(session["started_at"])).total_seconds()) if session.get("started_at") else 0
    available = max(0, budget["max_duration_seconds"] - budget["closing_reserve_seconds"] - elapsed)
    if session.get("scheduled_end_at"):
        available = min(available, max(0, (_instant(session["scheduled_end_at"]) - _instant(now)).total_seconds()
                                       - budget["closing_reserve_seconds"]))
    roots = root_turns(session)
    selected = {(turn.get("question_id"), turn.get("inquiry_unit_id")) for turn in roots}
    closed = closed_source_question_ids(session)
    remaining_units = sum(1 for candidate in contract["candidate_questions"] if candidate["question_id"] not in closed
                          for unit in candidate.get("inquiry_units") or [{"id": None}]
                          if (candidate["question_id"], unit["id"]) not in selected)
    return {"remaining_seconds": max(0, int(available)),
            "remaining_root_questions": max(0, budget["max_root_questions"] - len(root_turns(session))),
            "remaining_eligible_units": remaining_units,
            "exhausted": available <= 0 or len(root_turns(session)) >= budget["max_root_questions"] or remaining_units == 0}


def source_topic_closed(turn: Document) -> bool:
    understanding = turn.get("current_understanding") or {}
    return (understanding.get("turn_intent") in {"decline_topic", "finish_topic", "stop_interview"}
            or understanding.get("intent") == "answer_declined" or turn.get("status") == "skipped")


def closed_source_question_ids(session: Document) -> set:
    """A refusal on any follow-up closes its original source's remaining units."""
    turns = {turn["id"]: turn for turn in session.get("turns", [])}
    closed = set()
    for turn in turns.values():
        if source_topic_closed(turn):
            root = turns.get(turn.get("root_turn_id"), turn)
            if root.get("question_id"):
                closed.add(root["question_id"])
    return closed


def validate_decision(session: Document, payload: Document, kind: str) -> bool:
    """Return False for an exact replay; stale/conflicting decisions fail closed."""
    identity = payload.get("decision_id")
    revision = payload.get("expected_decision_revision")
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 200:
        _invalid("A bounded decision_id is required.", "AGENT_DECISION_INVALID")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        _invalid("expected_decision_revision is required.", "AGENT_DECISION_INVALID")
    fingerprint = _hash({"kind": kind, "payload": payload})
    prior = next((item for item in session.get("adaptive_decision_receipts", []) if item["decision_id"] == identity), None)
    if prior:
        if prior["request_hash"] != fingerprint:
            _invalid("A decision identity cannot be reused for a different request.", "AGENT_DECISION_CONFLICT")
        return False
    if revision != int(session.get("decision_revision", 0)):
        _invalid("The interview decision context has changed.", "AGENT_DECISION_STALE")
    return True


def record_decision(session: Document, payload: Document, kind: str, now: str) -> None:
    revision = int(session.get("decision_revision", 0)) + 1
    session["decision_revision"] = revision
    receipt = {
        "decision_id": payload["decision_id"], "request_hash": _hash({"kind": kind, "payload": payload}),
        "kind": kind, "revision": revision, "committed_at": now,
        "question_id": payload.get("question_id"), "inquiry_unit_id": payload.get("inquiry_unit_id"),
        "reason": payload.get("reason"),
    }
    planning = session.get("agent_runtime", {}).get("last_planning") or {}
    if planning.get("decision_id") == payload["decision_id"]:
        receipt.update({key: deepcopy(planning[key]) for key in
                        ("reason_code", "tools_used", "model_calls", "prompt_version", "context_hash") if key in planning})
    session.setdefault("adaptive_decision_receipts", []).append(receipt)


def append_adaptive_question(session: Document, question_id: str, decision_id: str, now: str,
                             inquiry_unit_id: Optional[str] = None) -> Document:
    """Mutate the lifecycle's isolated copy and return its newly approved root."""
    contract = assessment_contract(session)
    if session.get("status") != "in_progress" or session.get("candidate_input_completed_at"):
        _invalid("This interview cannot select another question.", "ADAPTIVE_SELECTION_NOT_ALLOWED")
    if session.get("current_turn_id") or any(turn.get("status") in {"asking", "transcribing", "pending"}
                                            for turn in session.get("turns", [])):
        _invalid("A question is already active.", "ADAPTIVE_SELECTION_NOT_ALLOWED")
    if budget_summary(session, now)["exhausted"]:
        _invalid("The frozen assessment budget is exhausted.", "ASSESSMENT_BUDGET_EXHAUSTED")
    candidate = next((item for item in contract["candidate_questions"] if item["question_id"] == question_id), None)
    if candidate is None:
        _invalid("The question does not belong to the frozen approved pool.", "ADAPTIVE_QUESTION_NOT_APPROVED")
    question = candidate["frozen_question"]
    previous = [turn for turn in root_turns(session) if turn.get("question_id") == question_id]
    if question_id in closed_source_question_ids(session):
        _invalid("The candidate has closed this source question's topic.", "ADAPTIVE_TOPIC_CLOSED")
    unit = None
    if candidate.get("inquiry_units"):
        unit = next((item for item in candidate["inquiry_units"] if item["id"] == inquiry_unit_id), None)
        if unit is None:
            _invalid("An approved inquiry_unit_id is required.", "ADAPTIVE_INQUIRY_UNIT_NOT_APPROVED")
    elif inquiry_unit_id is not None:
        _invalid("This contract has no selectable inquiry units.", "ADAPTIVE_INQUIRY_UNIT_NOT_APPROVED")
    if any(turn.get("inquiry_unit_id") == inquiry_unit_id for turn in previous):
        _invalid("This approved question scope has already been selected.", "ADAPTIVE_QUESTION_ALREADY_SELECTED")
    snapshot_id, turn_id = new_id("question_snapshot"), new_id("turn")
    full_scope = [point["id"] if isinstance(point, dict) else str(point) for point in question["key_points"]]
    scope = deepcopy(unit["assessed_rubric_point_ids"]) if unit else full_scope
    scoped_points = [point for point in question["key_points"] if (point["id"] if isinstance(point, dict) else str(point)) in scope]
    presented = [unit["id"]] if unit else ["complete_question"]
    spoken = unit["question_text"] if unit else question["question_text"]
    mapped_competencies = deepcopy(unit["competency_ids"] if unit else candidate["competency_ids"])
    minutes = candidate["expected_minutes"]
    if unit:
        minutes = max(1, (minutes + len(candidate["inquiry_units"]) - 1) // len(candidate["inquiry_units"]))
    snapshot = {
        "id": snapshot_id, "source_question_id": question_id, "source_question_version": question["version"],
        "source_type": candidate["source_type"], "title": question.get("title") or "面试题",
        "question_text": spoken, "spoken_text": spoken,
        "standard_answer": unit["standard_answer_quote"] if unit else question["standard_answer"], "key_points": deepcopy(scoped_points),
        "rubric": deepcopy(question.get("rubric", {})), "followup_probes": [] if unit else deepcopy(question.get("followup_probes", [])),
        "difficulty": question.get("difficulty", "mid"), "type": question.get("type", "technical"),
        "skills": deepcopy(question.get("skills", [])), "speech_asset_id": None if unit else question.get("speech_asset_id"),
        "assessed_rubric_point_ids": scope, "presented_unit_ids": presented, "created_at": now,
        "inquiry_unit_id": inquiry_unit_id, "source_rubric_point_ids": full_scope,
        "not_assessed_rubric_point_ids": [point for point in full_scope if point not in scope],
        "source_question_text": question["question_text"],
    }
    if unit:
        # A long question's prose rubric/probes can contain additional asks.
        # Keep numeric scoring dimensions; the sole knowledge criterion is the
        # approved unit's scoped key point and exact answer reference.
        snapshot["rubric"] = {key: value for key, value in question.get("rubric", {}).items()
                              if isinstance(value, (int, float)) and not isinstance(value, bool)}
    order = max((item["order"] for item in session.get("turns", [])), default=0) + 1
    turn = {"id": turn_id, "interview_id": session["id"], "turn_blueprint_id": "adaptive_" + decision_id,
            "question_id": question_id, "question_snapshot_id": snapshot_id, "question_snapshot": snapshot,
            "order": order, "phase": candidate["source_type"], "is_followup": False,
            "parent_turn_id": None, "root_turn_id": turn_id, "followup_depth": 0, "followup_reason": None,
            "target_key_points": [], "allow_followup": candidate["allow_followup"] and contract["budget"]["max_followups_per_root"] > 0,
            "weight": 1.0, "expected_minutes": minutes, "status": "asking",
            "deferred_speech": False, "question_spoken_text": snapshot["spoken_text"], "started_at": now,
            "completed_at": None, "utterances": [], "current_understanding": None, "conversation_acts": [],
            "competency_ids": mapped_competencies, "assessed_rubric_point_ids": scope,
            "presented_unit_ids": presented, "inquiry_unit_id": inquiry_unit_id}
    session.setdefault("turns", []).append(turn)
    session.setdefault("turn_ids", []).append(turn_id)
    selection = {"id": new_id("selection"), "decision_id": decision_id, "question_id": question_id,
                 "question_version": question["version"], "question_hash": candidate["question_hash"],
                 "contract_hash": contract["contract_hash"], "competency_ids": deepcopy(mapped_competencies),
                 "selected_at": now, "strategy": "interviewer_supervisor.v1", "inquiry_unit_id": inquiry_unit_id}
    session.setdefault("question_selections", []).append(selection)
    plan = session["plan_snapshot"]
    plan.setdefault("question_selections", []).append(deepcopy(selection))
    plan.setdefault("question_snapshots", []).append({"question_snapshot_id": snapshot_id, "question_id": question_id,
        "order": order, "weight": 1.0, "dimension": mapped_competencies[0],
        "competency_ids": deepcopy(mapped_competencies), "assessed_rubric_point_ids": scope,
        "presented_unit_ids": presented, "expected_minutes": minutes,
        "inquiry_unit_id": inquiry_unit_id, "source_rubric_point_ids": full_scope,
        "not_assessed_rubric_point_ids": [point for point in full_scope if point not in scope]})
    session.update(current_turn_id=turn_id, phase=turn["phase"], dialogue_state="asking")
    return turn


def adaptive_score_summary(session: Document, evaluations: List[Document]) -> Document:
    contract = assessment_contract(session)
    snapshots = {item["question_snapshot_id"]: item for item in session["plan_snapshot"]["question_snapshots"]}
    dimensions = []
    weighted = 0.0
    complete = True
    coverage = coverage_summary(session)
    for competency in contract["competencies"]:
        scores = [item["score"] for item in evaluations
                  if competency["id"] in snapshots.get(item["question_snapshot_id"], {}).get("competency_ids", [])
                  and isinstance(item.get("score"), (int, float)) and not isinstance(item["score"], bool)]
        raw_score = sum(scores) / len(scores) if scores else None
        score = round(raw_score) if raw_score is not None else None
        sufficient = len(scores) >= competency["min_evidence_roots"]
        complete = complete and sufficient
        if raw_score is not None:
            weighted += raw_score * competency["weight"]
        dimensions.append({"dimension": competency["id"], "score": score, "weight": competency["weight"],
                           "evidence_root_count": len(scores),
                           "evidence_status": "sufficient" if sufficient else "insufficient_evidence"})
    evaluated_snapshots = {item["question_snapshot_id"] for item in evaluations}
    source_scopes = []
    for candidate in contract["candidate_questions"]:
        selected = [item for item in snapshots.values() if item.get("question_id") == candidate["question_id"]]
        if not candidate.get("inquiry_units") or not selected:
            continue
        full = {point["id"] if isinstance(point, dict) else str(point) for point in candidate["frozen_question"]["key_points"]}
        assessed = {point for item in selected if item["question_snapshot_id"] in evaluated_snapshots
                    for point in item.get("assessed_rubric_point_ids", [])}
        source_scopes.append({"source_question_id": candidate["question_id"],
            "assessed_rubric_point_ids": sorted(assessed), "not_assessed_rubric_point_ids": sorted(full - assessed),
            "assessed_point_count": len(assessed), "approved_point_count": len(full),
            "scope_status": "complete" if assessed == full else "partial" if assessed else "not_assessed"})
    return {"overall_score": int(round(weighted)) if complete and dimensions and coverage["sufficient"] else None,
            "dimension_scores": dimensions, "coverage_status": "sufficient" if complete and coverage["sufficient"] else "insufficient_evidence",
            "assessment_coverage": coverage, "scoring_policy": contract["scoring_policy"],
            "assessment_contract_hash": contract["contract_hash"], "source_question_scopes": source_scopes}


def interrupted_fixed_plan_summary(session: Document, evaluations: List[Document]) -> Document:
    """Keep v2 voluntary exits explicit without renormalizing missing questions."""
    roots = root_turns(session)
    evaluated = {item["question_snapshot_id"] for item in evaluations}
    expected = {turn.get("question_snapshot_id") for turn in roots
                if turn.get("skip_reason") != "resume_speech_not_ready"}
    missing = sorted(value for value in expected - evaluated if value)
    result = {"coverage_status": "insufficient_evidence" if missing else "sufficient",
              "assessment_coverage": {"answered_root_count": len(expected & evaluated),
                                      "planned_root_count": len(expected),
                                      "unassessed_question_snapshot_ids": missing}}
    if missing:
        result.update(overall_score=None, job_fit_level="insufficient_evidence", recommendation="insufficient_evidence")
    return result
