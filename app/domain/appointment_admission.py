import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.core.errors import ApiError
from app.model_gateway import capabilities as cap
from app.model_gateway.registry import get_provider_manifest


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Clock must return a timezone-aware datetime.")
    return value.astimezone(timezone.utc)


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError("DATETIME_INVALID", "Datetime must use ISO-8601 format.") from exc
    if parsed.tzinfo is None:
        raise ApiError("DATETIME_INVALID", "Datetime must include a timezone.")
    return parsed.astimezone(timezone.utc)


def format_utc(value: datetime) -> str:
    return ensure_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class AppointmentAdmission:
    """Pure admission policy used by HTTP orchestration and the final start transaction."""

    def plan_readiness(
        self,
        transaction: Any,
        plan: Dict[str, Any],
        *,
        now: datetime,
        ttl_seconds: int = 60,
    ) -> Dict[str, Any]:
        checks = []
        local_mode = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() != "production"
        slots = plan.get("bank_slots", []) if plan else []
        checks.append(
            {
                "name": "candidate_pools",
                "ready": bool(slots) and all(slot.get("candidate_pool") for slot in slots),
            }
        )
        referenced = {
            candidate["question_id"]
            for slot in slots
            for candidate in slot.get("candidate_pool", [])
        }
        questions = [transaction.questions.get(item) for item in referenced]
        question_speech_local_ready = bool(questions) and all(
            item and item.get("speech_status") == "ready" for item in questions
        )
        question_speech_production_ready = question_speech_local_ready and all(
            item and self._speech_asset_is_production_ready(transaction, item.get("speech_asset_id"))
            for item in questions
        )
        checks.append(
            {
                "name": "question_speech",
                "ready": question_speech_local_ready if local_mode else question_speech_production_ready,
                "production_ready": question_speech_production_ready,
            }
        )
        answer_production_ready = self._route_ready(
            transaction, cap.LLM_CHAT_JSON, "answer_evaluation", now
        )
        checks.append(
            {
                "name": "answer_evaluation",
                "ready": local_mode or answer_production_ready,
                "production_ready": answer_production_ready,
                "mode": "mock" if local_mode else "configured_route_required",
            }
        )
        batch_production_ready = self._route_ready(
            transaction, cap.STT_BATCH, "candidate_answer_repair", now
        )
        checks.append(
            {
                "name": "stt_batch",
                "ready": local_mode or batch_production_ready,
                "production_ready": batch_production_ready,
                "mode": "mock_development" if local_mode else "real_provider_required",
            }
        )
        streaming_production_ready = self._route_ready(
            transaction, cap.STT_STREAMING, "candidate_answer_transcription", now
        )
        checks.append(
            {
                "name": "stt_streaming",
                "ready": local_mode or streaming_production_ready,
                "production_ready": streaming_production_ready,
                "mode": "mock_development" if local_mode else "real_provider_required",
            }
        )
        local_ready = all(item["ready"] for item in checks)
        production_ready = all(item.get("production_ready", item["ready"]) for item in checks)
        checked_at = ensure_utc(now)
        return {
            "checked_at": format_utc(checked_at),
            "expires_at": format_utc(checked_at + timedelta(seconds=max(1, ttl_seconds))),
            "runtime_environment": "development" if local_mode else "production",
            "checks": checks,
            "local_ready": local_ready,
            "production_ready": production_ready,
            "can_invite": local_ready if local_mode else production_ready,
            "can_start": local_ready if local_mode else production_ready,
        }

    def _speech_asset_is_production_ready(self, transaction: Any, asset_id: Optional[str]) -> bool:
        asset = transaction.question_speech_assets.get(asset_id) if asset_id else None
        return bool(asset and asset.get("production_ready"))

    def _route_ready(
        self,
        transaction: Any,
        capability: str,
        purpose: str,
        now: datetime,
    ) -> bool:
        routes = [
            item
            for item in transaction.model_routes.list()
            if item.get("enabled", True)
            and item.get("capability") == capability
            and item.get("purpose") in {purpose, "default"}
        ]
        route = next((item for item in routes if item.get("purpose") == purpose), None)
        route = route or next((item for item in routes if item.get("purpose") == "default"), None)
        if route is None:
            return False
        provider_config = transaction.provider_configs.get((route.get("primary") or {}).get("provider_config_id"))
        if provider_config is None or not provider_config.get("enabled", True):
            return False
        manifest = get_provider_manifest(provider_config.get("provider_id", ""))
        if (
            provider_config.get("provider_id") == "mock"
            or not manifest.get("implemented", False)
            or capability not in manifest.get("capabilities", [])
        ):
            return False
        health = route.get("last_health") or {}
        if health.get("status") != "healthy" or not health.get("checked_at"):
            return False
        ttl = max(1, int((route.get("policy") or {}).get("readiness_ttl_seconds", 60)))
        return parse_utc(health["checked_at"]) + timedelta(seconds=ttl) >= ensure_utc(now)

    def validate_start(
        self,
        appointment: Dict[str, Any],
        intake: Optional[Dict[str, Any]],
        plan_readiness: Dict[str, Any],
        *,
        now: datetime,
    ) -> None:
        current = ensure_utc(now)
        if not intake or intake.get("consent_evidence_status") != "verified" or not intake.get("privacy_accepted"):
            raise ApiError("CONSENT_REQUIRED", "Verified privacy consent is required.", status_code=409)
        if appointment.get("settings", {}).get("record_audio", True) and not intake.get("recording_accepted"):
            raise ApiError(
                "RECORDING_CONSENT_REQUIRED",
                "Recording consent is required for this appointment.",
                status_code=409,
            )

        policy = appointment.get("admission_policy", {})
        early_grace = int(policy.get("early_start_grace_seconds", 0))
        late_grace = int(policy.get("late_start_grace_seconds", 0))
        start = parse_utc(appointment["scheduled_start_at"]) - timedelta(seconds=max(0, early_grace))
        end = parse_utc(appointment["scheduled_end_at"]) + timedelta(seconds=max(0, late_grace))
        if current < start:
            raise ApiError("APPOINTMENT_TOO_EARLY", "Appointment start window has not opened.", status_code=409)
        if current > end:
            raise ApiError("APPOINTMENT_WINDOW_CLOSED", "Appointment start window has closed.", status_code=410)

        device = appointment.get("device_readiness") or {}
        if not device.get("ready") or not device.get("expires_at") or parse_utc(device["expires_at"]) < current:
            raise ApiError(
                "APPOINTMENT_DEVICE_NOT_READY",
                "A current successful device readiness check is required.",
                status_code=409,
            )
        if not plan_readiness.get("can_start") or parse_utc(plan_readiness["expires_at"]) < current:
            raise ApiError(
                "APPOINTMENT_NOT_READY",
                "Appointment readiness checks failed.",
                status_code=409,
                details=plan_readiness,
            )
