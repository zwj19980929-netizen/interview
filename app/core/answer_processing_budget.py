"""A shared invocation allowance for one unchanged answer's preparation.

The endpoint owns identity, lifetime and explicit renewal. Async child tasks
inherit the same counter, so speculative work and format repair cannot each
obtain another allowance. This grants no permission to submit an answer.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional
from app.core.interview_agent_metrics import observe_interview_agent_metric


@dataclass
class AnswerProcessingBudget:
    limit: int = 2
    used: int = 0

    def consume(self) -> bool:
        if self.used >= self.limit:
            return False
        # No await between check and increment: sibling event-loop tasks share
        # one allowance, including calls whose callers are later cancelled.
        self.used += 1
        return True


_budget: ContextVar[Optional[AnswerProcessingBudget]] = ContextVar("answer_processing_budget", default=None)


def consume_answer_model_call() -> bool:
    budget = _budget.get()
    allowed = budget is None or budget.consume()
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
