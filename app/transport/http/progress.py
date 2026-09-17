"""A request-scoped progress stream with one authoritative terminal result."""
import asyncio
import logging

from fastapi.responses import StreamingResponse

from app.core.errors import ApiError
from app.persistence.errors import ConcurrencyConflict
from app.transport.http.responses import api_response


logger = logging.getLogger(__name__)


def progress_response(operation):
    # Resolve the service's organization binding while still in the route.
    queue = asyncio.Queue(maxsize=1)

    def notify(value):
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(value)

    pending = operation(notify)

    def event(name, data):
        return b"event: " + name.encode("ascii") + b"\ndata: " + api_response(data).body + b"\n\n"

    async def stream():
        task = asyncio.create_task(pending)
        try:
            while not task.done():
                update = asyncio.create_task(queue.get())
                try:
                    done, _ = await asyncio.wait({task, update}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
                    if update in done:
                        yield event("progress", update.result())
                    elif not done:
                        yield b": keep-alive\n\n"
                finally:
                    update.cancel()
                    await asyncio.gather(update, return_exceptions=True)
            if not queue.empty():
                yield event("progress", queue.get_nowait())
            result = task.result()
            yield event("complete", result)
        except ApiError as exc:
            yield event("error", {"status": exc.status_code, "error": {
                "code": exc.code, "message": exc.message, "details": exc.details}})
        except ConcurrencyConflict:
            yield event("error", {"status": 409, "error": {"code": "PERSISTENCE_CONFLICT",
                "message": "准备期间资料发生变化，计划尚未创建。请重试。", "details": {}}})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("preparation_stream_failed")
            yield event("error", {"status": 500, "error": {"code": "PLAN_PREPARATION_FAILED",
                "message": "创建结果暂时无法确认，请先到计划列表查看后再重试。", "details": {}}})
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
