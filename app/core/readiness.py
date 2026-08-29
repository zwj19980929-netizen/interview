"""Read-only deployment readiness behind one small probe interface."""

import importlib.util
import asyncio
import os
import shutil
from typing import Any, Dict, List

from app.operations.production_config import security_configuration_ready
from app.persistence.provider import persistence_for
from app.repositories.postgresql import PostgreSQLStore


def _check(name: str, ready: bool, message: str) -> Dict[str, Any]:
    return {"name": name, "ready": bool(ready), "message": message}


async def _object_storage_ready() -> bool:
    backend = os.getenv("INTERVIEWER_FILE_STORAGE_BACKEND", "").lower()
    recording = os.getenv("INTERVIEWER_MEDIA_RECORDING_BACKEND", "").lower()
    required = (
        "INTERVIEWER_OSS_ENDPOINT",
        "INTERVIEWER_OSS_BUCKET",
        "INTERVIEWER_OSS_ACCESS_KEY_ID",
        "INTERVIEWER_OSS_ACCESS_KEY_SECRET",
    )
    configured = bool(
        backend in {"aliyun", "aliyun_oss", "oss"}
        and recording in {"private", "file_storage"}
        and all(os.getenv(name, "").strip() for name in required)
        and importlib.util.find_spec("oss2") is not None
    )
    if not configured:
        return False
    try:
        from app.file_storage.aliyun_oss import AliyunOssFileAdapter

        adapter = AliyunOssFileAdapter()
        await asyncio.to_thread(adapter.healthcheck)
        return True
    except Exception:
        return False


async def _scanner_ready() -> bool:
    command = os.getenv("INTERVIEWER_FILE_SCANNER_COMMAND", "").strip()
    if command:
        return bool(shutil.which(command))
    host = os.getenv("INTERVIEWER_FILE_SCANNER_CLAMD_HOST", "").strip()
    if not host:
        return False
    from app.services.resume_ingestion import clamd_ping

    try:
        port = int(os.getenv("INTERVIEWER_FILE_SCANNER_CLAMD_PORT", "3310"))
    except ValueError:
        return False
    return await asyncio.to_thread(clamd_ping, host, port)


async def deployment_readiness(store: Any) -> Dict[str, Any]:
    """Probe runtime dependencies without writing data or invoking paid models."""
    production = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production"
    checks: List[Dict[str, Any]] = []
    try:
        with persistence_for(store).transaction(
            os.getenv("INTERVIEWER_ORGANIZATION_ID", "org_default")
        ) as transaction:
            transaction.model_routes.list()
        database_live = True
    except Exception:
        database_live = False
    checks.append(_check("database", database_live, "database query succeeded" if database_live else "database query failed"))

    postgresql_ready = isinstance(store, PostgreSQLStore)
    checks.append(
        _check(
            "database_backend",
            not production or postgresql_ready,
            "PostgreSQL runtime adapter" if postgresql_ready else "production requires PostgreSQL",
        )
    )

    redis_url = os.getenv("INTERVIEWER_REDIS_URL", "").strip()
    redis_live = not production and not redis_url
    if redis_url:
        client = None
        try:
            from redis.asyncio import Redis

            client = Redis.from_url(redis_url, decode_responses=True)
            redis_live = bool(await client.ping())
        except Exception:
            redis_live = False
        finally:
            if client is not None:
                await client.aclose()
    checks.append(_check("redis", redis_live, "Redis ping succeeded" if redis_live else "Redis is required and unavailable"))

    if production:
        checks.extend(
            [
                _check("security_secrets", security_configuration_ready(), "production authentication and encryption configuration"),
                _check("object_storage", await _object_storage_ready(), "private OSS read-only bucket probe"),
                _check("malware_scanner", await _scanner_ready(), "production scanner connection"),
            ]
        )
    ready = all(item["ready"] for item in checks)
    return {
        "status": "ready" if ready else "not_ready",
        "runtime_environment": "production" if production else "development",
        "ready": ready,
        "checks": checks,
    }
