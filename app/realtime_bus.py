import json
import os
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol

from app.core.ids import new_id


RemoteEventHandler = Callable[[str, Dict[str, Any]], Awaitable[None]]


class RealtimeEventBus(Protocol):
    enabled: bool
    instance_id: str

    async def publish(self, interview_id: str, event: Dict[str, Any]) -> None: ...

    async def subscribe(self, handler: RemoteEventHandler) -> None: ...

    async def close(self) -> None: ...


class LocalEventBus:
    enabled = False

    def __init__(self) -> None:
        self.instance_id = new_id("instance")

    async def publish(self, interview_id: str, event: Dict[str, Any]) -> None:
        return None

    async def subscribe(self, handler: RemoteEventHandler) -> None:
        return None

    async def close(self) -> None:
        return None


class RedisRealtimeEventBus:
    enabled = True

    def __init__(self, url: str, *, channel: str = "interviewer:realtime") -> None:
        from redis.asyncio import Redis

        self.client = Redis.from_url(url, decode_responses=True)
        self.channel = channel
        self.instance_id = new_id("instance")
        self._closed = False

    async def publish(self, interview_id: str, event: Dict[str, Any]) -> None:
        await self.client.publish(
            self.channel,
            json.dumps(
                {"instance_id": self.instance_id, "interview_id": interview_id, "event": event},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    async def subscribe(self, handler: RemoteEventHandler) -> None:
        pubsub = self.client.pubsub()
        await pubsub.subscribe(self.channel)
        try:
            async for message in pubsub.listen():
                if self._closed:
                    break
                if message.get("type") != "message":
                    continue
                payload = json.loads(message["data"])
                if payload.get("instance_id") == self.instance_id:
                    continue
                await handler(str(payload["interview_id"]), dict(payload["event"]))
        except Exception:
            # Closing the Redis client unblocks a subscriber waiting in listen().
            # That transport close is the expected shutdown path, not an outage.
            if not self._closed:
                raise
        finally:
            await pubsub.aclose()

    async def close(self) -> None:
        self._closed = True
        await self.client.aclose()


_event_bus: Optional[RealtimeEventBus] = None


def realtime_event_bus() -> RealtimeEventBus:
    global _event_bus
    if _event_bus is not None:
        return _event_bus
    url = os.getenv("INTERVIEWER_REDIS_URL", "").strip()
    _event_bus = RedisRealtimeEventBus(
        url,
        channel=os.getenv("INTERVIEWER_REALTIME_CHANNEL", "interviewer:realtime"),
    ) if url else LocalEventBus()
    return _event_bus
