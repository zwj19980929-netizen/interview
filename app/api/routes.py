import json
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from app.repositories.provider import get_store
from app.schemas.api import (
    AudioAnswerSubmit,
    AvatarSpeakCommand,
    CandidateIntakeCreate,
    CandidateProfilePatch,
    CandidateReadinessCreate,
    CandidateProfileCreate,
    ExperienceQuestionPatch,
    InterviewAppointmentCreate,
    InterviewAppointmentPatch,
    InterviewControlCommand,
    InterviewInvitationCreate,
    InterviewPlanGenerateRequest,
    InterviewPlanPatch,
    JobPositionCreate,
    KnowledgeBaseCreate,
    KnowledgeBaseImport,
    KnowledgeBaseRebuild,
    ModelRouteCreate,
    OutboxReplay,
    ProviderConfigCreate,
    ProviderConfigPatch,
    ProviderConfigTest,
    QuestionCreate,
    QuestionPatch,
    QuestionSearchRequest,
    RetentionRun,
    ResumeUrlImport,
    ResumeReviewCreate,
    ReviewComplete,
    RoleRequirementCreate,
    TranscriptCorrection,
    VersionedPatch,
)
from app.core.errors import ApiError
from app.core.auth import authenticate_interviewer_websocket
from app.services.avatar import AvatarService
from app.services.appointments import AppointmentService
from app.services.catalog import CatalogService
from app.services.interviews import InterviewService
from app.services.model_admin import ModelAdminService
from app.services.operations import OperationsService
from app.services.plan_assembly import (
    InterviewPlanAssembly,
    PlanAssemblyPolicy,
    PlanAssemblyRequest,
)
from app.services.plans import InterviewPlanService
from app.services.reports import ReportService
from app.services.review import EnterpriseReviewService
from app.services.roles import RoleRequirementService
from app.services.realtime import RealtimeInterviewSession
from app.services.resume_ingestion import ResumeIngestionService
from app.services.streaming_stt import StreamingInterviewSTT
from app.services.fairness import FairnessEvaluationService
from app.services.session_monitor import SessionHeartbeatMonitor
from app.services.retention import RetentionService
from app.services.talent import TalentService
from app.realtime_bus import realtime_event_bus


router = APIRouter()


class LiveConnectionManager:
    def __init__(self) -> None:
        self.connections: Dict[str, Set[WebSocket]] = {}
        self.bus = realtime_event_bus()

    def connect(self, interview_id: str, websocket: WebSocket) -> None:
        self.connections.setdefault(interview_id, set()).add(websocket)

    def disconnect(self, interview_id: str, websocket: WebSocket) -> None:
        interview_connections = self.connections.get(interview_id)
        if not interview_connections:
            return
        interview_connections.discard(websocket)
        if not interview_connections:
            self.connections.pop(interview_id, None)

    async def broadcast(self, interview_id: str, events: List[Dict[str, Any]]) -> None:
        await self._broadcast_local(interview_id, events)
        for event in events:
            await self.bus.publish(interview_id, event)

    async def _broadcast_local(self, interview_id: str, events: List[Dict[str, Any]]) -> None:
        disconnected: List[WebSocket] = []
        for websocket in list(self.connections.get(interview_id, set())):
            try:
                for event in events:
                    await websocket.send_json(event)
            except (RuntimeError, WebSocketDisconnect):
                disconnected.append(websocket)
        for websocket in disconnected:
            self.disconnect(interview_id, websocket)

    async def consume_remote(self) -> None:
        await self.bus.subscribe(
            lambda interview_id, event: self._broadcast_local(interview_id, [event])
        )


live_connections = LiveConnectionManager()


class ServiceLocator:
    """Constructs only the deep module requested by a route."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def __getitem__(self, name: str) -> Any:
        factories = {
            "model_admin": ModelAdminService,
            "catalog": CatalogService,
            "talent": TalentService,
            "roles": RoleRequirementService,
            "plan_assembly": InterviewPlanAssembly,
            "plans": InterviewPlanService,
            "interviews": InterviewService,
            "avatar": AvatarService,
            "reports": ReportService,
            "appointments": AppointmentService,
            "review": EnterpriseReviewService,
            "resume_ingestion": ResumeIngestionService,
            "operations": OperationsService,
            "fairness": FairnessEvaluationService,
            "session_monitor": SessionHeartbeatMonitor,
            "retention": RetentionService,
        }
        factory = factories.get(name)
        if factory is None:
            raise KeyError(name)
        return factory(self.store)


def services() -> ServiceLocator:
    return ServiceLocator(get_store())


async def broadcast_interview_state(interview_id: str) -> None:
    projection = RealtimeInterviewSession(get_store(), interview_id)
    await live_connections.broadcast(interview_id, projection.state_change_events())


@router.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@router.get("/api/v1/admin/model-providers/catalog")
async def model_provider_catalog() -> Dict[str, Any]:
    return {"items": services()["model_admin"].catalog()}


@router.post("/api/v1/admin/model-provider-configs")
async def create_model_provider_config(payload: ProviderConfigCreate) -> Dict[str, Any]:
    return services()["model_admin"].create_provider_config(payload.model_dump())


@router.get("/api/v1/admin/model-provider-configs")
async def list_model_provider_configs() -> Dict[str, Any]:
    return {"items": services()["model_admin"].list_provider_configs(), "next_cursor": None}


@router.patch("/api/v1/admin/model-provider-configs/{config_id}")
async def patch_model_provider_config(config_id: str, payload: ProviderConfigPatch) -> Dict[str, Any]:
    return services()["model_admin"].patch_provider_config(config_id, payload.model_dump(exclude_unset=True))


@router.post("/api/v1/admin/model-provider-configs/{config_id}/test")
async def test_model_provider_config(
    config_id: str,
    payload: Optional[ProviderConfigTest] = None,
) -> Dict[str, Any]:
    return await services()["model_admin"].test_provider_config(
        config_id,
        payload.model_dump(exclude_none=True) if payload else {},
    )


@router.post("/api/v1/admin/model-routes")
async def create_model_route(payload: ModelRouteCreate) -> Dict[str, Any]:
    return services()["model_admin"].create_route(payload.model_dump())


@router.get("/api/v1/admin/model-routes")
async def list_model_routes() -> Dict[str, Any]:
    return {"items": services()["model_admin"].list_routes(), "next_cursor": None}


@router.post("/api/v1/admin/model-routes/{route_id}/test")
async def test_model_route(route_id: str) -> Dict[str, Any]:
    return await services()["model_admin"].test_route(route_id)


@router.get("/api/v1/admin/work-items")
async def list_work_items(status: Optional[str] = None) -> Dict[str, Any]:
    return services()["operations"].work_items(status)


@router.post("/api/v1/admin/work-items/{work_item_id}/replay")
async def replay_work_item(
    work_item_id: str,
    payload: OutboxReplay,
    x_actor_id: str = Header(default="admin_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["operations"].replay(
        work_item_id, reason=payload.reason, actor_id=x_actor_id
    )


@router.get("/api/v1/admin/audit-events")
async def list_audit_events() -> Dict[str, Any]:
    return {"items": services()["operations"].audit_events(), "next_cursor": None}


@router.get("/api/v1/admin/evaluations/question-selection-fairness")
async def evaluate_question_selection_fairness(
    job_position_id: str,
    x_actor_id: str = Header(default="admin_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["fairness"].selection_distribution(
        job_position_id, actor_id=x_actor_id
    )


@router.post("/api/v1/admin/session-monitor/run")
async def run_session_heartbeat_monitor() -> Dict[str, Any]:
    items = services()["session_monitor"].run_once()
    return {"items": items, "timed_out_count": len(items)}


@router.post("/api/v1/admin/retention/run")
async def run_retention(
    payload: RetentionRun,
    x_actor_id: str = Header(default="admin_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["retention"].run(
        dry_run=payload.dry_run,
        actor_id=x_actor_id,
        now=payload.now,
    )


@router.post("/api/v1/job-positions")
async def create_job_position(payload: JobPositionCreate) -> Dict[str, Any]:
    return services()["catalog"].create_position(payload.model_dump())


@router.get("/api/v1/job-positions")
async def list_job_positions() -> Dict[str, Any]:
    return {"items": services()["catalog"].list_positions(), "next_cursor": None}


@router.get("/api/v1/job-positions/{position_id}")
async def get_job_position(position_id: str) -> Dict[str, Any]:
    return services()["catalog"].get_position(position_id)


@router.patch("/api/v1/job-positions/{position_id}")
async def patch_job_position(position_id: str, payload: VersionedPatch) -> Dict[str, Any]:
    return services()["catalog"].patch_position(position_id, payload.model_dump(exclude_unset=True))


@router.post("/api/v1/job-positions/{position_id}/knowledge-bases")
async def create_position_knowledge_base(position_id: str, payload: KnowledgeBaseCreate) -> Dict[str, Any]:
    return services()["catalog"].create_knowledge_base(position_id, payload.model_dump())


@router.get("/api/v1/job-positions/{position_id}/knowledge-bases")
async def list_position_knowledge_bases(position_id: str) -> Dict[str, Any]:
    return {"items": services()["catalog"].list_knowledge_bases(position_id), "next_cursor": None}


@router.get("/api/v1/knowledge-bases/{knowledge_base_id}")
async def get_knowledge_base(knowledge_base_id: str) -> Dict[str, Any]:
    return services()["catalog"].get_knowledge_base(knowledge_base_id)


@router.patch("/api/v1/knowledge-bases/{knowledge_base_id}")
async def patch_knowledge_base(knowledge_base_id: str, payload: VersionedPatch) -> Dict[str, Any]:
    return services()["catalog"].patch_knowledge_base(
        knowledge_base_id, payload.model_dump(exclude_unset=True)
    )


@router.post("/api/v1/knowledge-bases/{knowledge_base_id}/imports")
async def import_knowledge_base(
    knowledge_base_id: str,
    payload: KnowledgeBaseImport,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    build = services()["catalog"].queue_import(
        knowledge_base_id,
        [item.model_dump() for item in payload.questions],
        idempotency_key=idempotency_key or "",
    )
    return JSONResponse(
        {
            "job_id": build["id"],
            "knowledge_base_id": knowledge_base_id,
            "status": build["status"],
            "tasks": ["parse", "validate_candidate_pool", "question_speech"],
        },
        status_code=202,
    )


@router.post("/api/v1/knowledge-bases/{knowledge_base_id}/rebuild")
async def rebuild_knowledge_base(
    knowledge_base_id: str,
    payload: KnowledgeBaseRebuild,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    build = services()["catalog"].queue_rebuild(
        knowledge_base_id,
        reason=payload.reason,
        idempotency_key=idempotency_key or "",
    )
    return JSONResponse(build, status_code=202)


@router.get("/api/v1/knowledge-bases/{knowledge_base_id}/builds/{job_id}")
async def get_knowledge_base_build(knowledge_base_id: str, job_id: str) -> Dict[str, Any]:
    build = services()["catalog"].get_build(job_id)
    if build["knowledge_base_id"] != knowledge_base_id:
        raise ApiError("KNOWLEDGE_BASE_BUILD_NOT_FOUND", "Knowledge base build does not exist.", status_code=404)
    return build


@router.post("/api/v1/knowledge-bases/{knowledge_base_id}/questions")
async def create_knowledge_base_question(knowledge_base_id: str, payload: QuestionCreate) -> Dict[str, Any]:
    return await services()["catalog"].create_question(knowledge_base_id, payload.model_dump())


@router.get("/api/v1/knowledge-bases/{knowledge_base_id}/questions")
async def list_knowledge_base_questions(knowledge_base_id: str) -> Dict[str, Any]:
    return {"items": services()["catalog"].list_questions(knowledge_base_id), "next_cursor": None}


@router.post("/api/v1/candidate-profiles")
async def create_candidate_profile(payload: CandidateProfileCreate) -> Dict[str, Any]:
    return services()["talent"].create_candidate(payload.model_dump())


@router.get("/api/v1/candidate-profiles")
async def list_candidate_profiles() -> Dict[str, Any]:
    return {"items": services()["talent"].list_candidates(), "next_cursor": None}


@router.get("/api/v1/candidate-profiles/{candidate_id}")
async def get_candidate_profile(candidate_id: str) -> Dict[str, Any]:
    return services()["talent"].get_candidate(candidate_id)


@router.patch("/api/v1/candidate-profiles/{candidate_id}")
async def patch_candidate_profile(candidate_id: str, payload: CandidateProfilePatch) -> Dict[str, Any]:
    return services()["talent"].patch_candidate(candidate_id, payload.model_dump(exclude_unset=True))


@router.post("/api/v1/candidate-profiles/{candidate_id}/resumes")
async def create_resume_document(
    candidate_id: str,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    content_type = request.headers.get("content-type", "")
    if content_type.lower().startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", None):
            raise ApiError("RESUME_FILE_REQUIRED", "A PDF file field is required.")
        limit = services()["resume_ingestion"].max_bytes
        chunks: List[bytes] = []
        byte_count = 0
        while True:
            chunk = await upload.read(min(1024 * 1024, limit + 1 - byte_count))
            if not chunk:
                break
            chunks.append(chunk)
            byte_count += len(chunk)
            if byte_count > limit:
                raise ApiError("PDF_FILE_TOO_LARGE", "PDF exceeds the configured size limit.", status_code=413)
        display_name = str(form.get("display_name") or upload.filename)
        result = services()["resume_ingestion"].queue_upload(
            candidate_id,
            file_name=display_name,
            content_type=getattr(upload, "content_type", None) or "application/octet-stream",
            content=b"".join(chunks),
            idempotency_key=idempotency_key or "",
        )
        return JSONResponse(
            {
                "resume_document_id": result["resume_document"]["id"],
                "ingestion_job_id": result["job"]["id"],
                "source_type": "local_upload",
                "ingestion_status": result["job"]["status"],
            },
            status_code=202,
        )
    raise ApiError(
        "RESUME_MULTIPART_REQUIRED",
        "Resume ingestion accepts multipart PDF uploads only; use the URL import endpoint for remote PDFs.",
        status_code=415,
    )


@router.post("/api/v1/candidate-profiles/{candidate_id}/resumes/import-url")
async def import_resume_url(
    candidate_id: str,
    payload: ResumeUrlImport,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    result = services()["resume_ingestion"].queue_url(
        candidate_id,
        source_url=payload.url,
        file_name=payload.display_name,
        idempotency_key=idempotency_key or "",
    )
    return JSONResponse(
        {
            "resume_document_id": result["resume_document"]["id"],
            "ingestion_job_id": result["job"]["id"],
            "source_type": "url_import",
            "ingestion_status": result["job"]["status"],
        },
        status_code=202,
    )


@router.get("/api/v1/candidate-profiles/{candidate_id}/resumes")
async def list_resume_documents(candidate_id: str) -> Dict[str, Any]:
    return {"items": services()["talent"].list_resumes(candidate_id), "next_cursor": None}


@router.get("/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}")
async def get_resume_document(candidate_id: str, resume_id: str) -> Dict[str, Any]:
    return services()["talent"].get_resume(candidate_id, resume_id)


@router.post("/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}/content-url")
async def get_resume_content_url(
    candidate_id: str,
    resume_id: str,
    x_actor_id: str = Header(default="reviewer_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["resume_ingestion"].issue_content_access(candidate_id, resume_id, actor_id=x_actor_id)


@router.get("/api/v1/file-ingestion-jobs/{work_item_id}")
async def get_file_ingestion_job(work_item_id: str) -> Dict[str, Any]:
    return services()["resume_ingestion"].get_job(work_item_id)


@router.get("/api/v1/private-files/{token}", include_in_schema=False)
async def get_private_file(token: str) -> Response:
    opened = services()["resume_ingestion"].open_local_grant(token)
    return Response(
        content=opened["content"],
        media_type=opened["content_type"],
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/api/v1/question-speech-assets/{asset_id}/content-url")
async def get_question_speech_content_url(
    asset_id: str,
    x_actor_id: str = Header(default="interviewer_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["catalog"].issue_speech_access(asset_id, actor_id=x_actor_id)


@router.post("/api/v1/candidate-profiles/{candidate_id}/resume-reviews")
async def create_resume_review(candidate_id: str, payload: ResumeReviewCreate) -> Dict[str, Any]:
    return await services()["talent"].request_review(candidate_id, payload.model_dump())


@router.get("/api/v1/resume-reviews/{review_id}")
async def get_resume_review(review_id: str) -> Dict[str, Any]:
    return services()["talent"].get_review(review_id)


@router.get("/api/v1/resume-reviews/{review_id}/experience-questions")
async def list_experience_questions(review_id: str) -> Dict[str, Any]:
    return {"items": services()["talent"].list_experience_questions(review_id), "next_cursor": None}


@router.patch("/api/v1/experience-questions/{question_id}")
async def patch_experience_question(question_id: str, payload: ExperienceQuestionPatch) -> Dict[str, Any]:
    return await services()["talent"].patch_experience_question(question_id, payload.model_dump(exclude_unset=True))


@router.post("/api/v1/experience-questions/{question_id}/speech/regenerate")
async def regenerate_experience_question_speech(question_id: str, payload: VersionedPatch) -> Dict[str, Any]:
    return await services()["talent"].regenerate_experience_question_speech(
        question_id, expected_version=payload.expected_version
    )


@router.post("/api/v1/questions/search")
async def search_questions(payload: QuestionSearchRequest) -> Dict[str, Any]:
    return services()["catalog"].search_questions(payload.model_dump())


@router.get("/api/v1/questions/{question_id}")
async def get_question(question_id: str) -> Dict[str, Any]:
    return services()["catalog"].get_question(question_id)


@router.patch("/api/v1/questions/{question_id}")
async def patch_question(question_id: str, payload: QuestionPatch) -> Dict[str, Any]:
    return services()["catalog"].patch_question(question_id, payload.model_dump(exclude_unset=True))


@router.post("/api/v1/questions/{question_id}/speech/regenerate")
async def regenerate_question_speech(question_id: str, payload: VersionedPatch) -> Dict[str, Any]:
    return await services()["catalog"].regenerate_question_speech(
        question_id, expected_version=payload.expected_version
    )


@router.post("/api/v1/role-requirements")
async def create_role_requirement(payload: RoleRequirementCreate) -> Dict[str, Any]:
    return services()["roles"].create_role_requirement(payload.model_dump())


@router.post("/api/v1/job-positions/{position_id}/role-requirements")
async def create_position_role_requirement(position_id: str, payload: RoleRequirementCreate) -> Dict[str, Any]:
    value = payload.model_dump()
    value["job_position_id"] = position_id
    return services()["roles"].create_role_requirement(value)


@router.get("/api/v1/job-positions/{position_id}/role-requirements")
async def list_position_role_requirements(position_id: str) -> Dict[str, Any]:
    items = [
        item for item in services()["roles"].list_role_requirements() if item.get("job_position_id") == position_id
    ]
    return {"items": items, "next_cursor": None}


@router.get("/api/v1/role-requirements")
async def list_role_requirements() -> Dict[str, Any]:
    return {"items": services()["roles"].list_role_requirements(), "next_cursor": None}


@router.post("/api/v1/interview-plans/generate")
async def generate_interview_plan(payload: InterviewPlanGenerateRequest) -> Dict[str, Any]:
    return await services()["plan_assembly"].assemble(
        PlanAssemblyRequest(
            role_requirement_id=payload.role_requirement_id,
            knowledge_base_ids=tuple(payload.knowledge_base_ids),
            question_count=payload.question_count,
            policy=PlanAssemblyPolicy(
                coverage=tuple(payload.strategy.coverage),
                allow_followups=payload.strategy.allow_followups,
                max_same_skill_questions=payload.strategy.max_same_skill_questions,
                difficulty_curve=payload.strategy.difficulty_curve,
                deduplication_threshold=payload.strategy.deduplication_threshold,
            ),
            job_position_id=payload.job_position_id,
            candidate_profile_id=payload.candidate_profile_id,
            resume_review_id=payload.resume_review_id,
        )
    )


@router.get("/api/v1/interview-plans")
async def list_interview_plans() -> Dict[str, Any]:
    return {"items": services()["plans"].list_plans(), "next_cursor": None}


@router.get("/api/v1/interview-plans/{plan_id}")
async def get_interview_plan(plan_id: str) -> Dict[str, Any]:
    return services()["plans"].get_plan(plan_id)


@router.patch("/api/v1/interview-plans/{plan_id}")
async def patch_interview_plan(plan_id: str, payload: InterviewPlanPatch) -> Dict[str, Any]:
    return services()["plans"].patch_plan(plan_id, payload.model_dump(exclude_unset=True))


@router.post("/api/v1/interview-appointments")
async def create_interview_appointment(payload: InterviewAppointmentCreate) -> Dict[str, Any]:
    return services()["appointments"].create(payload.model_dump())


@router.get("/api/v1/interview-appointments")
async def list_interview_appointments() -> Dict[str, Any]:
    return {"items": services()["appointments"].list(), "next_cursor": None}


@router.get("/api/v1/interview-appointments/{appointment_id}")
async def get_interview_appointment(appointment_id: str) -> Dict[str, Any]:
    return services()["appointments"].get(appointment_id)


@router.patch("/api/v1/interview-appointments/{appointment_id}")
async def patch_interview_appointment(
    appointment_id: str, payload: InterviewAppointmentPatch
) -> Dict[str, Any]:
    return services()["appointments"].patch(appointment_id, payload.model_dump(exclude_unset=True))


@router.post("/api/v1/interview-appointments/{appointment_id}/invite")
async def invite_interview_appointment(
    appointment_id: str, payload: InterviewInvitationCreate
) -> Dict[str, Any]:
    return services()["appointments"].invite(appointment_id, payload.model_dump())


@router.post("/api/v1/interview-appointments/{appointment_id}/cancel")
async def cancel_interview_appointment(appointment_id: str) -> Dict[str, Any]:
    return services()["appointments"].cancel(appointment_id)


@router.get("/api/v1/public/interview-invitations/{token}")
async def get_public_interview_invitation(token: str) -> Dict[str, Any]:
    return services()["appointments"].public_invitation(token)


@router.post("/api/v1/public/interview-invitations/{token}/intake")
async def submit_candidate_intake(token: str, payload: CandidateIntakeCreate) -> Dict[str, Any]:
    return services()["appointments"].intake(token, payload.model_dump())


@router.post("/api/v1/public/interview-invitations/{token}/readiness")
async def get_candidate_readiness(
    token: str,
    payload: Optional[CandidateReadinessCreate] = None,
) -> Dict[str, Any]:
    return services()["appointments"].readiness(
        token,
        payload.model_dump() if payload is not None else None,
    )


@router.post("/api/v1/public/interview-invitations/{token}/start")
async def start_public_interview(token: str) -> Dict[str, Any]:
    return services()["appointments"].start(token)


@router.get("/api/v1/public/interviews/{interview_id}")
async def get_public_candidate_interview(
    interview_id: str,
    x_candidate_session_token: str = Header(alias="X-Candidate-Session-Token"),
) -> Dict[str, Any]:
    return services()["interviews"].get_candidate_interview(interview_id, x_candidate_session_token)


@router.post("/api/v1/public/interviews/{interview_id}/audio-answers")
async def submit_public_candidate_audio_answer(
    interview_id: str,
    payload: AudioAnswerSubmit,
    x_candidate_session_token: str = Header(alias="X-Candidate-Session-Token"),
) -> Dict[str, Any]:
    return await services()["interviews"].submit_candidate_audio_answer(
        interview_id,
        x_candidate_session_token,
        payload.model_dump(),
    )


@router.post("/api/v1/public/interviews/{interview_id}/avatar/speak")
async def speak_public_candidate_question(
    interview_id: str,
    payload: AvatarSpeakCommand,
    x_candidate_session_token: str = Header(alias="X-Candidate-Session-Token"),
) -> Dict[str, Any]:
    services()["interviews"].validate_candidate_token(interview_id, x_candidate_session_token)
    return await services()["avatar"].speak(interview_id, payload.model_dump())


@router.get("/api/v1/interviews")
async def list_interviews() -> Dict[str, Any]:
    return {"items": services()["interviews"].list_interviews(), "next_cursor": None}


@router.post("/api/v1/interviews/{interview_id}/pause")
async def pause_interview(interview_id: str, payload: InterviewControlCommand) -> Dict[str, Any]:
    result = services()["interviews"].pause_interview(interview_id, payload.reason)
    await broadcast_interview_state(interview_id)
    return result


@router.post("/api/v1/interviews/{interview_id}/resume")
async def resume_interview(interview_id: str, payload: InterviewControlCommand) -> Dict[str, Any]:
    result = services()["interviews"].resume_interview(interview_id, payload.reason)
    await broadcast_interview_state(interview_id)
    return result


@router.post("/api/v1/interviews/{interview_id}/timeout")
async def timeout_interview(interview_id: str, payload: InterviewControlCommand) -> Dict[str, Any]:
    result = services()["interviews"].timeout_interview(interview_id, payload.reason)
    await broadcast_interview_state(interview_id)
    return result


@router.post("/api/v1/interviews/{interview_id}/recover")
async def recover_interview(interview_id: str, payload: InterviewControlCommand) -> Dict[str, Any]:
    result = services()["interviews"].recover_interview(interview_id, payload.reason)
    await broadcast_interview_state(interview_id)
    return result


@router.post("/api/v1/interviews/{interview_id}/cancel")
async def cancel_interview(interview_id: str, payload: InterviewControlCommand) -> Dict[str, Any]:
    result = services()["interviews"].cancel_interview(interview_id, payload.reason)
    await broadcast_interview_state(interview_id)
    return result


@router.post("/api/v1/interviews/{interview_id}/skip")
async def skip_interview_turn(interview_id: str, payload: InterviewControlCommand) -> Dict[str, Any]:
    result = services()["interviews"].skip_current_turn(interview_id, payload.reason)
    await broadcast_interview_state(interview_id)
    return result


@router.get("/api/v1/interviews/{interview_id}")
async def get_interview(interview_id: str) -> Dict[str, Any]:
    return services()["interviews"].get_interview(interview_id)


@router.get("/api/v1/interviews/{interview_id}/events")
async def list_interview_lifecycle_events(interview_id: str) -> Dict[str, Any]:
    return {
        "items": services()["interviews"].list_lifecycle_events(interview_id),
        "next_cursor": None,
    }


@router.post("/api/v1/interviews/{interview_id}/audio-answers")
async def submit_audio_answer(interview_id: str, payload: AudioAnswerSubmit) -> Dict[str, Any]:
    return await services()["interviews"].submit_audio_answer(interview_id, payload.model_dump())


@router.post("/api/v1/interviews/{interview_id}/answers/{answer_id}/regrade")
async def regrade_answer(interview_id: str, answer_id: str) -> Dict[str, Any]:
    return await services()["interviews"].regrade_answer(interview_id, answer_id)


@router.get("/api/v1/interviews/{interview_id}/answers/{answer_id}/evaluations")
async def list_answer_evaluations(interview_id: str, answer_id: str) -> Dict[str, Any]:
    return {
        "items": services()["interviews"].list_answer_evaluations(interview_id, answer_id),
        "next_cursor": None,
    }


@router.post("/api/v1/interviews/{interview_id}/avatar/speak")
async def speak_interview_question(interview_id: str, payload: AvatarSpeakCommand) -> Dict[str, Any]:
    return await services()["avatar"].speak(interview_id, payload.model_dump())


@router.post("/api/v1/interviews/{interview_id}/complete")
async def complete_interview(interview_id: str) -> Dict[str, Any]:
    result = services()["interviews"].complete_interview(interview_id)
    await broadcast_interview_state(interview_id)
    return result


@router.get("/api/v1/interviews/{interview_id}/report")
async def get_report(interview_id: str) -> Dict[str, Any]:
    return services()["reports"].get_report(interview_id)


@router.get("/api/v1/interviews/{interview_id}/report/export")
async def export_report(
    interview_id: str,
    format: str = "csv",
    x_actor_id: str = Header(default="reviewer_local", alias="X-Actor-Id"),
) -> Response:
    exported = services()["reports"].export_report(
        interview_id, export_format=format, actor_id=x_actor_id
    )
    return Response(
        content=exported["content"],
        media_type=exported["content_type"],
        headers={"Content-Disposition": 'attachment; filename="%s"' % exported["filename"]},
    )


@router.get("/api/v1/interviews/{interview_id}/reports")
async def list_report_revisions(interview_id: str) -> Dict[str, Any]:
    return {
        "items": services()["reports"].list_report_revisions(interview_id),
        "next_cursor": None,
    }


@router.get("/api/v1/interviews/{interview_id}/review")
async def get_enterprise_review(interview_id: str) -> Dict[str, Any]:
    return services()["review"].get_review(interview_id)


@router.post("/api/v1/interviews/{interview_id}/answers/{answer_id}/audio-url")
async def get_answer_audio_url(
    interview_id: str,
    answer_id: str,
    x_actor_id: str = Header(default="reviewer_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["review"].audio_url(interview_id, answer_id, reviewer_id=x_actor_id)


@router.get("/api/v1/private-media/{token}", include_in_schema=False)
async def get_private_media(token: str) -> Response:
    opened = services()["review"].open_audio_grant(token)
    return Response(
        content=opened["content"],
        media_type=opened["content_type"],
        headers={"Cache-Control": "private, no-store"},
    )


@router.patch("/api/v1/interviews/{interview_id}/answers/{answer_id}/transcript")
async def correct_answer_transcript(
    interview_id: str, answer_id: str, payload: TranscriptCorrection
) -> Dict[str, Any]:
    return await services()["review"].correct_transcript(
        interview_id, answer_id, payload.model_dump()
    )


@router.post("/api/v1/interviews/{interview_id}/review-complete")
async def complete_enterprise_review(interview_id: str, payload: ReviewComplete) -> Dict[str, Any]:
    return services()["review"].complete_review(interview_id, payload.model_dump())


@router.websocket("/api/v1/interviews/{interview_id}/live")
async def interview_live(websocket: WebSocket, interview_id: str) -> None:
    await websocket.accept()
    store = get_store()
    role = websocket.query_params.get("role", "interviewer")
    session = RealtimeInterviewSession(store, interview_id, participant_role=role)
    try:
        try:
            if role != "candidate" and authenticate_interviewer_websocket(websocket) is not None:
                raise ApiError(
                    "AUTHORIZATION_FORBIDDEN",
                    "Interviewer WebSocket authorization failed.",
                    status_code=403,
                )
            if role == "candidate":
                session.validate_candidate_token(websocket.query_params.get("token"))
            initial_events = session.initial_events()
        except ApiError as exc:
            await websocket.send_json(
                {
                    "type": "error",
                    "interview_id": interview_id,
                    "payload": {"code": exc.code, "message": exc.message, "details": exc.details},
                }
            )
            await websocket.close(code=4403 if exc.status_code == 403 else 4404)
            return
        live_connections.connect(interview_id, websocket)
        for event in initial_events:
            await websocket.send_json(event)
        while True:
            packet = await websocket.receive()
            if packet["type"] == "websocket.disconnect":
                break
            try:
                if packet.get("bytes") is not None:
                    session.handle_binary(packet["bytes"])
                    continue
                if packet.get("text") is None:
                    continue
                message = json.loads(packet["text"])
                events = await session.handle_event(message)
                await live_connections.broadcast(interview_id, events)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {
                        "type": "error",
                        "interview_id": interview_id,
                        "payload": {"code": "REALTIME_MESSAGE_INVALID", "message": "Realtime message must be valid JSON."},
                    }
                )
            except ApiError as exc:
                await websocket.send_json(
                    {
                        "type": "error",
                        "interview_id": interview_id,
                        "payload": {"code": exc.code, "message": exc.message, "details": exc.details},
                    }
                )
    except WebSocketDisconnect:
        return
    finally:
        session.close()
        live_connections.disconnect(interview_id, websocket)


@router.websocket("/api/v1/interviews/{interview_id}/stt-stream")
async def interview_stt_stream(websocket: WebSocket, interview_id: str) -> None:
    await websocket.accept()
    stream = StreamingInterviewSTT(get_store(), interview_id)
    try:
        stream.validate_candidate_token(websocket.query_params.get("token"))
        while True:
            packet = await websocket.receive()
            if packet["type"] == "websocket.disconnect":
                break
            try:
                if packet.get("bytes") is not None:
                    for event in await stream.send_audio(packet["bytes"]):
                        await websocket.send_json(event)
                    continue
                if packet.get("text") is None:
                    continue
                message = json.loads(packet["text"])
                event_type = message.get("type")
                if event_type == "stream.open":
                    events = await stream.open(message.get("payload") or {})
                elif event_type == "stream.finish":
                    events = await stream.finish(message.get("payload") or {})
                else:
                    raise ApiError("STT_STREAM_EVENT_UNSUPPORTED", "STT stream event is not supported.")
                for event in events:
                    await websocket.send_json(event)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {"type": "stream.error", "error_code": "STT_STREAM_MESSAGE_INVALID"}
                )
            except ApiError as exc:
                await websocket.send_json(
                    {
                        "type": "stream.error",
                        "error_code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    }
                )
    except ApiError as exc:
        await websocket.send_json(
            {"type": "stream.error", "error_code": exc.code, "message": exc.message}
        )
        await websocket.close(code=4403 if exc.status_code == 403 else 4404)
    except WebSocketDisconnect:
        return
    finally:
        await stream.close()
