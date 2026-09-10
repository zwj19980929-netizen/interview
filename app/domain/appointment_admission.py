import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.adapters.livekit_media import LiveKitConfiguration
from app.core.errors import ApiError
from app.core.interview_agent_release import realtime_agent_release_status
from app.domain.avatar_asset import inspect_licensed_vrm
from app.domain.speech_profile import speech_asset_matches_profile
from app.model_gateway import capabilities as cap
from app.services.model_route_readiness import purpose_readiness


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
        appointment: Optional[Dict[str, Any]] = None,
        now: datetime,
        ttl_seconds: int = 60,
    ) -> Dict[str, Any]:
        checks = []
        local_mode = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() != "production"
        organization_id = str(
            (appointment or {}).get("organization_id")
            or (plan or {}).get("organization_id")
            or "org_default"
        )
        release = realtime_agent_release_status(organization_id)
        checks.extend(
            [
                {
                    "name": "interview_agent_organization_rollout",
                    "ready": local_mode or release["organization_enabled"],
                    "production_ready": release["organization_enabled"],
                    "invite_ready": local_mode or release["organization_enabled"],
                    "mode": "organization_feature_flag",
                },
                {
                    "name": "interview_agent_release_scope",
                    "ready": local_mode or release["release_scope_configured"],
                    "production_ready": release["release_scope_configured"],
                    "invite_ready": local_mode or release["release_scope_configured"],
                    "mode": "deployment_and_revision_bound",
                },
                {
                    "name": "interview_agent_acceptance_report",
                    "ready": local_mode or release["acceptance_report_ready"],
                    "production_ready": release["acceptance_report_ready"],
                    "invite_ready": local_mode or release["acceptance_report_ready"],
                    "mode": "signed_release_bound_acceptance_v2",
                },
            ]
        )
        slots = plan.get("bank_slots", []) if plan else []
        checks.append(
            {
                "name": "candidate_pools",
                "ready": bool(slots) and all(slot.get("candidate_pool") for slot in slots),
            }
        )
        experience_snapshots = plan.get("experience_question_snapshots", []) if plan else []
        preparation = (appointment or {}).get("speech_preparation") or {}
        prepared_items = preparation.get("items", [])
        prepared_by_source = {
            (item.get("question_id"), int(item.get("source_version", 0))): item
            for item in prepared_items
        }
        speech_profile = (plan or {}).get("speech_profile_snapshot") or {}
        experience_speech_local_ready = not experience_snapshots or (
            preparation.get("status") == "ready"
            and len(prepared_items) == len(experience_snapshots)
            and all(
                self._prepared_experience_asset_ready(
                    transaction,
                    prepared_by_source.get((snapshot.get("id"), int(snapshot.get("version", 0)))),
                    snapshot,
                    speech_profile,
                )
                for snapshot in experience_snapshots
            )
        )
        experience_speech_production_ready = experience_speech_local_ready and all(
            self._speech_asset_is_production_ready(transaction, item.get("asset_id"))
            for item in prepared_items
        )
        if not experience_snapshots:
            experience_speech_production_ready = True
        checks.append(
            {
                "name": "experience_question_speech",
                "blocking": False,
                "ready": (
                    experience_speech_local_ready
                    if local_mode
                    else experience_speech_production_ready
                ),
                "production_ready": experience_speech_production_ready,
                "invite_ready": bool(speech_profile)
                or not experience_snapshots
                or experience_speech_local_ready,
                "status": preparation.get("status", "not_requested"),
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
        streaming_route_configured = self._route_configured(
            transaction, cap.STT_STREAMING, "candidate_answer_transcription"
        )
        checks.append(
            {
                "name": "stt_streaming",
                "ready": streaming_production_ready
                or (local_mode and not streaming_route_configured),
                "production_ready": streaming_production_ready,
                "mode": (
                    "configured_route"
                    if streaming_production_ready
                    else "configured_route_unhealthy"
                    if streaming_route_configured
                    else "mock_development"
                    if local_mode
                    else "real_provider_required"
                ),
            }
        )
        warmup_production_ready = self._route_ready(
            transaction, cap.STT_STREAMING, "warmup_calibration", now
        )
        warmup_route_configured = self._route_configured(
            transaction, cap.STT_STREAMING, "warmup_calibration"
        )
        checks.append(
            {
                "name": "warmup_stt_streaming",
                "ready": warmup_production_ready
                or (local_mode and not warmup_route_configured),
                "production_ready": warmup_production_ready,
                "mode": (
                    "configured_route"
                    if warmup_production_ready
                    else "configured_route_unhealthy"
                    if warmup_route_configured
                    else "ephemeral_mock_development"
                    if local_mode
                    else "real_provider_required"
                ),
            }
        )
        understanding_ready = self._route_ready(
            transaction,
            cap.LLM_CHAT_JSON,
            "interview_turn_understanding",
            now,
        )
        understanding_route_configured = self._route_configured(
            transaction, cap.LLM_CHAT_JSON, "interview_turn_understanding"
        )
        checks.append(
            {
                "name": "turn_understanding",
                "ready": understanding_ready
                or (local_mode and not understanding_route_configured),
                "production_ready": understanding_ready,
                "mode": (
                    "configured_route"
                    if understanding_ready
                    else "configured_route_unhealthy"
                    if understanding_route_configured
                    else "mock_development"
                    if local_mode
                    else "configured_route_required"
                ),
            }
        )
        controlled_followup_ready = self._route_ready(
            transaction, cap.LLM_CHAT_JSON, "controlled_followup", now
        )
        controlled_followup_route_configured = self._route_configured(
            transaction, cap.LLM_CHAT_JSON, "controlled_followup"
        )
        checks.append(
            {
                "name": "controlled_followup",
                "ready": controlled_followup_ready
                or (local_mode and not controlled_followup_route_configured),
                "production_ready": controlled_followup_ready,
                "mode": (
                    "configured_route"
                    if controlled_followup_ready
                    else "configured_route_unhealthy"
                    if controlled_followup_route_configured
                    else "mock_development"
                    if local_mode
                    else "configured_route_required"
                ),
            }
        )
        file_backend = os.getenv("INTERVIEWER_FILE_STORAGE_BACKEND", "local").lower()
        recording_backend = os.getenv("INTERVIEWER_MEDIA_RECORDING_BACKEND", "").lower() or (
            "private" if not local_mode else "local"
        )
        private_media_ready = recording_backend in {"private", "file_storage"} and file_backend in {
            "aliyun", "aliyun_oss", "oss"
        }
        checks.append(
            {
                "name": "candidate_audio_storage",
                "ready": local_mode or private_media_ready,
                "production_ready": private_media_ready,
                "mode": "local_development" if local_mode else "private_object_storage_required",
            }
        )
        settings = (appointment or {}).get("settings") or {}
        formal_audio_evidence_ready = bool(settings.get("record_audio", True))
        checks.append(
            {
                "name": "formal_audio_evidence_consent_scope",
                "ready": local_mode or formal_audio_evidence_ready,
                "production_ready": formal_audio_evidence_ready,
                "formal_ready": formal_audio_evidence_ready,
                "mode": (
                    "audio_recording_scope_required"
                    if formal_audio_evidence_ready
                    else "legacy_no_recording_compatibility_only"
                ),
            }
        )
        livekit = LiveKitConfiguration.from_environment()
        recording_required = bool(
            settings.get("record_audio", True) or settings.get("record_video")
        )
        livekit_ready = (
            livekit.recording_ready()
            if recording_required
            else livekit.media_ready()
        )
        checks.append(
            {
                "name": "livekit_media_plane",
                "ready": livekit_ready,
                "production_ready": livekit_ready,
                "formal_ready": livekit_ready,
                "mode": "self_hosted_livekit" if livekit_ready else "formal_agent_unavailable",
            }
        )
        authoritative_ingress_ready = (
            livekit.authoritative_audio_ingress_ready()
        )
        checks.append(
            {
                "name": "livekit_authoritative_audio_ingress",
                "ready": authoritative_ingress_ready,
                "production_ready": authoritative_ingress_ready,
                "formal_ready": authoritative_ingress_ready,
                "mode": (
                    "server_livekit_subscriber"
                    if authoritative_ingress_ready
                    else "authoritative_ingress_required"
                ),
            }
        )
        vrm_ready = bool(inspect_licensed_vrm()["ready"])
        local_vrm_required = settings.get("avatar_mode", "local") == "local"
        checks.append(
            {
                "name": "licensed_local_vrm",
                "ready": not local_vrm_required or vrm_ready,
                "production_ready": not local_vrm_required or vrm_ready,
                "formal_ready": not local_vrm_required or vrm_ready,
                "mode": (
                    "not_required_for_cloud_avatar"
                    if not local_vrm_required
                    else "licensed_vrm_1_0"
                    if vrm_ready
                    else "asset_required"
                ),
            }
        )
        expression_tts_ready = self._route_ready(
            transaction, cap.TTS_SYNTHESIZE, "interview_agent_expression", now
        )
        expression_tts_configured = self._route_configured(
            transaction, cap.TTS_SYNTHESIZE, "interview_agent_expression"
        )
        checks.append(
            {
                "name": "agent_expression_tts",
                # 开发环境没有配置真实路由时仍保留自动化测试用 mock；一旦管理员
                # 明确配置了路由，就必须通过健康探测，不能静默回落后在面试中途停场。
                "ready": expression_tts_ready
                or (local_mode and not expression_tts_configured),
                "production_ready": expression_tts_ready,
                "formal_ready": expression_tts_ready,
                "mode": (
                    "configured_route"
                    if expression_tts_ready
                    else "configured_route_unhealthy"
                    if expression_tts_configured
                    else "mock_development_only"
                ),
            }
        )
        realtime_speech_ready = self._route_ready(
            transaction,
            cap.SPEECH_DIALOGUE_REALTIME,
            "candidate_followup_dialogue",
            now,
        )
        checks.append(
            {
                "name": "agent_realtime_speech",
                "ready": local_mode or realtime_speech_ready,
                "production_ready": realtime_speech_ready,
                "formal_ready": realtime_speech_ready,
                "mode": (
                    "configured_s2s_with_cascade_fallback"
                    if realtime_speech_ready
                    else "production_route_required"
                ),
            }
        )
        route_checks = {
            "answer_evaluation": (cap.LLM_CHAT_JSON, "answer_evaluation"),
            "stt_batch": (cap.STT_BATCH, "candidate_answer_repair"),
            "stt_streaming": (cap.STT_STREAMING, "candidate_answer_transcription"),
            "warmup_stt_streaming": (cap.STT_STREAMING, "warmup_calibration"),
            "turn_understanding": (cap.LLM_CHAT_JSON, "interview_turn_understanding"),
            "controlled_followup": (cap.LLM_CHAT_JSON, "controlled_followup"),
            "agent_expression_tts": (cap.TTS_SYNTHESIZE, "interview_agent_expression"),
            "agent_realtime_speech": (cap.SPEECH_DIALOGUE_REALTIME, "candidate_followup_dialogue"),
        }
        for check in checks:
            if check["name"] not in route_checks:
                continue
            capability, purpose = route_checks[check["name"]]
            state = purpose_readiness(transaction, capability, purpose, now)
            check["route_readiness"] = state
            if state["route_id"] is not None and not state["ready"]:
                check["mode"] = "configured_route_" + ("unhealthy" if state["status"] == "failed" else state["status"])
        local_ready = all(item["ready"] for item in checks if item.get("blocking", True))
        production_ready = all(item.get("production_ready", item["ready"]) for item in checks if item.get("blocking", True))
        invite_ready = all(
            item.get("invite_ready", item["ready"] if local_mode else item.get("production_ready", item["ready"]))
            for item in checks if item.get("blocking", True)
        )
        checked_at = ensure_utc(now)
        return {
            "checked_at": format_utc(checked_at),
            "expires_at": format_utc(checked_at + timedelta(seconds=max(1, ttl_seconds))),
            "runtime_environment": "development" if local_mode else "production",
            "checks": checks,
            "local_ready": local_ready,
            "production_ready": production_ready,
            "can_invite": invite_ready,
            "can_start": local_ready if local_mode else production_ready,
        }

    def _speech_asset_is_production_ready(self, transaction: Any, asset_id: Optional[str]) -> bool:
        asset = transaction.question_speech_assets.get(asset_id) if asset_id else None
        return bool(asset and asset.get("production_ready"))

    @staticmethod
    def _prepared_experience_asset_ready(
        transaction: Any,
        item: Optional[Dict[str, Any]],
        snapshot: Dict[str, Any],
        profile: Dict[str, Any],
    ) -> bool:
        if not item or item.get("status") != "ready":
            return False
        asset = transaction.question_speech_assets.get(item.get("asset_id"))
        return bool(
            asset
            and asset.get("owner_type") == "experience_question"
            and asset.get("owner_id") == snapshot.get("id")
            and int(asset.get("source_version", 0)) == int(snapshot.get("version", 0))
            and asset.get("status") == "ready"
            and speech_asset_matches_profile(asset, profile)
        )

    def _route_ready(
        self,
        transaction: Any,
        capability: str,
        purpose: str,
        now: datetime,
    ) -> bool:
        return bool(purpose_readiness(transaction, capability, purpose, now)["ready"])

    @staticmethod
    def _route_configured(transaction: Any, capability: str, purpose: str) -> bool:
        """判断管理员是否显式选择了某条运行时路由，不把未配置等同于故障。"""

        return any(
            item.get("capability") == capability
            and item.get("purpose") == purpose
            for item in transaction.model_routes.list()
        )

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
        scopes = set(intake.get("media_consent_scopes") or [])
        if appointment.get("settings", {}).get("record_audio", True) and "audio_recording" not in scopes:
            raise ApiError(
                "AUDIO_RECORDING_CONSENT_REQUIRED",
                "Explicit audio recording consent is required for this appointment.",
                status_code=409,
            )
        if appointment.get("settings", {}).get("record_video", False) and "video_recording" not in scopes:
            raise ApiError(
                "VIDEO_RECORDING_CONSENT_REQUIRED",
                "Explicit video recording consent is required for this appointment.",
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
