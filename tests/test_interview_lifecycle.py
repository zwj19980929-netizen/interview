from copy import deepcopy

import pytest

from app.core.errors import ApiError
from app.domain.interview_lifecycle import (
    InterviewSessionLifecycle,
    LifecycleCommand,
    LifecycleCommandType,
)


NOW = "2026-08-24T00:00:00Z"


def session(turn_count: int = 2) -> dict:
    return {
        "id": "iv_lifecycle",
        "organization_id": "org_a",
        "plan_id": "plan_a",
        "status": "scheduled",
        "current_turn_id": None,
        "turns": [
            {
                "id": "turn_%s" % index,
                "order": index,
                "status": "pending",
                "started_at": None,
                "completed_at": None,
            }
            for index in range(1, turn_count + 1)
        ],
        "answers": [],
        "evaluation_revisions": [],
        "report_revisions": [],
        "current_report_id": None,
        "report_id": None,
        "lifecycle_events": [],
        "created_at": NOW,
        "updated_at": NOW,
        "started_at": None,
        "completed_at": None,
    }


def command(kind: LifecycleCommandType, **payload) -> LifecycleCommand:
    return LifecycleCommand(kind, payload)


def started_session(turn_count: int = 2) -> tuple:
    lifecycle = InterviewSessionLifecycle()
    created = lifecycle.execute(session(turn_count), command(LifecycleCommandType.CREATE), now=NOW)
    ready = lifecycle.execute(
        created.session,
        command(LifecycleCommandType.PARTICIPANT_READY, participant="candidate", source="test"),
        now="2026-08-24T00:00:01Z",
    )
    started = lifecycle.execute(
        ready.session,
        command(LifecycleCommandType.START),
        now="2026-08-24T00:00:02Z",
    )
    return lifecycle, started.session


def answer(answer_id: str, turn_id: str) -> dict:
    return {
        "id": answer_id,
        "interview_id": "iv_lifecycle",
        "turn_id": turn_id,
        "evaluation_status": "pending",
        "current_evaluation_id": None,
        "evaluation_id": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def evaluation(evaluation_id: str, answer_id: str) -> dict:
    return {
        "id": evaluation_id,
        "interview_id": "iv_lifecycle",
        "answer_id": answer_id,
        "score": 80,
        "created_at": NOW,
        "updated_at": NOW,
    }


def test_lifecycle_interface_owns_start_and_event_sequence() -> None:
    lifecycle, started = started_session()

    assert started["status"] == "in_progress"
    assert started["current_turn_id"] == "turn_1"
    assert started["turns"][0]["status"] == "asking"
    assert [item["type"] for item in started["lifecycle_events"]] == [
        "interview.created",
        "interview.participant_ready",
        "interview.started",
    ]
    assert [item["sequence"] for item in started["lifecycle_events"]] == [1, 2, 3]
    assert lifecycle.require_active_turn(started, "turn_1")["id"] == "turn_1"


def test_invalid_transition_is_rejected_at_lifecycle_seam() -> None:
    lifecycle = InterviewSessionLifecycle()
    created = lifecycle.execute(session(), command(LifecycleCommandType.CREATE), now=NOW)

    with pytest.raises(ApiError) as exc_info:
        lifecycle.execute(created.session, command(LifecycleCommandType.PAUSE), now=NOW)
    assert exc_info.value.code == "INTERVIEW_LIFECYCLE_INVALID"

    with pytest.raises(ApiError) as exc_info:
        lifecycle.require_active_turn(created.session, "turn_1")
    assert exc_info.value.code == "INTERVIEW_NOT_IN_PROGRESS"


def test_answer_evaluation_advances_turn_and_emits_effects() -> None:
    lifecycle, started = started_session()
    submitted = lifecycle.execute(
        started,
        command(
            LifecycleCommandType.ANSWER_SUBMITTED,
            answer=answer("ans_1", "turn_1"),
            trigger_reason="initial_scoring",
        ),
        now="2026-08-24T00:00:03Z",
    )
    assert submitted.session["turns"][0]["status"] == "evaluating"
    assert submitted.effects == [
        {
            "type": "evaluation.requested",
            "payload": {"answer_id": "ans_1", "revision": 1, "trigger_reason": "initial_scoring"},
        }
    ]

    completed = lifecycle.execute(
        submitted.session,
        command(
            LifecycleCommandType.EVALUATION_SUCCEEDED,
            answer_id="ans_1",
            evaluation=evaluation("eval_1", "ans_1"),
            revision=1,
            trigger_reason="initial_scoring",
        ),
        now="2026-08-24T00:00:04Z",
    )
    assert completed.session["turns"][0]["status"] == "completed"
    assert completed.session["turns"][1]["status"] == "asking"
    assert completed.session["current_turn_id"] == "turn_2"
    assert completed.effects == []
    assert completed.session["answers"][0]["current_evaluation_id"] == "eval_1"


def test_last_evaluation_requests_report_without_transport_decision() -> None:
    lifecycle, started = started_session(turn_count=1)
    submitted = lifecycle.execute(
        started,
        command(LifecycleCommandType.ANSWER_SUBMITTED, answer=answer("ans_1", "turn_1")),
        now="2026-08-24T00:00:03Z",
    )
    assert submitted.session["status"] == "in_progress"
    assert submitted.session["candidate_input_completed_at"] == "2026-08-24T00:00:03Z"
    assert submitted.session["answers"][0]["evaluation_status"] == "pending"
    completed = lifecycle.execute(
        submitted.session,
        command(
            LifecycleCommandType.EVALUATION_SUCCEEDED,
            answer_id="ans_1",
            evaluation=evaluation("eval_1", "ans_1"),
            revision=1,
        ),
        now="2026-08-24T00:00:04Z",
    )

    assert completed.session["status"] == "report_generating"
    assert completed.session["current_turn_id"] is None
    assert completed.effects[0]["type"] == "report.requested"
    assert completed.effects[0]["payload"]["revision"] == 1
    assert [item["type"] for item in completed.events][-3:] == [
        "interview.questions_completed",
        "interview.completed",
        "report.requested",
    ]


def test_timeout_then_recovery_repairs_progress_and_requests_report() -> None:
    lifecycle, started = started_session(turn_count=1)
    submitted = lifecycle.execute(
        started,
        command(LifecycleCommandType.ANSWER_SUBMITTED, answer=answer("ans_1", "turn_1")),
        now="2026-08-24T00:00:03Z",
    )
    timed_out = lifecycle.execute(
        submitted.session,
        command(LifecycleCommandType.TIMEOUT, reason="no heartbeat"),
        now="2026-08-24T00:00:04Z",
    )
    assert timed_out.session["status"] == "paused"
    assert timed_out.session["interruption"]["kind"] == "timeout"

    evaluated = lifecycle.execute(
        timed_out.session,
        command(
            LifecycleCommandType.EVALUATION_SUCCEEDED,
            answer_id="ans_1",
            evaluation=evaluation("eval_1", "ans_1"),
            revision=1,
        ),
        now="2026-08-24T00:00:05Z",
    )
    assert evaluated.session["status"] == "paused"
    assert evaluated.effects == []

    recovered = lifecycle.execute(
        evaluated.session,
        command(LifecycleCommandType.RECOVER, reason="heartbeat restored"),
        now="2026-08-24T00:00:06Z",
    )
    assert recovered.session["status"] == "report_generating"
    assert recovered.effects[0]["type"] == "report.requested"
    assert recovered.session["interruption"] is None


def test_report_revisions_and_cancel_are_lifecycle_decisions() -> None:
    lifecycle, started = started_session(turn_count=1)
    requested = lifecycle.execute(
        started,
        command(LifecycleCommandType.COMPLETE, reason="interviewer ended", trigger_reason="manual_completion"),
        now="2026-08-24T00:00:03Z",
    )
    assert requested.session["turns"][0]["status"] == "skipped"
    assert [item["type"] for item in requested.events] == ["interview.completed", "report.requested"]
    report_payload = requested.effects[0]["payload"]
    reported = lifecycle.execute(
        requested.session,
        command(
            LifecycleCommandType.REPORT_SUCCEEDED,
            report={"id": "report_1", "interview_id": "iv_lifecycle"},
            revision=report_payload["revision"],
            previous_report_id=report_payload["previous_report_id"],
        ),
        now="2026-08-24T00:00:04Z",
    )
    assert reported.session["status"] == "report_ready"
    assert reported.session["current_report_id"] == "report_1"

    _, another = started_session(turn_count=2)
    cancelled = lifecycle.execute(
        deepcopy(another),
        command(LifecycleCommandType.CANCEL, reason="candidate withdrew"),
        now="2026-08-24T00:00:05Z",
    )
    assert cancelled.session["status"] == "cancelled"
    assert cancelled.session["current_turn_id"] is None
    assert {item["status"] for item in cancelled.session["turns"]} == {"skipped"}


def test_skip_current_turn_uses_the_same_progression_and_report_rules() -> None:
    lifecycle, started = started_session(turn_count=2)
    skipped = lifecycle.execute(
        started,
        command(LifecycleCommandType.SKIP_CURRENT_TURN, reason="out of scope"),
        now="2026-08-24T00:00:03Z",
    )
    assert skipped.session["turns"][0]["status"] == "skipped"
    assert skipped.session["turns"][1]["status"] == "asking"
    assert skipped.session["current_turn_id"] == "turn_2"
    assert [item["type"] for item in skipped.events] == ["turn.skipped", "turn.advanced"]
