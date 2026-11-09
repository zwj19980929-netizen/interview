import base64
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from app.core.errors import ApiError
from app.core.answer_processing_budget import answer_correction_binding
from app.core.prompt.interviewer_supervisor import SUPERVISOR_VERSION
from app.model_gateway.errors import ProviderError
from app.domain.speech_quality import confidence_source, minimum_reported_confidence, transcript_identity, transcript_confidence_segments
from app.core.ids import new_id
from app.core.time import utc_now
from app.adapters.private_media import read_managed_audio
from app.domain.appointment_admission import AppointmentAdmission, ensure_utc, format_utc, parse_utc
from app.domain.interview_lifecycle import (
    InterviewSessionLifecycle,
    LifecycleCommand,
    LifecycleCommandType,
    LifecycleDecision,
)
from app.domain.interview_agent import ConversationUtterance, TurnUnderstanding
from app.domain.evidence_coordination import EvidenceCommitFence
from app.domain.candidate_runtime import CANDIDATE_RUNTIME_PROBLEM_REASONS
from app.model_gateway import capabilities as cap
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import (
    BatchSTTRequest,
    ProviderMeta,
    StableTranscriptPreview,
    StreamingSTTEvent,
    TranscriptSegment,
)
from app.persistence.interface import Persistence, new_work_item
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.evaluation import EvaluationService
from app.services.evidence_coordination import assert_current_evidence_fence
from app.services.evidence_media import DurableEvidenceMedia
from app.services.conversation_understanding import ConversationUnderstandingService
from app.services.candidate_exit_confirmation import (
    PreparedCandidateExit, confirmed_exit_receipt, validated_delivered_exit_context,
)
from app.services.plan_assembly import InterviewPlanAssembly
from app.services.reports import ReportService
from app.services.livekit_room_binding import interview_room_name
from app.services.interviewer_supervisor import InterviewerSupervisor, NextInterviewDecision
from app.services.interviewer_supervisor.context import context_fingerprint as supervisor_fingerprint
from app.services.interviewer_supervisor.context import conversation_memory
from app.domain.adaptive_interview import is_adaptive, initialize_adaptive_snapshot, budget_summary


@dataclass(frozen=True)
class PreparedTurnDecision:
    """Process-local, validated decision; never accepted from a client payload."""
    interview_id: str
    turn_id: str
    text: str
    understanding: TurnUnderstanding
    followup: Dict[str, Any]
    stt_fingerprint: str
    context_fingerprint: str
    semantic_first: bool = False
    completion_confirmed: bool = False
    exit_confirmation: Optional[Dict[str, Any]] = None
    content_clarification: Optional[Dict[str, Any]] = None
    supplement_context_fingerprint: Optional[str] = None
    capture_binding: Any = None
    completion_receipt: Optional["PreparedCompletionReceipt"] = None


@dataclass(frozen=True)
class PreparedCompletionReceipt:
    """Process-local composition of unchanged answer evidence and a new reply."""
    source: PreparedTurnDecision
    base_final: StreamingSTTEvent
    boundary_final: StreamingSTTEvent
    reply_text: str
    confidence: float
    question_act_id: str


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
        self.conversation = ConversationUnderstandingService(
            store, gateway=self.gateway, persistence=self.persistence
        )
        self.supervisor = InterviewerSupervisor(gateway=self.gateway, conversation=self.conversation,
                                               persistence=self.persistence)
        from app.services.interview_skills import InterviewSkillService
        self.skills = InterviewSkillService(store, persistence=self.persistence)
        self.reports = ReportService(store, persistence=self.persistence)
        self.plan_assembly = InterviewPlanAssembly(store, persistence=self.persistence)

    def list_interviews(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return [
                item
                for item in transaction.interview_sessions.list()
                if not item.get("list_removed_at")
            ]

    def expire_overdue_interviews(
        self,
        organization_id: str = "org_default",
    ) -> List[Dict[str, Any]]:
        """Close unfinished sessions after their frozen appointment deadline.

        Candidate input completion and asynchronous scoring are separate
        milestones. Once input is complete, the session is intentionally left
        alone so scoring and report generation can finish after the appointment
        window. All earlier states are closed through the lifecycle seam.
        """

        current = ensure_utc(self.clock())
        occurred_at = format_utc(current)
        expired: List[Dict[str, Any]] = []
        # Discovery never takes a writer lock or scans completed history.
        # It grants no authority: each candidate is read again below before
        # changing its lifecycle, so concurrent completion/extension is safe.
        with self.persistence.transaction(organization_id, read_only=True) as transaction:
            due_ids = [
                source["id"]
                for source in transaction.interview_sessions.watchdog_candidates("deadline")
                if self._overdue_deadline(transaction, source, current) is not None
            ]
        for interview_id in due_ids:
            with self.persistence.transaction(organization_id) as transaction:
                source = transaction.interview_sessions.get(interview_id)
                resolved = self._overdue_deadline(transaction, source, current)
                if resolved is None:
                    continue
                deadline, scheduled_end_at = resolved
                previous_status = str(source.get("status") or "")
                adaptive = is_adaptive(source)
                command = LifecycleCommand(LifecycleCommandType.CANCEL, {"reason": "appointment_window_expired"})
                if adaptive:
                    # Resolve legacy placement once, then bind the stop command
                    # to the same trusted deadline used by the watchdog.
                    source = deepcopy(source)
                    source["scheduled_end_at"] = format_utc(deadline)
                    command = LifecycleCommand(LifecycleCommandType.END_CANDIDATE_INPUT, {
                        "decision_id": "appointment_deadline:%s:%s" % (source["id"], source["scheduled_end_at"]),
                        "expected_decision_revision": int(source.get("decision_revision", 0)),
                        "reason": "appointment_window_expired",
                    })
                decision = self.lifecycle.execute(
                    source,
                    command,
                    now=occurred_at,
                )
                if decision.effects and not adaptive:
                    raise RuntimeError(
                        "Deadline cancellation must not enqueue model or report work."
                    )
                if not decision.changed:
                    continue
                if adaptive:
                    self._enqueue_effects(transaction, decision.effects, source["id"], organization_id)
                decision.session = transaction.interview_sessions.update(
                    decision.session,
                    expected_version=source["version"],
                )
                transaction.audit_events.add(
                    {
                        "id": new_id("audit"),
                        "organization_id": organization_id,
                        "actor_id": "system:interview-deadline",
                        "action": "interview.appointment_window_expired",
                        "resource_type": "interview",
                        "resource_id": source["id"],
                        "metadata": {
                            "scheduled_end_at": str(scheduled_end_at),
                            "previous_status": previous_status,
                            "status": decision.session["status"],
                        },
                        "created_at": occurred_at,
                    }
                )
                expired.append(deepcopy(decision.session))
        return expired

    @staticmethod
    def _overdue_deadline(transaction: Any, source: Optional[Dict[str, Any]],
                          current: datetime) -> Optional[Tuple[datetime, str]]:
        if not source or source.get("status") not in {
            "scheduled", "waiting", "in_progress", "paused",
        } or source.get("candidate_input_completed_at"):
            return None
        scheduled_end_at = source.get("scheduled_end_at") or (
            source.get("settings") or {}
        ).get("scheduled_end_at")
        if not scheduled_end_at and source.get("appointment_id"):
            appointment = transaction.interview_appointments.get(source["appointment_id"])
            scheduled_end_at = (appointment or {}).get("scheduled_end_at")
        if not scheduled_end_at:
            return None
        try:
            deadline = parse_utc(str(scheduled_end_at))
        except (TypeError, ValueError):
            return None
        return (deadline, str(scheduled_end_at)) if current >= deadline else None

    def remove_from_list(
        self,
        interview_id: str,
        *,
        expected_version: int,
        actor_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        """Hide a terminal session from workspace lists without deleting evidence."""
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            if session.get("status") not in {"cancelled", "report_ready"}:
                raise ApiError(
                    "INTERVIEW_REMOVAL_NOT_ALLOWED",
                    "Only a cancelled or report-ready interview can be removed from the list.",
                    status_code=409,
                )
            now = utc_now()
            session["list_removed_at"] = now
            session["list_removed_by"] = actor_id
            session["updated_at"] = now
            updated = transaction.interview_sessions.update(
                session, expected_version=expected_version
            )
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "interview.removed_from_list",
                    "resource_type": "interview",
                    "resource_id": interview_id,
                    "metadata": {"status": session["status"]},
                    "created_at": now,
                }
            )
            return updated

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
            role = (transaction.role_requirements.get(plan["role_requirement_id"])
                    if plan.get("role_requirement_id") else None)
            if role is None and plan.get("preparation_mode") != "question_bank":
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
            if plan.get("execution_schema_version") == 3:
                question_selections, turn_blueprints = [], []
            else:
                question_selections, turn_blueprints = self.plan_assembly.materialize_execution(
                    transaction, plan, session_seed,
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
                "media_consent_scopes": deepcopy((candidate_intake or {}).get("media_consent_scopes") or []),
                "audio_recording_accepted": (candidate_intake or {}).get("audio_recording_accepted", False),
                "video_recording_accepted": (candidate_intake or {}).get(
                    "video_recording_accepted", False
                ),
                "consent_notice_hash": (candidate_intake or {}).get("notice_hash"),
                "consented_at": (candidate_intake or {}).get("consented_at"),
                "created_at": now,
            }
            turns: List[Dict[str, Any]] = []
            question_snapshots: List[Dict[str, Any]] = []
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
                    question["speech_asset_id"] = None
                    question["speech_status"] = "deferred"
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
                        "deferred_speech": source_type == "resume_experience",
                        "question_spoken_text": question_snapshot["spoken_text"],
                        "started_at": None,
                        "completed_at": None,
                        "utterances": [],
                        "current_understanding": None,
                        "conversation_acts": [],
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
            if plan.get("execution_schema_version") == 3:
                plan_snapshot.update(initialize_adaptive_snapshot(plan))
            if plan.get("preparation_mode") == "question_bank":
                plan_snapshot["preparation_mode"] = "question_bank"
                plan_snapshot["assessment_basis"] = deepcopy(plan["assessment_basis"])
            if plan.get("company_context"):
                plan_snapshot["company_context"] = plan["company_context"]
            if plan.get("enterprise_skill_snapshot"):
                self.skills.verify_current_authorization(transaction, plan["enterprise_skill_snapshot"])
                plan_snapshot["enterprise_skill_snapshot"] = deepcopy(plan["enterprise_skill_snapshot"])
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
                "scheduled_end_at": appointment.get("scheduled_end_at"),
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
                "agent_runtime": {
                    "floor": "none",
                    "floor_reason": "not_connected",
                    "last_sequence": 0,
                    "processed_signal_keys": [],
                    "active_performance_id": None,
                    "takeover": None,
                    "calibration_status": "pending",
                    "calibration_updated_at": now,
                },
                "agent_events": [],
                "last_activity_at": now,
                "started_at": None,
                "candidate_input_completed_at": None,
                "completed_at": None,
                "created_at": now,
                "updated_at": now,
            }
            self._refresh_deferred_speech(transaction, session)
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
            if payload.get("settings", {}).get("record_audio", True) or payload.get("settings", {}).get("record_video", False):
                requested_scopes = [
                    scope
                    for scope, enabled in (
                        ("audio_recording", payload.get("settings", {}).get("record_audio", True)),
                        ("video_recording", payload.get("settings", {}).get("record_video", False)),
                    )
                    if enabled
                ]
                transaction.interview_media_captures.add(
                    {
                        "id": new_id("media_capture"),
                        "organization_id": organization_id,
                        "interview_id": interview_id,
                        "candidate_id": candidate["id"],
                        "provider": "livekit",
                        "room_name": interview_room_name(
                            organization_id, interview_id
                        ),
                        "participant_identity": None,
                        "requested_scopes": requested_scopes,
                        "consented_scopes": deepcopy(candidate.get("media_consent_scopes", [])),
                        "status": "pending",
                        "egress_id": None,
                        "private_uri": None,
                        "content_hash": None,
                        "encryption": None,
                        "encryption_verified_at": None,
                        "encryption_policy": None,
                        "encryption_policy_verified_at": None,
                        "storage_protection": None,
                        "storage_protection_verified_at": None,
                        "storage_protection_policy": None,
                        "storage_protection_policy_verified_at": None,
                        "retention_expires_at": candidate_profile.get("retention_expires_at"),
                        "started_at": None,
                        "stopped_at": None,
                        "failure_code": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
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
        *,
        evidence_fence: Optional[EvidenceCommitFence] = None,
    ) -> Dict[str, Any]:
        return self._control(interview_id, LifecycleCommandType.PAUSE, reason, organization_id,
                             evidence_fence=evidence_fence)

    def report_candidate_runtime_problem(
        self,
        interview_id: str,
        token: Optional[str],
        code: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        """Validate the candidate and persist a real fail-closed pause.

        The browser sends only one allow-listed code. Raw renderer/provider
        messages are deliberately excluded from lifecycle facts and logs.
        """

        self.validate_candidate_token(interview_id, token, organization_id)
        session = self.pause_for_candidate_runtime_problem(interview_id, code, organization_id)
        interruption = session.get("interruption") or {}
        return {
            "interview_id": interview_id,
            "accepted": True,
            "status": session.get("status"),
            "problem_code": code,
            "action": "await_human_takeover",
            "paused_at": interruption.get("occurred_at"),
        }

    def pause_for_candidate_runtime_problem(
        self, interview_id: str, code: str, organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        """Shared by the token-verified HTTP report and authenticated channel."""
        if code not in CANDIDATE_RUNTIME_PROBLEM_REASONS:
            raise ApiError(
                "CANDIDATE_RUNTIME_PROBLEM_INVALID",
                "Candidate runtime problem code is not supported.",
                status_code=422,
            )
        session, work_items = self._apply_command(
            interview_id,
            LifecycleCommand(LifecycleCommandType.PAUSE, {"runtime_problem_code": code}),
            organization_id,
        )
        self._process_report_effects(work_items, organization_id)
        return session

    def record_agent_problem(
        self,
        interview_id: str,
        payload: Dict[str, Any],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        """Persist privileged runtime diagnostics without requiring a channel.

        Media ownership renewal, repair and fatal ingress monitoring deliberately
        outlive one AgentChannel. Keeping this operation on InterviewService lets
        those connection-independent workers fail the domain session closed
        without retaining a stale WebSocket object.
        """

        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(
                transaction.interview_sessions.get(interview_id)
            )
            problems = session.setdefault("agent_runtime", {}).setdefault(
                "problems", []
            )
            problem = {
                "code": str(
                    payload.get("code") or "AGENT_RUNTIME_PROBLEM"
                )[:128],
                "message": str(payload.get("message") or "")[:500],
                "recoverable": bool(payload.get("recoverable")),
                "action": str(payload.get("action") or "")[:128],
                "occurred_at": utc_now(),
            }
            cause_code = str(payload.get("cause_code") or "").strip().upper()
            if (
                cause_code
                and len(cause_code) <= 96
                and all(
                    character.isalnum() or character == "_"
                    for character in cause_code
                )
            ):
                problem["cause_code"] = cause_code
            cause_type = str(payload.get("cause_type") or "").strip()
            if (
                cause_type
                and len(cause_type) <= 96
                and cause_type.isidentifier()
            ):
                problem["cause_type"] = cause_type
            stage = payload.get("stage")
            if stage in {"send", "snapshot", "resume", "commit", "retry", "understanding", "unknown"}:
                problem["stage"] = stage
            problems.append(problem)
            del problems[:-20]
            session["updated_at"] = utc_now()
            return transaction.interview_sessions.update(
                session, expected_version=session["version"]
            )

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
        *,
        evidence_fence: Optional[EvidenceCommitFence] = None,
    ) -> Dict[str, Any]:
        session, work_items = self._apply_command(
            interview_id,
            LifecycleCommand(command_type, {"reason": reason}),
            organization_id,
            evidence_fence=evidence_fence,
        )
        self._process_report_effects(work_items, organization_id)
        return self.get_interview(interview_id, organization_id) if work_items else session

    def get_interview(
        self,
        interview_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id, read_only=True) as transaction:
            return self._required(transaction.interview_sessions.get(interview_id))

    def get_realtime_state(
        self, interview_id: str, organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        """Read current guard fields without loading interview content/history."""
        with self.persistence.transaction(organization_id, read_only=True) as transaction:
            return self._required(transaction.interview_sessions.realtime_state(interview_id))

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
            if turn["id"] == current_turn_id or turn["status"] in {"transcribing", "evaluating", "completed", "skipped"}:
                projection["question_spoken_text"] = turn.get("question_spoken_text", "")
            turns.append(projection)
        return {
            "id": session["id"],
            "status": session["status"],
            "completion": self.candidate_completion_receipt(session),
            "phase": session.get("phase"),
            "avatar_mode": session.get("settings", {}).get("avatar_mode", "cloud"),
            "record_audio": bool(
                session.get("settings", {}).get("record_audio", True)
            ),
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
                    "final_transcript": answer.get("final_transcript", ""),
                }
                for answer in session.get("answers", [])
            ],
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
        }

    @staticmethod
    def candidate_completion_receipt(session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        terminal = session.get("status") in {"completed", "report_generating", "report_ready"}
        submitted_at = session.get("candidate_input_completed_at")
        if not terminal and not submitted_at:
            return None
        return {
            "interview_id": session["id"],
            "status": session["status"] if terminal else "submitted_for_human_review",
            "submitted_at": submitted_at or session.get("completed_at") or session.get("updated_at"),
            "recording_retention_notice": "录音录像按邀请页同意范围和企业保留策略保存。",
            "human_review_required": True,
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
        *,
        evidence_fence: Optional[EvidenceCommitFence] = None,
        prepared_decision: Optional[PreparedTurnDecision] = None,
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
            if evidence_fence is not None:
                assert_current_evidence_fence(transaction, evidence_fence)
            source_session = self._required(transaction.interview_sessions.get(interview_id))
            starting_event_sequence = len(source_session.get("lifecycle_events", []))
            turn_id = self._resolve_turn_id(
                source_session, payload.get("turn_id"), payload.get("question_id")
            )
            source_turn = self.lifecycle.require_active_turn(
                source_session, turn_id, allowed_statuses=("asking", "transcribing")
            )

        now = utc_now()
        utterance = ConversationUtterance(
            utterance_id=new_id("utterance"),
            revision=1,
            speaker="candidate",
            text=str(payload["final_transcript"]).strip(),
            is_final=True,
            authoritative=True,
            audio_uri=str(payload["audio_uri"]),
            stt_confidence=payload.get("stt_confidence"),
            stt_segments=transcript_confidence_segments(str(payload["final_transcript"]).strip(), payload.get("transcript_segments", [])),
            source=transcript_source,
            created_at=now,
        )
        if prepared_decision is not None:
            self._assert_prepared_decision(
                prepared_decision, source_session, source_turn, payload,
            )
            understanding = prepared_decision.understanding.model_copy(
                update={"utterance_id": utterance.utterance_id}, deep=True,
            )
        else:
            from app.services.conversation_input_context import conversation_input_context
            # Batch and other non-prepared evidence still need to distinguish
            # selected conversational responses from the candidate's new words.
            # Preserve this path's existing semantic/confirmation permissions.
            understanding_session = {**source_session, "_interviewer_context": {
                **(source_session.get("_interviewer_context") or {}),
                "input_context": conversation_input_context(source_session, source_turn, utterance.text,
                    segments=utterance.stt_segments, source_capture=payload.get("media_evidence")),
            }}
            understanding = await self.conversation.understand(
                utterance, source_turn, understanding_session
            )
        if understanding.turn_intent == "stop_interview":
            from app.services.candidate_exit_confirmation import confirmed_exit_receipt
            confirmed_exit_receipt(
                prepared_decision.exit_confirmation if prepared_decision is not None else None,
                utterance.text, confirmed_at=now,
            )
        if not ConversationUnderstandingService.accepts_as_answer(understanding, utterance.text) or understanding.suggested_action in {
            "clarify", "repeat", "continue_listening", "pause", "respond_company"
        }:
            understanding_problem = (
                understanding.problem.model_dump(mode="json")
                if understanding.problem is not None
                else None
            )
            with self.persistence.transaction(organization_id) as transaction:
                if evidence_fence is not None:
                    assert_current_evidence_fence(transaction, evidence_fence)
                session = self._required(transaction.interview_sessions.get(interview_id))
                turn = self.lifecycle.require_active_turn(
                    session, turn_id, allowed_statuses=("transcribing",)
                )
                self.assert_skill_authorized(transaction, session)
                if prepared_decision is not None:
                    self._assert_prepared_decision(prepared_decision, session, turn, payload)
                mutable = self._turn_by_id(session, turn["id"])
                mutable.setdefault("utterances", []).append(
                    utterance.model_dump(mode="json")
                )
                mutable["current_understanding"] = understanding.model_dump(mode="json")
                decision, _ = self._decide_and_persist(
                    transaction,
                    session,
                    LifecycleCommand(
                        LifecycleCommandType.UTTERANCE_REJECTED,
                        {
                            "turn_id": turn["id"],
                            "utterance_id": utterance.utterance_id,
                            "intent": understanding.intent,
                            "suggested_action": understanding.suggested_action,
                            "confidence": understanding.confidence,
                            "problem": understanding_problem,
                        },
                    ),
                    organization_id,
                )
                if evidence_fence is not None and payload.get("media_evidence"):
                    DurableEvidenceMedia.release_rejected_capture(
                        transaction,
                        interview_id=interview_id,
                        turn_id=turn_id,
                        media_evidence=payload["media_evidence"],
                        utterance_id=utterance.utterance_id,
                        fence=evidence_fence,
                    )
            return {
                "accepted": False,
                "answer": None,
                "understanding": understanding.model_dump(mode="json"),
                "conversation_action": understanding.suggested_action,
                "understanding_problem": understanding_problem,
                "evaluation": None,
                "evaluation_work_id": None,
                "next_turn_id": turn_id,
                "status": decision.session["status"],
                "report": self._current_report(decision.session),
                "followup": None,
                "events": [
                    deepcopy(item)
                    for item in decision.session.get("lifecycle_events", [])
                    if item["sequence"] > starting_event_sequence
                ],
            }

        followup_decision = (deepcopy(prepared_decision.followup) if prepared_decision is not None
                             else await self.conversation.select_followup(
                                 source_session, source_turn, utterance, understanding))
        answer = {
            "id": new_id("ans"),
            "organization_id": organization_id,
            "interview_id": interview_id,
            "turn_id": source_turn["id"],
            "question_id": source_turn["question_id"],
            "question_snapshot_id": source_turn["question_snapshot_id"],
            "utterance_id": utterance.utterance_id,
            "understanding_id": understanding.understanding_id,
            "root_turn_id": source_turn.get("root_turn_id") or source_turn["id"],
            "raw_transcript": payload.get("raw_transcript") or payload["final_transcript"],
            "final_transcript": payload["final_transcript"],
            "audio_uri": payload.get("audio_uri"),
            "stt_confidence": payload.get("stt_confidence"),
            "stt_confidence_source": confidence_source(payload.get("stt_confidence"),
                payload.get("transcript_segments", []), provider.get("provider_id")),
            "transcript_source": transcript_source,
            "stt_provider": deepcopy(payload.get("stt_provider")),
            "transcript_segments": deepcopy(payload.get("transcript_segments", [])),
            "media_evidence": deepcopy(payload.get("media_evidence")),
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
            "recording_started_at": payload.get("recording_started_at"),
            "recording_finished_at": payload.get("recording_finished_at"),
            "evaluation_status": "pending",
            "current_evaluation_id": None,
            "evaluation_id": None,
            "created_at": now,
            "updated_at": now,
        }
        selected_followup_id: Optional[str] = None
        with self.persistence.transaction(organization_id) as transaction:
            # This is the authoritative effect boundary.  The ownership row is
            # locked before InterviewSession by the PostgreSQL Adapter, so a
            # delayed old STT final cannot create a CandidateAnswer after a
            # newer owner has claimed the interview.
            if evidence_fence is not None:
                assert_current_evidence_fence(transaction, evidence_fence)
                if payload.get("media_evidence"):
                    DurableEvidenceMedia.assert_complete_capture(
                        transaction,
                        interview_id=interview_id,
                        turn_id=turn_id,
                        media_evidence=payload["media_evidence"],
                    )
            session = self._required(transaction.interview_sessions.get(interview_id))
            turn = self.lifecycle.require_active_turn(
                session, turn_id, allowed_statuses=("asking", "transcribing")
            )
            self.assert_skill_authorized(transaction, session)
            if prepared_decision is not None:
                self._assert_prepared_decision(prepared_decision, session, turn, payload)
            mutable = self._turn_by_id(session, turn["id"])
            mutable.setdefault("utterances", []).append(utterance.model_dump(mode="json"))
            mutable["current_understanding"] = understanding.model_dump(mode="json")
            if understanding.turn_intent == "stop_interview":
                from app.services.candidate_exit_confirmation import confirmed_exit_receipt
                mutable["candidate_exit_confirmation"] = confirmed_exit_receipt(
                    prepared_decision.exit_confirmation if prepared_decision is not None else None,
                    utterance.text, confirmed_at=now,
                )
            if mutable.get("company_question_exchanges"):
                from app.services.company_questions import scoring_projection
                scoring_text, excluded = scoring_projection(answer["final_transcript"], mutable,
                    media_evidence=answer.get("media_evidence"))
                if excluded:
                    answer["scoring_transcript"] = scoring_text
                    answer["non_scoring_spans"] = excluded
            decision, work_items = self._decide_and_persist(
                transaction,
                session,
                LifecycleCommand(
                    LifecycleCommandType.ANSWER_SUBMITTED,
                    {
                        "answer": answer,
                        "trigger_reason": "initial_scoring",
                        "hold_for_followup": bool(followup_decision.get("selected")),
                    },
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
                selected_followup_id = followup_turn["id"]
                decision, followup_work = self._decide_and_persist(
                    transaction,
                    decision.session,
                    LifecycleCommand(
                        LifecycleCommandType.FOLLOWUP_REQUESTED,
                        {
                            "answer_id": answer["id"],
                            "root_turn_id": followup_decision["root_turn_id"],
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
                if item.get("id") == selected_followup_id
            ),
            None,
        )
        return {
            "accepted": True,
            "answer": persisted_answer,
            "understanding": understanding.model_dump(mode="json"),
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
        *,
        evidence_fence: Optional[EvidenceCommitFence] = None,
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
        active_turn = self.lifecycle.require_active_turn(
            session, turn_id, allowed_statuses=("asking", "transcribing")
        )
        if active_turn.get("status") == "asking":
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
                evidence_fence=evidence_fence,
                media_evidence=payload.get("media_evidence"),
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
            if getattr(exc, "code", None) == "provider_final_transcript_missing":
                return self.record_untranscribed_capture(
                    interview_id, payload, organization_id, evidence_fence=evidence_fence,
                )
            self._apply_command(
                interview_id,
                LifecycleCommand(
                    LifecycleCommandType.TRANSCRIPTION_FAILED,
                    {"turn_id": turn_id, "error": str(exc)},
                ),
                organization_id,
                evidence_fence=evidence_fence,
                media_evidence=payload.get("media_evidence"),
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
                "recording_started_at": payload.get("recording_started_at"),
                "recording_finished_at": payload.get("recording_finished_at"),
                "audio_uri": payload["audio_uri"],
                "transcript_source": response.source,
                "stt_provider": response.provider.model_dump(),
                "transcript_segments": [item.model_dump() for item in response.segments],
                "media_evidence": deepcopy(payload.get("media_evidence")),
            },
            organization_id,
            evidence_fence=evidence_fence,
        )
        result["transcription"] = response.model_dump()
        return result

    def record_untranscribed_capture(
        self, interview_id: str, payload: Dict[str, Any], organization_id: str = "org_default",
        *, evidence_fence: Optional[EvidenceCommitFence] = None,
    ) -> Dict[str, Any]:
        """Retain audio without inventing an utterance, understanding or answer."""
        with self.persistence.transaction(organization_id) as transaction:
            if evidence_fence is not None:
                assert_current_evidence_fence(transaction, evidence_fence)
            session = self._required(transaction.interview_sessions.get(interview_id))
            turn_id = self._resolve_turn_id(session, payload.get("turn_id"), None)
            turn = self.lifecycle.require_active_turn(session, turn_id, allowed_statuses=("asking", "transcribing"))
            if evidence_fence is not None:
                DurableEvidenceMedia.assert_complete_capture(
                    transaction, interview_id=interview_id, turn_id=turn_id,
                    media_evidence=payload.get("media_evidence") or {},
                )
            if turn["status"] == "asking":
                decision, _ = self._decide_and_persist(
                    transaction, session,
                    LifecycleCommand(LifecycleCommandType.TRANSCRIPTION_STARTED, {
                        "turn_id": turn_id, "recording": {"audio_uri": payload["audio_uri"], "content_type": payload.get("content_type", "audio/wav")},
                    }), organization_id,
                )
                session = decision.session
            decision, _ = self._decide_and_persist(
                transaction, session,
                LifecycleCommand(LifecycleCommandType.TRANSCRIPTION_FAILED, {
                    "turn_id": turn_id, "error": "STT_TRANSCRIPT_UNAVAILABLE",
                }), organization_id,
            )
            if evidence_fence is not None:
                DurableEvidenceMedia.release_untranscribed_capture(
                    transaction, interview_id=interview_id, turn_id=turn_id,
                    media_evidence=payload["media_evidence"], audio_uri=payload["audio_uri"], fence=evidence_fence,
                )
        return {
            "accepted": False, "answer": None, "understanding": None,
            "conversation_action": "continue_listening", "next_turn_id": turn_id,
            "status": decision.session["status"],
            "understanding_problem": {
                "code": "STT_TRANSCRIPT_UNAVAILABLE", "recoverable": True, "action": "continue_listening",
            },
        }

    def assert_skill_authorized(self, transaction: Any, session: Dict[str, Any]) -> None:
        snapshot = (session.get("plan_snapshot") or {}).get("enterprise_skill_snapshot")
        if snapshot:
            self.skills.verify_current_authorization(transaction, snapshot)

    @staticmethod
    def _exit_stt_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "final_transcript": payload.get("final_transcript"),
            "stt_confidence": payload.get("confidence"),
            "language": payload.get("language", "zh-CN"),
            "transcript_segments": payload.get("segments", []),
            "stt_provider": payload.get("provider") or {},
            "transcript_source": "server_streaming",
        }

    def _assert_prepared_exit(
        self, prepared: PreparedCandidateExit, session: Dict[str, Any],
        turn: Dict[str, Any], payload: Dict[str, Any],
    ) -> None:
        if not isinstance(prepared, PreparedCandidateExit):
            raise ApiError("EXIT_CONFIRMATION_REQUIRED", "A private prepared exit is required.", status_code=409)
        text = str(payload.get("final_transcript") or "").strip()
        context = validated_delivered_exit_context(session, turn, text, prepared.exit_confirmation)
        state = session.get("agent_runtime") or {}
        matches = (
            prepared.interview_id == session["id"] and prepared.turn_id == turn["id"]
            and session.get("current_turn_id") == turn["id"] and prepared.text == text
            and state.get("floor") != "human" and (state.get("takeover") or {}).get("status") != "active"
            and prepared.exit_confirmation == context
            and hmac.compare_digest(prepared.stt_fingerprint, self._prepared_stt_fingerprint(payload))
            and hmac.compare_digest(prepared.context_fingerprint, self._prepared_context_fingerprint(session, turn))
        )
        if not matches:
            raise ApiError("TURN_DECISION_STALE", "Prepared exit evidence or context changed.", status_code=409)

    def prepare_streaming_exit(
        self, interview_id: str, turn_id: str, final: StreamingSTTEvent,
        organization_id: str = "org_default", *, exit_confirmation: Dict[str, Any],
    ) -> PreparedCandidateExit:
        """Bind a separately confirmed control request without analyzing answers."""
        self._require_streaming_final(final)
        session = self.get_interview(interview_id, organization_id)
        turn = self.lifecycle.require_active_turn(session, turn_id, allowed_statuses=("asking", "transcribing"))
        context = validated_delivered_exit_context(session, turn, final.text.strip(), exit_confirmation)
        payload = self._exit_stt_payload({
            "final_transcript": final.text, "confidence": final.confidence, "language": final.language,
            "segments": [item.model_dump(mode="json") for item in final.segments],
            "provider": final.provider.model_dump(mode="json") if final.provider else {},
        })
        prepared = PreparedCandidateExit(
            interview_id=interview_id, turn_id=turn_id, text=final.text.strip(),
            stt_fingerprint=self._prepared_stt_fingerprint(payload),
            context_fingerprint=self._prepared_context_fingerprint(session, turn),
            exit_confirmation=context,
        )
        self._assert_prepared_exit(prepared, session, turn, payload)
        return prepared

    def assert_prepared_streaming_exit(
        self, prepared: PreparedCandidateExit, final: StreamingSTTEvent,
        organization_id: str = "org_default",
    ) -> None:
        """Check the exact final before its recognizer or recording is sealed."""
        self._require_streaming_final(final)
        if not isinstance(prepared, PreparedCandidateExit):
            raise ApiError("EXIT_CONFIRMATION_REQUIRED", "A private prepared exit is required.", status_code=409)
        session = self.get_interview(prepared.interview_id, organization_id)
        turn = self.lifecycle.require_active_turn(session, prepared.turn_id, allowed_statuses=("asking", "transcribing"))
        self._assert_prepared_exit(prepared, session, turn, self._exit_stt_payload({
            "final_transcript": final.text, "confidence": final.confidence, "language": final.language,
            "segments": [item.model_dump(mode="json") for item in final.segments],
            "provider": final.provider.model_dump(mode="json") if final.provider else {},
        }))

    async def submit_confirmed_exit(
        self, interview_id: str, payload: Dict[str, Any], organization_id: str = "org_default", *,
        evidence_fence: EvidenceCommitFence, prepared_exit: PreparedCandidateExit,
    ) -> Dict[str, Any]:
        """Atomically retain speech and end input, independently of answer analysis."""
        if not isinstance(prepared_exit, PreparedCandidateExit) or prepared_exit.interview_id != interview_id:
            raise ApiError("EXIT_CONFIRMATION_REQUIRED", "A private prepared exit is required.", status_code=409)
        if (not payload.get("final_transcript") or not (payload.get("provider") or {}).get("provider_id")
                or not payload.get("audio_uri")):
            raise ApiError("AUTHORITATIVE_TRANSCRIPT_REQUIRED", "Exit requires sealed server speech and audio.", status_code=409)
        from app.services.evidence_coordination import EvidenceOwnershipCoordinator
        if (evidence_fence is None
                or evidence_fence.ownership_id != EvidenceOwnershipCoordinator.ownership_id(interview_id)):
            raise ApiError("EVIDENCE_OWNER_FENCED", "A current Evidence owner is required.", status_code=409)
        if payload.get("turn_id") != prepared_exit.turn_id:
            raise ApiError("TURN_DECISION_STALE", "Prepared exit turn changed.", status_code=409)
        canonical = self._exit_stt_payload(payload)
        now = utc_now()
        with self.persistence.transaction(organization_id) as transaction:
            assert_current_evidence_fence(transaction, evidence_fence)
            session = self._required(transaction.interview_sessions.get(interview_id))
            turn = self._turn_by_id(session, prepared_exit.turn_id)
            previous = turn.get("exit_evidence") or {}
            if session.get("candidate_input_completed_at"):
                # Same committed control is idempotent. Different speech,
                # media, confirmation or preparation cannot borrow its result.
                context = validated_delivered_exit_context(session, turn,
                    str(payload["final_transcript"]).strip(), prepared_exit.exit_confirmation)
                original_receipt = turn.get("candidate_exit_confirmation") or {}
                matches = (session.get("candidate_input_completion_reason") == "candidate_requested"
                    and previous.get("version") == "candidate_exit_evidence.v1"
                    and str(previous.get("final_transcript") or "").strip() == prepared_exit.text
                    and previous.get("stt_fingerprint") == prepared_exit.stt_fingerprint
                    and previous.get("stt_fingerprint") == self._prepared_stt_fingerprint(canonical)
                    and previous.get("context_fingerprint") == prepared_exit.context_fingerprint
                    and previous.get("audio_uri") == payload["audio_uri"]
                    and previous.get("media_evidence") == payload.get("media_evidence")
                    and original_receipt == confirmed_exit_receipt(context, prepared_exit.text,
                        confirmed_at=original_receipt.get("confirmed_at")))
                if not matches:
                    raise ApiError("TURN_DECISION_STALE", "The completed exit belongs to different evidence.", status_code=409)
                return self._exit_result(session, events=[])
            turn = self.lifecycle.require_active_turn(session, prepared_exit.turn_id,
                                                       allowed_statuses=("asking", "transcribing"))
            self.assert_skill_authorized(transaction, session)
            self._assert_prepared_exit(prepared_exit, session, turn, canonical)
            DurableEvidenceMedia.assert_complete_capture(transaction, interview_id=interview_id,
                turn_id=turn["id"], media_evidence=payload.get("media_evidence") or {})
            receipt = confirmed_exit_receipt(prepared_exit.exit_confirmation, prepared_exit.text, confirmed_at=now)
            utterance = ConversationUtterance(
                utterance_id=new_id("utterance"), revision=1, speaker="candidate", text=prepared_exit.text,
                is_final=True, authoritative=True, audio_uri=payload["audio_uri"],
                stt_confidence=payload.get("confidence"),
                stt_segments=transcript_confidence_segments(prepared_exit.text, payload.get("segments", [])),
                source="server_streaming", created_at=now,
            )
            mutable = self._turn_by_id(session, turn["id"])
            mutable.setdefault("utterances", []).append(utterance.model_dump(mode="json"))
            mutable["candidate_exit_confirmation"] = receipt
            mutable["exit_evidence"] = {
                "version": "candidate_exit_evidence.v1", "assessment_status": "not_evaluated",
                "reason": "candidate_confirmed_exit", "utterance_id": utterance.utterance_id,
                "raw_transcript": payload["final_transcript"], "final_transcript": payload["final_transcript"],
                "audio_uri": payload["audio_uri"], "content_type": payload.get("content_type", "audio/wav"),
                "stt_confidence": payload.get("confidence"), "transcript_source": "server_streaming",
                "stt_provider": deepcopy(payload["provider"]), "transcript_segments": deepcopy(payload.get("segments", [])),
                "media_evidence": deepcopy(payload.get("media_evidence")), "language": payload.get("language", "zh-CN"),
                "duration_seconds": payload.get("duration_seconds", 0),
                "recording_started_at": payload.get("recording_started_at"),
                "recording_finished_at": payload.get("recording_finished_at"),
                "stt_fingerprint": prepared_exit.stt_fingerprint, "context_fingerprint": prepared_exit.context_fingerprint,
                "retained_at": now,
            }
            session["candidate_exit_confirmed_at"] = now
            starting_sequence = len(session.get("lifecycle_events", []))
            decision, _ = self._decide_and_persist(transaction, session, LifecycleCommand(
                LifecycleCommandType.END_CANDIDATE_INPUT, {
                    "decision_id": "candidate_stop_" + turn["id"],
                    "expected_decision_revision": int(session.get("decision_revision", 0)),
                    "reason": "candidate_requested",
                }), organization_id)
            events = [deepcopy(event) for event in decision.session.get("lifecycle_events", [])
                      if event["sequence"] > starting_sequence]
        return self._exit_result(decision.session, events=events)

    def _exit_result(self, session: Dict[str, Any], *, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"exit_confirmed": True, "accepted": False, "answer": None, "understanding": None,
                "conversation_action": None, "evaluation": None, "evaluation_work_id": None,
                "next_turn_id": session.get("current_turn_id"), "status": session["status"],
                "report": self._current_report(session), "followup": None, "events": events}

    def finish_on_candidate_intent(
        self, interview_id: str, organization_id: str = "org_default", *,
        evidence_fence: Optional[EvidenceCommitFence] = None,
    ) -> Dict[str, Any]:
        """Only a committed server understanding can request conversational closure."""
        with self.persistence.transaction(organization_id) as transaction:
            if evidence_fence is not None:
                assert_current_evidence_fence(transaction, evidence_fence)
            session = self._required(transaction.interview_sessions.get(interview_id))
            if session.get("status") not in {"in_progress", "paused"} or session.get("candidate_input_completed_at"):
                return session
            answered = {answer["turn_id"] for answer in session.get("answers", [])}
            previous = next((turn for turn in reversed(session.get("turns", [])) if turn["id"] in answered), None)
            understanding = (previous or {}).get("current_understanding") or {}
            if understanding.get("turn_intent") != "stop_interview":
                return session
            from app.services.candidate_exit_confirmation import text_hash
            receipt = previous.get("candidate_exit_confirmation") or {}
            answer = next((item for item in session.get("answers", []) if item.get("turn_id") == previous["id"]), {})
            if (receipt.get("state") != "confirmed" or not receipt.get("confirmed_at")
                    or receipt.get("transcript_sha256") != text_hash(str(answer.get("final_transcript") or "").strip())):
                return session
            session["candidate_exit_confirmed_at"] = receipt["confirmed_at"]
            decision, _ = self._decide_and_persist(transaction, session, LifecycleCommand(
                LifecycleCommandType.END_CANDIDATE_INPUT, {
                    "decision_id": "candidate_stop_" + previous["id"],
                    "expected_decision_revision": int(session.get("decision_revision", 0)),
                    "reason": "candidate_requested",
                }), organization_id)
            return decision.session

    @staticmethod
    def _assert_planning_control(transaction: Any, evidence_fence: Optional[EvidenceCommitFence],
                                 planning_control: Optional[Tuple[str, int]]) -> None:
        if evidence_fence is not None:
            assert_current_evidence_fence(transaction, evidence_fence)
        if planning_control is not None:
            if (evidence_fence is None or len(planning_control) != 2
                    or not isinstance(planning_control[0], str) or not planning_control[0]
                    or type(planning_control[1]) is not int or planning_control[1] < 1):
                raise ApiError("EVIDENCE_CONTROL_STALE", "A valid planning controller is required.", status_code=409)
            owner = transaction.evidence_ownerships.get(evidence_fence.ownership_id)
            if (not owner or owner.get("control_connection_id") != planning_control[0]
                    or owner.get("control_generation") != planning_control[1]):
                raise ApiError("EVIDENCE_CONTROL_STALE", "The planning controller has been replaced.", status_code=409)

    async def advance_adaptive_interview(
        self, interview_id: str, organization_id: str = "org_default", *,
        evidence_fence: Optional[EvidenceCommitFence] = None,
        planning_control: Optional[Tuple[str, int]] = None,
    ) -> Dict[str, Any]:
        """Propose outside the transaction; commit one frozen decision with all fences."""
        with self.persistence.transaction(organization_id) as transaction:
            self._assert_planning_control(transaction, evidence_fence, planning_control)
            source = self._required(transaction.interview_sessions.get(interview_id))
        if not is_adaptive(source) or source.get("current_turn_id") or source.get("candidate_input_completed_at"):
            return source
        if source.get("status") != "in_progress":
            raise ApiError("INTERVIEW_DECISION_NOT_EXPECTED", "The interview is not accepting new decisions.", status_code=409)
        now = format_utc(ensure_utc(self.clock()))
        budget = budget_summary(source, now)
        fingerprint = supervisor_fingerprint(source)
        revision = int(source.get("decision_revision", 0))
        if budget["exhausted"]:
            decision_id = "deadline_" + hashlib.sha256((interview_id + ":" + str(revision)).encode()).hexdigest()[:32]
            proposal = NextInterviewDecision(interview_id, revision, fingerprint, decision_id,
                "finish_interview", None, "budget_exhausted", "none", (), 0)
        else:
            proposal = await self.supervisor.propose_next(source)
        with self.persistence.transaction(organization_id) as transaction:
            self._assert_planning_control(transaction, evidence_fence, planning_control)
            session = self._required(transaction.interview_sessions.get(interview_id))
            self.assert_skill_authorized(transaction, session)
            if (session.get("status") != "in_progress"
                    or not hmac.compare_digest(proposal.context_fingerprint, supervisor_fingerprint(session))):
                raise ApiError("AGENT_DECISION_STALE", "The interview changed while planning; discard the old proposal.", status_code=409)
            session = deepcopy(session)
            payload = {"decision_id": proposal.decision_id, "expected_decision_revision": proposal.revision,
                       "reason": proposal.reason_code}
            if proposal.action == "select_question":
                payload["question_id"] = proposal.question_id
                if proposal.inquiry_unit_id is not None:
                    payload["inquiry_unit_id"] = proposal.inquiry_unit_id
                command = LifecycleCommand(LifecycleCommandType.SELECT_NEXT, payload)
            else:
                payload["reason"] = "evidence_sufficient" if proposal.reason_code == "coverage_complete" else proposal.reason_code
                command = LifecycleCommand(LifecycleCommandType.END_CANDIDATE_INPUT, payload)
            session.setdefault("agent_runtime", {})["planning_problem"] = None
            session["agent_runtime"]["next_transition_key"] = proposal.transition_key
            # Safe metadata only; raw model output and candidate speech are not duplicated here.
            session["agent_runtime"]["last_planning"] = {
                "decision_id": proposal.decision_id, "reason_code": proposal.reason_code,
                "tools_used": list(proposal.tools_used), "model_calls": proposal.model_calls,
                "prompt_version": SUPERVISOR_VERSION, "context_hash": proposal.context_fingerprint,
            }
            if proposal.selection_strategy is not None:
                session["agent_runtime"]["last_planning"].update(
                    selection_strategy=proposal.selection_strategy, fallback_reason=proposal.fallback_reason)
            decision, _ = self._decide_and_persist(transaction, session, command, organization_id)
            return decision.session

    async def prepare_streaming_decision(
        self, interview_id: str, turn_id: str,
        final: Union[StreamingSTTEvent, StableTranscriptPreview],
        organization_id: str = "org_default", *, snapshot_ref: str,
        completion_confirmed: bool = False,
        semantic_first: bool = False,
        exit_confirmation: Optional[Dict[str, Any]] = None,
        content_clarification: Optional[Dict[str, Any]] = None,
    ) -> PreparedTurnDecision:
        """Prepare from a stable server prefix or final; neither commits an answer."""
        is_preview = isinstance(final, StableTranscriptPreview)
        if is_preview:
            try:
                final = StableTranscriptPreview.model_validate(final.model_dump(mode="json"))
                if final.has_unstable_tail or not final.text.strip():
                    raise ValueError("unstable or empty preview")
            except (ValueError, TypeError, AttributeError):
                raise ApiError(
                    "STREAMING_TRANSCRIPT_INVALID", "Stable server preview required.", status_code=409,
                ) from None
        else:
            self._require_streaming_final(final)
        session = self.get_interview(interview_id, organization_id)
        turn = self.lifecycle.require_active_turn(session, turn_id, allowed_statuses=("asking",))
        # Freeze both identities before awaiting inference. A final may keep
        # the same text while confidence, timestamps or provider change.
        stt_fingerprint = self._prepared_stt_fingerprint({
            "final_transcript": final.text,
            "stt_confidence": final.confidence,
            "language": final.language,
            "transcript_segments": [item.model_dump(mode="json") for item in final.segments],
            "stt_provider": final.provider.model_dump(mode="json"),
            "transcript_source": "server_streaming",
        })
        context_fingerprint = self._prepared_context_fingerprint(session, turn)
        supplement_context_fingerprint = self._supplement_context_fingerprint(session, turn)
        if content_clarification is not None:
            from app.services.content_clarification import validated_content_clarification
            content_clarification = validated_content_clarification(session, turn, final.text.strip(), content_clarification)
            session = {**session, "_content_clarification": content_clarification}
        if exit_confirmation is not None:
            from app.services.candidate_exit_confirmation import validated_exit_context
            exit_confirmation = validated_exit_context(session, turn, final.text.strip(), exit_confirmation)
            session = {**session, "_candidate_exit_confirmation": exit_confirmation}
        if completion_confirmed:
            session = {**session, "_answer_completion_confirmed": True}
        if semantic_first:
            session = {**session, "_semantic_first": True}
        # Reuse the complete identities already frozen above; no additional
        # persistence read is needed to bind a failed contract continuation.
        correction_identity = hashlib.sha256(json.dumps((
            organization_id, interview_id, turn_id, stt_fingerprint,
            context_fingerprint, supplement_context_fingerprint,
            semantic_first, completion_confirmed,
            exit_confirmation, content_clarification,
        ), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        with answer_correction_binding(correction_identity):
            if is_preview:
                understanding, followup = await self.supervisor.prepare_preview(
                    final, turn, session, snapshot_ref=snapshot_ref,
                )
            else:
                utterance = ConversationUtterance(
                    utterance_id=new_id("utterance_preview"), revision=1, speaker="candidate",
                    text=final.text.strip(), is_final=True, authoritative=True, audio_uri=snapshot_ref,
                    stt_confidence=final.confidence, source="server_streaming", created_at=utc_now(),
                    stt_segments=transcript_confidence_segments(final.text.strip(), final.segments),
                )
                understanding, followup = await self.supervisor.prepare_turn(utterance, turn, session)
        return PreparedTurnDecision(
            interview_id, turn_id, final.text.strip(), understanding, followup,
            stt_fingerprint=stt_fingerprint, context_fingerprint=context_fingerprint,
            semantic_first=semantic_first,
            completion_confirmed=completion_confirmed,
            exit_confirmation=exit_confirmation,
            content_clarification=content_clarification,
            supplement_context_fingerprint=supplement_context_fingerprint,
        )

    def confirm_prepared_streaming_decision(
        self, prepared: PreparedTurnDecision, base_final: StreamingSTTEvent,
        delivered_boundary_final: StreamingSTTEvent, final: StreamingSTTEvent,
        organization_id: str = "org_default", *, reply_text: str,
        decision: Dict[str, Any], question_delivered: bool,
    ) -> Optional[PreparedTurnDecision]:
        """Compose a delivered control reply with unchanged technical evidence.

        A cache miss has no lifecycle effect and grants no completion authority.
        In particular, an appended technical detail or ASR rewrite requires a
        fresh understanding rather than silently inheriting the old decision.
        """
        if (question_delivered is not True or not isinstance(decision, dict)
                or decision.get("intent") != "finish"
                or type(decision.get("confidence")) not in {float, int}
                or not .75 <= decision["confidence"] <= 1):
            return None
        try:
            self._require_streaming_final(final)
            session = self.get_interview(prepared.interview_id, organization_id)
            turn = self.lifecycle.require_active_turn(session, prepared.turn_id, allowed_statuses=("asking",))
            question = self._delivered_supplement_question(session, turn)
            if question is None:
                return None
            receipt = PreparedCompletionReceipt(
                source=prepared, base_final=base_final.model_copy(deep=True),
                boundary_final=delivered_boundary_final.model_copy(deep=True),
                reply_text=reply_text, confidence=minimum_reported_confidence(
                    float(decision["confidence"]), final.confidence,
                    *(item.confidence for item in final.segments[len(base_final.segments):])),
                question_act_id=question,
            )
            payload = self._streaming_prepared_payload(final)
            understanding = self._compose_supplement_understanding(receipt, final.text.strip())
            composed = replace(prepared, text=final.text.strip(), understanding=understanding,
                followup=deepcopy(prepared.followup), completion_confirmed=True,
                stt_fingerprint=self._prepared_stt_fingerprint(payload),
                context_fingerprint=self._prepared_context_fingerprint(session, turn),
                completion_receipt=receipt)
            self._assert_prepared_decision(composed, session, turn, payload)
            from app.core.interview_agent_metrics import observe_interview_agent_metric
            from app.core.speech_diagnostics import record_turn_control
            observe_interview_agent_metric("confirmed_answer_preparation_reused", 1)
            record_turn_control("prepared_answer_reused", interview_id=prepared.interview_id,
                turn_id=prepared.turn_id, source_understanding_id=prepared.understanding.understanding_id,
                current_understanding_id=composed.understanding.understanding_id,
                question_act_id=question)
            return composed
        except (ApiError, ValueError, TypeError, KeyError, AttributeError):
            return None

    @staticmethod
    def _streaming_prepared_payload(final: StreamingSTTEvent) -> Dict[str, Any]:
        return {"final_transcript": final.text, "stt_confidence": final.confidence,
                "language": final.language,
                "transcript_segments": [item.model_dump(mode="json") for item in final.segments],
                "stt_provider": final.provider.model_dump(mode="json") if final.provider else {},
                "transcript_source": "server_streaming"}

    @staticmethod
    def _delivered_supplement_question(session: Dict[str, Any], turn: Dict[str, Any]) -> Optional[str]:
        from app.core.prompt.contracts import SUPPLEMENT_SPEECH, SUPPLEMENT_SPEECH_VERSION
        from app.services.conversation_delivery import selected_events
        acts = {act.get("act_id"): act for act in turn.get("conversation_acts", [])
                if act.get("act_type") in {"supplement_check", "supplement_clarify"}
                and act.get("prompt_version") == SUPPLEMENT_SPEECH_VERSION}
        events = [event for event in selected_events(session, turn)
                  if (event.get("payload") or {}).get("act_id") in acts]
        if not events:
            return None
        event = max(events, key=lambda item: item.get("session_sequence", 0))
        act_id = event["payload"]["act_id"]
        delivery = event.get("delivery") or {}
        if (delivery.get("status") != "delivered" or not delivery.get("performance_id")
                or acts[act_id].get("text") not in {SUPPLEMENT_SPEECH["check"], SUPPLEMENT_SPEECH["clarify"]}):
            return None
        return act_id

    @classmethod
    def _supplement_context_fingerprint(cls, session: Dict[str, Any], turn: Dict[str, Any]) -> str:
        # Supplement checks add dialogue but cannot change the technical answer.
        # Remove only these known fixed acts; policy, questions, ownership-related
        # lifecycle events, clarifications and every other exchange remain bound.
        normalized = deepcopy(session)
        normalized_turn = next(item for item in normalized["turns"] if item["id"] == turn["id"])
        normalized_turn["conversation_acts"] = [act for act in normalized_turn.get("conversation_acts", [])
            if act.get("act_type") not in {"supplement_check", "supplement_clarify", "supplement_continue"}]
        return cls._prepared_context_fingerprint(normalized, normalized_turn)

    @staticmethod
    def _compose_supplement_understanding(receipt: PreparedCompletionReceipt, transcript: str,
                                          *, understanding_id: Optional[str] = None,
                                          created_at: Optional[str] = None) -> TurnUnderstanding:
        from app.services.prepared_answer_completion import pure_completion_reply
        source = receipt.source
        understanding = source.understanding
        if (source.completion_receipt is not None or source.completion_confirmed
                or not source.semantic_first or source.exit_confirmation is not None
                or source.content_clarification is not None or understanding.problem is not None
                or understanding.intent not in {"answer", "off_topic"}
                or understanding.turn_intent not in {"answering", "finish_answer"}
                or understanding.suggested_action not in {"accept", "followup", "next", "continue_listening"}
                or understanding.ambiguities or understanding.contradictions
                or understanding.clarification_target is not None
                or not pure_completion_reply(receipt.reply_text)):
            raise ValueError("Preparation cannot be composed with a control reply")
        data = understanding.model_dump(mode="json")
        data.update(understanding_id=understanding_id or new_id("understanding"),
            created_at=created_at or utc_now(), turn_intent="finish_answer", suggested_action="next",
            followup_allowed=understanding.intent != "off_topic",
            completion_basis={"source": "explicit_server_intent", "evidence_quotes": [receipt.reply_text]},
            confidence=min(understanding.confidence, receipt.confidence))
        composed = TurnUnderstanding.model_validate(data)
        from app.core.prompt.semantic_turn import validate_semantic_turn
        validate_semantic_turn(composed.model_dump(mode="json"), transcript)
        return composed

    def _assert_completion_receipt(self, prepared: PreparedTurnDecision, session: Dict[str, Any],
                                   turn: Dict[str, Any], payload: Dict[str, Any]) -> None:
        receipt = prepared.completion_receipt
        if receipt is None:
            return
        from app.services.prepared_answer_completion import unchanged_answer_prefix
        source = receipt.source
        self._require_streaming_final(receipt.base_final)
        self._require_streaming_final(receipt.boundary_final)
        base_payload = self._streaming_prepared_payload(receipt.base_final)
        boundary_payload = self._streaming_prepared_payload(receipt.boundary_final)
        expected = self._compose_supplement_understanding(receipt, str(payload["final_transcript"]).strip(),
            understanding_id=prepared.understanding.understanding_id, created_at=prepared.understanding.created_at)
        valid = (source.interview_id == session["id"] and source.turn_id == turn["id"]
                 and prepared.completion_confirmed and prepared.semantic_first
                 and prepared.capture_binding is source.capture_binding
                 and source.text == receipt.base_final.text.strip()
                 and source.stt_fingerprint == self._prepared_stt_fingerprint(base_payload)
                 and source.stt_fingerprint == self._prepared_stt_fingerprint(boundary_payload)
                 and source.supplement_context_fingerprint == self._supplement_context_fingerprint(session, turn)
                 and receipt.question_act_id == self._delivered_supplement_question(session, turn)
                 and unchanged_answer_prefix(base_payload, payload, receipt.reply_text)
                 and prepared.followup == source.followup
                 and prepared.understanding == expected)
        if not valid:
            raise ValueError("Completion evidence no longer matches preparation")
        self.conversation._validate_understanding_content(expected.model_dump(mode="json"),
            str(payload["final_transcript"]).strip(), self.conversation._capability_points(turn))

    @staticmethod
    def _require_streaming_final(final: StreamingSTTEvent) -> None:
        if (
            not isinstance(final, StreamingSTTEvent)
            or final.type != "transcript.final"
            or not final.is_final
            or final.provider is None
            or not final.provider.provider_id
            or not final.text.strip()
        ):
            raise ApiError("STREAMING_TRANSCRIPT_INVALID", "Server final required.", status_code=409)

    @staticmethod
    def _prepared_stt_fingerprint(payload: Dict[str, Any]) -> str:
        """Bind all normalized semantic/provenance fields, not transport ticks."""

        identity = {
            "text": str(payload["final_transcript"]).strip(),
            "confidence": payload.get("stt_confidence"),
            "language": str(payload.get("language", "zh-CN")),
            "segments": [
                TranscriptSegment.model_validate(item).model_dump(mode="json")
                for item in payload.get("transcript_segments", [])
            ],
            "provider": ProviderMeta.model_validate(payload["stt_provider"]).model_dump(mode="json"),
            "source": payload.get("transcript_source"),
        }
        return hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")).hexdigest()

    @staticmethod
    def _prepared_context_fingerprint(session: Dict[str, Any], turn: Dict[str, Any]) -> str:
        """Freeze decision inputs without coupling to heartbeat/store versions."""
        from app.services.conversation_delivery import selected_events
        from app.services.supplement_context import supplement_context
        from app.core.prompt.conversation_reception import RECEPTION_SPEECH, RECEPTION_SPEECH_VERSION

        # A fixed processing-status notice may be selected/delivered while a
        # bounded retry is running. It conveys no new question or candidate
        # evidence. Ignore only that exact versioned notice in this reuse
        # fingerprint, while keeping its actual dialogue/delivery records.
        # Ordinary reception and any source-linked exchange still invalidate.
        memory_turns = []
        for item in session.get("turns", []):
            exchanges = {exchange.get("act_id") for exchange in item.get("reception_exchanges", [])}
            acts = [act for act in item.get("conversation_acts", []) if not (
                act.get("act_type") == "conversation_acknowledgement"
                and act.get("prompt_version") == RECEPTION_SPEECH_VERSION
                and act.get("text") == RECEPTION_SPEECH["unavailable"]
                and act.get("act_id") not in exchanges
            )]
            memory_turns.append({**item, "conversation_acts": acts})
        memory_session = {**session, "turns": memory_turns}

        root_id = turn.get("root_turn_id") or turn["id"]
        root = next((item for item in session.get("turns", []) if item.get("id") == root_id), turn)
        question_fields = (
            "id", "question_id", "question_snapshot_id", "question_snapshot",
            "question_spoken_text", "root_turn_id", "parent_turn_id", "is_followup",
            "followup_depth", "allow_followup", "target_key_points", "phase", "weight",
        )
        budget_fields = (
            "id", "is_followup", "root_turn_id", "parent_turn_id",
            "followup_depth", "allow_followup", "target_key_points",
        )
        current_understanding = turn.get("current_understanding") or {}
        settings = session.get("settings") or {}
        context = {
            "organization_id": session.get("organization_id", "org_default"),
            "interview_id": session["id"],
            "current_turn_id": session.get("current_turn_id"),
            "turn": {key: turn.get(key) for key in question_fields},
            "reception_exchanges": turn.get("reception_exchanges") or [],
            "content_clarification": turn.get("content_clarification"),
            "supplement_context": supplement_context(session, turn),
            "content_clarification_acts": [act for act in turn.get("conversation_acts", [])
                                            if act.get("prompt_version") == "answer_clarification.v1"],
            "exit_confirmation_acts": [act for act in turn.get("conversation_acts", [])
                                       if act.get("prompt_version") == "candidate_exit_confirmation.v1"],
            "reception_selections": [{
                "event_id": event.get("event_id"), "turn_id": event.get("turn_id"),
                "session_sequence": event.get("session_sequence"),
                "payload": {key: (event.get("payload") or {}).get(key) for key in ("act_id", "text", "approved")},
            } for event in selected_events(session, turn)
                                     if event.get("event_id") in {
                                         item.get("selected_event_id") for item in turn.get("reception_exchanges", [])}],
            "reception_acts": [{key: act.get(key) for key in ("act_id", "turn_id", "text", "selection_receipts")}
                               for act in turn.get("conversation_acts", [])
                               if act.get("act_id") in {
                                   item.get("act_id") for item in (turn.get("reception_exchanges") or [])[-6:]}],
            "root": {key: root.get(key) for key in question_fields},
            "understanding_revision": {
                "understanding_id": current_understanding.get("understanding_id"),
                "revision": current_understanding.get("revision", 0),
            },
            "budget_turns": sorted(
                ({key: item.get(key) for key in budget_fields} for item in session.get("turns", [])),
                key=lambda item: str(item["id"]),
            ),
            "followup_policy": {
                **ConversationUnderstandingService.DEFAULT_POLICY,
                **(session.get("followup_policy") or {}),
            },
            "scheduled_end_at": session.get("scheduled_end_at"),
            "settings_scheduled_end_at": settings.get("scheduled_end_at"),
            "language": settings.get("language", "zh-CN"),
            "started_at": session.get("started_at"),
            "estimated_minutes": (session.get("plan_snapshot") or {}).get("estimated_minutes", 0),
            "supervisor_context": {
                "decision_revision": session.get("decision_revision", 0),
                "control_events": [event.get("id") for event in session.get("lifecycle_events", [])
                                   if event.get("type") in {"interview.paused", "interview.resumed", "interview.recovered", "interview.timed_out"}],
                "memory": conversation_memory(memory_session),
                "skill_snapshot": (session.get("plan_snapshot") or {}).get("enterprise_skill_snapshot"),
                "company_context": (session.get("plan_snapshot") or {}).get("company_context"),
                "contract_hash": ((session.get("plan_snapshot") or {}).get("assessment_contract") or {}).get("contract_hash"),
            },
        }
        return hashlib.sha256(json.dumps(
            context, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")).hexdigest()

    def _assert_prepared_decision(
        self, prepared: PreparedTurnDecision, session: Dict[str, Any],
        turn: Dict[str, Any], payload: Dict[str, Any],
    ) -> None:
        try:
            self._assert_completion_receipt(prepared, session, turn, payload)
            from app.services.content_clarification import validated_content_clarification
            clarification = validated_content_clarification(session, turn, str(payload["final_transcript"]).strip(), prepared.content_clarification)
            if prepared.understanding.intent == "answer_unresolved":
                from app.core.prompt.semantic_turn import validate_unresolved_completion
                validate_unresolved_completion(prepared.understanding.model_dump(mode="json"),
                    str(payload["final_transcript"]).strip(), clarification)
            from app.services.candidate_exit_confirmation import validated_exit_context, confirmed_exit_receipt
            exit_context = validated_exit_context(session, turn, str(payload["final_transcript"]).strip(), prepared.exit_confirmation)
            if prepared.understanding.turn_intent == "stop_interview":
                confirmed_exit_receipt(exit_context, str(payload["final_transcript"]).strip(), confirmed_at=utc_now())
            matches = (
                prepared.interview_id == session["id"]
                and prepared.turn_id == turn["id"]
                and prepared.text == str(payload["final_transcript"]).strip()
                and hmac.compare_digest(prepared.stt_fingerprint, self._prepared_stt_fingerprint(payload))
                and hmac.compare_digest(prepared.context_fingerprint, self._prepared_context_fingerprint(session, turn))
            )
            if matches and prepared.followup.get("selected"):
                policy = {
                    **ConversationUnderstandingService.DEFAULT_POLICY,
                    **(session.get("followup_policy") or {}),
                }
                matches = self.conversation._within_time_budget(
                    session, int(policy["min_remaining_seconds"]), now=self.clock(),
                )
            if (matches and prepared.semantic_first and not prepared.completion_confirmed
                    and prepared.understanding.intent in {"answer", "answer_declined", "answer_unresolved", "off_topic"}
                    and prepared.understanding.suggested_action not in {"clarify", "repeat", "continue_listening", "pause", "respond_company"}):
                matches = self.conversation.can_complete_without_confirmation(
                    prepared.understanding, str(payload["final_transcript"]).strip())
        except (ValueError, TypeError, KeyError, AttributeError):
            matches = False
        if not matches:
            raise ApiError(
                "TURN_DECISION_STALE", "Prepared evidence or decision context no longer matches final.",
                status_code=409,
            )

    def assert_streaming_preparation_current(
        self, prepared: PreparedTurnDecision, transcript: Any, organization_id: str, *,
        completion_confirmed: bool = False, semantic_first: bool = False,
        exit_confirmation: Optional[Dict[str, Any]] = None,
        content_clarification: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Validate reusable analysis, independently of permission to finish.

        A stable preview and an unconfirmed semantic result can be retained,
        but neither becomes an authoritative answer through this check. The
        final effect still uses assert_prepared_streaming_decision and its
        transaction fence. Heartbeats and recovery notifications are not new
        reasoning inputs; question, policy, evidence and confirmation are.
        """
        if isinstance(transcript, StableTranscriptPreview):
            try:
                transcript = StableTranscriptPreview.model_validate(transcript.model_dump(mode="json"))
                if transcript.has_unstable_tail or not transcript.text.strip():
                    raise ValueError("unstable preview")
            except (ValueError, TypeError, AttributeError):
                raise ApiError("STREAMING_TRANSCRIPT_INVALID", "Stable server preview required.", status_code=409) from None
        else:
            self._require_streaming_final(transcript)
        session = self.get_interview(prepared.interview_id, organization_id)
        turn = self.lifecycle.require_active_turn(session, prepared.turn_id, allowed_statuses=("asking",))
        state = session.get("agent_runtime") or {}
        try:
            from app.services.content_clarification import validated_content_clarification
            from app.services.candidate_exit_confirmation import validated_exit_context
            clarification = validated_content_clarification(session, turn, transcript.text.strip(), content_clarification)
            exit_context = validated_exit_context(session, turn, transcript.text.strip(), exit_confirmation)
            matches = (
                state.get("floor") != "human"
                and (state.get("takeover") or {}).get("status") != "active"
                and prepared.interview_id == session["id"]
                and prepared.turn_id == turn["id"]
                and prepared.text == transcript.text.strip()
                and prepared.semantic_first == semantic_first
                and prepared.completion_confirmed == completion_confirmed
                and prepared.exit_confirmation == exit_context
                and prepared.content_clarification == clarification
                and hmac.compare_digest(prepared.context_fingerprint, self._prepared_context_fingerprint(session, turn))
                and hmac.compare_digest(prepared.stt_fingerprint, self._prepared_stt_fingerprint({
                    "final_transcript": transcript.text,
                    "stt_confidence": transcript.confidence,
                    "language": transcript.language,
                    "transcript_segments": [item.model_dump(mode="json") for item in transcript.segments],
                    "stt_provider": transcript.provider.model_dump(mode="json") if transcript.provider else {},
                    "transcript_source": "server_streaming",
                }))
            )
            if matches and prepared.followup.get("selected"):
                policy = {**ConversationUnderstandingService.DEFAULT_POLICY, **(session.get("followup_policy") or {})}
                matches = self.conversation._within_time_budget(
                    session, int(policy["min_remaining_seconds"]), now=self.clock(),
                )
        except (ValueError, TypeError, KeyError, AttributeError):
            matches = False
        if not matches:
            raise ApiError("TURN_DECISION_STALE", "Prepared evidence or reasoning context has changed.", status_code=409)

    def assert_prepared_streaming_decision(
        self, prepared: PreparedTurnDecision, final: Any, organization_id: str,
    ) -> None:
        """Side-effect-free preflight before the capture is detached or sealed."""
        self._require_streaming_final(final)
        session = self.get_interview(prepared.interview_id, organization_id)
        turn = self.lifecycle.require_active_turn(session, prepared.turn_id, allowed_statuses=("asking",))
        self._assert_prepared_decision(prepared, session, turn, {
            "final_transcript": final.text,
            "stt_confidence": final.confidence,
            "language": final.language,
            "transcript_segments": [item.model_dump(mode="json") for item in final.segments],
            "stt_provider": final.provider.model_dump(mode="json") if final.provider else {},
            "transcript_source": "server_streaming",
        })

    async def submit_streaming_answer(
        self,
        interview_id: str,
        payload: Dict[str, Any],
        organization_id: str = "org_default",
        *,
        evidence_fence: Optional[EvidenceCommitFence] = None,
        prepared_decision: Optional[PreparedTurnDecision] = None,
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
        active_turn = self.lifecycle.require_active_turn(
            session, turn_id, allowed_statuses=("asking", "transcribing")
        )
        if prepared_decision is not None:
            # Reject before TRANSCRIPTION_STARTED as well as inside the final
            # effect transaction, so mismatches have no lifecycle side effect.
            self._assert_prepared_decision(prepared_decision, session, active_turn, {
                "final_transcript": payload["final_transcript"],
                "stt_confidence": payload.get("confidence"),
                "language": payload.get("language", "zh-CN"),
                "transcript_segments": payload.get("segments", []),
                "stt_provider": provider,
                "transcript_source": "server_streaming",
            })
        if active_turn.get("status") == "asking":
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
                evidence_fence=evidence_fence,
                media_evidence=payload.get("media_evidence"),
            )
        return await self._accept_authoritative_transcript(
            interview_id,
            {
                "turn_id": turn_id,
                "final_transcript": payload["final_transcript"],
                "raw_transcript": payload["final_transcript"],
                "stt_confidence": payload.get("confidence"),
                "language": payload.get("language", "zh-CN"),
                "duration_seconds": payload.get("duration_seconds", 0),
                "recording_started_at": payload.get("recording_started_at"),
                "recording_finished_at": payload.get("recording_finished_at"),
                "audio_uri": payload["audio_uri"],
                "transcript_source": "server_streaming",
                "stt_provider": deepcopy(provider),
                "transcript_segments": deepcopy(payload.get("segments", [])),
                "media_evidence": deepcopy(payload.get("media_evidence")),
            },
            organization_id,
            evidence_fence=evidence_fence,
            prepared_decision=prepared_decision,
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

    def verify_transcription(self, interview_id, answer_id, payload, organization_id="org_default", *, actor_id):
        """Version-bound human evidence review and regrade request commit together."""
        if payload.get("audio_reviewed") is not True or not str(payload.get("reason") or "").strip() or not actor_id:
            raise ApiError("TRANSCRIPTION_REVIEW_REQUIRED", "请先回听录音并填写核验说明。", status_code=422)
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            if session["status"] in {"cancelled", "retention_purged", "report_generating"}:
                raise ApiError("TRANSCRIPTION_REVIEW_UNAVAILABLE", "当前面试状态不允许核验转写。", status_code=409)
            answer = self._answer_by_id(session, answer_id)
            root = answer.get("root_turn_id") or answer["turn_id"]
            if any(item.get("evaluation_status") == "pending" for item in session.get("answers", [])
                   if (item.get("root_turn_id") or item["turn_id"]) == root):
                raise ApiError("TRANSCRIPTION_REVIEW_BUSY", "本题正在评分，请等待后刷新。", status_code=409)
            revision = int(answer.get("current_transcript_revision", 1))
            if (answer.get("current_evaluation_id") != payload.get("expected_evaluation_id")
                or revision != payload.get("expected_transcript_revision")):
                raise ApiError("TRANSCRIPTION_REVIEW_STALE", "评分或转写已变化，请刷新后重新核验。", status_code=409)
            if not answer.get("audio_uri"):
                raise ApiError("ANSWER_AUDIO_NOT_FOUND", "本题缺少可回听的录音。", status_code=409)
            text = payload.get("final_transcript")
            if text is not None and (not str(text).strip() or len(text) > 12_000):
                raise ApiError("TRANSCRIPT_TEXT_REQUIRED", "转写必须为非空且不超过12000字的文本。", status_code=422)
            now = utc_now()
            if text is not None and text.strip() != answer["final_transcript"]:
                revision += 1
                answer["final_transcript"] = text.strip()
                answer["current_transcript_revision"] = revision
                answer["transcript_source"] = "human_correction"
                answer.setdefault("transcript_revisions", []).append({"revision": revision, "text": text.strip(),
                    "source": "human_correction", "reason": payload["reason"], "reviewer_id": actor_id, "created_at": now})
            verification = {"reviewer_id": actor_id, "audio_reviewed": True, "verified_at": now,
                "reason": payload["reason"], "transcript_identity": transcript_identity(answer),
                "evaluation_id": payload["expected_evaluation_id"]}
            answer["transcription_verification"] = verification
            answer.setdefault("transcription_verification_history", []).append(deepcopy(verification))
            answer["updated_at"] = now
            session.pop("review_completion", None)
            _, work = self._decide_and_persist(transaction, session, LifecycleCommand(
                LifecycleCommandType.REGRADE_REQUESTED, {"answer_id": answer_id, "trigger_reason": "transcription_verified"}), organization_id)
            transaction.audit_events.add({"id": new_id("audit"), "organization_id": organization_id,
                "actor_id": actor_id, "action": "answer.transcription.verified", "resource_type": "candidate_answer",
                "resource_id": answer_id, "metadata": {"interview_id": interview_id, "transcript_revision": revision,
                    "previous_evaluation_id": payload["expected_evaluation_id"]}, "created_at": now})
        return {"answer_id": answer_id, "transcript_revision": revision, "status": "queued", "work_count": len(work)}

    def retry_processing(
        self, interview_id: str, organization_id: str = "org_default", *, actor_id: str = "reviewer_local"
    ) -> Dict[str, Any]:
        """Requeue only current failed work; duplicate clicks share durable identities."""
        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            if session.get("status") in {"cancelled", "retention_purged"}:
                raise ApiError("INTERVIEW_PROCESSING_UNAVAILABLE", "该面试已取消或已清除。", status_code=409)
            queued = []
            for answer in session.get("answers", []):
                if answer.get("evaluation_status") not in {"pending", "failed"}:
                    continue
                revision = max((item["revision"] for item in session.get("evaluation_revisions", [])
                                if item["answer_id"] == answer["id"]), default=0) + 1
                key = "answer.evaluate:%s:%s" % (answer["id"], revision)
                work = transaction.outbox.enqueue(new_work_item(
                    organization_id=organization_id, kind="answer.evaluate", aggregate_id=interview_id,
                    idempotency_key=key, payload={"interview_id": interview_id, "answer_id": answer["id"],
                        "revision": revision, "trigger_reason": "processing_recovery",
                        "evidence_answer_ids": answer.get("pending_evidence_answer_ids")
                            or answer.get("current_evidence_answer_ids") or [answer["id"]]},
                ))
                if work["status"] in {"failed", "dead_letter"}:
                    work = transaction.outbox.replay(work["id"], reason="enterprise_processing_retry", actor_id=actor_id)
                if work["status"] in {"pending", "running"}:
                    queued.append(work["id"])
            for work in transaction.outbox.list():
                if (work["kind"] == "interview.report.generate"
                    and work.get("payload", {}).get("interview_id") == interview_id
                    and int(work.get("payload", {}).get("revision", 0)) == len(session.get("report_revisions", [])) + 1
                    and work["status"] in {"failed", "dead_letter"}):
                    transaction.outbox.replay(work["id"], reason="enterprise_processing_retry", actor_id=actor_id)
                    queued.append(work["id"])
            if not self.lifecycle._has_unresolved_evaluations(session) and (
                session.get("candidate_input_completed_at") or session.get("completed_at")
            ) and not session.get("current_report_id"):
                if session["status"] in {"in_progress", "completed"}:
                    _, report_works = self._decide_and_persist(transaction, session,
                        LifecycleCommand(LifecycleCommandType.REPORT_REQUESTED, {"trigger_reason": "processing_recovery"}), organization_id)
                    queued.extend(item["id"] for item in report_works)
                elif session["status"] in {"report_generating", "report_failed"}:
                    revision = len(session.get("report_revisions", [])) + 1
                    work = transaction.outbox.enqueue(new_work_item(
                        organization_id=organization_id, kind="interview.report.generate", aggregate_id=interview_id,
                        idempotency_key="interview.report:%s:%s" % (interview_id, revision),
                        payload={"interview_id": interview_id, "revision": revision, "trigger_reason": "processing_recovery"}))
                    if work["id"] not in queued:
                        queued.append(work["id"])
            transaction.audit_events.add({
                "id": new_id("audit"), "organization_id": organization_id, "actor_id": actor_id,
                "action": "interview.processing.retry", "resource_type": "interview", "resource_id": interview_id,
                "metadata": {"work_count": len(queued)}, "created_at": utc_now(),
            })
        return {"interview_id": interview_id, "status": "queued", "work_count": len(queued)}

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
            running_work = transaction.outbox.start(work_item_id, lease_seconds=300)
            payload = running_work["payload"]
            interview_id = payload["interview_id"]
            answer_id = payload["answer_id"]
            session = self._required(transaction.interview_sessions.get(interview_id))
            answer = deepcopy(self._answer_by_id(session, answer_id))
            next_revision = max((item["revision"] for item in session.get("evaluation_revisions", [])
                                 if item["answer_id"] == answer_id), default=0) + 1
            if int(payload["revision"]) < next_revision:
                transaction.outbox.complete(work_item_id, lease_token=running_work["lease_token"])
                return {"status": "superseded", "answer_id": answer_id}
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
            evidence_answer_ids = list(
                payload.get("evidence_answer_ids")
                or answer.get("current_evidence_answer_ids")
                or [answer_id]
            )
            from app.domain.evaluation_evidence import evidence_fingerprint
            input_fingerprint = evidence_fingerprint([
                self._answer_by_id(session, item_id) for item_id in evidence_answer_ids
            ])
            if len(evidence_answer_ids) > 1:
                answer = self._merged_authoritative_answer(
                    session,
                    root_answer_id=answer_id,
                    evidence_answer_ids=evidence_answer_ids,
                )
            question_snapshot = deepcopy(
                self._turn_by_id(session, answer["turn_id"])["question_snapshot"]
            )

        try:
            evaluation = await self.evaluation.evaluate_answer(
                answer,
                question_snapshot,
                session.get("plan_snapshot", {}).get("role_requirement"),
            )
            evaluation["evidence_answer_ids"] = list(evidence_answer_ids)
            evaluation["evidence_fingerprint"] = input_fingerprint
            evaluation["evidence_utterance_ids"] = [
                item
                for item in answer.get("evidence_utterance_ids", [])
                if item
            ] or ([answer["utterance_id"]] if answer.get("utterance_id") else [])
            evaluation["evidence_root_turn_id"] = answer.get("root_turn_id") or answer["turn_id"]
            evaluation["evidence_mode"] = (
                "root_with_followups" if len(evidence_answer_ids) > 1 else "single_answer"
            )
        except Exception as exc:
            failure_message = (
                "评分模型供应商账户计费或额度需要处理，请管理员处理后重试评分。"
                if isinstance(exc, ProviderError) and exc.code == "provider_billing_required"
                else "评分服务未完成，请检查模型配置后重试。"
            )
            with self.persistence.transaction(organization_id) as transaction:
                failed = transaction.outbox.fail(
                    work_item_id,
                    failure_message,
                    lease_token=running_work["lease_token"],
                    error_code=exc.code if isinstance(exc, ProviderError) else "evaluation_failed",
                    retryable=exc.retryable if isinstance(exc, ProviderError) else True,
                    retry_after_seconds=35 if isinstance(exc, ProviderError) and exc.retryable else None,
                )
                if failed.get("status") == "dead_letter":
                    session = self._required(transaction.interview_sessions.get(interview_id))
                    self._decide_and_persist(
                        transaction,
                        session,
                        LifecycleCommand(
                            LifecycleCommandType.EVALUATION_FAILED,
                            {"answer_id": answer_id, "error": failure_message,
                             "error_code": exc.code if isinstance(exc, ProviderError) else "evaluation_failed"},
                        ),
                        organization_id,
                    )
            raise

        with self.persistence.transaction(organization_id) as transaction:
            session = self._required(transaction.interview_sessions.get(interview_id))
            if evidence_fingerprint([self._answer_by_id(session, item_id)
                                     for item_id in evidence_answer_ids]) != input_fingerprint:
                failed = transaction.outbox.fail(work_item_id, "回答版本已变化，正在重新整理评分证据。",
                    lease_token=running_work["lease_token"], error_code="evaluation_evidence_changed",
                    retryable=True, retry_after_seconds=1)
                if failed.get("status") == "dead_letter":
                    self._decide_and_persist(transaction, session, LifecycleCommand(
                        LifecycleCommandType.EVALUATION_FAILED,
                        {"answer_id": answer_id, "error": "回答版本发生变化，请重试评分。",
                         "error_code": "evaluation_evidence_changed"}), organization_id)
                return {"id": evaluation["id"], "status": "superseded", "evidence_answer_ids": evidence_answer_ids}
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
                        "evidence_answer_ids": list(evidence_answer_ids),
                    },
                ),
                organization_id,
            )
            transaction.outbox.complete(work_item_id, lease_token=running_work["lease_token"])
            persisted_evaluation = next(
                item for item in decision.session["evaluation_revisions"] if item["id"] == evaluation["id"]
            )

        await self._process_evidence_group_effects(work_items, organization_id)
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
        *,
        evidence_fence: Optional[EvidenceCommitFence] = None,
        media_evidence: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        with self.persistence.transaction(organization_id) as transaction:
            if evidence_fence is not None:
                assert_current_evidence_fence(transaction, evidence_fence)
            session = self._required(transaction.interview_sessions.get(interview_id))
            if command.type in {LifecycleCommandType.START, LifecycleCommandType.RESUME, LifecycleCommandType.RECOVER}:
                self.assert_skill_authorized(transaction, session)
            if evidence_fence is not None and media_evidence:
                DurableEvidenceMedia.assert_complete_capture(
                    transaction,
                    interview_id=interview_id,
                    turn_id=command.payload["turn_id"],
                    media_evidence=media_evidence,
                )
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
        session = deepcopy(session)
        self._refresh_deferred_speech(transaction, session)
        decision = self.lifecycle.execute(session, command)
        work_items = self._enqueue_effects(transaction, decision.effects, session["id"], organization_id)
        if decision.changed:
            decision.session = transaction.interview_sessions.update(
                decision.session,
                expected_version=session["version"],
            )
        return decision, work_items

    def _refresh_deferred_speech(self, transaction: Any, session: Dict[str, Any]) -> None:
        """Bind ready assets inside the same transaction that advances a turn."""
        pending = [turn for turn in session.get("turns", [])
                   if turn.get("deferred_speech") and turn.get("status") == "pending"]
        if not pending:
            return
        appointment = transaction.interview_appointments.get(session.get("appointment_id")) or {}
        profile = (session.get("plan_snapshot") or {}).get("speech_profile_snapshot") or {}
        items = {(item.get("question_id"), item.get("source_version")): item
                 for item in (appointment.get("speech_preparation") or {}).get("items", [])}
        production = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production"
        for turn in pending:
            frozen = turn["question_snapshot"]
            source = {"id": frozen["source_question_id"], "version": frozen["source_question_version"]}
            item = items.get((source["id"], source["version"]))
            ready = (appointment.get("status") in {"registered", "consumed"}
                     and appointment.get("plan_id") == session.get("plan_id")
                     and self.admission._prepared_experience_asset_ready(transaction, item, source, profile))
            if ready and production:
                ready = self.admission._speech_asset_is_production_ready(transaction, item.get("asset_id"))
            turn["speech_preparation"] = {
                "status": "ready" if ready else "unavailable",
                "asset_id": item["asset_id"] if ready else None,
            }

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
                if payload.get("evidence_answer_ids"):
                    work_payload["evidence_answer_ids"] = list(
                        payload["evidence_answer_ids"]
                    )
                if payload.get("evidence_fingerprint"):
                    work_payload["evidence_fingerprint"] = payload["evidence_fingerprint"]
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

    async def _process_evidence_group_effects(
        self,
        work_items: List[Dict[str, Any]],
        organization_id: str,
    ) -> None:
        """Finish a newly settled evidence group in the same worker turn when possible.

        The dependent work is durable before this optimization runs. If another
        worker owns it or its provider fails, the normal outbox retry path remains
        authoritative and the already-completed child evaluation is not rolled back.
        """

        for item in work_items:
            if (
                item.get("kind") != "answer.evaluate"
                or item.get("payload", {}).get("trigger_reason")
                != "followup_evidence_merged"
            ):
                continue
            try:
                await self._process_evaluation_work(item["id"], organization_id)
            except Exception:
                continue

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
        primary_count = len(plan.get("bank_slots", [])) + len(
            plan.get("experience_question_snapshots", [])
        )
        policy = {
            "max_depth": 2,
            "max_total": min(4, max(0, primary_count)),
            "max_per_root": 2,
            "min_answer_chars": max(1, int(source.get("followup_min_answer_chars", 4))),
            "max_answer_chars": max(1, int(source.get("followup_max_answer_chars", 4000))),
            "min_remaining_seconds": max(90, int(source.get("followup_min_remaining_seconds", 90))),
            "max_probe_chars": min(300, max(40, int(source.get("followup_max_probe_chars", 180)))),
            "low_confidence_threshold": 0.65,
        }
        if plan.get("execution_schema_version") == 3:
            budget = plan["assessment_contract"]["budget"]
            policy.update(max_total=budget["max_total_followups"], max_per_root=budget["max_followups_per_root"],
                          max_depth=budget["max_followups_per_root"])
        return policy

    def _merged_authoritative_answer(
        self,
        session: Dict[str, Any],
        *,
        root_answer_id: str,
        evidence_answer_ids: List[str],
    ) -> Dict[str, Any]:
        """Build an ephemeral score input from a frozen root evidence group.

        Only persisted server-authoritative answers belonging to the same root
        chain can enter the aggregate. The synthetic transcript is never accepted
        as a new CandidateAnswer; it exists solely at the evaluation seam.
        """

        root_answer = deepcopy(self._answer_by_id(session, root_answer_id))
        root_turn = self._turn_by_id(session, root_answer["turn_id"])
        if root_turn.get("is_followup"):
            raise ApiError(
                "EVIDENCE_GROUP_ROOT_INVALID",
                "A merged evidence evaluation must target a primary answer.",
                status_code=409,
            )
        requested = list(dict.fromkeys(str(item) for item in evidence_answer_ids))
        if not requested or requested[0] != root_answer_id:
            raise ApiError(
                "EVIDENCE_GROUP_INVALID",
                "Merged evidence must start with the root answer.",
                status_code=409,
            )
        answers_by_id = {item["id"]: item for item in session.get("answers", [])}
        try:
            evidence = [deepcopy(answers_by_id[item_id]) for item_id in requested]
        except KeyError as exc:
            raise ApiError(
                "EVIDENCE_GROUP_INVALID",
                "Merged evidence refers to an answer that does not exist.",
                status_code=409,
            ) from exc
        root_turn_id = root_turn["id"]
        turn_order = {
            item["id"]: int(item.get("order", 0)) for item in session.get("turns", [])
        }
        if any(
            (item.get("root_turn_id") or item.get("turn_id")) != root_turn_id
            or not str(item.get("final_transcript") or "").strip()
            or not item.get("audio_uri")
            for item in evidence
        ):
            raise ApiError(
                "EVIDENCE_GROUP_INVALID",
                "Merged scoring accepts only persisted authoritative answers from one root chain.",
                status_code=409,
            )
        ordered = sorted(evidence, key=lambda item: turn_order.get(item["turn_id"], 0))
        if [item["id"] for item in ordered] != requested:
            raise ApiError(
                "EVIDENCE_GROUP_ORDER_INVALID",
                "Merged evidence is not in frozen interview turn order.",
                status_code=409,
            )
        sections = []
        for index, item in enumerate(ordered):
            label = "主回答" if index == 0 else "追问%d回答" % index
            sections.append("【%s】\n%s" % (label, str(item["final_transcript"]).strip()))
        merged = deepcopy(root_answer)
        merged["final_transcript"] = "\n\n".join(sections)
        merged["raw_transcript"] = merged["final_transcript"]
        merged["stt_confidence"] = minimum_reported_confidence(*(item.get("stt_confidence") for item in ordered))
        merged["transcript_source"] = "server_authoritative_evidence_group"
        merged["evidence_answer_ids"] = requested
        merged["evidence_utterance_ids"] = [
            item.get("utterance_id") for item in ordered if item.get("utterance_id")
        ]
        merged["root_turn_id"] = root_turn_id
        merged["evidence_chain"] = []
        for item in ordered:
            turn = self._turn_by_id(session, item["turn_id"])
            snapshot = turn["question_snapshot"]
            merged["evidence_chain"].append({
                "answer_id": item["id"], "turn_id": turn["id"],
                "parent_turn_id": turn.get("parent_turn_id"), "root_turn_id": root_turn_id,
                "question_text": turn.get("question_spoken_text") or snapshot.get("question_text", ""),
                "answer_text": item["final_transcript"],
                "target_point_ids": list(turn.get("assessed_rubric_point_ids") or snapshot.get("assessed_rubric_point_ids")
                    or [p["id"] if isinstance(p, dict) else str(p) for p in snapshot.get("key_points", [])]),
                "transcript_revision": int(item.get("current_transcript_revision", 1)),
            })
        return merged

    def _build_followup_turn(
        self,
        session: Dict[str, Any],
        parent_turn: Dict[str, Any],
        answer: Dict[str, Any],
        decision: Dict[str, Any],
        *,
        now: str,
    ) -> Dict[str, Any]:
        root_turn_id = str(decision.get("root_turn_id") or parent_turn.get("root_turn_id") or parent_turn["id"])
        root_turn = self._turn_by_id(session, root_turn_id)
        followup_depth = int(decision.get("followup_depth", int(parent_turn.get("followup_depth", 0)) + 1))
        stable_key = "%s:%s:%s" % (session["id"], parent_turn["id"], answer["id"])
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
            "parent_question_snapshot_id": parent_turn["question_snapshot_id"],
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
            "phase": parent_turn.get("phase", "position_bank"),
            "is_followup": True,
            "parent_turn_id": parent_turn["id"],
            "root_turn_id": root_turn_id,
            "followup_depth": followup_depth,
            "followup_reason": decision["reason"],
            "target_key_points": deepcopy(decision.get("target_key_points", [])),
            "probe_source": decision.get("probe_source", "deterministic_template"),
            "allow_followup": followup_depth < 2,
            "weight": 0.0,
            "expected_minutes": 1,
            "status": "pending",
            "question_spoken_text": decision["question_text"],
            "started_at": None,
            "completed_at": None,
            "utterances": [],
            "current_understanding": None,
            "conversation_acts": [deepcopy(decision["conversation_act"])]
            if decision.get("conversation_act")
            else [],
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
