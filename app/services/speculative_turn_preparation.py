"""One cancellable preparation for an exact stable server transcript.

This module owns no domain writes or completion permission. The endpoint must
still obtain and fence an authoritative final before using a matching result.
"""
from __future__ import annotations

import asyncio
import time
from weakref import WeakSet
from typing import Any, Callable
from app.core.interaction_budget import remaining_interaction_seconds
from app.core.model_diagnostics import (
    diagnosed_wait_for, model_call_scope, record_model_diagnostic, safe_model_call_context,
)


class SpeculativeTurnPreparation:
    def __init__(self, prepare: Callable, identity: Callable, *, timeout: float = 15.0,
                 diagnostic_context: Any = None, on_done: Any = None) -> None:
        self._prepare, self._identity, self._timeout = prepare, identity, timeout
        self._on_done = on_done
        self._diagnostic_context = {
            key: (diagnostic_context or {}).get(key) for key in ("interview_id", "turn_id", "capture_id")
        }
        self._key = None
        self._task = None
        self._scope = None
        self._diagnostic_cancelled_tasks = WeakSet()
        self._tasks = set()

    def start(self, preview: Any) -> Any:
        if preview is None or getattr(preview, "has_unstable_tail", True) or not preview.text.strip():
            return
        key = self._identity(preview)
        if key == self._key:
            return self._task  # An unchanged failed preview cannot buy a retry.
        self.cancel()
        self._key = key
        predecessors = self.pending_tasks
        with model_call_scope(**self._diagnostic_context, stage="answer_preparation", inherit=False) as scope:
            task = asyncio.create_task(self._run(preview.model_copy(deep=True), scope, predecessors))
        self._task = task
        self._scope = scope
        self._tasks.add(task)
        task.add_done_callback(lambda done: self._done(done, preview))
        return task

    async def _run(self, preview: Any, scope: Any, predecessors: tuple = ()) -> Any:
        remaining = remaining_interaction_seconds()
        timeout = min(self._timeout, remaining) if remaining is not None else self._timeout
        fields = {**safe_model_call_context(scope=scope), "operation": "speculative_prepare",
                  "requested_timeout_ms": self._timeout * 1000, "effective_timeout_ms": max(0, timeout) * 1000,
                  "interaction_remaining_ms": remaining * 1000 if remaining is not None else None}
        started = time.monotonic()
        outcome, reason = "success", None
        record_model_diagnostic("scope_started", **fields, outcome="started")
        async def prepare():
            if predecessors:
                await asyncio.gather(*(asyncio.shield(task) for task in predecessors), return_exceptions=True)
            return await self._prepare(preview, semantic_first=True)

        try:
            result = await diagnosed_wait_for(prepare(), timeout=timeout,
                                              stage="answer_preparation", timeout_reason="speculative_timeout")
            problem = getattr(getattr(result, "understanding", None), "problem", None)
            if problem is not None:
                outcome = "failed"
                reason = getattr(problem, "reason_code", None) or getattr(problem, "code", None)
            return result
        except asyncio.TimeoutError:
            outcome = "timeout"
            reason = "speculative_timeout" if time.monotonic() - started >= timeout else "child_timeout"
            raise
        except asyncio.CancelledError:
            outcome, reason = "cancelled", scope.cancel_reason
            raise
        except Exception as exc:
            outcome, reason = "failed", getattr(exc, "code", None)
            raise
        finally:
            record_model_diagnostic("scope_finished", **fields, outcome=outcome,
                                    elapsed_ms=(time.monotonic() - started) * 1000,
                                    cancel_reason=reason if outcome in {"timeout", "cancelled"} else None,
                                    failure_reason=reason if outcome == "failed" else None)

    def matching(self, snapshot: Any, *, include_failed: bool = False) -> bool:
        task = self._task
        return bool(task is not None and self._key == self._identity(snapshot)
                    and not task.cancelled()
                    and (not task.done() or include_failed or (task.exception() is None
                         and task.result().understanding.problem is None)))

    @property
    def diagnostic_scope(self) -> Any:
        """Keep failure accounting tied to the work, not its later waiter."""
        return self._scope

    @property
    def has_preparation(self) -> bool:
        return self._task is not None

    def owns(self, task: asyncio.Task) -> bool:
        return task is self._task

    @property
    def pending_tasks(self) -> tuple:
        """Include invalidated work that is still unwinding cancellation."""
        return tuple(task for task in self._tasks if not task.done())

    def ready(self, snapshot: Any) -> Any:
        """Read successful work without creating another timed waiter."""
        if self.matching(snapshot) and self._task.done():
            return self._task.result()
        return None

    async def take(self, snapshot: Any, *, include_failed: bool = False) -> Any:
        if not self.matching(snapshot, include_failed=include_failed):
            return None
        # Revoking this waiter does not manufacture a result or commit. The
        # endpoint cancels owned work only when its evidence is superseded.
        return await asyncio.shield(self._task)

    def cancel(self, *, reason: str = "superseded", source: Any = None) -> Any:
        task, self._task = self._task, None
        scope, self._scope = self._scope, None
        self._key = None
        if task is not None and not task.done():
            if scope is not None and task not in self._diagnostic_cancelled_tasks:
                self._diagnostic_cancelled_tasks.add(task)
                scope.cancel(reason)
                record_model_diagnostic("scope_cancel_requested", **safe_model_call_context(scope=scope),
                                        cancel_reason=scope.cancel_reason, source=source)
            task.cancel()
        return task

    def _done(self, task: asyncio.Task, preview: Any) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()  # Consume abandoned failures without logging speech.
        if self._on_done is not None:
            self._on_done(task, preview)

    async def close(self) -> None:
        self.cancel(reason="capture_closed")
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
