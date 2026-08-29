from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from app.adapters.email import EmailSender, OutboundEmail, SmtpEmailSender
from app.core.sensitive_data import SensitiveDataProtector
from app.core.time import utc_now
from app.domain.appointment_admission import ensure_utc, parse_utc
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore


class AppointmentReminderService:
    """Delivers one durable reminder without placing contact data in the work payload."""

    def __init__(
        self,
        store: InMemoryStore,
        *,
        persistence: Optional[Persistence] = None,
        sender: Optional[EmailSender] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.sender = sender or SmtpEmailSender()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sensitive = SensitiveDataProtector()

    def process_work_item(self, work_item_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            work = transaction.outbox.start(work_item_id)
            appointment = transaction.interview_appointments.get(work["aggregate_id"])
            if appointment is None:
                return transaction.outbox.complete(
                    work_item_id,
                    lease_token=work["lease_token"],
                    result_status="skipped_appointment_missing",
                )
            if appointment.get("status") in {"cancelled", "consumed"}:
                return self._complete_skipped(transaction, work, appointment, "skipped_appointment_inactive")
            if parse_utc(appointment["scheduled_start_at"]) <= self._now():
                return self._complete_skipped(transaction, work, appointment, "skipped_interview_started")
            candidate = transaction.candidate_profiles.get(appointment["candidate_profile_id"])
            position = transaction.job_positions.get(appointment["job_position_id"])
            if not candidate or not candidate.get("email_encrypted"):
                return self._complete_skipped(transaction, work, appointment, "skipped_email_unavailable")
            recipient = self.sensitive.decrypt(candidate["email_encrypted"])
            appointment_snapshot = deepcopy(appointment)
            position_name = (position or {}).get("name", "面试岗位")

        try:
            if not self.sender.configured:
                raise ReminderDeliveryError("SMTP_CONFIG_MISSING", "SMTP email delivery is not configured.")
            self.sender.send(
                OutboundEmail(
                    recipient=recipient,
                    subject="面试将在 30 分钟后开始｜%s" % position_name,
                    text_body=self._body(appointment_snapshot, position_name),
                    message_id="<appointment-reminder-%s@interviewer.local>" % appointment_snapshot["id"],
                )
            )
        except ReminderDeliveryError as exc:
            return self._fail(work, exc.code, str(exc), organization_id)
        except Exception as exc:
            return self._fail(work, "SMTP_DELIVERY_FAILED", str(exc), organization_id)

        with self.persistence.transaction(organization_id) as transaction:
            current = transaction.interview_appointments.get(appointment_snapshot["id"])
            completed = transaction.outbox.complete(
                work_item_id,
                lease_token=work["lease_token"],
                result_status="sent",
            )
            if current is not None:
                reminder = deepcopy(current.get("email_reminder") or {})
                reminder.update({"status": "sent", "sent_at": utc_now(), "last_error_code": None})
                current["email_reminder"] = reminder
                current["updated_at"] = utc_now()
                transaction.interview_appointments.update(current, expected_version=current["version"])
            return completed

    def _complete_skipped(self, transaction: Any, work: Dict[str, Any], appointment: Dict[str, Any], status: str) -> Dict[str, Any]:
        reminder = deepcopy(appointment.get("email_reminder") or {})
        reminder.update({"status": "skipped", "result_status": status, "updated_at": utc_now()})
        appointment["email_reminder"] = reminder
        appointment["updated_at"] = utc_now()
        transaction.interview_appointments.update(appointment, expected_version=appointment["version"])
        return transaction.outbox.complete(work["id"], lease_token=work["lease_token"], result_status=status)

    def _fail(self, work: Dict[str, Any], code: str, message: str, organization_id: str) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            current = transaction.interview_appointments.get(work["aggregate_id"])
            failed = transaction.outbox.fail(
                work["id"],
                message,
                lease_token=work["lease_token"],
                error_code=code,
                retryable=True,
            )
            if current is not None:
                reminder = deepcopy(current.get("email_reminder") or {})
                reminder.update({"status": "waiting_configuration" if code == "SMTP_CONFIG_MISSING" else "retrying", "last_error_code": code, "updated_at": utc_now()})
                current["email_reminder"] = reminder
                current["updated_at"] = utc_now()
                transaction.interview_appointments.update(current, expected_version=current["version"])
            return failed

    def _body(self, appointment: Dict[str, Any], position_name: str) -> str:
        return (
            "您好：\n\n"
            "您预约的「%s」面试将在 30 分钟后开始。\n"
            "开始时间：%s\n"
            "请使用此前收到的邀请链接进入页面，并提前检查网络、麦克风和摄像头。\n\n"
            "本邮件由 Interviewer 智能面试系统自动发送。"
        ) % (position_name, appointment["scheduled_start_at"])

    def _now(self) -> datetime:
        return ensure_utc(self.clock())


class ReminderDeliveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
