from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.domain.interview_lifecycle import (
    InterviewSessionLifecycle,
    LifecycleCommand,
    LifecycleCommandType,
    LifecycleDecision,
)
from app.persistence.interface import Persistence, new_work_item
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.evaluation import EvaluationService
from app.services.reports import ReportService


class InterviewService:
    """Transactional orchestration behind the InterviewSession lifecycle seam."""

    def __init__(self, store: InMemoryStore, *, persistence: Optional[Persistence] = None) -> None:
        self.persistence = persistence or persistence_for(store)
        self.lifecycle = InterviewSessionLifecycle()
        self.evaluation = EvaluationService(store, persistence=self.persistence)
        self.reports = ReportService(store, persistence=self.persistence)

    def list_interviews(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return transaction.interview_sessions.list()

    def create_interview(
        self,
        payload: Dict[str, Any],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            plan = transaction.interview_plans.get(payload["plan_id"])
            if plan is None:
                raise ApiError("INTERVIEW_PLAN_NOT_FOUND", "Interview plan does not exist.", status_code=404)
            if plan["status"] != "approved":
                raise ApiError(
                    "INTERVIEW_PLAN_NOT_APPROVED",
                    "A formal interview can only be created from an approved plan.",
                    status_code=409,
                )
            role = transaction.role_requirements.get(plan["role_requirement_id"])
            if role is None:
                raise ApiError("ROLE_REQUIREMENT_NOT_FOUND", "Role requirement does not exist.", status_code=404)

            interview_id = new_id("iv")
            now = utc_now()
            candidate = {
                "id": new_id("cand"),
                "organization_id": organization_id,
                "interview_id": interview_id,
                "name": payload["candidate"]["name"],
                "email": payload["candidate"].get("email"),
                "metadata": deepcopy(payload["candidate"].get("metadata", {})),
                "created_at": now,
            }
            turns: List[Dict[str, Any]] = []
            snapshot_items: List[Dict[str, Any]] = []
            for plan_item in sorted(plan["items"], key=lambda item: item["order"]):
                question = transaction.questions.get(plan_item["question_id"])
                if question is None:
                    raise ApiError(
                        "INTERVIEW_PLAN_QUESTION_NOT_FOUND",
                        "An approved plan refers to a question that no longer exists.",
                        status_code=409,
                    )
                question_snapshot = self._question_snapshot(question, now)
                snapshot_item = deepcopy(plan_item)
                snapshot_item["question_snapshot_id"] = question_snapshot["id"]
                snapshot_items.append(snapshot_item)
                turns.append(
                    {
                        "id": new_id("turn"),
                        "interview_id": interview_id,
                        "plan_item_id": plan_item["id"],
                        "plan_item_snapshot_id": plan_item["id"],
                        "question_id": question["id"],
                        "question_snapshot_id": question_snapshot["id"],
                        "question_snapshot": question_snapshot,
                        "order": plan_item["order"],
                        "status": "pending",
                        "question_spoken_text": question_snapshot["spoken_text"],
                        "started_at": None,
                        "completed_at": None,
                    }
                )

            plan_snapshot = {
                "id": new_id("plan_snapshot"),
                "source_plan_id": plan["id"],
                "source_plan_version": plan["version"],
                "approved_at": plan.get("approved_at") or plan["updated_at"],
                "estimated_minutes": plan["estimated_minutes"],
                "assembly_policy": deepcopy(plan.get("assembly_policy", {})),
                "assembly_summary": deepcopy(plan.get("assembly_summary", {})),
                "role_requirement": deepcopy(role),
                "items": snapshot_items,
                "created_at": now,
            }
            candidate_session_token = new_id("candidate_token")
            session = {
                "id": interview_id,
                "organization_id": organization_id,
                "plan_id": plan["id"],
                "plan_snapshot": plan_snapshot,
                "candidate_id": candidate["id"],
                "candidate": candidate,
                "status": "scheduled",
                "settings": deepcopy(payload.get("settings", {})),
                "scheduled_at": payload.get("scheduled_at"),
                "current_turn_id": None,
                "turn_ids": [turn["id"] for turn in turns],
                "turns": turns,
                "answers": [],
                "evaluation_revisions": [],
                "report_revisions": [],
                "current_report_id": None,
                "report_id": None,
                "lifecycle_events": [],
                "interruption": None,
                "last_activity_at": now,
                "started_at": None,
                "completed_at": None,
                "created_at": now,
                "updated_at": now,
                "candidate_session_token": candidate_session_token,
                "candidate_join_url": "/#candidate/%s?token=%s" % (interview_id, candidate_session_token),
            }
            decision = self.lifecycle.execute(
                session,
                LifecycleCommand(LifecycleCommandType.CREATE),
                now=now,
            )
            return transaction.interview_sessions.add(decision.session)

    def mark_participant_ready(
        self,
        interview_id: str,
        *,
        participant: str,
        source: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        session, _ = self._apply_command(
            interview_id,
            LifecycleCommand(
                LifecycleCommandType.PARTICIPANT_READY,
                {"participant": participant, "source": source},
            ),
            organization_id,
        )
        return session

    def start_interview(
        self,
        interview_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        session, _ = self._apply_command(
            interview_id,
            LifecycleCommand(LifecycleCommandType.START),
            organization_id,
        )
        return self._start_response(session)

    def pause_interview(
        self,
        interview_id: str,
        reason: str = "manual",
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        return self._control(interview_id, LifecycleCommandType.PAUSE, reason, organization_id)

    def timeout_interview(
        self,
        interview_id: str,
        reason: str = "activity_timeout",
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        return self._control(interview_id, LifecycleCommandType.TIMEOUT, reason, organization_id)

    def resume_interview(
        self,
        interview_id: str,
        reason: str = "manual",
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        return self._control(interview_id, LifecycleCommandType.RESUME, reason, organization_id)

    def recover_interview(
        self,
        interview_id: str,
        reason: str = "reconnected",
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        return self._control(interview_id, LifecycleCommandType.RECOVER, reason, organization_id)

    def cancel_interview(
        self,
        interview_id: str,
        reason: str = "cancelled",
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        return self._control(interview_id, LifecycleCommandType.CANCEL, reason, organization_id)

    def skip_current_turn(
        self,
        interview_id: str,
        reason: str = "interviewer_skipped",
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        return self._control(interview_id, LifecycleCommandType.SKIP_CURRENT_TURN, reason, organization_id)

    def _control(
        self,
        interview_id: str,
        command_type: LifecycleCommandType,
        reason: str,
        organization_id: str,
    ) -> Dict[str, Any]:
        session, work_items = self._apply_command(
            interview_id,
            LifecycleCommand(command_type, {"reason": reason}),
            organization_id,
        )
        self._process_report_effects(work_items, organization_id)
        return self.get_interview(interview_id, organization_id) if work_items else session

    def get_interview(
        self,
        interview_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            return self._required(transaction.interview_sessions.get(interview_id))

    def list_lifecycle_events(
        self,
        interview_id: str,
        organization_id: str = "org_default",
    ) -> List[Dict[str, Any]]:
        return deepcopy(self.get_interview(interview_id, organization_id).get("lifecycle_events", []))

    def require_active_turn(
        self,
        interview_id: str,
        turn_id: Optional[str],
        *,
        allowed_statuses: Tuple[str, ...] = ("asking",),
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        session = self.get_interview(interview_id, organization_id)
        return self.lifecycle.require_active_turn(session, turn_id, allowed_statuses=allowed_statuses)

    def active_turn_context(
        self,
        interview_id: str,
        turn_id: Optional[str],
        *,
        allowed_statuses: Tuple[str, ...] = ("asking",),
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        session = self.get_interview(interview_id, organization_id)
        turn = self.lifecycle.require_active_turn(session, turn_id, allowed_statuses=allowed_statuses)
        return {"interview": session, "turn": turn}

    def validate_candidate_token(
        self,
        interview_id: str,
        token: Optional[str],
        organization_id: str = "org_default",
    ) -> None:
        session = self.get_interview(interview_id, organization_id)
        if not token or token != session.get("candidate_session_token"):
            raise ApiError("CANDIDATE_SESSION_TOKEN_INVALID", "Candidate session token is invalid.", status_code=403)

    async def submit_answer(
        self,
        interview_id: str,
        payload: Dict[str, Any],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            starting_event_sequence = len(session.get("lifecycle_events", []))
            turn_id = self._resolve_turn_id(session, payload.get("turn_id"), payload.get("question_id"))
            turn = self.lifecycle.require_active_turn(session, turn_id, allowed_statuses=("asking",))
            now = utc_now()
            answer = {
                "id": new_id("ans"),
                "organization_id": organization_id,
                "interview_id": interview_id,
                "turn_id": turn["id"],
                "question_id": turn["question_id"],
                "question_snapshot_id": turn["question_snapshot_id"],
                "raw_transcript": payload.get("raw_transcript") or payload["final_transcript"],
                "final_transcript": payload["final_transcript"],
                "audio_uri": payload.get("audio_uri"),
                "stt_confidence": payload.get("stt_confidence", 1.0),
                "language": payload.get("language", "zh-CN"),
                "duration_seconds": payload.get("duration_seconds", 0),
                "evaluation_status": "pending",
                "current_evaluation_id": None,
                "evaluation_id": None,
                "created_at": now,
                "updated_at": now,
            }
            decision, work_items = self._decide_and_persist(
                transaction,
                session,
                LifecycleCommand(
                    LifecycleCommandType.ANSWER_SUBMITTED,
                    {"answer": answer, "trigger_reason": "initial_scoring"},
                ),
                organization_id,
            )

        evaluation_work = self._work_by_kind(work_items, "answer.evaluate")
        if evaluation_work is None:
            raise RuntimeError("Lifecycle did not request evaluation for a submitted answer.")
        evaluation = await self._process_evaluation_work(evaluation_work["id"], organization_id)
        session = self.get_interview(interview_id, organization_id)
        persisted_answer = self._answer_by_id(session, answer["id"])
        return {
            "answer": persisted_answer,
            "evaluation": evaluation,
            "next_turn_id": session.get("current_turn_id"),
            "status": session["status"],
            "report": self._current_report(session),
            "events": [
                deepcopy(item)
                for item in session.get("lifecycle_events", [])
                if item["sequence"] > starting_event_sequence
            ],
        }

    async def regrade_answer(
        self,
        interview_id: str,
        answer_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        session, work_items = self._apply_command(
            interview_id,
            LifecycleCommand(
                LifecycleCommandType.REGRADE_REQUESTED,
                {"answer_id": answer_id, "trigger_reason": "manual_regrade"},
            ),
            organization_id,
        )
        evaluation_work = self._work_by_kind(work_items, "answer.evaluate")
        if evaluation_work is None:
            raise RuntimeError("Lifecycle did not request evaluation for regrade.")
        evaluation = await self._process_evaluation_work(evaluation_work["id"], organization_id)
        session = self.get_interview(interview_id, organization_id)
        result = deepcopy(evaluation)
        if session.get("current_report_id"):
            result["current_report_id"] = session["current_report_id"]
        return result

    def complete_interview(
        self,
        interview_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        session, work_items = self._apply_command(
            interview_id,
            LifecycleCommand(
                LifecycleCommandType.COMPLETE,
                {"reason": "interviewer_completed", "trigger_reason": "interviewer_completed"},
            ),
            organization_id,
        )
        report_work = self._work_by_kind(work_items, "interview.report.generate")
        if report_work is not None:
            report = self._process_report_work(report_work["id"], organization_id)
            session = self.get_interview(interview_id, organization_id)
            return {"interview": session, "report": report}
        report = self._current_report(session)
        if report is None:
            raise ApiError("REPORT_GENERATION_IN_PROGRESS", "Interview report is already being generated.", status_code=409)
        return {"interview": session, "report": report}

    def list_answer_evaluations(
        self,
        interview_id: str,
        answer_id: str,
        organization_id: str = "org_default",
    ) -> List[Dict[str, Any]]:
        session = self.get_interview(interview_id, organization_id)
        self._answer_by_id(session, answer_id)
        return [
            deepcopy(item)
            for item in session.get("evaluation_revisions", [])
            if item["answer_id"] == answer_id
        ]

    async def process_outbox_work(
        self,
        work_item_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            work_item = transaction.outbox.get(work_item_id)
        if work_item is None:
            raise ApiError("OUTBOX_WORK_NOT_FOUND", "Outbox work item does not exist.", status_code=404)
        if work_item["kind"] == "answer.evaluate":
            return await self._process_evaluation_work(work_item_id, organization_id)
        if work_item["kind"] == "interview.report.generate":
            return self._process_report_work(work_item_id, organization_id)
        raise ApiError("OUTBOX_WORK_KIND_UNSUPPORTED", "Outbox work item kind is not supported.")

    async def _process_evaluation_work(
        self,
        work_item_id: str,
        organization_id: str,
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            running_work = transaction.outbox.start(work_item_id)
            payload = running_work["payload"]
            interview_id = payload["interview_id"]
            answer_id = payload["answer_id"]
            session = self._required(transaction.interview_sessions.get(interview_id))
            answer = deepcopy(self._answer_by_id(session, answer_id))
            if answer.get("evaluation_status") == "failed":
                decision, _ = self._decide_and_persist(
                    transaction,
                    session,
                    LifecycleCommand(
                        LifecycleCommandType.EVALUATION_RETRY_STARTED,
                        {"answer_id": answer_id, "revision": int(payload["revision"])},
                    ),
                    organization_id,
                )
                session = decision.session
                answer = deepcopy(self._answer_by_id(session, answer_id))
            question_snapshot = deepcopy(self._turn_by_id(session, answer["turn_id"])["question_snapshot"])

        try:
            evaluation = await self.evaluation.evaluate_answer(answer, question_snapshot)
        except Exception as exc:
            with self.persistence.transaction(organization_id) as transaction:
                session = self._required(transaction.interview_sessions.get(interview_id))
                self._decide_and_persist(
                    transaction,
                    session,
                    LifecycleCommand(
                        LifecycleCommandType.EVALUATION_FAILED,
                        {"answer_id": answer_id, "error": str(exc)},
                    ),
                    organization_id,
                )
                transaction.outbox.fail(
                    work_item_id,
                    str(exc),
                    lease_token=running_work["lease_token"],
                )
            raise

        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            decision, work_items = self._decide_and_persist(
                transaction,
                session,
                LifecycleCommand(
                    LifecycleCommandType.EVALUATION_SUCCEEDED,
                    {
                        "answer_id": answer_id,
                        "evaluation": evaluation,
                        "revision": int(payload["revision"]),
                        "trigger_reason": payload.get("trigger_reason", "initial_scoring"),
                    },
                ),
                organization_id,
            )
            transaction.outbox.complete(work_item_id, lease_token=running_work["lease_token"])
            persisted_evaluation = next(
                item for item in decision.session["evaluation_revisions"] if item["id"] == evaluation["id"]
            )

        self._process_report_effects(work_items, organization_id)
        return deepcopy(persisted_evaluation)

    def _process_report_work(
        self,
        work_item_id: str,
        organization_id: str,
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            running_work = transaction.outbox.start(work_item_id)
            payload = running_work["payload"]
            interview_id = payload["interview_id"]
            source = self._required(transaction.interview_sessions.get(interview_id))
            if source["status"] != "report_generating":
                decision, _ = self._decide_and_persist(
                    transaction,
                    source,
                    LifecycleCommand(
                        LifecycleCommandType.REPORT_RETRY_STARTED,
                        {"revision": int(payload["revision"])},
                    ),
                    organization_id,
                )
                source = decision.session

        try:
            report = self.reports.build_report(source, trigger_reason=payload["trigger_reason"])
        except Exception as exc:
            with self.persistence.transaction(organization_id) as transaction:
                session = self._required(transaction.interview_sessions.get(interview_id))
                self._decide_and_persist(
                    transaction,
                    session,
                    LifecycleCommand(LifecycleCommandType.REPORT_FAILED, {"error": str(exc)}),
                    organization_id,
                )
                transaction.outbox.fail(
                    work_item_id,
                    str(exc),
                    lease_token=running_work["lease_token"],
                )
            raise

        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            decision, _ = self._decide_and_persist(
                transaction,
                session,
                LifecycleCommand(
                    LifecycleCommandType.REPORT_SUCCEEDED,
                    {
                        "report": report,
                        "revision": int(payload["revision"]),
                        "previous_report_id": payload.get("previous_report_id"),
                    },
                ),
                organization_id,
            )
            transaction.outbox.complete(work_item_id, lease_token=running_work["lease_token"])
            persisted_report = next(
                item for item in decision.session["report_revisions"] if item["id"] == report["id"]
            )
        return deepcopy(persisted_report)

    def _apply_command(
        self,
        interview_id: str,
        command: LifecycleCommand,
        organization_id: str,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            decision, work_items = self._decide_and_persist(
                transaction,
                session,
                command,
                organization_id,
            )
            return decision.session, work_items

    def _decide_and_persist(
        self,
        transaction: Any,
        session: Dict[str, Any],
        command: LifecycleCommand,
        organization_id: str,
    ) -> Tuple[LifecycleDecision, List[Dict[str, Any]]]:
        decision = self.lifecycle.execute(session, command)
        work_items = self._enqueue_effects(transaction, decision.effects, session["id"], organization_id)
        if decision.changed:
            decision.session = transaction.interview_sessions.update(
                decision.session,
                expected_version=session["version"],
            )
        return decision, work_items

    def _enqueue_effects(
        self,
        transaction: Any,
        effects: List[Dict[str, Any]],
        interview_id: str,
        organization_id: str,
    ) -> List[Dict[str, Any]]:
        work_items: List[Dict[str, Any]] = []
        for effect in effects:
            payload = effect["payload"]
            if effect["type"] == "evaluation.requested":
                kind = "answer.evaluate"
                idempotency_key = "answer.evaluate:%s:%s" % (payload["answer_id"], payload["revision"])
                work_payload = {
                    "interview_id": interview_id,
                    "answer_id": payload["answer_id"],
                    "revision": payload["revision"],
                    "trigger_reason": payload["trigger_reason"],
                }
            elif effect["type"] == "report.requested":
                kind = "interview.report.generate"
                idempotency_key = "interview.report:%s:%s" % (interview_id, payload["revision"])
                work_payload = deepcopy(payload)
            else:
                raise RuntimeError("Unsupported lifecycle effect: %s" % effect["type"])
            work_items.append(
                transaction.outbox.enqueue(
                    new_work_item(
                        organization_id=organization_id,
                        kind=kind,
                        aggregate_id=interview_id,
                        idempotency_key=idempotency_key,
                        payload=work_payload,
                    )
                )
            )
        return work_items

    def _process_report_effects(self, work_items: List[Dict[str, Any]], organization_id: str) -> None:
        for item in work_items:
            if item["kind"] == "interview.report.generate":
                self._process_report_work(item["id"], organization_id)

    def _work_by_kind(self, work_items: List[Dict[str, Any]], kind: str) -> Optional[Dict[str, Any]]:
        return next((item for item in work_items if item["kind"] == kind), None)

    def _resolve_turn_id(
        self,
        session: Dict[str, Any],
        turn_id: Optional[str],
        question_id: Optional[str],
    ) -> Optional[str]:
        if turn_id:
            return turn_id
        if question_id:
            turn = next((item for item in session["turns"] if item["question_id"] == question_id), None)
            if turn is not None:
                return turn["id"]
        return session.get("current_turn_id")

    def _question_snapshot(self, question: Dict[str, Any], created_at: str) -> Dict[str, Any]:
        return {
            "id": new_id("question_snapshot"),
            "source_question_id": question["id"],
            "source_question_version": question["version"],
            "title": question["title"],
            "question_text": question["question_text"],
            "spoken_text": question["question_text"],
            "standard_answer": question["standard_answer"],
            "key_points": deepcopy(question["key_points"]),
            "rubric": deepcopy(question.get("rubric", {})),
            "difficulty": question["difficulty"],
            "type": question["type"],
            "skills": deepcopy(question.get("skills", [])),
            "created_at": created_at,
        }

    def _required(self, session: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if session is None:
            raise ApiError("INTERVIEW_NOT_FOUND", "Interview does not exist.", status_code=404)
        return session

    def _turn_by_id(self, session: Dict[str, Any], turn_id: str) -> Dict[str, Any]:
        turn = next((item for item in session["turns"] if item["id"] == turn_id), None)
        if turn is None:
            raise ApiError("INTERVIEW_TURN_NOT_FOUND", "Interview turn does not exist.", status_code=404)
        return turn

    def _answer_by_id(self, session: Dict[str, Any], answer_id: str) -> Dict[str, Any]:
        answer = next((item for item in session["answers"] if item["id"] == answer_id), None)
        if answer is None:
            raise ApiError("ANSWER_NOT_FOUND", "Answer does not exist.", status_code=404)
        return answer

    def _current_report(self, session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        report_id = session.get("current_report_id")
        if not report_id:
            return None
        report = next((item for item in session.get("report_revisions", []) if item["id"] == report_id), None)
        return deepcopy(report) if report else None

    def _start_response(self, session: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": session["id"],
            "status": session["status"],
            "live_ws_url": "/api/v1/interviews/%s/live" % session["id"],
            "current_turn_id": session.get("current_turn_id"),
            "version": session["version"],
        }
