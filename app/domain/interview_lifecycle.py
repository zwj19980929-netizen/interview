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
    UTTERANCE_REJECTED = "utterance_rejected"
    ANSWER_SUBMITTED = "answer_submitted"
    FOLLOWUP_REQUESTED = "followup_requested"
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
            LifecycleCommandType.UTTERANCE_REJECTED: self._utterance_rejected,
            LifecycleCommandType.ANSWER_SUBMITTED: self._answer_submitted,
            LifecycleCommandType.FOLLOWUP_REQUESTED: self._followup_requested,
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
        if mutable_turn.get("is_followup"):
            self._emit(
                session,
                events,
                "followup.completed",
                {
                    "turn_id": turn["id"],
                    "root_turn_id": mutable_turn.get("root_turn_id"),
                    "outcome": "skipped",
                },
                now,
            )
            self._maybe_request_evidence_group_evaluation(
                session,
                str(mutable_turn.get("root_turn_id") or ""),
                now,
                events,
                effects,
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
        if self._has_unresolved_evaluations(session):
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
        if session.get("current_turn_id") == turn["id"]:
            # The accepted answer closes the input gate immediately. A bounded
            # follow-up command may install a new current turn in the same
            # transaction; otherwise the client waits for the worker to advance.
            session["current_turn_id"] = None
        self._emit(
            session,
            events,
            "answer.submitted",
            {"answer_id": answer["id"], "turn_id": turn["id"]},
            now,
        )
        self._request_evaluation(session, answer, 1, payload.get("trigger_reason", "initial_scoring"), now, events, effects)
        if not payload.get("hold_for_followup", False):
            self._activate_next_without_waiting_for_evaluation(
                session, now, events, effects
            )

    def _followup_requested(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        root_turn_id = str(payload.get("root_turn_id") or "")
        root = self._turn(session, root_turn_id)
        followup = deepcopy(payload.get("followup_turn") or {})
        parent = self._turn(session, str(followup.get("parent_turn_id") or ""))
        existing = next(
            (
                item
                for item in session.get("turns", [])
                if item.get("is_followup")
                and item.get("parent_turn_id") == parent["id"]
                and int(item.get("followup_depth", 0))
                == int(followup.get("followup_depth", 0))
            ),
            None,
        )
        if existing is not None:
            return
        if session.get("status") != "in_progress" or parent.get("status") != "evaluating":
            self._invalid("A follow-up can only be selected while its parent answer is awaiting evaluation.")
        if root.get("is_followup") or int(root.get("followup_depth", 0)) != 0:
            self._invalid("A follow-up root must be an approved primary turn.")
        if not parent.get("allow_followup", False):
            self._invalid("The approved root turn does not allow a follow-up.")

        policy = session.get("followup_policy") or {}
        primary_count = len(
            [item for item in session.get("turns", []) if not item.get("is_followup")]
        )
        max_total = min(4, primary_count, max(0, int(policy.get("max_total", 0))))
        max_per_root = min(2, max(0, int(policy.get("max_per_root", 2))))
        max_depth = min(2, max(0, int(policy.get("max_depth", 2))))
        all_followups = [item for item in session.get("turns", []) if item.get("is_followup")]
        root_followups = [
            item for item in all_followups if item.get("root_turn_id") == root_turn_id
        ]
        if (
            len(all_followups) >= max_total
            or len(root_followups) >= max_per_root
            or max_per_root < 1
        ):
            self._invalid("The InterviewSession follow-up budget is exhausted.")

        if not followup.get("id") or not str(followup.get("question_spoken_text") or "").strip():
            self._invalid("A selected follow-up requires an id and a non-empty probe.")
        expected_depth = int(parent.get("followup_depth", 0)) + 1
        if int(followup.get("followup_depth", 0)) != expected_depth or expected_depth > max_depth:
            self._invalid("A selected follow-up exceeds the depth budget.")
        if followup.get("parent_turn_id") != parent["id"] or followup.get("root_turn_id") != root["id"]:
            self._invalid("A selected follow-up must bind its parent and root turns.")
        if float(followup.get("weight", 0.0)) != 0.0:
            self._invalid("A follow-up cannot change the approved plan score weight.")

        insertion_order = int(parent.get("order", 0)) + 1
        for turn in session.get("turns", []):
            if int(turn.get("order", 0)) >= insertion_order:
                turn["order"] = int(turn["order"]) + 1
        followup["order"] = insertion_order
        followup["status"] = "asking"
        followup["started_at"] = followup.get("started_at") or now
        followup["completed_at"] = None
        followup["allow_followup"] = expected_depth < max_depth
        followup["weight"] = 0.0
        session.setdefault("turns", []).append(followup)
        session.setdefault("turn_ids", []).append(followup["id"])
        session["current_turn_id"] = followup["id"]
        session["phase"] = followup.get("phase", parent.get("phase", session.get("phase")))
        requested_payload = {
            "root_turn_id": root["id"],
            "parent_turn_id": parent["id"],
            "answer_id": payload.get("answer_id"),
            "reason": followup.get("followup_reason"),
            "target_key_points": deepcopy(followup.get("target_key_points", [])),
        }
        self._emit(session, events, "followup.requested", requested_payload, now)
        self._emit(
            session,
            events,
            "followup.selected",
            {
                **requested_payload,
                "turn_id": followup["id"],
                "followup_depth": expected_depth,
                "question_text": followup["question_spoken_text"],
            },
            now,
        )

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

    def _utterance_rejected(
        self,
        session: Document,
        payload: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        turn = self.require_active_turn(
            session, payload.get("turn_id"), allowed_statuses=("transcribing",)
        )
        mutable_turn = self._turn(session, turn["id"])
        mutable_turn["status"] = "asking"
        mutable_turn["last_non_answer_intent"] = payload.get("intent")
        session["current_turn_id"] = turn["id"]
        problem = deepcopy(payload.get("problem"))
        self._emit(
            session,
            events,
            "utterance.not_accepted",
            {
                "turn_id": turn["id"],
                "utterance_id": payload.get("utterance_id"),
                "intent": payload.get("intent"),
                "suggested_action": payload.get("suggested_action"),
                "confidence": payload.get("confidence"),
                "problem": problem,
            },
            now,
        )
        if problem and payload.get("suggested_action") == "pause":
            session["status"] = "paused"
            self._emit(
                session,
                events,
                "interview.paused",
                {
                    "reason": "understanding_safety_pause",
                    "problem_code": problem.get("code"),
                },
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
        evidence_answer_ids = answer.get("pending_evidence_answer_ids") or answer.get(
            "current_evidence_answer_ids"
        )
        if evidence_answer_ids:
            event_payload["evidence_answer_ids"] = list(evidence_answer_ids)
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
        evidence_answer_ids = list(
            evaluation.get("evidence_answer_ids")
            or payload.get("evidence_answer_ids")
            or [answer["id"]]
        )
        answer["current_evidence_answer_ids"] = evidence_answer_ids
        answer["evidence_group_status"] = "completed"
        answer.pop("pending_evidence_answer_ids", None)
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
            if turn.get("is_followup"):
                self._emit(
                    session,
                    events,
                    "followup.completed",
                    {
                        "turn_id": turn["id"],
                        "root_turn_id": turn.get("root_turn_id"),
                        "answer_id": answer["id"],
                        "evaluation_id": evaluation["id"],
                        "outcome": "answered",
                    },
                    now,
                )
            self._maybe_request_evidence_group_evaluation(
                session,
                str(turn.get("root_turn_id") or turn["id"]),
                now,
                events,
                effects,
            )
            if session.get("current_turn_id") == turn["id"]:
                session["current_turn_id"] = None
            active_child = next(
                (
                    item
                    for item in session.get("turns", [])
                    if item.get("is_followup")
                    and item.get("root_turn_id") == turn["id"]
                    and item.get("status") in {"asking", "transcribing", "evaluating"}
                ),
                None,
            )
            if (
                session["status"] == "in_progress"
                and session.get("current_turn_id") is None
                and active_child is None
                and not self._has_unresolved_evaluations(session)
            ):
                self._activate_next_or_report(session, now, events, effects)
        elif previous_id and session.get("current_report_id"):
            self._request_report(session, "answer_regraded", now, events, effects, force=True)
        elif (
            session.get("status") == "in_progress"
            and session.get("current_turn_id") is None
            and not self._has_unresolved_evaluations(session)
        ):
            self._activate_next_or_report(session, now, events, effects)

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
        if self._has_unresolved_evaluations(session):
            return
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

    def _activate_next_without_waiting_for_evaluation(
        self,
        session: Document,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        """Advance the conversational floor while scoring continues off-path."""

        next_turn = self._first_turn_with_status(session, ("pending",))
        if next_turn is None:
            session["current_turn_id"] = None
            # Candidate input and asynchronous scoring are deliberately
            # separate milestones.  Closing the conversational/media plane
            # must not wait for a scoring worker, while the domain session
            # remains in progress until all evaluation revisions are durable.
            session["candidate_input_completed_at"] = (
                session.get("candidate_input_completed_at") or now
            )
            self._emit(
                session,
                events,
                "interview.awaiting_evaluations",
                {"pending_count": sum(1 for item in session.get("turns", []) if item.get("status") == "evaluating")},
                now,
            )
            return
        previous_phase = session.get("phase")
        next_turn["status"] = "asking"
        session["candidate_input_completed_at"] = None
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
        self._emit(
            session,
            events,
            "turn.advanced",
            {"turn_id": next_turn["id"], "scoring_in_background": True},
            now,
        )

    @staticmethod
    def _has_unresolved_evaluations(session: Document) -> bool:
        return any(turn.get("status") == "evaluating" for turn in session.get("turns", [])) or any(
            answer.get("evaluation_status") in {"pending", "failed"}
            for answer in session.get("answers", [])
        )

    def _maybe_request_evidence_group_evaluation(
        self,
        session: Document,
        root_turn_id: str,
        now: str,
        events: List[Document],
        effects: List[Document],
    ) -> None:
        """Queue one root-answer revision after its complete follow-up chain settles.

        Follow-up evaluations remain audit evidence, but never become weighted plan
        items.  The root answer is the sole score-bearing aggregate and is regraded
        from every authoritative answer in turn order.
        """

        root = self._turn_or_none(session, root_turn_id)
        if root is None or root.get("is_followup"):
            return
        group_turns = sorted(
            [
                item
                for item in session.get("turns", [])
                if item.get("id") == root_turn_id
                or (
                    item.get("is_followup")
                    and item.get("root_turn_id") == root_turn_id
                )
            ],
            key=lambda item: int(item.get("order", 0)),
        )
        if any(
            item.get("status") in {"pending", "asking", "transcribing", "evaluating"}
            for item in group_turns
        ):
            return
        answers_by_turn = {
            item["turn_id"]: item for item in session.get("answers", [])
        }
        evidence_answers = [
            answers_by_turn[item["id"]]
            for item in group_turns
            if item["id"] in answers_by_turn
        ]
        if len(evidence_answers) < 2:
            return
        root_answer = answers_by_turn.get(root_turn_id)
        if root_answer is None:
            return
        evidence_answer_ids = [item["id"] for item in evidence_answers]
        if root_answer.get("pending_evidence_answer_ids") == evidence_answer_ids:
            return
        if root_answer.get("current_evidence_answer_ids") == evidence_answer_ids:
            return
        existing = [
            item
            for item in session.get("evaluation_revisions", [])
            if item.get("answer_id") == root_answer["id"]
        ]
        if not existing:
            # The initial root score must be an immutable revision before merged
            # evidence can supersede it. A later evaluation completion retries
            # this gate once the prerequisite exists.
            return
        revision = max(int(item["revision"]) for item in existing) + 1
        root_answer["evaluation_status"] = "pending"
        root_answer["evidence_group_status"] = "pending"
        root_answer["pending_evidence_answer_ids"] = evidence_answer_ids
        root_answer["updated_at"] = now
        self._emit(
            session,
            events,
            "evaluation.evidence_group_requested",
            {
                "root_turn_id": root_turn_id,
                "root_answer_id": root_answer["id"],
                "evidence_answer_ids": evidence_answer_ids,
                "revision": revision,
            },
            now,
        )
        self._request_evaluation(
            session,
            root_answer,
            revision,
            "followup_evidence_merged",
            now,
            events,
            effects,
        )

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
