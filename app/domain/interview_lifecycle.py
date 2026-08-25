from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now


Document = Dict[str, Any]


class LifecycleCommandType(str, Enum):
    CREATE = "create"
    PARTICIPANT_READY = "participant_ready"
    START = "start"
    PAUSE = "pause"
    TIMEOUT = "timeout"
    RESUME = "resume"
    RECOVER = "recover"
    CANCEL = "cancel"
    SKIP_CURRENT_TURN = "skip_current_turn"
    COMPLETE = "complete"
    TRANSCRIPTION_STARTED = "transcription_started"
    TRANSCRIPTION_FAILED = "transcription_failed"
    ANSWER_SUBMITTED = "answer_submitted"
    REGRADE_REQUESTED = "regrade_requested"
    EVALUATION_SUCCEEDED = "evaluation_succeeded"
    EVALUATION_FAILED = "evaluation_failed"
    EVALUATION_RETRY_STARTED = "evaluation_retry_started"
    REPORT_REQUESTED = "report_requested"
    REPORT_RETRY_STARTED = "report_retry_started"
    REPORT_SUCCEEDED = "report_succeeded"
    REPORT_FAILED = "report_failed"


@dataclass(frozen=True)
class LifecycleCommand:
    type: LifecycleCommandType
    payload: Document = field(default_factory=dict)


@dataclass
class LifecycleDecision:
    session: Document
    events: List[Document]
    effects: List[Document]
    changed: bool


class InterviewSessionLifecycle:
    """The single in-process seam for InterviewSession lifecycle decisions."""

    def execute(
        self,
        source: Document,
        command: LifecycleCommand,
        *,
        now: Optional[str] = None,
    ) -> LifecycleDecision:
        session = deepcopy(source)
        occurred_at = now or utc_now()
        session.setdefault("lifecycle_events", [])
        session.setdefault("last_activity_at", session.get("updated_at") or occurred_at)
        session.setdefault("interruption", None)
        events: List[Document] = []
        effects: List[Document] = []

        handlers = {
            LifecycleCommandType.CREATE: self._create,
            LifecycleCommandType.PARTICIPANT_READY: self._participant_ready,
            LifecycleCommandType.START: self._start,
            LifecycleCommandType.PAUSE: self._pause,
            LifecycleCommandType.TIMEOUT: self._timeout,
            LifecycleCommandType.RESUME: self._resume,
            LifecycleCommandType.RECOVER: self._recover,
            LifecycleCommandType.CANCEL: self._cancel,
            LifecycleCommandType.SKIP_CURRENT_TURN: self._skip_current_turn,
            LifecycleCommandType.COMPLETE: self._complete,
            LifecycleCommandType.TRANSCRIPTION_STARTED: self._transcription_started,
            LifecycleCommandType.TRANSCRIPTION_FAILED: self._transcription_failed,
            LifecycleCommandType.ANSWER_SUBMITTED: self._answer_submitted,
            LifecycleCommandType.REGRADE_REQUESTED: self._regrade_requested,
            LifecycleCommandType.EVALUATION_SUCCEEDED: self._evaluation_succeeded,
            LifecycleCommandType.EVALUATION_FAILED: self._evaluation_failed,
            LifecycleCommandType.EVALUATION_RETRY_STARTED: self._evaluation_retry_started,
            LifecycleCommandType.REPORT_REQUESTED: self._report_requested,
            LifecycleCommandType.REPORT_RETRY_STARTED: self._report_retry_started,
            LifecycleCommandType.REPORT_SUCCEEDED: self._report_succeeded,
            LifecycleCommandType.REPORT_FAILED: self._report_failed,
        }
        handler = handlers[command.type]
        handler(session, command.payload, occurred_at, events, effects)
        changed = bool(events or effects or session != source)
        if changed:
            session["updated_at"] = occurred_at
            session["last_activity_at"] = occurred_at
        return LifecycleDecision(session=session, events=events, effects=effects, changed=changed)

    def require_active_turn(
        self,
        session: Document,
        turn_id: Optional[str],
        *,
        allowed_statuses: Sequence[str] = ("asking",),
    ) -> Document:
        if session.get("status") != "in_progress":
            raise ApiError("INTERVIEW_NOT_IN_PROGRESS", "Interview is not in progress.", status_code=409)
        active_turn_id = session.get("current_turn_id")
        if not turn_id or turn_id != active_turn_id:
            raise ApiError("INTERVIEW_TURN_NOT_ACTIVE", "Command does not belong to the active turn.", status_code=409)
        turn = self._turn(session, turn_id)
        if turn.get("status") not in set(allowed_statuses):
            raise ApiError("INTERVIEW_TURN_NOT_ACTIVE", "Interview turn cannot accept this command.", status_code=409)
        return deepcopy(turn)

    def _create(self, session: Document, payload: Document, now: str, events: List[Document], effects: List[Document]) -> None:
        if session.get("status") != "scheduled":
            self._invalid("A new InterviewSession must be scheduled.")
        if not session.get("turns"):
            self._invalid("An InterviewSession must contain at least one turn.")
        if events or session.get("lifecycle_events"):
            return
        self._emit(session, events, "interview.created", {"plan_id": session.get("plan_id")}, now)

    def _participant_ready(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        if session["status"] == "scheduled":
            session["status"] = "waiting"
        if session["status"] not in {"waiting", "in_progress", "paused"}:
            self._invalid("Participant readiness is not accepted in the current state.")
        self._emit(
            session,
            events,
            "interview.participant_ready",
            {"participant": payload.get("participant", "candidate"), "source": payload.get("source", "web")},
            now,
        )

    def _start(self, session: Document, payload: Document, now: str, events: List[Document], effects: List[Document]) -> None:
        if session["status"] == "in_progress":
            return
        if session["status"] not in {"scheduled", "waiting"}:
            self._invalid("Only a scheduled or waiting InterviewSession can be started.")
        session["status"] = "in_progress"
        session["started_at"] = session.get("started_at") or now
        session["interruption"] = None
        turn = self._first_turn_with_status(session, ("asking", "pending"))
        if turn is not None:
            turn["status"] = "asking"
            turn["started_at"] = turn.get("started_at") or now
            session["current_turn_id"] = turn["id"]
            session["phase"] = turn.get("phase", session.get("phase", "position_bank"))
        self._emit(session, events, "interview.started", {"current_turn_id": session.get("current_turn_id")}, now)

    def _pause(self, session: Document, payload: Document, now: str, events: List[Document], effects: List[Document]) -> None:
        self._interrupt(session, payload, now, events, event_type="interview.paused", kind="manual")

    def _timeout(self, session: Document, payload: Document, now: str, events: List[Document], effects: List[Document]) -> None:
        self._interrupt(session, payload, now, events, event_type="interview.timed_out", kind="timeout")

    def _interrupt(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        *,
        event_type: str,
        kind: str,
    ) -> None:
        if session["status"] == "paused":
            return
        if session["status"] != "in_progress":
            self._invalid("Only an in-progress InterviewSession can be interrupted.")
        session["status"] = "paused"
        session["interruption"] = {
            "kind": kind,
            "reason": payload.get("reason", kind),
            "turn_id": session.get("current_turn_id"),
            "occurred_at": now,
        }
        self._emit(session, events, event_type, deepcopy(session["interruption"]), now)

    def _resume(self, session: Document, payload: Document, now: str, events: List[Document], effects: List[Document]) -> None:
        self._restore(session, payload, now, events, effects, "interview.resumed")

    def _recover(self, session: Document, payload: Document, now: str, events: List[Document], effects: List[Document]) -> None:
        self._restore(session, payload, now, events, effects, "interview.recovered")

    def _restore(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
        event_type: str,
    ) -> None:
        if session["status"] == "in_progress":
            return
        if session["status"] != "paused":
            self._invalid("Only a paused InterviewSession can be restored.")
        interruption = deepcopy(session.get("interruption"))
        session["status"] = "in_progress"
        session["interruption"] = None
        current = self._turn_or_none(session, session.get("current_turn_id"))
        if current is None or current.get("status") in {"completed", "skipped"}:
            session["current_turn_id"] = None
            self._activate_next_or_report(session, now, events, effects)
        self._emit(session, events, event_type, {"interruption": interruption}, now)

    def _cancel(self, session: Document, payload: Document, now: str, events: List[Document], effects: List[Document]) -> None:
        if session["status"] == "cancelled":
            return
        if session["status"] not in {"scheduled", "waiting", "in_progress", "paused"}:
            self._invalid("InterviewSession cannot be cancelled from its current state.")
        for turn in session["turns"]:
            if turn["status"] in {"pending", "asking", "answering", "evaluating"}:
                turn["status"] = "skipped"
                turn["completed_at"] = now
        session["status"] = "cancelled"
        session["current_turn_id"] = None
        session["completed_at"] = now
        session["interruption"] = None
        self._emit(session, events, "interview.cancelled", {"reason": payload.get("reason", "cancelled")}, now)

    def _skip_current_turn(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        turn = self.require_active_turn(session, session.get("current_turn_id"), allowed_statuses=("asking",))
        mutable_turn = self._turn(session, turn["id"])
        mutable_turn["status"] = "skipped"
        mutable_turn["completed_at"] = now
        session["current_turn_id"] = None
        self._emit(
            session,
            events,
            "turn.skipped",
            {"turn_id": turn["id"], "reason": payload.get("reason", "interviewer_skipped")},
            now,
        )
        self._activate_next_or_report(session, now, events, effects)

    def _complete(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        if session["status"] in {"report_generating", "report_ready"}:
            return
        if session["status"] not in {"in_progress", "paused", "completed"}:
            self._invalid("InterviewSession cannot be completed from its current state.")
        if any(turn["status"] == "evaluating" for turn in session["turns"]):
            self._invalid("InterviewSession cannot be completed while an answer evaluation is pending.")
        for turn in session["turns"]:
            if turn["status"] in {"pending", "asking", "answering"}:
                turn["status"] = "skipped"
                turn["completed_at"] = now
        session["status"] = "completed"
        session["current_turn_id"] = None
        session["completed_at"] = session.get("completed_at") or now
        session["interruption"] = None
        self._emit(
            session,
            events,
            "interview.completed",
            {"reason": payload.get("reason", "interviewer_completed")},
            now,
        )
        self._request_report(session, payload.get("trigger_reason", "interviewer_completed"), now, events, effects)

    def _answer_submitted(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        answer = deepcopy(payload["answer"])
        turn = self.require_active_turn(session, answer["turn_id"], allowed_statuses=("asking", "transcribing"))
        mutable_turn = self._turn(session, turn["id"])
        if any(item["turn_id"] == turn["id"] for item in session.get("answers", [])):
            self._invalid("The active turn already has an answer.")
        session.setdefault("answers", []).append(answer)
        mutable_turn["status"] = "evaluating"
        self._emit(
            session,
            events,
            "answer.submitted",
            {"answer_id": answer["id"], "turn_id": turn["id"]},
            now,
        )
        self._request_evaluation(session, answer, 1, payload.get("trigger_reason", "initial_scoring"), now, events, effects)

    def _transcription_started(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        turn = self.require_active_turn(session, payload.get("turn_id"), allowed_statuses=("asking",))
        mutable_turn = self._turn(session, turn["id"])
        mutable_turn["status"] = "transcribing"
        mutable_turn["recording"] = deepcopy(payload.get("recording", {}))
        self._emit(
            session,
            events,
            "transcription.started",
            {"turn_id": turn["id"], "audio_uri": payload.get("recording", {}).get("audio_uri")},
            now,
        )

    def _transcription_failed(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        turn = self.require_active_turn(session, payload.get("turn_id"), allowed_statuses=("transcribing",))
        mutable_turn = self._turn(session, turn["id"])
        mutable_turn["status"] = "asking"
        mutable_turn["transcription_error"] = str(payload.get("error", "transcription failed"))[:500]
        self._emit(
            session,
            events,
            "transcription.failed",
            {"turn_id": turn["id"], "error": mutable_turn["transcription_error"]},
            now,
        )

    def _regrade_requested(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        if session["status"] == "report_generating":
            self._invalid("An answer cannot be regraded while a report is being generated.")
        answer = self._answer(session, payload["answer_id"])
        current_revision = max(
            [item["revision"] for item in session.get("evaluation_revisions", []) if item["answer_id"] == answer["id"]],
            default=0,
        )
        revision = current_revision + 1
        answer["evaluation_status"] = "pending"
        answer["updated_at"] = now
        self._request_evaluation(session, answer, revision, payload.get("trigger_reason", "manual_regrade"), now, events, effects)

    def _request_evaluation(
        self,
        session: Document,
        answer: Document,
        revision: int,
        trigger_reason: str,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        event_payload = {
            "answer_id": answer["id"],
            "revision": revision,
            "trigger_reason": trigger_reason,
        }
        self._emit(session, events, "evaluation.requested", event_payload, now)
        effects.append({"type": "evaluation.requested", "payload": deepcopy(event_payload)})

    def _evaluation_succeeded(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        answer = self._answer(session, payload["answer_id"])
        evaluation = deepcopy(payload["evaluation"])
        existing = [item for item in session.get("evaluation_revisions", []) if item["answer_id"] == answer["id"]]
        expected_revision = max([item["revision"] for item in existing], default=0) + 1
        revision = int(payload.get("revision", expected_revision))
        if revision != expected_revision:
            self._invalid("Evaluation revision is not the next append-only revision.")
        previous_id = answer.get("current_evaluation_id")
        evaluation["revision"] = revision
        evaluation["supersedes_evaluation_id"] = previous_id
        evaluation["trigger_reason"] = payload.get("trigger_reason", "initial_scoring")
        session.setdefault("evaluation_revisions", []).append(evaluation)
        answer["current_evaluation_id"] = evaluation["id"]
        answer["evaluation_id"] = evaluation["id"]
        answer["evaluation_status"] = "completed"
        answer["updated_at"] = now
        self._emit(
            session,
            events,
            "evaluation.completed",
            {"answer_id": answer["id"], "evaluation_id": evaluation["id"], "revision": revision},
            now,
        )

        turn = self._turn(session, answer["turn_id"])
        if turn["status"] == "evaluating":
            turn["status"] = "completed"
            turn["completed_at"] = now
            self._emit(session, events, "turn.completed", {"turn_id": turn["id"]}, now)
            if session["status"] == "in_progress":
                self._activate_next_or_report(session, now, events, effects)
        elif previous_id and session.get("current_report_id"):
            self._request_report(session, "answer_regraded", now, events, effects, force=True)

    def _evaluation_failed(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        answer = self._answer(session, payload["answer_id"])
        answer["evaluation_status"] = "failed"
        answer["updated_at"] = now
        self._emit(
            session,
            events,
            "evaluation.failed",
            {"answer_id": answer["id"], "error": str(payload.get("error", "evaluation failed"))[:500]},
            now,
        )

    def _evaluation_retry_started(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        answer = self._answer(session, payload["answer_id"])
        turn = self._turn(session, answer["turn_id"])
        if answer.get("evaluation_status") != "failed" or turn.get("status") != "evaluating":
            self._invalid("A failed evaluation cannot be retried from the current answer state.")
        expected_revision = max(
            [item["revision"] for item in session.get("evaluation_revisions", []) if item["answer_id"] == answer["id"]],
            default=0,
        ) + 1
        revision = int(payload.get("revision", expected_revision))
        if revision != expected_revision:
            self._invalid("Evaluation retry revision does not match the pending append-only revision.")
        answer["evaluation_status"] = "pending"
        answer["updated_at"] = now
        self._emit(
            session,
            events,
            "evaluation.retry_started",
            {"answer_id": answer["id"], "revision": revision},
            now,
        )

    def _report_requested(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        self._request_report(
            session,
            payload.get("trigger_reason", "interview_completed"),
            now,
            events,
            effects,
            force=bool(payload.get("force", False)),
        )

    def _request_report(
        self,
        session: Document,
        trigger_reason: str,
        now: str,
        events: List[Document],
        effects: List[Document],
        *,
        force: bool = False,
    ) -> None:
        if session["status"] == "report_generating":
            return
        if session["status"] == "report_ready" and not force:
            return
        if session["status"] not in {"in_progress", "completed", "report_ready"}:
            self._invalid("A report cannot be requested from the current InterviewSession state.")
        revision = len(session.get("report_revisions", [])) + 1
        payload = {
            "interview_id": session["id"],
            "revision": revision,
            "trigger_reason": trigger_reason,
            "previous_report_id": session.get("current_report_id"),
        }
        session["status"] = "report_generating"
        session["current_turn_id"] = None
        session["completed_at"] = session.get("completed_at") or now
        self._emit(session, events, "report.requested", payload, now)
        effects.append({"type": "report.requested", "payload": deepcopy(payload)})

    def _report_succeeded(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        if session["status"] != "report_generating":
            self._invalid("A report result is not expected in the current InterviewSession state.")
        report = deepcopy(payload["report"])
        expected_revision = len(session.get("report_revisions", [])) + 1
        revision = int(payload.get("revision", expected_revision))
        if revision != expected_revision:
            self._invalid("Report revision is not the next append-only revision.")
        report["revision"] = revision
        report["supersedes_report_id"] = payload.get("previous_report_id")
        session.setdefault("report_revisions", []).append(report)
        session["current_report_id"] = report["id"]
        session["report_id"] = report["id"]
        session["status"] = "report_ready"
        self._emit(
            session,
            events,
            "report.completed",
            {"report_id": report["id"], "revision": revision},
            now,
        )

    def _report_retry_started(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        if session["status"] == "report_generating":
            return
        if session["status"] not in {"completed", "report_ready"}:
            self._invalid("A failed report cannot be retried from the current InterviewSession state.")
        expected_revision = len(session.get("report_revisions", [])) + 1
        revision = int(payload.get("revision", expected_revision))
        if revision != expected_revision:
            self._invalid("Report retry revision does not match the pending append-only revision.")
        session["status"] = "report_generating"
        self._emit(session, events, "report.retry_started", {"revision": revision}, now)

    def _report_failed(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        if session["status"] != "report_generating":
            self._invalid("A report failure is not expected in the current InterviewSession state.")
        session["status"] = "report_ready" if session.get("current_report_id") else "completed"
        self._emit(
            session,
            events,
            "report.failed",
            {"error": str(payload.get("error", "report failed"))[:500]},
            now,
        )

    def _activate_next_or_report(
        self,
        session: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        next_turn = self._first_turn_with_status(session, ("pending",))
        if next_turn is not None:
            previous_phase = session.get("phase")
            next_turn["status"] = "asking"
            next_turn["started_at"] = next_turn.get("started_at") or now
            session["current_turn_id"] = next_turn["id"]
            session["phase"] = next_turn.get("phase", previous_phase or "position_bank")
            if previous_phase and previous_phase != session["phase"]:
                self._emit(
                    session,
                    events,
                    "interview.phase_changed",
                    {"from": previous_phase, "to": session["phase"]},
                    now,
                )
            self._emit(session, events, "turn.advanced", {"turn_id": next_turn["id"]}, now)
            return
        session["current_turn_id"] = None
        self._emit(session, events, "interview.questions_completed", {}, now)
        session["status"] = "completed"
        session["completed_at"] = session.get("completed_at") or now
        self._emit(session, events, "interview.completed", {"reason": "questions_completed"}, now)
        self._request_report(session, "interview_completed", now, events, effects)

    def _emit(
        self,
        session: Document,
        events: List[Document],
        event_type: str,
        payload: Document,
        now: str,
    ) -> None:
        event = {
            "id": new_id("lifecycle_event"),
            "organization_id": session["organization_id"],
            "interview_id": session["id"],
            "sequence": len(session.setdefault("lifecycle_events", [])) + 1,
            "type": event_type,
            "payload": deepcopy(payload),
            "occurred_at": now,
        }
        session["lifecycle_events"].append(event)
        events.append(deepcopy(event))

    def _turn(self, session: Document, turn_id: str) -> Document:
        turn = self._turn_or_none(session, turn_id)
        if turn is None:
            raise ApiError("INTERVIEW_TURN_NOT_FOUND", "Interview turn does not exist.", status_code=404)
        return turn

    def _turn_or_none(self, session: Document, turn_id: Optional[str]) -> Optional[Document]:
        if not turn_id:
            return None
        return next((item for item in session.get("turns", []) if item["id"] == turn_id), None)

    def _first_turn_with_status(self, session: Document, statuses: Sequence[str]) -> Optional[Document]:
        allowed = set(statuses)
        return next(
            (item for item in sorted(session.get("turns", []), key=lambda value: value["order"]) if item["status"] in allowed),
            None,
        )

    def _answer(self, session: Document, answer_id: str) -> Document:
        answer = next((item for item in session.get("answers", []) if item["id"] == answer_id), None)
        if answer is None:
            raise ApiError("ANSWER_NOT_FOUND", "Answer does not exist.", status_code=404)
        return answer

    def _invalid(self, message: str) -> None:
        raise ApiError("INTERVIEW_LIFECYCLE_INVALID", message, status_code=409)
