"""Cancellation-safe ownership of a LiveKit native room join.

The SDK's connect coroutine must finish its FFI ready handshake even when its
caller no longer needs the room. Cancelling that coroutine can terminate the
whole process. Callers may stop waiting immediately; an abandoned join can only
finish and disconnect, never publish or subscribe on their behalf.
"""
import asyncio
import logging

_LOG = logging.getLogger(__name__)
_ABANDONED_JOINS = set()


async def _disconnect_after_join(join, room):
    try:
        await asyncio.shield(join)
    except Exception:
        # Consume a failed join without exposing vendor messages or tokens.
        pass
    try:
        await asyncio.wait_for(room.disconnect(), timeout=5)
    except Exception as exc:
        _LOG.warning("Abandoned LiveKit room cleanup failed: error_type=%s", type(exc).__name__)


async def connect_room(room, url, token, options):
    """Keep native join alive on cancellation, then reclaim a late connection."""
    join = asyncio.create_task(room.connect(url, token, options))
    try:
        return await asyncio.shield(join)
    except asyncio.CancelledError:
        cleanup = asyncio.create_task(_disconnect_after_join(join, room))
        _ABANDONED_JOINS.add(cleanup)
        def finished(task):
            _ABANDONED_JOINS.discard(task)
            if not task.cancelled():
                task.exception()
        cleanup.add_done_callback(finished)
        raise
