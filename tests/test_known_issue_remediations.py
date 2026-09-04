from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.core.errors import ApiError
from app.domain.appointment_admission import AppointmentAdmission, format_utc
from app.migrations.plan_execution_v2 import migrate_plan_document
from app.repositories.memory import InMemoryStore
from app.services.reports import ReportService


UTC = timezone.utc


def _admission_inputs(now: datetime) -> tuple[dict, dict, dict]:
    appointment = {
        "scheduled_start_at": format_utc(now - timedelta(minutes=5)),
        "scheduled_end_at": format_utc(now + timedelta(minutes=55)),
        "settings": {"record_audio": True},
        "admission_policy": {
            "early_start_grace_seconds": 0,
            "late_start_grace_seconds": 0,
        },
        "device_readiness": {
            "ready": True,
            "expires_at": format_utc(now + timedelta(minutes=5)),
        },
    }
    intake = {
        "consent_evidence_status": "verified",
        "privacy_accepted": True,
        "media_consent_scopes": ["audio_recording"],
    }
    readiness = {
        "can_start": True,
        "expires_at": format_utc(now + timedelta(minutes=1)),
    }
    return appointment, intake, readiness


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda appointment, intake, readiness, now: intake.update(privacy_accepted=False), "CONSENT_REQUIRED"),
        (
            lambda appointment, intake, readiness, now: intake.update(media_consent_scopes=[]),
            "AUDIO_RECORDING_CONSENT_REQUIRED",
        ),
        (
            lambda appointment, intake, readiness, now: appointment.update(
                scheduled_start_at=format_utc(now + timedelta(seconds=1))
            ),
            "APPOINTMENT_TOO_EARLY",
        ),
        (
            lambda appointment, intake, readiness, now: appointment.update(
                scheduled_end_at=format_utc(now - timedelta(seconds=1))
            ),
            "APPOINTMENT_WINDOW_CLOSED",
        ),
        (
            lambda appointment, intake, readiness, now: appointment["device_readiness"].update(
                expires_at=format_utc(now - timedelta(seconds=1))
            ),
            "APPOINTMENT_DEVICE_NOT_READY",
        ),
        (
            lambda appointment, intake, readiness, now: readiness.update(
                expires_at=format_utc(now - timedelta(seconds=1))
            ),
            "APPOINTMENT_NOT_READY",
        ),
    ],
)
def test_appointment_admission_rejects_every_stale_or_invalid_gate(mutate, expected_code: str) -> None:
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    appointment, intake, readiness = _admission_inputs(now)
    mutate(appointment, intake, readiness, now)

    with pytest.raises(ApiError) as exc_info:
        AppointmentAdmission().validate_start(appointment, intake, readiness, now=now)

    assert exc_info.value.code == expected_code


def test_appointment_admission_accepts_current_verified_evidence() -> None:
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    appointment, intake, readiness = _admission_inputs(now)

    AppointmentAdmission().validate_start(appointment, intake, readiness, now=now)


def test_plan_execution_v2_migration_removes_items_without_semantic_drift() -> None:
    question = {
        "id": "q_legacy",
        "organization_id": "org_default",
        "job_position_id": "position_legacy",
        "knowledge_base_id": "kb_legacy",
        "status": "active",
        "validation_status": "valid",
        "speech_status": "ready",
        "difficulty": "mid",
        "skills": ["python"],
        "content_hash": "content-1",
        "version": 3,
    }
    legacy = {
        "id": "plan_legacy",
        "organization_id": "org_default",
        "job_position_id": "position_legacy",
        "knowledge_base_ids": ["kb_legacy"],
        "status": "approved",
        "estimated_minutes": 15,
        "experience_question_snapshots": [],
        "items": [
            {
                "id": "legacy_item_1",
                "question_id": "q_legacy",
                "dimension": "python",
                "weight": 1.0,
                "expected_minutes": 15,
                "allow_followup": False,
            }
        ],
        "version": 1,
    }

    migrated = migrate_plan_document(legacy, {question["id"]: question})

    assert migrated["execution_schema_version"] == 2
    assert "items" not in migrated
    assert migrated["bank_slots"][0]["candidate_pool"] == [
        {
            "question_id": "q_legacy",
            "question_version": 3,
            "question_hash": "content-1",
        }
    ]
    assert migrated["bank_slots"][0]["allow_followup"] is False
    assert migrated["migration_history"][0]["migration"] == "plan_execution_v2"


def test_report_uses_only_each_answers_current_evaluation_revision() -> None:
    current = {
        "id": "eval_current",
        "revision": 2,
        "question_id": "question_1",
        "question_snapshot_id": "snapshot_1",
        "score": 90,
        "confidence": 0.95,
        "covered_key_points": [{"key_point": "事务边界", "evidence": "说明了提交与回滚"}],
        "missing_key_points": [],
        "feedback": "当前修订证据充分。",
        "review_flags": [],
    }
    stale = {
        **deepcopy(current),
        "id": "eval_stale",
        "revision": 1,
        "score": 20,
        "confidence": 0.1,
        "feedback": "已被替换的旧修订。",
        "review_flags": ["low_confidence"],
    }
    interview = {
        "id": "interview_1",
        "organization_id": "org_default",
        "plan_snapshot": {
            "question_snapshots": [
                {
                    "question_snapshot_id": "snapshot_1",
                    "dimension": "database",
                    "weight": 1.0,
                }
            ]
        },
        "answers": [{"id": "answer_1", "current_evaluation_id": "eval_current"}],
        "evaluation_revisions": [stale, current],
    }

    report = ReportService(InMemoryStore()).build_report(interview, trigger_reason="revision_test")

    assert report["overall_score"] == 90
    assert report["job_fit_level"] == "strong_match"
    assert report["evaluation_ids"] == ["eval_current"]
    assert report["question_evaluations"][0]["summary"] == "当前修订证据充分。"

    interview["answers"][0]["current_evaluation_id"] = "eval_stale"
    flagged_report = ReportService(InMemoryStore()).build_report(
        interview,
        trigger_reason="current_low_confidence_test",
    )
    assert flagged_report["job_fit_level"] == "manual_review"
    assert flagged_report["evaluation_ids"] == ["eval_stale"]
