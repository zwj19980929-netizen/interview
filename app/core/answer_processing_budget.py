"""A shared invocation allowance for one unchanged answer's preparation.

The endpoint owns identity, lifetime and explicit renewal. Async child tasks
inherit the same counter, so speculative work and format repair cannot each
obtain another allowance. This grants no permission to submit an answer.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Callable, Optional, Tuple
from app.core.interview_agent_metrics import observe_interview_agent_metric
from app.core.interaction_budget import remaining_interaction_seconds
from app.core.errors import ApiError


# Only application-owned contract failures can cross a preparation boundary.
# No response, exception text or prompt is retained in the allowance.
_CORRECTION_REASONS = frozenset({
    "wire_schema_invalid", "reference_resolution_invalid", "semantic_control_contract_invalid",
    "semantic_current_speech_conflict", "answer_declined_contract_invalid", "answer_evidence_empty",
    "claim_evidence_undeclared", "clarification_target_contract_invalid", "clarification_targets_conversation_control",
    "company_question_contract_invalid", "company_question_covers_technical_evidence", "completion_action_invalid",
    "completion_basis_required", "completion_basis_unexpected", "completion_confidence_insufficient",
    "completion_followup_permission_invalid", "completion_has_unresolved_ambiguity", "completion_intent_content_mismatch",
    "content_none_has_technical_evidence", "declined_answer_completion_scope_invalid", "declined_answer_has_technical_content",
    "evidence_not_verbatim", "finish_answer_without_content", "off_topic_contract_invalid", "point_duplicates",
    "point_not_frozen", "point_partition_invalid", "semantic_completion_evidence_invalid",
    "technical_content_claims_missing", "unresolved_answer_contract_invalid", "unresolved_clarification_required",
    "unresolved_clarification_source_invalid",
    "clarification_focus_not_verbatim", "company_question_span_invalid",
})


@dataclass
class AnswerProcessingBudget:
    limit: int = 2
    used: int = 0
    defer_corrections: bool = field(default=False, repr=False)
    _generation: int = field(default=0, init=False, repr=False)
    _binding_key: Any = field(default=None, init=False, repr=False)
    _correction: Optional[str] = field(default=None, init=False, repr=False)

    @property
    def correction_budget(self) -> "AnswerProcessingBudget":
        return self

    def invalidate_corrections(self) -> None:
        """Revoke continuations without refunding calls already started."""
        self._generation += 1
        self._binding_key = None
        self._correction = None

    def consume(self) -> bool:
        if self.used >= self.limit:
            return False
        # No await between check and increment: sibling event-loop tasks share
        # one allowance, including calls whose callers are later cancelled.
        self.used += 1
        return True


_budget: ContextVar[Optional[AnswerProcessingBudget]] = ContextVar("answer_processing_budget", default=None)
_bindings: ContextVar[Tuple[Any, ...]] = ContextVar("answer_correction_binding", default=())
_required_continuation: ContextVar[Any] = ContextVar("required_answer_correction", default=None)


@contextmanager
def answer_correction_binding(identity: Any, *, guard: Optional[Callable[[], None]] = None):
    """Bind a continuation to live capture and complete, already frozen context."""
    token = _bindings.set((*_bindings.get(), (identity, guard)))
    try:
        yield
    finally:
        _bindings.reset(token)


@dataclass(repr=False)
class CorrectionAttempt:
    budget: AnswerProcessingBudget
    generation: int
    key: Any
    guards: Tuple[Callable[[], None], ...]
    correction_reason: Optional[str] = None
    charged_at: Optional[int] = None

    def following_attempt(self) -> "CorrectionAttempt":
        # An in-flight inference may swallow cancellation. Its internal repair
        # cannot acquire a newer generation after its evidence was revoked.
        return CorrectionAttempt(self.budget, self.generation, self.key, self.guards)


class AnswerCorrectionContinuation(Exception):
    """A bound rejected call yields recovery scheduling to its endpoint owner."""
    source = "schema_or_content"
    retryable = True

    def __init__(self, attempt: CorrectionAttempt, reason: str) -> None:
        super().__init__("A bound answer correction requires its recovery allowance.")
        self.attempt, self.reason_code = attempt, reason

    def assert_current(self) -> None:
        attempt = self.attempt
        if (attempt.generation != attempt.budget._generation
                or attempt.key != attempt.budget._binding_key
                or attempt.budget._correction != self.reason_code
                or attempt.budget.used >= attempt.budget.limit):
            raise ApiError("TURN_DECISION_STALE", "Answer correction is no longer current.", status_code=409)
        for guard in attempt.guards:
            guard()


@contextmanager
def require_answer_correction(continuation: AnswerCorrectionContinuation):
    """The next preparation must claim this exact receipt, not silently replace it."""
    token = _required_continuation.set(continuation)
    try:
        yield
    finally:
        _required_continuation.reset(token)


def deferred_answer_correction(attempt: Optional[CorrectionAttempt], reason: str) -> Optional[AnswerCorrectionContinuation]:
    """Only the authoritative endpoint yields; standalone and preview calls keep their limits."""
    current = _budget.get()
    if (attempt is None or current is not attempt.budget or not current.defer_corrections
            or current.used >= current.limit or current._correction != reason
            or attempt.generation != current._generation or attempt.key != current._binding_key):
        return None
    return AnswerCorrectionContinuation(attempt, reason)


def answer_model_call_budget_exhausted() -> bool:
    budget = _budget.get()
    return budget is not None and budget.correction_budget.used >= budget.correction_budget.limit


def new_correction_attempt(base_request: Any) -> Optional[CorrectionAttempt]:
    """Digest the original contract, before adding any correction instruction."""
    budget, bindings = _budget.get(), _bindings.get()
    required = _required_continuation.get()
    if budget is None or not bindings:
        if required is not None:
            raise ApiError("TURN_DECISION_STALE", "Answer correction binding is missing.", status_code=409)
        return None
    shared = budget.correction_budget
    data = base_request.model_dump(mode="json") if hasattr(base_request, "model_dump") else base_request
    signature = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode("utf-8")).hexdigest()
    key = (tuple(identity for identity, _ in bindings), signature)
    if required is not None:
        if required.attempt.budget is not shared or required.attempt.key != key:
            raise ApiError("TURN_DECISION_STALE", "Answer correction context changed.", status_code=409)
        required.assert_current()
    if shared._binding_key != key:
        shared.invalidate_corrections()
        shared._binding_key = key
    return CorrectionAttempt(shared, shared._generation, key,
                             tuple(guard for _, guard in bindings if guard is not None))


def remember_answer_correction(attempt: Optional[CorrectionAttempt], reason: str) -> bool:
    current = _budget.get()
    if (attempt is None or current is None or current.correction_budget is not attempt.budget
            or reason not in _CORRECTION_REASONS
            or attempt.charged_at is None or attempt.charged_at != attempt.budget.used
            or attempt.budget.used >= attempt.budget.limit
            or attempt.generation != attempt.budget._generation
            or attempt.key != attempt.budget._binding_key):
        return False
    try:
        for guard in attempt.guards:
            guard()
    except Exception:
        return False
    attempt.budget._correction = reason
    return True


def consume_answer_model_call(*, correction: Optional[CorrectionAttempt] = None) -> bool:
    budget = _budget.get()
    if correction is not None:
        if budget is None or budget.correction_budget is not correction.budget or correction.charged_at is not None:
            return False
        if correction.budget._correction is not None or correction.budget.used > 0:
            for guard in correction.guards:
                guard()
        remaining = remaining_interaction_seconds()
        if (correction.generation != correction.budget._generation
                or correction.key != correction.budget._binding_key
                or (remaining is not None and remaining <= 0)):
            return False
    allowed = budget is None or budget.consume()
    if allowed and correction is not None:
        correction.charged_at = correction.budget.used
        correction.correction_reason = correction.budget._correction
        correction.budget._correction = None
    observe_interview_agent_metric("answer_model_call_started" if allowed else "answer_model_call_budget_exhausted", 1)
    return allowed


@contextmanager
def answer_processing_budget(budget: AnswerProcessingBudget):
    inherited = _budget.get()
    token = _budget.set(inherited if inherited is not None else budget)
    try:
        yield
    finally:
        _budget.reset(token)
