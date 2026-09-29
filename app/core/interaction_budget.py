"""A monotonic deadline shared by work preparing one candidate interaction.

The context travels with async tasks. Nested work may shorten the deadline, but
cannot grant a fresh budget to an already running interaction.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import time
from typing import Optional


_deadline: ContextVar[Optional[float]] = ContextVar("interaction_deadline", default=None)


def remaining_interaction_seconds() -> Optional[float]:
    deadline = _deadline.get()
    return None if deadline is None else max(0.0, deadline - time.monotonic())


@contextmanager
def interaction_deadline(deadline: Optional[float]):
    inherited = _deadline.get()
    effective = inherited if deadline is None else deadline
    if inherited is not None and effective is not None:
        effective = min(inherited, effective)
    token = _deadline.set(effective)
    try:
        yield
    finally:
        _deadline.reset(token)
