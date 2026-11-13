"""One correction continues within the existing allowance and live evidence."""
import time

import pytest

from app.core.answer_processing_budget import (
    AnswerProcessingBudget, answer_processing_budget, answer_correction_binding,
    new_correction_attempt, consume_answer_model_call, remember_answer_correction,
)
from app.core.interaction_budget import interaction_deadline
from app.services.answer_endpoint import _SpeculativeModelAllowance


REASON = "completion_has_unresolved_ambiguity"
REQUEST = {"contract": "synthetic.v1", "messages": [{"content": "synthetic private input"}]}


def test_preview_limit_preserves_one_correction_for_the_remaining_shared_call():
    shared = AnswerProcessingBudget(limit=2)
    preview = _SpeculativeModelAllowance(shared, lambda: None)
    with interaction_deadline(time.monotonic() + 20), answer_correction_binding(("capture", "evidence")):
        with answer_processing_budget(preview):
            first = new_correction_attempt(REQUEST)
            assert consume_answer_model_call(correction=first)
            assert first.correction_reason is None
            assert remember_answer_correction(first, REASON)
            denied = new_correction_attempt(REQUEST)
            assert not consume_answer_model_call(correction=denied)
            assert denied.correction_reason is None and shared.used == 1
        with answer_processing_budget(shared):
            second = new_correction_attempt(REQUEST)
            assert consume_answer_model_call(correction=second)
            assert second.correction_reason == REASON
            assert shared.used == shared.limit == 2
            assert not consume_answer_model_call(correction=new_correction_attempt(REQUEST))
            assert not remember_answer_correction(second, REASON)
    assert "synthetic private input" not in repr(shared)


@pytest.mark.parametrize("change", ["binding", "request", "generation", "expired"])
def test_changed_scope_cannot_reuse_correction_or_replenish_calls(change):
    budget = AnswerProcessingBudget(limit=2)
    with interaction_deadline(time.monotonic() + 20), answer_processing_budget(budget):
        with answer_correction_binding("old"):
            original = new_correction_attempt(REQUEST)
            assert consume_answer_model_call(correction=original)
            assert remember_answer_correction(original, REASON)
        if change == "generation":
            budget.invalidate_corrections()
        with answer_correction_binding("new" if change == "binding" else "old"):
            context = interaction_deadline(time.monotonic() - 1 if change == "expired" else None)
            with context:
                request = {**REQUEST, "contract": "different.v1"} if change == "request" else REQUEST
                attempt = new_correction_attempt(request)
                allowed = consume_answer_model_call(correction=attempt)
                assert allowed is (change != "expired")
                assert attempt.correction_reason is None
        if change != "expired":
            assert not remember_answer_correction(original, REASON), "An older request cannot republish a correction"
        assert budget.used == (1 if change == "expired" else 2)


def test_live_guard_rejects_claim_without_spending_or_exposing_correction():
    budget = AnswerProcessingBudget(limit=2)
    live = True
    def guard():
        if not live:
            raise ValueError("synthetic owner revoked")
    with interaction_deadline(time.monotonic() + 20), answer_processing_budget(budget):
        with answer_correction_binding("owner", guard=guard):
            attempt = new_correction_attempt(REQUEST)
            assert consume_answer_model_call(correction=attempt)
            assert remember_answer_correction(attempt, REASON)
            live = False
            with pytest.raises(ValueError, match="owner revoked"):
                consume_answer_model_call(correction=new_correction_attempt(REQUEST))
            assert budget.used == 1
            assert not remember_answer_correction(attempt, REASON)


def test_unknown_reason_and_missing_binding_do_not_create_cross_call_state():
    budget = AnswerProcessingBudget(limit=2)
    with answer_processing_budget(budget), interaction_deadline(time.monotonic() + 20):
        assert new_correction_attempt(REQUEST) is None
        with answer_correction_binding("capture"):
            attempt = new_correction_attempt(REQUEST)
            assert consume_answer_model_call(correction=attempt)
            assert not remember_answer_correction(attempt, "untrusted model text")
            next_attempt = new_correction_attempt(REQUEST)
            assert consume_answer_model_call(correction=next_attempt)
            assert next_attempt.correction_reason is None


def test_nested_binding_is_required_and_correction_is_taken_only_once():
    budget = AnswerProcessingBudget(limit=3)
    with answer_processing_budget(budget), interaction_deadline(time.monotonic() + 20):
        with answer_correction_binding("capture"), answer_correction_binding("full evidence"):
            first = new_correction_attempt(REQUEST)
            assert consume_answer_model_call(correction=first)
            assert remember_answer_correction(first, REASON)
            second = new_correction_attempt(REQUEST)
            assert consume_answer_model_call(correction=second)
            assert second.correction_reason == REASON
            third = new_correction_attempt(REQUEST)
            assert consume_answer_model_call(correction=third)
            assert third.correction_reason is None


def test_attempt_cannot_cross_budgets_or_be_charged_twice():
    old, new = AnswerProcessingBudget(), AnswerProcessingBudget()
    with answer_correction_binding("same"):
        with answer_processing_budget(old):
            first = new_correction_attempt(REQUEST)
            assert consume_answer_model_call(correction=first)
            assert not consume_answer_model_call(correction=first)
            assert remember_answer_correction(first, REASON)
            pending = new_correction_attempt(REQUEST)
        assert not consume_answer_model_call(correction=pending)
        assert not remember_answer_correction(first, REASON)
        with answer_processing_budget(new):
            assert not consume_answer_model_call(correction=pending)
            assert not remember_answer_correction(first, REASON)
    assert old.used == 1 and new.used == 0


def test_normal_first_request_adds_no_guard_read():
    calls = []
    with answer_processing_budget(AnswerProcessingBudget()), answer_correction_binding("capture", guard=lambda: calls.append(1)):
        first = new_correction_attempt(REQUEST)
        assert consume_answer_model_call(correction=first)
        assert calls == []
        assert remember_answer_correction(first, REASON)
        assert len(calls) == 1
        assert consume_answer_model_call(correction=new_correction_attempt(REQUEST))
        assert len(calls) == 2


def test_revoked_inflight_work_cannot_renew_its_generation_for_internal_repair():
    budget = AnswerProcessingBudget()
    with answer_processing_budget(budget), answer_correction_binding("old evidence"):
        first = new_correction_attempt(REQUEST)
        assert consume_answer_model_call(correction=first)
        budget.invalidate_corrections()
        assert not remember_answer_correction(first, REASON)
        assert not consume_answer_model_call(correction=first.following_attempt())
        assert budget.used == 1


def test_all_application_content_failure_reasons_can_continue():
    from typing import get_args
    from app.domain.interview_agent import UnderstandingProblem
    literal = get_args(UnderstandingProblem.model_fields["reason_code"].annotation)[0]
    reasons = {reason for reason in get_args(literal)
               if not reason.startswith("provider_") and reason != "call_budget_exhausted"}
    assert REASON in reasons
    for reason in reasons:
        with answer_processing_budget(AnswerProcessingBudget()), answer_correction_binding("capture"):
            attempt = new_correction_attempt(REQUEST)
            assert consume_answer_model_call(correction=attempt)
            assert remember_answer_correction(attempt, reason), reason
