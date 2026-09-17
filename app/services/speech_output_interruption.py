"""Revoke a precise output before awaited transport teardown."""

from typing import Optional

from app.domain.interview_agent import Replayability


async def revoke_active_performance(source, performance_id: Optional[str], *, reason: str,
                                    turn_id: Optional[str] = None, causation_id: Optional[str] = None) -> bool:
    if not performance_id or not source.runtime._clear_active_performance(
            source.interview_id, performance_id, source.organization_id):
        return False
    # Revocation synchronously invalidates the source's PCM fence. Send the
    # authoritative interruption before room unpublish/disconnect can block.
    await source._emit("avatar.performance.interrupted", {
        "performance_id": performance_id, "reason": reason, "deadline_ms": 200,
    }, turn_id=turn_id, causation_id=causation_id, replayability=Replayability.TRANSIENT)
    return True
