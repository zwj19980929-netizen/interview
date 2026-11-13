"""Revoke a precise output before awaited transport teardown."""

import asyncio
from typing import Optional

from app.domain.interview_agent import Replayability


async def notify_performance_interrupted(source, performance_id: str, *, reason: str,
                                         turn_id: Optional[str] = None, causation_id: Optional[str] = None) -> None:
    """Deduplicate exact-output notification across explicit and fence cleanup.

    Register before the first yield so native publication cleanup can join a
    revocation already clearing its authority. This notice cannot change the
    current performance, floor or answer. A failed control transport remains a
    failure; the native adapter separately bounds its wait before releasing.
    """
    notifications = getattr(source.runtime, "_speech_interruption_notifications", None)
    if notifications is None:
        notifications = {}
        source.runtime._speech_interruption_notifications = notifications
    key = (source.organization_id, source.interview_id, performance_id)
    task = notifications.get(key)
    if task is None:
        for old_key, old_task in list(notifications.items()):
            if len(notifications) < 128:
                break
            if old_task.done():
                notifications.pop(old_key)
        async def send():
            await asyncio.wait_for(source._emit("avatar.performance.interrupted", {
                "performance_id": performance_id, "reason": reason, "deadline_ms": 200,
            }, turn_id=turn_id, causation_id=causation_id, replayability=Replayability.TRANSIENT), timeout=1.0)
        task = asyncio.create_task(send())
        notifications[key] = task
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    # One cancelled waiter must not cancel the other path's shared notice.
    await asyncio.shield(task)


async def revoke_active_performance(source, performance_id: Optional[str], *, reason: str,
                                    turn_id: Optional[str] = None, causation_id: Optional[str] = None) -> bool:
    if not performance_id or not source.runtime._clear_active_performance(
            source.interview_id, performance_id, source.organization_id):
        return False
    # Revocation synchronously invalidates the source's PCM fence. Send the
    # authoritative interruption before room unpublish/disconnect can block.
    await notify_performance_interrupted(source, performance_id, reason=reason,
                                        turn_id=turn_id, causation_id=causation_id)
    return True
