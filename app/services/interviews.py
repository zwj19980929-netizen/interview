import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.adapters.private_media import read_managed_audio
from app.domain.appointment_admission import AppointmentAdmission, ensure_utc, format_utc
from app.domain.interview_lifecycle import (
    InterviewSessionLifecycle,
    LifecycleCommand,
    LifecycleCommandType,
    LifecycleDecision,
)
from app.model_gateway import capabilities as cap
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import BatchSTTRequest
from app.persistence.interface import Persistence, new_work_item
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.evaluation import EvaluationService
from app.services.plan_assembly import InterviewPlanAssembly
from app.services.reports import ReportService


class InterviewService:
    """Transactional orchestration behind the InterviewSession lifecycle seam."""

    def __init__(
        self,
        store: InMemoryStore,
        *,
        persistence: Optional[Persistence] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.admission = AppointmentAdmission()
        self.gateway = ModelGateway(store, persistence=self.persistence)
        self.lifecycle = InterviewSessionLifecycle()
        self.evaluation = EvaluationService(store, persistence=self.persistence)
        self.reports = ReportService(store, persistence=self.persistence)
        self.plan_assembly = InterviewPlanAssembly(store, persistence=self.persistence)

    def list_interviews(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return transaction.interview_sessions.list()

    def create_from_admitted_appointment(
        self,
        appointment_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            existing = next(
                (
                    item
                    for item in transaction.interview_sessions.list()
                    if item.get("appointment_id") == appointment_id
                ),
                None,
            )
            if existing:
                return existing
            appointment = transaction.interview_appointments.get(appointment_id)
            if appointment is None:
                raise ApiError(
                    "INTERVIEW_APPOINTMENT_NOT_FOUND",
                    "Interview appointment does not exist.",
                    status_code=404,
                )
            plan = transaction.interview_plans.get(appointment["plan_id"])
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

            now_dt = ensure_utc(self.clock())
            candidate_intake = next(
                (
                    item
                    for item in transaction.candidate_intakes.list()
                    if item.get("appointment_id") == appointment_id
                ),
                None,
            )
            readiness = self.admission.plan_readiness(
                transaction,
                plan,
                appointment=appointment,
                now=now_dt,
                ttl_seconds=int(
                    appointment.get("admission_policy", {}).get(
                        "model_readiness_ttl_seconds",
                        60,
                    )
                ),
            )
            self.admission.validate_start(
                appointment,
                candidate_intake,
                readiness,
                now=now_dt,
            )
            candidate_profile = transaction.candidate_profiles.get(appointment["candidate_profile_id"])
            if candidate_profile is None:
                raise ApiError("CANDIDATE_PROFILE_NOT_FOUND", "Candidate profile does not exist.", status_code=404)
            payload = {
                "candidate_profile_id": candidate_profile["id"],
                "candidate": {
                    "name": candidate_profile["name"],
                    "email": candidate_profile.get("email_masked") or "protected",
                    "phone": candidate_profile.get("phone_masked") or "protected",
                    "metadata": {"candidate_profile_id": candidate_profile["id"]},
                },
                "scheduled_at": appointment["scheduled_start_at"],
                "settings": deepcopy(appointment.get("settings", {})),
            }

            session_seed = payload.get("session_seed") or new_id("session_seed")
            question_selections, turn_blueprints = self.plan_assembly.materialize_execution(
                transaction,
                plan,
                session_seed,
            )
            interview_id = new_id("iv")
            now = format_utc(now_dt)
            candidate = {
                "id": payload.get("candidate_profile_id") or new_id("cand"),
                "organization_id": organization_id,
                "interview_id": interview_id,
                "name": payload["candidate"]["name"],
                "email": payload["candidate"].get("email"),
                "phone": payload["candidate"].get("phone"),
                "metadata": deepcopy(payload["candidate"].get("metadata", {})),
                "candidate_intake_id": (candidate_intake or {}).get("id"),
                "consent_version": (candidate_intake or {}).get("consent_version"),
                "privacy_accepted": (candidate_intake or {}).get("privacy_accepted"),
                "recording_accepted": (candidate_intake or {}).get("recording_accepted"),
                "consent_notice_hash": (candidate_intake or {}).get("notice_hash"),
                "consented_at": (candidate_intake or {}).get("consented_at"),
                "created_at": now,
            }
            turns: List[Dict[str, Any]] = []
            question_snapshots: List[Dict[str, Any]] = []
            prepared_speech = {
                (item.get("question_id"), int(item.get("source_version", 0))): item
                for item in (appointment.get("speech_preparation") or {}).get("items", [])
            }
            for blueprint in sorted(turn_blueprints, key=lambda item: item["order"]):
                source_type = blueprint.get("source_type", "position_bank")
                question = deepcopy(blueprint.get("frozen_question")) or (
                    transaction.experience_questions.get(blueprint["question_id"])
                    if source_type == "resume_experience"
                    else transaction.questions.get(blueprint["question_id"])
                )
                if question is None:
                    raise ApiError(
                        "INTERVIEW_PLAN_QUESTION_NOT_FOUND",
                        "An approved plan refers to a question that no longer exists.",
                        status_code=409,
                    )
                if source_type == "resume_experience":
                    prepared = prepared_speech.get(
                        (question["id"], int(question.get("version", 0)))
                    )
                    if not prepared or prepared.get("status") != "ready" or not prepared.get("asset_id"):
                        raise ApiError(
                            "APPOINTMENT_SPEECH_NOT_READY",
                            "Resume question speech is not ready for this appointment.",
                            status_code=409,
                        )
                    question["speech_asset_id"] = prepared["asset_id"]
                    question["speech_status"] = "ready"
                question_snapshot = self._question_snapshot(question, now, source_type=source_type)
                snapshot_entry = deepcopy(blueprint)
                snapshot_entry["question_snapshot_id"] = question_snapshot["id"]
                question_snapshots.append(snapshot_entry)
                turn_id = new_id("turn")
                turns.append(
                    {
                        "id": turn_id,
                        "interview_id": interview_id,
                        "turn_blueprint_id": blueprint["id"],
                        "question_id": question["id"],
                        "question_snapshot_id": question_snapshot["id"],
                        "question_snapshot": question_snapshot,
                        "order": blueprint["order"],
                        "phase": source_type,
                        "is_followup": False,
                        "parent_turn_id": None,
                        "root_turn_id": turn_id,
                        "followup_depth": 0,
                        "followup_reason": None,
                        "target_key_points": [],
                        "allow_followup": bool(blueprint.get("allow_followup", True)),
                        "weight": float(blueprint.get("weight", 0.0)),
                        "expected_minutes": int(blueprint.get("expected_minutes", 0)),
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
                "selection_policy": deepcopy(plan.get("selection_policy", plan.get("assembly_policy", {}))),
                "assembly_summary": deepcopy(plan.get("assembly_summary", {})),
                "role_requirement": deepcopy(role),
                "question_snapshots": question_snapshots,
                "job_position_id": plan.get("job_position_id"),
                "candidate_profile_id": plan.get("candidate_profile_id"),
                "resume_review_id": plan.get("resume_review_id"),
                "knowledge_base_ids": deepcopy(plan.get("knowledge_base_ids", [])),
                "knowledge_base_snapshots": deepcopy(plan.get("knowledge_base_snapshots", [])),
                "bank_slots": deepcopy(plan.get("bank_slots", [])),
                "experience_question_ids": deepcopy(plan.get("experience_question_ids", [])),
                "experience_question_snapshots": deepcopy(plan.get("experience_question_snapshots", [])),
                "speech_profile_snapshot": deepcopy(plan.get("speech_profile_snapshot")),
                "appointment_speech_preparation": deepcopy(
                    appointment.get("speech_preparation")
                ),
                "question_selections": deepcopy(question_selections),
                "created_at": now,
            }
            session = {
                "id": interview_id,
                "organization_id": organization_id,
                "appointment_id": appointment_id,
                "plan_id": plan["id"],
                "plan_snapshot": plan_snapshot,
                "candidate_id": candidate["id"],
                "candidate": candidate,
                "status": "scheduled",
                "phase": turns[0].get("phase", "position_bank") if turns else "position_bank",
                "session_seed": session_seed,
                "question_selections": deepcopy(question_selections),
                "settings": deepcopy(payload.get("settings", {})),
                "followup_policy": self._followup_policy(plan),
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
            }
            decision = self.lifecycle.execute(
                session,
                LifecycleCommand(LifecycleCommandType.CREATE),
                now=now,
            )
            decision = self.lifecycle.execute(
                decision.session,
                LifecycleCommand(LifecycleCommandType.START),
                now=now,
            )
            session = transaction.interview_sessions.add(decision.session)
            appointment = transaction.interview_appointments.get(appointment_id)
            if appointment is None or appointment["status"] != "registered":
                raise ApiError(
                    "APPOINTMENT_NOT_REGISTERED",
                    "Appointment must be registered before session creation.",
                    status_code=409,
                )
            appointment["status"] = "consumed"
            appointment["consumed_at"] = now
            appointment["consumed_token_hash"] = appointment.get("invitation_token_hash")
            appointment["invitation_token_hash"] = None
            reminder = deepcopy(appointment.get("email_reminder") or {})
            reminder_work_id = reminder.get("work_item_id")
            if reminder_work_id:
                reminder_work = transaction.outbox.get(reminder_work_id)
                if reminder_work and reminder_work.get("status") not in {"completed", "cancelled"}:
                    transaction.outbox.cancel(
                        reminder_work_id,
                        reason="appointment_consumed",
                        actor_id="appointment_admission",
                    )
                    reminder.update(
                        {
                            "status": "skipped",
                            "result_status": "skipped_appointment_consumed",
                            "updated_at": now,
                        }
                    )
                    appointment["email_reminder"] = reminder
            appointment["updated_at"] = now
            transaction.interview_appointments.update(
                appointment, expected_version=appointment["version"]
            )
            return session

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

    def get_candidate_interview(
        self,
        interview_id: str,
        token: Optional[str],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        self.validate_candidate_token(interview_id, token, organization_id)
        session = deepcopy(self.get_interview(interview_id, organization_id))
        current_turn_id = session.get("current_turn_id")
        turns = []
        for turn in session.get("turns", []):
            projection = {
                "id": turn["id"],
                "order": turn["order"],
                "status": turn["status"],
                "is_followup": bool(turn.get("is_followup", False)),
                "parent_turn_id": turn.get("parent_turn_id"),
                "root_turn_id": turn.get("root_turn_id") or turn["id"],
                "followup_depth": int(turn.get("followup_depth", 0)),
            }
            if turn["id"] == current_turn_id or turn["status"] in {"completed", "skipped"}:
                projection["question_spoken_text"] = turn.get("question_spoken_text", "")
            turns.append(projection)
        return {
            "id": session["id"],
            "status": session["status"],
            "phase": session.get("phase"),
            "avatar_mode": session.get("settings", {}).get("avatar_mode", "cloud"),
            "record_video": bool(session.get("settings", {}).get("record_video", False)),
            "speech_dialogue_mode": session.get("settings", {}).get(
                "speech_dialogue_mode", "cascade"
            ),
            "current_turn_id": current_turn_id,
            "candidate": {"name": session.get("candidate", {}).get("name", "候选人")},
            "turns": turns,
            "answers": [
                {
                    "id": answer.get("id"),
                    "turn_id": answer.get("turn_id"),
                    "evaluation_status": answer.get("evaluation_status"),
                }
                for answer in session.get("answers", [])
            ],
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
        }

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
        expected = self._candidate_session_token(session)
        if not token or not hmac.compare_digest(str(token), expected):
            raise ApiError("CANDIDATE_SESSION_TOKEN_INVALID", "Candidate session token is invalid.", status_code=403)

    def candidate_join_url(self, session: Dict[str, Any]) -> str:
        return "/#candidate/%s?token=%s" % (session["id"], self._candidate_session_token(session))

    def _candidate_session_token(self, session: Dict[str, Any]) -> str:
        secret = os.getenv("INTERVIEWER_CANDIDATE_TOKEN_SECRET", "").strip()
        if not secret:
            if os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production":
                raise ApiError(
                    "CANDIDATE_TOKEN_SECRET_REQUIRED",
                    "Candidate session token signing is not configured.",
                    status_code=503,
                )
            secret = "local-development-candidate-token-secret"
        if os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production" and len(secret) < 32:
            raise ApiError(
                "CANDIDATE_TOKEN_SECRET_WEAK",
                "Candidate session token signing secret must contain at least 32 characters.",
                status_code=503,
            )
        payload = "%s:%s" % (session["id"], session.get("created_at", ""))
        digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
        encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return "candidate.%s" % encoded

    async def submit_candidate_audio_answer(
        self,
        interview_id: str,
        token: Optional[str],
        payload: Dict[str, Any],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        self.validate_candidate_token(interview_id, token, organization_id)
        session = self.get_interview(interview_id, organization_id)
        turn_id = self._resolve_turn_id(session, payload.get("turn_id"), None)
        audio_uri = str(payload.get("audio_uri") or "")
        expected_prefix = "/media/%s/%s/" % (interview_id, turn_id)
        private_scope_valid = False
        if audio_uri.startswith("private-file://"):
            file_id = audio_uri.removeprefix("private-file://")
            with self.persistence.transaction(organization_id) as transaction:
                file_object = transaction.file_objects.get(file_id)
            private_scope_valid = bool(
                file_object
                and file_object.get("purpose") == "candidate_answer_audio"
                and file_object.get("interview_id") == interview_id
                and file_object.get("turn_id") == turn_id
                and file_object.get("status") == "ready"
            )
        if not audio_uri.startswith(expected_prefix) and not private_scope_valid:
            raise ApiError(
                "CANDIDATE_AUDIO_SCOPE_INVALID",
                "Candidate audio must belong to the active interview turn.",
                status_code=403,
            )
        return await self.submit_audio_answer(interview_id, payload, organization_id)

    def record_heartbeat(
        self,
        interview_id: str,
        *,
        participant: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            now = utc_now()
            session.setdefault("heartbeats", {})[participant] = now
            session["last_activity_at"] = now
            session["updated_at"] = now
            updated = transaction.interview_sessions.update(session, expected_version=session["version"])
        return {"interview_id": interview_id, "participant": participant, "received_at": now, "version": updated["version"]}

    async def _accept_authoritative_transcript(
        self,
        interview_id: str,
        payload: Dict[str, Any],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        transcript_source = payload.get("transcript_source")
        provider = payload.get("stt_provider") or {}
        if (
            transcript_source not in {"server_batch", "server_streaming"}
            or not provider.get("provider_id")
            or not payload.get("audio_uri")
        ):
            raise ApiError(
                "AUTHORITATIVE_TRANSCRIPT_REQUIRED",
                "Answers require server STT provenance and a persisted audio reference.",
                status_code=409,
            )
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            starting_event_sequence = len(session.get("lifecycle_events", []))
            turn_id = self._resolve_turn_id(session, payload.get("turn_id"), payload.get("question_id"))
            turn = self.lifecycle.require_active_turn(
                session, turn_id, allowed_statuses=("asking", "transcribing")
            )
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
                "transcript_source": transcript_source,
                "stt_provider": deepcopy(payload.get("stt_provider")),
                "transcript_segments": deepcopy(payload.get("transcript_segments", [])),
                "transcript_revisions": [
                    {
                        "revision": 1,
                        "text": payload["final_transcript"],
                        "source": transcript_source,
                        "created_at": now,
                    }
                ],
                "language": payload.get("language", "zh-CN"),
                "duration_seconds": payload.get("duration_seconds", 0),
                "evaluation_status": "pending",
                "current_evaluation_id": None,
                "evaluation_id": None,
                "created_at": now,
                "updated_at": now,
            }
            followup_decision = self.evaluation.decide_followup(
                session,
                turn,
                answer,
                now=now,
            )
            decision, work_items = self._decide_and_persist(
                transaction,
                session,
                LifecycleCommand(
                    LifecycleCommandType.ANSWER_SUBMITTED,
                    {"answer": answer, "trigger_reason": "initial_scoring"},
                ),
                organization_id,
            )
            if followup_decision.get("selected"):
                followup_turn = self._build_followup_turn(
                    decision.session,
                    turn,
                    answer,
                    followup_decision,
                    now=now,
                )
                decision, followup_work = self._decide_and_persist(
                    transaction,
                    decision.session,
                    LifecycleCommand(
                        LifecycleCommandType.FOLLOWUP_REQUESTED,
                        {
                            "answer_id": answer["id"],
                            "root_turn_id": turn["id"],
                            "followup_turn": followup_turn,
                            "decision": deepcopy(followup_decision),
                        },
                    ),
                    organization_id,
                )
                work_items.extend(followup_work)

        evaluation_work = self._work_by_kind(work_items, "answer.evaluate")
        if evaluation_work is None:
            raise RuntimeError("Lifecycle did not request evaluation for a submitted answer.")
        session = self.get_interview(interview_id, organization_id)
        persisted_answer = self._answer_by_id(session, answer["id"])
        selected_followup = next(
            (
                item
                for item in session.get("turns", [])
                if item.get("is_followup") and item.get("root_turn_id") == turn["id"]
            ),
            None,
        )
        return {
            "answer": persisted_answer,
            "evaluation": {
                "status": "pending",
                "work_item_id": evaluation_work["id"],
            },
            "evaluation_work_id": evaluation_work["id"],
            "next_turn_id": session.get("current_turn_id"),
            "status": session["status"],
            "report": self._current_report(session),
            "followup": self._followup_projection(selected_followup),
            "events": [
                deepcopy(item)
                for item in session.get("lifecycle_events", [])
                if item["sequence"] > starting_event_sequence
            ],
        }

    async def submit_audio_answer(
        self,
        interview_id: str,
        payload: Dict[str, Any],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        if (
            payload.get("development_transcript")
            and os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production"
        ):
            raise ApiError(
                "DEVELOPMENT_TRANSCRIPT_NOT_ALLOWED",
                "Development transcript injection is disabled in production.",
                status_code=409,
            )
        session = self.get_interview(interview_id, organization_id)
        turn_id = self._resolve_turn_id(session, payload.get("turn_id"), None)
        self.lifecycle.require_active_turn(session, turn_id, allowed_statuses=("asking",))
        self._apply_command(
            interview_id,
            LifecycleCommand(
                LifecycleCommandType.TRANSCRIPTION_STARTED,
                {
                    "turn_id": turn_id,
                    "recording": {
                        "audio_uri": payload["audio_uri"],
                        "content_type": payload.get("content_type", "audio/webm;codecs=opus"),
                    },
                },
            ),
            organization_id,
        )
        try:
            try:
                audio_bytes = read_managed_audio(
                    self.persistence, organization_id, payload["audio_uri"]
                )
            except ApiError:
                # Preserve the documented development-only transcript fixture path.
                # Production never accepts development_transcript and therefore
                # always requires server-managed audio to exist.
                if not payload.get("development_transcript"):
                    raise
                audio_bytes = b""
            response = await self.gateway.invoke(
                cap.STT_BATCH,
                BatchSTTRequest(
                    organization_id=organization_id,
                    purpose="candidate_answer_repair",
                    audio_uri=payload["audio_uri"],
                    content_type=payload.get("content_type", "audio/webm;codecs=opus"),
                    language=payload.get("language", "zh-CN"),
                    metadata={
                        "development_transcript": payload.get("development_transcript"),
                        "confidence": payload.get("development_confidence", 0.9),
                        "duration_ms": int(payload.get("duration_seconds", 0)) * 1000,
                    },
                    audio_bytes=audio_bytes,
                ),
            )
        except Exception as exc:
            self._apply_command(
                interview_id,
                LifecycleCommand(
                    LifecycleCommandType.TRANSCRIPTION_FAILED,
                    {"turn_id": turn_id, "error": str(exc)},
                ),
                organization_id,
            )
            raise
        result = await self._accept_authoritative_transcript(
            interview_id,
            {
                "turn_id": turn_id,
                "final_transcript": response.text,
                "raw_transcript": response.text,
                "stt_confidence": response.confidence,
                "language": response.language,
                "duration_seconds": payload.get("duration_seconds", 0),
                "audio_uri": payload["audio_uri"],
                "transcript_source": response.source,
                "stt_provider": response.provider.model_dump(),
                "transcript_segments": [item.model_dump() for item in response.segments],
            },
            organization_id,
        )
        result["transcription"] = response.model_dump()
        return result

    async def submit_streaming_answer(
        self,
        interview_id: str,
        payload: Dict[str, Any],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        """Accept only a validated provider final from the server-side stream module."""
        provider = payload.get("provider") or {}
        if not payload.get("final_transcript") or not provider.get("provider_id"):
            raise ApiError(
                "STREAMING_TRANSCRIPT_INVALID",
                "Streaming answers require an authoritative provider final.",
                status_code=409,
            )
        session = self.get_interview(interview_id, organization_id)
        turn_id = self._resolve_turn_id(session, payload.get("turn_id"), None)
        self.lifecycle.require_active_turn(session, turn_id, allowed_statuses=("asking",))
        self._apply_command(
            interview_id,
            LifecycleCommand(
                LifecycleCommandType.TRANSCRIPTION_STARTED,
                {
                    "turn_id": turn_id,
                    "recording": {
                        "audio_uri": payload["audio_uri"],
                        "content_type": payload.get("content_type", "audio/webm;codecs=opus"),
                    },
                },
            ),
            organization_id,
        )
        return await self._accept_authoritative_transcript(
            interview_id,
            {
                "turn_id": turn_id,
                "final_transcript": payload["final_transcript"],
                "raw_transcript": payload["final_transcript"],
                "stt_confidence": payload.get("confidence", 0.0),
                "language": payload.get("language", "zh-CN"),
                "duration_seconds": payload.get("duration_seconds", 0),
                "audio_uri": payload["audio_uri"],
                "transcript_source": "server_streaming",
                "stt_provider": deepcopy(provider),
                "transcript_segments": deepcopy(payload.get("segments", [])),
            },
            organization_id,
        )

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
            evaluation = await self.evaluation.evaluate_answer(
                answer,
                question_snapshot,
                session.get("plan_snapshot", {}).get("role_requirement"),
            )
        except Exception as exc:
            with self.persistence.transaction(organization_id) as transaction:
                failed = transaction.outbox.fail(
                    work_item_id,
                    str(exc),
                    lease_token=running_work["lease_token"],
                )
                if failed.get("status") == "dead_letter":
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
                failed = transaction.outbox.fail(
                    work_item_id,
                    str(exc),
                    lease_token=running_work["lease_token"],
                )
                if failed.get("status") == "dead_letter":
                    session = self._required(transaction.interview_sessions.get(interview_id))
                    self._decide_and_persist(
                        transaction,
                        session,
                        LifecycleCommand(LifecycleCommandType.REPORT_FAILED, {"error": str(exc)}),
                        organization_id,
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

    def _question_snapshot(
        self,
        question: Dict[str, Any],
        created_at: str,
        *,
        source_type: str = "position_bank",
    ) -> Dict[str, Any]:
        return {
            "id": new_id("question_snapshot"),
            "source_question_id": question["id"],
            "source_question_version": question["version"],
            "source_type": source_type,
            "title": question.get("title") or ("简历经历问题" if source_type == "resume_experience" else "面试题"),
            "question_text": question["question_text"],
            "spoken_text": question["question_text"],
            "standard_answer": question["standard_answer"],
            "key_points": deepcopy(question["key_points"]),
            "rubric": deepcopy(question.get("rubric", {})),
            "followup_probes": deepcopy(
                question.get("followup_probes")
                or question.get("rubric", {}).get("followup_probes", [])
            ),
            "difficulty": question.get("difficulty", "mid"),
            "type": question.get("type", "resume_experience"),
            "skills": deepcopy(question.get("skills", [])),
            "speech_asset_id": question.get("speech_asset_id"),
            "created_at": created_at,
        }

    def _followup_policy(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        source = plan.get("selection_policy") or plan.get("assembly_policy") or {}
        return {
            "max_depth": 1,
            "max_total": max(0, int(source.get("max_followups_total", 2))),
            "max_per_root": min(1, max(0, int(source.get("max_followups_per_root", 1)))),
            "min_answer_chars": max(1, int(source.get("followup_min_answer_chars", 24))),
            "max_answer_chars": max(1, int(source.get("followup_max_answer_chars", 1200))),
            "min_remaining_seconds": max(0, int(source.get("followup_min_remaining_seconds", 45))),
            "max_probe_chars": min(300, max(40, int(source.get("followup_max_probe_chars", 180)))),
        }

    def _build_followup_turn(
        self,
        session: Dict[str, Any],
        root_turn: Dict[str, Any],
        answer: Dict[str, Any],
        decision: Dict[str, Any],
        *,
        now: str,
    ) -> Dict[str, Any]:
        stable_key = "%s:%s:%s" % (session["id"], root_turn["id"], answer["id"])
        digest = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:24]
        question_id = "followup_question_%s" % digest
        snapshot_id = "followup_snapshot_%s" % digest
        target_texts = set(decision.get("target_key_points", []))
        root_snapshot = root_turn["question_snapshot"]
        target_points = [
            deepcopy(item)
            for item in root_snapshot.get("key_points", [])
            if str(item.get("text") if isinstance(item, dict) else item).strip() in target_texts
        ]
        question_snapshot = {
            "id": snapshot_id,
            "source_question_id": question_id,
            "source_question_version": 1,
            "source_type": "followup",
            "parent_question_snapshot_id": root_snapshot["id"],
            "title": "澄清追问",
            "question_text": decision["question_text"],
            "spoken_text": decision["question_text"],
            "standard_answer": root_snapshot["standard_answer"],
            "key_points": target_points,
            "rubric": deepcopy(root_snapshot.get("rubric", {})),
            "difficulty": root_snapshot.get("difficulty", "mid"),
            "type": "clarification_probe",
            "skills": deepcopy(root_snapshot.get("skills", [])),
            "speech_asset_id": None,
            "created_at": now,
        }
        return {
            "id": "followup_turn_%s" % digest,
            "interview_id": session["id"],
            "turn_blueprint_id": "followup_blueprint_%s" % digest,
            "question_id": question_id,
            "question_snapshot_id": snapshot_id,
            "question_snapshot": question_snapshot,
            "phase": root_turn.get("phase", "position_bank"),
            "is_followup": True,
            "parent_turn_id": root_turn["id"],
            "root_turn_id": root_turn["id"],
            "followup_depth": 1,
            "followup_reason": decision["reason"],
            "target_key_points": deepcopy(decision.get("target_key_points", [])),
            "probe_source": decision.get("probe_source", "deterministic_template"),
            "allow_followup": False,
            "weight": 0.0,
            "expected_minutes": 1,
            "status": "pending",
            "question_spoken_text": decision["question_text"],
            "started_at": None,
            "completed_at": None,
        }

    @staticmethod
    def _followup_projection(turn: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if turn is None:
            return None
        return {
            "turn_id": turn["id"],
            "parent_turn_id": turn.get("parent_turn_id"),
            "root_turn_id": turn.get("root_turn_id"),
            "followup_depth": turn.get("followup_depth", 1),
            "reason": turn.get("followup_reason"),
            "target_key_points": deepcopy(turn.get("target_key_points", [])),
            "question_text": turn.get("question_spoken_text"),
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
