import asyncio
import os
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.core.rate_limit import PublicRateLimiter
from app.realtime_bus import RedisRealtimeEventBus


REDIS_URL = os.getenv("INTERVIEWER_TEST_REDIS_URL", "").strip()


@pytest.mark.skipif(not REDIS_URL, reason="INTERVIEWER_TEST_REDIS_URL is not configured")
def test_real_redis_delivers_events_between_application_instances() -> None:
    async def scenario() -> None:
        channel = "interviewer:validation:%s" % uuid4().hex
        publisher = RedisRealtimeEventBus(REDIS_URL, channel=channel)
        subscriber = RedisRealtimeEventBus(REDIS_URL, channel=channel)
        received = asyncio.Event()
        payload = {}

        async def handler(interview_id, event):
            payload.update({"interview_id": interview_id, "event": event})
            received.set()

        task = asyncio.create_task(subscriber.subscribe(handler))
        try:
            await asyncio.sleep(0.05)
            await publisher.publish("interview_validation", {"type": "validation.event"})
            await asyncio.wait_for(received.wait(), timeout=2)
            assert payload == {
                "interview_id": "interview_validation",
                "event": {"type": "validation.event"},
            }
        finally:
            await subscriber.close()
            await publisher.close()
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())


@pytest.mark.skipif(not REDIS_URL, reason="INTERVIEWER_TEST_REDIS_URL is not configured")
def test_real_redis_enforces_production_public_rate_limit(monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
        monkeypatch.setenv("INTERVIEWER_REDIS_URL", REDIS_URL)
        monkeypatch.setenv("INTERVIEWER_PUBLIC_RATE_LIMIT_MAX", "1")
        monkeypatch.setenv("INTERVIEWER_PUBLIC_RATE_LIMIT_WINDOW_SECONDS", "60")
        limiter = PublicRateLimiter()
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/api/v1/public/interview-invitations/validation",
                "raw_path": b"/api/v1/public/interview-invitations/validation",
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "client": ("validation-%s" % uuid4().hex, 12345),
                "server": ("testserver", 80),
            }
        )
        try:
            assert await limiter.check(request) is None
            rejected = await limiter.check(request)
            assert rejected is not None
            assert rejected.status_code == 429
        finally:
            await limiter.close()

    asyncio.run(scenario())
