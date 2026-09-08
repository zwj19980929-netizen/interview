"""Read-only deployment readiness behind one small probe interface."""

import importlib.util
import asyncio
import os
import shutil
from typing import Any, Dict, List

from app.adapters.livekit_media import LiveKitMediaPlane
from app.model_gateway import capabilities as cap
from app.services.model_route_readiness import purpose_readiness
from app.domain.avatar_asset import inspect_licensed_vrm
from app.operations.production_config import security_configuration_ready
from app.core.interview_agent_release import realtime_agent_release_status
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


def _route_ready(transaction: Any, capability: str, purpose: str) -> bool:
    """Verify an explicit, healthy, non-mock production route."""
    return bool(purpose_readiness(transaction, capability, purpose)["ready"])


async def deployment_readiness(store: Any) -> Dict[str, Any]:
    """Probe runtime dependencies without writing data or invoking paid models."""
    production = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production"
    local_media = os.getenv("INTERVIEWER_LOCAL_MEDIA", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    checks: List[Dict[str, Any]] = []
    route_readiness: Dict[str, bool] = {}
    try:
        with persistence_for(store).transaction(
            os.getenv("INTERVIEWER_ORGANIZATION_ID", "org_default")
        ) as transaction:
            transaction.model_routes.list()
            route_readiness = {
                "agent_stt_streaming": _route_ready(
                    transaction,
                    cap.STT_STREAMING,
                    "candidate_answer_transcription",
                ),
                "warmup_stt_streaming": _route_ready(
                    transaction, cap.STT_STREAMING, "warmup_calibration"
                ),
                "stt_batch_repair": _route_ready(
                    transaction, cap.STT_BATCH, "candidate_answer_repair"
                ),
                "turn_understanding": _route_ready(
                    transaction,
                    cap.LLM_CHAT_JSON,
                    "interview_turn_understanding",
                ),
                "controlled_followup": _route_ready(
                    transaction, cap.LLM_CHAT_JSON, "controlled_followup"
                ),
                "answer_evaluation": _route_ready(
                    transaction, cap.LLM_CHAT_JSON, "answer_evaluation"
                ),
                "agent_expression_tts": _route_ready(
                    transaction,
                    cap.TTS_SYNTHESIZE,
                    "interview_agent_expression",
                ),
                "agent_realtime_speech": _route_ready(
                    transaction,
                    cap.SPEECH_DIALOGUE_REALTIME,
                    "candidate_followup_dialogue",
                ),
            }
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

    vrm = inspect_licensed_vrm()
    checks.append(
        _check(
            "licensed_vrm_1_0",
            bool(vrm["ready"]),
            (
                "licensed VRM 1.0 asset, expression contract and manifest are ready"
                if vrm["ready"]
                else "licensed VRM 1.0 asset is unavailable: %s"
                % vrm.get("reason", "unknown")
            ),
        )
    )

    if production or local_media:
        livekit = LiveKitMediaPlane()
        checks.extend(
            [
                _check(
                    "livekit_media_and_egress",
                    await livekit.healthcheck(recording=True),
                    (
                        "本地 LiveKit、Egress 与录像目录可用"
                        if local_media and not production
                        else "self-hosted LiveKit media, Egress and private storage probe"
                    ),
                ),
                _check(
                    "livekit_authoritative_audio_ingress",
                    await livekit.authoritative_ingress_healthcheck(),
                    (
                        "本地服务端只读 LiveKit 收音链路可用"
                        if local_media and not production
                        else "native receive-only LiveKit RTC probe for the database-fenced Evidence ingress"
                    ),
                ),
            ]
        )
    if production:
        release = realtime_agent_release_status(
            os.getenv("INTERVIEWER_ORGANIZATION_ID", "org_default")
        )
        checks.extend(
            [
                _check("security_secrets", security_configuration_ready(), "production authentication and encryption configuration"),
                _check("object_storage", await _object_storage_ready(), "private OSS read-only bucket probe"),
                _check("malware_scanner", await _scanner_ready(), "production scanner connection"),
                _check(
                    "interview_agent_acceptance_report",
                    release["acceptance_report_ready"],
                    "fresh signed hard-SLO, quality, privacy, browser and pilot report bound to this release",
                ),
                _check(
                    "interview_agent_release_scope",
                    release["release_scope_configured"],
                    "deployment id and immutable release revision are configured",
                ),
                _check(
                    "interview_agent_organization_rollout",
                    release["organization_enabled"],
                    "organization is enabled by the explicit real-time-agent rollout flag",
                ),
            ]
        )
        checks.extend(
            _check(name, ready, "explicit healthy non-mock model route")
            for name, ready in route_readiness.items()
        )
    ready = all(item["ready"] for item in checks)
    return {
        "status": "ready" if ready else "not_ready",
        "runtime_environment": "production" if production else "development",
        "ready": ready,
        "checks": checks,
    }
