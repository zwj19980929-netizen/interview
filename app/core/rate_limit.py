import os
from collections import defaultdict, deque
from time import monotonic
from typing import Deque, Dict, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class PublicRateLimiter:
    """Redis-backed production limiter with a deterministic development fallback."""

    def __init__(self) -> None:
        self._local: Dict[str, Deque[float]] = defaultdict(deque)
        self._redis = None

    async def check(self, request: Request) -> Optional[Response]:
        if not self._protected(request.url.path):
            return None
        limit = max(1, int(os.getenv("INTERVIEWER_PUBLIC_RATE_LIMIT_MAX", "120")))
        window = max(1, int(os.getenv("INTERVIEWER_PUBLIC_RATE_LIMIT_WINDOW_SECONDS", "60")))
        client = request.client.host if request.client else "unknown"
        route_group = self._route_group(request.url.path)
        key = "interviewer:public-rate:%s:%s" % (client, route_group)
        redis_url = os.getenv("INTERVIEWER_REDIS_URL", "").strip()
        if redis_url:
            try:
                redis = self._redis_client(redis_url)
                count = int(await redis.incr(key))
                if count == 1:
                    await redis.expire(key, window)
            except Exception:
                if os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production":
                    return self._error(
                        "RATE_LIMITER_UNAVAILABLE",
                        "Public admission is temporarily unavailable.",
                        503,
                    )
                count = self._local_count(key, window)
        elif os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production":
            return self._error(
                "RATE_LIMITER_UNAVAILABLE",
                "Public admission requires the configured rate limiter.",
                503,
            )
        else:
            count = self._local_count(key, window)
        if count > limit:
            return self._error("RATE_LIMIT_EXCEEDED", "Too many requests. Try again later.", 429)
        return None

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    def reset(self) -> None:
        self._local.clear()

    def _redis_client(self, redis_url: str):
        if self._redis is None:
            from redis.asyncio import Redis

            self._redis = Redis.from_url(redis_url, decode_responses=True)
        return self._redis

    def _local_count(self, key: str, window: int) -> int:
        now = monotonic()
        values = self._local[key]
        while values and values[0] <= now - window:
            values.popleft()
        values.append(now)
        return len(values)

    def _protected(self, path: str) -> bool:
        return path.startswith(
            (
                "/api/v1/public/interview-invitations/",
                "/api/v1/public/interviews/",
                "/api/v1/private-files/",
                "/api/v1/private-media/",
            )
        )

    def _route_group(self, path: str) -> str:
        if path.startswith("/api/v1/public/interview-invitations/"):
            suffix = path.split("/", 6)[-1]
            operation = suffix.split("/", 1)[1] if "/" in suffix else "get"
            return "invitation:%s" % operation
        if path.startswith("/api/v1/public/interviews/"):
            return "candidate-session"
        return "signed-file"

    def _error(self, code: str, message: str, status_code: int) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={"error": {"code": code, "message": message, "details": {}}},
            headers={"Retry-After": os.getenv("INTERVIEWER_PUBLIC_RATE_LIMIT_WINDOW_SECONDS", "60")},
        )


public_rate_limiter = PublicRateLimiter()
