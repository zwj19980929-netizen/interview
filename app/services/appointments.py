import hashlib
import json
import re
import secrets
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.sensitive_data import SensitiveDataProtector
from app.core.time import utc_now
from app.domain.appointment_admission import (
    AppointmentAdmission,
    ensure_utc,
    format_utc,
    parse_utc,
)
from app.persistence.interface import Persistence, new_work_item
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.interviews import InterviewService


CONSENT_NOTICE_CATALOG = {
    "v1": {
        "privacy_notice": "我们仅为本次面试处理您提交的身份信息、回答、转写和评分，并按企业留存策略限制访问与删除。",
        "recording_notice": "面试将录制音频，用于服务端转写、评分与授权复核；录音不用于自动作出录用或淘汰决定。",
    }
}


class AppointmentService:
    """Issues one-time invitations and turns a matched intake into one session."""

    def __init__(
        self,
        store: InMemoryStore,
        *,
        persistence: Optional[Persistence] = None,
        interviews: Optional[InterviewService] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.admission = AppointmentAdmission()
        self.sensitive = SensitiveDataProtector()
        self.interviews = interviews or InterviewService(
            store,
            persistence=self.persistence,
            clock=self.clock,
        )

    def create(self, payload: Dict[str, Any], organization_id: str = "org_default") -> Dict[str, Any]:
        if self._parse_time(payload["scheduled_start_at"]) >= self._parse_time(payload["scheduled_end_at"]):
            raise ApiError("APPOINTMENT_TIME_INVALID", "Appointment end must be after its start.")
        with self.persistence.transaction(organization_id) as transaction:
            plan = transaction.interview_plans.get(payload["plan_id"])
            self._required(plan, "INTERVIEW_PLAN_NOT_FOUND", "Interview plan does not exist.")
            if plan["status"] != "approved":
                raise ApiError("INTERVIEW_PLAN_NOT_APPROVED", "Appointment requires an approved plan.", status_code=409)
            candidate = transaction.candidate_profiles.get(payload["candidate_profile_id"])
            self._required(candidate, "CANDIDATE_PROFILE_NOT_FOUND", "Candidate profile does not exist.")
            position = transaction.job_positions.get(payload["job_position_id"])
            self._required(position, "JOB_POSITION_NOT_FOUND", "Job position does not exist.")
            if plan.get("job_position_id") != position["id"] or plan.get("candidate_profile_id") != candidate["id"]:
                raise ApiError("APPOINTMENT_PLAN_SCOPE_MISMATCH", "Plan does not match candidate and position.", status_code=409)
            now_dt = self._now()
            readiness = self._readiness(transaction, plan, now=now_dt)
            settings = self._normalized_settings(payload.get("settings"), default_avatar_mode="local")
            raw_policy = deepcopy(payload.get("admission_policy", {}))
            admission_policy = {
                "early_start_grace_seconds": max(0, int(raw_policy.get("early_start_grace_seconds", 0))),
                "late_start_grace_seconds": max(0, int(raw_policy.get("late_start_grace_seconds", 0))),
                "device_readiness_ttl_seconds": max(1, int(raw_policy.get("device_readiness_ttl_seconds", 300))),
                "model_readiness_ttl_seconds": max(1, int(raw_policy.get("model_readiness_ttl_seconds", 60))),
            }
            consent_version = str(raw_policy.get("consent_version", "v1")).strip()
            notice_content = CONSENT_NOTICE_CATALOG.get(consent_version)
            if notice_content is None:
                raise ApiError("CONSENT_VERSION_INVALID", "Consent version is not allowed.")
            frozen_notice = {
                "version": consent_version,
                "privacy_notice": notice_content["privacy_notice"],
                "recording_notice": notice_content["recording_notice"] if settings["record_audio"] else None,
                "recording_required": bool(settings["record_audio"]),
            }
            notice_hash = hashlib.sha256(
                json.dumps(frozen_notice, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            now = format_utc(now_dt)
            return transaction.interview_appointments.add(
                {
                    "id": new_id("appointment"),
                    "organization_id": organization_id,
                    "plan_id": plan["id"],
                    "plan_version": plan["version"],
                    "candidate_profile_id": candidate["id"],
                    "job_position_id": position["id"],
                    "knowledge_base_ids": list(plan.get("knowledge_base_ids", [])),
                    "resume_review_id": plan.get("resume_review_id"),
                    "scheduled_start_at": payload["scheduled_start_at"],
                    "scheduled_end_at": payload["scheduled_end_at"],
                    "settings": settings,
                    "admission_policy": admission_policy,
                    "consent_notice": {**frozen_notice, "notice_hash": notice_hash},
                    "status": "scheduled",
                    "readiness": readiness,
                    "device_readiness": None,
                    "invitation_token_hash": None,
                    "invitation_expires_at": None,
                    "invited_at": None,
                    "registered_at": None,
                    "consumed_at": None,
                    "cancelled_at": None,
                    "email_reminder": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )

    def list(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return [self._private_projection(item) for item in transaction.interview_appointments.list()]

    def get(self, appointment_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.interview_appointments.get(appointment_id)
        self._required(item, "INTERVIEW_APPOINTMENT_NOT_FOUND", "Interview appointment does not exist.")
        return self._private_projection(item)

    def patch(
        self, appointment_id: str, payload: Dict[str, Any], organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        expected_version = int(payload["expected_version"])
        with self.persistence.transaction(organization_id) as transaction:
            appointment = transaction.interview_appointments.get(appointment_id)
            self._required(appointment, "INTERVIEW_APPOINTMENT_NOT_FOUND", "Interview appointment does not exist.")
            if appointment["status"] != "scheduled":
                raise ApiError(
                    "APPOINTMENT_IMMUTABLE_AFTER_INVITE",
                    "Only a scheduled appointment can be changed.",
                    status_code=409,
                )
            start = payload.get("scheduled_start_at") or appointment["scheduled_start_at"]
            end = payload.get("scheduled_end_at") or appointment["scheduled_end_at"]
            if self._parse_time(start) >= self._parse_time(end):
                raise ApiError("APPOINTMENT_TIME_INVALID", "Appointment end must be after its start.")
            appointment["scheduled_start_at"] = start
            appointment["scheduled_end_at"] = end
            if payload.get("settings") is not None:
                appointment["settings"] = self._normalized_settings(
                    payload["settings"],
                    current=appointment.get("settings", {}),
                    default_avatar_mode="cloud",
                )
            if payload.get("admission_policy") is not None:
                policy = {**appointment.get("admission_policy", {}), **deepcopy(payload["admission_policy"])}
                for field in (
                    "early_start_grace_seconds",
                    "late_start_grace_seconds",
                    "device_readiness_ttl_seconds",
                    "model_readiness_ttl_seconds",
                ):
                    if field in policy:
                        policy[field] = max(0 if "grace" in field else 1, int(policy[field]))
                appointment["admission_policy"] = policy
            plan = transaction.interview_plans.get(appointment["plan_id"])
            appointment["readiness"] = self._readiness(transaction, plan, now=self._now())
            appointment["updated_at"] = format_utc(self._now())
            updated = transaction.interview_appointments.update(appointment, expected_version=expected_version)
            return self._private_projection(updated)

    def invite(
        self, appointment_id: str, payload: Dict[str, Any], organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        if self._parse_time(payload["expires_at"]) <= self._now():
            raise ApiError("INVITATION_EXPIRY_INVALID", "Invitation expiry must be in the future.")
        with self.persistence.transaction(organization_id) as transaction:
            appointment = transaction.interview_appointments.get(appointment_id)
            self._required(appointment, "INTERVIEW_APPOINTMENT_NOT_FOUND", "Interview appointment does not exist.")
            if appointment["status"] not in {"scheduled", "invited"}:
                raise ApiError("APPOINTMENT_NOT_INVITABLE", "Appointment cannot be invited in its current state.", status_code=409)
            plan = transaction.interview_plans.get(appointment["plan_id"])
            readiness = self._readiness(transaction, plan, now=self._now())
            if not readiness["can_invite"]:
                raise ApiError("APPOINTMENT_NOT_READY", "Appointment readiness checks failed.", status_code=409, details=readiness)
            token = secrets.token_urlsafe(32)
            now = format_utc(self._now())
            appointment["status"] = "invited"
            appointment["readiness"] = readiness
            appointment["invitation_token_hash"] = self._token_hash(token)
            appointment["invitation_expires_at"] = payload["expires_at"]
            appointment["invited_at"] = now
            appointment["updated_at"] = now
            appointment = transaction.interview_appointments.update(appointment, expected_version=appointment["version"])
            return {
                "appointment": self._private_projection(appointment),
                "invitation_token": token,
                "join_url": "/#invite/%s" % token,
            }

    def public_invitation(self, token: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            appointment = self._appointment_by_token(transaction, token)
            self._validate_public_token(appointment)
            position = transaction.job_positions.get(appointment["job_position_id"])
        return {
            "appointment_id": appointment["id"],
            "position_name": position["name"],
            "scheduled_start_at": appointment["scheduled_start_at"],
            "scheduled_end_at": appointment["scheduled_end_at"],
            "status": appointment["status"],
            "required_fields": ["name", "email", "phone", "consent"],
            "consent": deepcopy(appointment["consent_notice"]),
            "email_reminder": self._public_reminder_projection(appointment.get("email_reminder")),
        }

    def intake(self, token: str, payload: Dict[str, Any], organization_id: str = "org_default") -> Dict[str, Any]:
        generic_error = ApiError(
            "INVITATION_IDENTITY_NOT_CONFIRMED",
            "The invitation or submitted identity could not be confirmed.",
            status_code=403,
        )
        with self.persistence.transaction(organization_id) as transaction:
            try:
                appointment = self._appointment_by_token(transaction, token)
                self._validate_public_token(appointment)
            except ApiError:
                raise generic_error
            consent = payload["consent"]
            notice = appointment.get("consent_notice", {})
            if not consent.get("accepted"):
                raise ApiError("CONSENT_REQUIRED", "Privacy consent is required.", status_code=409)
            if str(consent.get("version", "")) != str(notice.get("version", "")):
                raise ApiError("CONSENT_VERSION_INVALID", "Consent version is not accepted.", status_code=409)
            if notice.get("recording_required") and not consent.get("recording_accepted"):
                raise ApiError(
                    "RECORDING_CONSENT_REQUIRED",
                    "Recording consent is required for this appointment.",
                    status_code=409,
                )
            candidate = transaction.candidate_profiles.get(appointment["candidate_profile_id"])
            normalized_name = self._normalize_name(payload["name"])
            email = self._normalize_email(payload["email"])
            phone = self._normalize_phone(payload["phone"])
            name_matches = normalized_name == self._normalize_name(candidate["name"])
            email_matches = self.sensitive.lookup_hash(organization_id, email) == candidate.get("email_lookup_hash")
            phone_matches = self.sensitive.lookup_hash(organization_id, phone) == candidate.get("phone_lookup_hash")
            if candidate.get("email"):
                email_matches = email_matches or email == candidate["email"]
            if candidate.get("phone"):
                phone_matches = phone_matches or phone == candidate["phone"]
            contact_matches = email_matches or phone_matches
            if not name_matches or not contact_matches:
                raise generic_error
            existing = next(
                (item for item in transaction.candidate_intakes.list() if item["appointment_id"] == appointment["id"]),
                None,
            )
            if existing and existing.get("consent_evidence_status") == "verified":
                reminder = self._ensure_email_reminder(transaction, appointment, organization_id)
                if not appointment.get("email_reminder"):
                    appointment["email_reminder"] = reminder
                    appointment["updated_at"] = format_utc(self._now())
                    appointment = transaction.interview_appointments.update(
                        appointment,
                        expected_version=appointment["version"],
                    )
                return {
                    "appointment_id": appointment["id"],
                    "status": appointment["status"],
                    "matched": True,
                    "scheduled_start_at": appointment["scheduled_start_at"],
                    "scheduled_end_at": appointment["scheduled_end_at"],
                    "email_reminder": self._public_reminder_projection(reminder),
                }
            now = format_utc(self._now())
            intake = {
                    "id": new_id("intake"),
                    "organization_id": organization_id,
                    "appointment_id": appointment["id"],
                    "matched_candidate_profile_id": candidate["id"],
                    "match_method": (
                        "email_and_phone" if email_matches and phone_matches else "email" if email_matches else "phone"
                    ),
                    "submitted_name": payload["name"],
                    "email_lookup_hash": self._lookup_hash(organization_id, email),
                    "phone_lookup_hash": self._lookup_hash(organization_id, phone),
                    "consent_version": consent["version"],
                    "privacy_accepted": True,
                    "recording_accepted": bool(consent.get("recording_accepted", False)),
                    "notice_hash": notice["notice_hash"],
                    "consent_evidence_status": "verified",
                    "consented_at": now,
                    "created_at": now,
                    "updated_at": now,
                }
            if existing:
                intake["id"] = existing["id"]
                intake["created_at"] = existing.get("created_at", now)
                transaction.candidate_intakes.update(intake, expected_version=existing["version"])
            else:
                transaction.candidate_intakes.add(intake)
            appointment["status"] = "registered"
            appointment["registered_at"] = now
            reminder = self._ensure_email_reminder(transaction, appointment, organization_id)
            appointment["email_reminder"] = reminder
            appointment["updated_at"] = now
            appointment = transaction.interview_appointments.update(appointment, expected_version=appointment["version"])
            return {
                "appointment_id": appointment["id"],
                "status": appointment["status"],
                "matched": True,
                "scheduled_start_at": appointment["scheduled_start_at"],
                "scheduled_end_at": appointment["scheduled_end_at"],
                "email_reminder": self._public_reminder_projection(reminder),
            }

    def readiness(
        self,
        token: str,
        payload: Optional[Dict[str, Any]] = None,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            appointment = self._appointment_by_token(transaction, token)
            self._validate_public_token(appointment)
            plan = transaction.interview_plans.get(appointment["plan_id"])
            now_dt = self._now()
            readiness = self._readiness(transaction, plan, now=now_dt)
            if payload is not None:
                content_type = str(payload.get("audio_content_type", "")).strip().lower()
                device_ready = bool(payload.get("browser_supported")) and bool(
                    payload.get("microphone_granted")
                ) and content_type.startswith("audio/")
                ttl = int(appointment.get("admission_policy", {}).get("device_readiness_ttl_seconds", 300))
                appointment["device_readiness"] = {
                    "ready": device_ready,
                    "browser_supported": bool(payload.get("browser_supported")),
                    "microphone_granted": bool(payload.get("microphone_granted")),
                    "audio_content_type": content_type,
                    "checked_at": format_utc(now_dt),
                    "expires_at": format_utc(now_dt + timedelta(seconds=max(1, ttl))),
                }
                appointment["readiness"] = readiness
                appointment["updated_at"] = format_utc(now_dt)
                appointment = transaction.interview_appointments.update(
                    appointment,
                    expected_version=appointment["version"],
                )
            device = deepcopy(appointment.get("device_readiness"))
        result = deepcopy(readiness)
        result["device_readiness"] = device
        result["can_start"] = bool(readiness.get("can_start")) and bool(device and device.get("ready"))
        return result

    def start(self, token: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            appointment = self._appointment_by_token(transaction, token)
            if appointment["status"] == "consumed":
                existing = next(
                    (item for item in transaction.interview_sessions.list() if item.get("appointment_id") == appointment["id"]),
                    None,
                )
                if existing:
                    return self._start_result(existing)
            self._validate_public_token(appointment, required_status="registered")
            plan = transaction.interview_plans.get(appointment["plan_id"])
            intake = next(
                (
                    item
                    for item in transaction.candidate_intakes.list()
                    if item["appointment_id"] == appointment["id"]
                ),
                None,
            )
            readiness = self._readiness(transaction, plan, now=self._now())
            self.admission.validate_start(
                appointment,
                intake,
                readiness,
                now=self._now(),
            )
        session = self.interviews.create_from_admitted_appointment(appointment["id"], organization_id)
        return self._start_result(session)

    def cancel(self, appointment_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            appointment = transaction.interview_appointments.get(appointment_id)
            self._required(appointment, "INTERVIEW_APPOINTMENT_NOT_FOUND", "Interview appointment does not exist.")
            if appointment["status"] == "consumed":
                raise ApiError("APPOINTMENT_ALREADY_CONSUMED", "Consumed appointment cannot be cancelled.", status_code=409)
            appointment["status"] = "cancelled"
            appointment["cancelled_at"] = utc_now()
            appointment["invitation_token_hash"] = None
            appointment["updated_at"] = utc_now()
            return self._private_projection(
                transaction.interview_appointments.update(appointment, expected_version=appointment["version"])
            )

    def _readiness(
        self,
        transaction: Any,
        plan: Dict[str, Any],
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        ttl = 60
        return self.admission.plan_readiness(
            transaction,
            plan,
            now=now or self._now(),
            ttl_seconds=ttl,
        )

    def _ensure_email_reminder(
        self,
        transaction: Any,
        appointment: Dict[str, Any],
        organization_id: str,
    ) -> Dict[str, Any]:
        existing = deepcopy(appointment.get("email_reminder"))
        if existing and existing.get("work_item_id"):
            return existing
        remind_at = self._parse_time(appointment["scheduled_start_at"]) - timedelta(minutes=30)
        if remind_at < self._now():
            remind_at = self._now()
        work = new_work_item(
            organization_id=organization_id,
            kind="appointment.reminder.email",
            aggregate_id=appointment["id"],
            idempotency_key="appointment.reminder.email:%s" % appointment["id"],
            payload={"appointment_id": appointment["id"]},
        )
        work["available_at"] = format_utc(remind_at)
        work["max_attempts"] = 288
        work["retry_base_seconds"] = 300
        work["retry_max_seconds"] = 1800
        queued = transaction.outbox.enqueue(work)
        return {
            "status": "scheduled",
            "scheduled_for": queued["available_at"],
            "work_item_id": queued["id"],
            "sent_at": None,
            "last_error_code": None,
        }

    def _appointment_by_token(self, transaction: Any, token: str) -> Dict[str, Any]:
        token_hash = self._token_hash(token)
        appointment = next(
            (
                item
                for item in transaction.interview_appointments.list()
                if item.get("invitation_token_hash") == token_hash
                or item.get("consumed_token_hash") == token_hash
            ),
            None,
        )
        if appointment is None:
            raise ApiError("INVITATION_INVALID", "Invitation is invalid or unavailable.", status_code=404)
        return appointment

    def _validate_public_token(self, appointment: Dict[str, Any], required_status: Optional[str] = None) -> None:
        allowed = {required_status} if required_status else {"invited", "registered"}
        if appointment["status"] not in allowed:
            raise ApiError("INVITATION_UNAVAILABLE", "Invitation is no longer available.", status_code=409)
        expires = self._parse_time(appointment["invitation_expires_at"])
        if expires <= self._now():
            raise ApiError("INVITATION_EXPIRED", "Invitation has expired.", status_code=410)

    def _private_projection(self, appointment: Dict[str, Any]) -> Dict[str, Any]:
        value = deepcopy(appointment)
        value.pop("invitation_token_hash", None)
        value.pop("consumed_token_hash", None)
        return value

    def _public_reminder_projection(self, reminder: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not reminder:
            return None
        return {
            key: reminder.get(key)
            for key in ("status", "scheduled_for", "sent_at")
            if reminder.get(key) is not None
        }

    def _start_result(self, session: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "interview_id": session["id"],
            "status": session["status"],
            "candidate_join_url": self.interviews.candidate_join_url(session),
            "current_turn_id": session.get("current_turn_id"),
        }

    def _token_hash(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _parse_time(self, value: str) -> datetime:
        return parse_utc(value)

    def _now(self) -> datetime:
        return ensure_utc(self.clock())

    def _lookup_hash(self, organization_id: str, value: str) -> str:
        return self.sensitive.lookup_hash(organization_id, value)

    def _normalized_settings(
        self,
        value: Optional[Dict[str, Any]],
        *,
        current: Optional[Dict[str, Any]] = None,
        default_avatar_mode: str,
    ) -> Dict[str, Any]:
        settings = {
            "record_audio": True,
            "record_video": False,
            "avatar_mode": default_avatar_mode,
            "speech_dialogue_mode": "cascade",
            "avatar_id": "avatar_default_cn",
            "language": "zh-CN",
            **deepcopy(current or {}),
            **deepcopy(value or {}),
        }
        if settings.get("avatar_mode") not in {"local", "cloud"}:
            raise ApiError(
                "APPOINTMENT_AVATAR_MODE_INVALID",
                "Appointment avatar mode must be local or cloud.",
            )
        if settings.get("speech_dialogue_mode") not in {"cascade", "s2s"}:
            raise ApiError(
                "APPOINTMENT_SPEECH_DIALOGUE_MODE_INVALID",
                "Appointment speech dialogue mode must be cascade or s2s.",
            )
        return settings

    def _normalize_name(self, value: str) -> str:
        return re.sub(r"\s+", "", str(value)).casefold()

    def _normalize_email(self, value: str) -> str:
        return str(value).strip().lower()

    def _normalize_phone(self, value: str) -> str:
        return re.sub(r"[^0-9+]", "", str(value))

    def _required(self, item: Optional[Dict[str, Any]], code: str, message: str) -> Dict[str, Any]:
        if item is None:
            raise ApiError(code, message, status_code=404)
        return item
