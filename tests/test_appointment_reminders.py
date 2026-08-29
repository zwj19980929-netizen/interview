from datetime import datetime, timedelta, timezone

from app.adapters.email import OutboundEmail
from app.core.sensitive_data import SensitiveDataProtector
from app.core.time import utc_now
from app.persistence.interface import new_work_item
from app.persistence.provider import persistence_for
from app.repositories.provider import get_store, reset_store_for_tests
from app.services.appointment_reminders import AppointmentReminderService


class FakeEmailSender:
    def __init__(self, configured: bool = True) -> None:
        self.configured = configured
        self.messages: list[OutboundEmail] = []

    def send(self, message: OutboundEmail) -> None:
        self.messages.append(message)


def _reminder_fixture(*, configured: bool = True):
    reset_store_for_tests()
    store = get_store()
    persistence = persistence_for(store)
    protector = SensitiveDataProtector()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with persistence.transaction("org_default") as transaction:
        position = transaction.job_positions.add(
            {"id": "position_reminder", "organization_id": "org_default", "name": "后端工程师"}
        )
        candidate = transaction.candidate_profiles.add(
            {
                "id": "candidate_reminder",
                "organization_id": "org_default",
                "name": "候选人",
                "email_encrypted": protector.encrypt("candidate@example.com"),
            }
        )
        appointment = transaction.interview_appointments.add(
            {
                "id": "appointment_reminder",
                "organization_id": "org_default",
                "candidate_profile_id": candidate["id"],
                "job_position_id": position["id"],
                "scheduled_start_at": (now + timedelta(minutes=25)).isoformat().replace("+00:00", "Z"),
                "status": "registered",
                "email_reminder": {"status": "scheduled", "scheduled_for": utc_now()},
                "updated_at": utc_now(),
            }
        )
        work = new_work_item(
            organization_id="org_default",
            kind="appointment.reminder.email",
            aggregate_id=appointment["id"],
            idempotency_key="appointment.reminder.email:test",
            payload={"appointment_id": appointment["id"]},
        )
        work["max_attempts"] = 2
        work["retry_base_seconds"] = 1
        work = transaction.outbox.enqueue(work)
    sender = FakeEmailSender(configured=configured)
    service = AppointmentReminderService(store, persistence=persistence, sender=sender, clock=lambda: now)
    return service, sender, persistence, work


def test_email_reminder_decrypts_recipient_only_at_delivery_and_marks_sent() -> None:
    service, sender, persistence, work = _reminder_fixture()

    result = service.process_work_item(work["id"])

    assert result["status"] == "completed"
    assert result["result_status"] == "sent"
    assert work["payload"] == {"appointment_id": "appointment_reminder"}
    assert sender.messages[0].recipient == "candidate@example.com"
    assert sender.messages[0].subject == "面试将在 30 分钟后开始｜后端工程师"
    with persistence.transaction("org_default") as transaction:
        appointment = transaction.interview_appointments.get("appointment_reminder")
    assert appointment["email_reminder"]["status"] == "sent"
    assert appointment["email_reminder"]["sent_at"]


def test_missing_smtp_configuration_keeps_reminder_retryable_without_fake_success() -> None:
    service, sender, persistence, work = _reminder_fixture(configured=False)

    result = service.process_work_item(work["id"])

    assert result["status"] == "failed"
    assert result["last_error_code"] == "SMTP_CONFIG_MISSING"
    assert result["error_retryable"] is True
    assert sender.messages == []
    with persistence.transaction("org_default") as transaction:
        appointment = transaction.interview_appointments.get("appointment_reminder")
    assert appointment["email_reminder"]["status"] == "waiting_configuration"
