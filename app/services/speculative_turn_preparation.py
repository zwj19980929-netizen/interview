"""One cancellable preparation for an exact stable server transcript.

This module owns no domain writes or completion permission. The endpoint must
still obtain and fence an authoritative final before using a matching result.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable


class SpeculativeTurnPreparation:
    def __init__(self, prepare: Callable, identity: Callable, *, timeout: float = 15.0) -> None:
        self._prepare, self._identity, self._timeout = prepare, identity, timeout
        self._key = None
        self._task = None
        self._tasks = set()

    def start(self, preview: Any) -> None:
        if preview is None or getattr(preview, "has_unstable_tail", True) or not preview.text.strip():
            return
        key = self._identity(preview)
        if key == self._key:
            return  # An unchanged failed preview must not create a retry loop.
        self.cancel()
        self._key = key
        task = asyncio.create_task(self._run(preview.model_copy(deep=True)))
        self._task = task
        self._tasks.add(task)
        task.add_done_callback(self._done)

    async def _run(self, preview: Any) -> Any:
        return await asyncio.wait_for(self._prepare(preview, semantic_first=True), timeout=self._timeout)

    def matching(self, snapshot: Any) -> bool:
        task = self._task
        return bool(task is not None and self._key == self._identity(snapshot)
                    and not task.cancelled()
                    and (not task.done() or (task.exception() is None
                         and task.result().understanding.problem is None)))

    async def take(self, snapshot: Any) -> Any:
        if not self.matching(snapshot):
            return None
        # Revoking this waiter does not manufacture a result or commit. The
        # endpoint cancels the owned task as soon as new input is observed.
        return await asyncio.shield(self._task)

    def cancel(self) -> None:
        task, self._task = self._task, None
        self._key = None
        if task is not None and not task.done():
            task.cancel()

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()  # Consume abandoned failures without logging speech.

    async def close(self) -> None:
        self.cancel()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
