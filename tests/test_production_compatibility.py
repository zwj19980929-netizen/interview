import pytest
from pydantic import ValidationError

from app.repositories.memory import InMemoryStore
from app.main import create_app
from app.migrations.plan_execution_v2 import migrate_session_document
from app.core.errors import ApiError
from app.services.interviews import InterviewService
from app.schemas.api import InterviewAppointmentCreate, InterviewPlanPatch


def test_appointment_avatar_mode_is_explicit_and_defaults_to_local() -> None:
    base = {
        "plan_id": "plan_1",
        "candidate_profile_id": "candidate_1",
        "job_position_id": "position_1",
        "scheduled_start_at": "2026-08-28T10:00:00Z",
        "scheduled_end_at": "2026-08-28T11:00:00Z",
    }
    assert InterviewAppointmentCreate.model_validate(base).settings.avatar_mode == "local"
    assert InterviewAppointmentCreate.model_validate(
        {**base, "settings": {"avatar_mode": "cloud"}}
    ).settings.avatar_mode == "cloud"
    with pytest.raises(ValidationError):
        InterviewAppointmentCreate.model_validate(
            {**base, "settings": {"avatar_mode": "unknown"}}
        )


def test_direct_interview_and_plan_items_interfaces_are_removed(monkeypatch) -> None:
    store = InMemoryStore()
    interviews = InterviewService(store)
    assert not hasattr(interviews, "create_interview")
    assert not hasattr(interviews, "start_interview")

    paths = create_app().openapi()["paths"]
    assert "/api/v1/interviews" not in {
        path for path, operations in paths.items() if "post" in operations
    }
    assert "/api/v1/interviews/{interview_id}/start" not in paths
    assert "/api/v1/interviews/{interview_id}/answers" not in paths
    assert "/api/v1/questions" not in paths

    with pytest.raises(ValidationError):
        InterviewPlanPatch.model_validate({"expected_version": 1, "items": []})

    migrated = migrate_session_document(
        {
            "id": "interview_legacy",
            "plan_snapshot": {"items": [{"question_snapshot_id": "snapshot_1"}]},
            "turns": [{"id": "turn_1", "plan_item_id": "legacy_blueprint"}],
            "updated_at": "before",
        }
    )
    assert "items" not in migrated["plan_snapshot"]
    assert migrated["plan_snapshot"]["question_snapshots"] == [
        {"question_snapshot_id": "snapshot_1"}
    ]
    assert migrated["turns"][0]["turn_blueprint_id"] == "legacy_blueprint"
    assert "plan_item_id" not in migrated["turns"][0]

    with interviews.persistence.transaction("org_default") as transaction:
        session = transaction.interview_sessions.add(
            {
                "id": "iv_projection",
                "organization_id": "org_default",
                "status": "in_progress",
                "phase": "position_bank",
                "current_turn_id": "turn_current",
                "candidate": {"name": "候选人", "email": "secret@example.com"},
                "turns": [
                    {
                        "id": "turn_current",
                        "order": 1,
                        "status": "asking",
                        "question_spoken_text": "当前题目",
                        "question_snapshot": {"standard_answer": "当前标准答案"},
                    },
                    {
                        "id": "turn_future",
                        "order": 2,
                        "status": "pending",
                        "question_spoken_text": "未来题目",
                        "question_snapshot": {"standard_answer": "未来标准答案"},
                    },
                ],
                "answers": [],
                "created_at": "2026-08-25T00:00:00Z",
                "updated_at": "2026-08-25T00:00:00Z",
            }
        )
    token = interviews.candidate_join_url(session).split("token=", 1)[1]
    projection = interviews.get_candidate_interview(session["id"], token)
    assert projection["candidate"] == {"name": "候选人"}
    assert projection["turns"][0]["question_spoken_text"] == "当前题目"
    assert "question_spoken_text" not in projection["turns"][1]
    assert "question_snapshot" not in projection["turns"][0]

    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.delenv("INTERVIEWER_CANDIDATE_TOKEN_SECRET", raising=False)
    with pytest.raises(ApiError) as missing_secret:
        interviews.candidate_join_url({"id": "iv_1", "created_at": "2026-08-25T00:00:00Z"})
    assert missing_secret.value.code == "CANDIDATE_TOKEN_SECRET_REQUIRED"
