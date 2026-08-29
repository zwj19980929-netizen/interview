import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from app.repositories.provider import get_store
from app.schemas.api import (
    AudioAnswerSubmit,
    AvatarSpeakCommand,
    AvatarSessionClose,
    CandidateIntakeCreate,
    CandidateProfilePatch,
    CandidateScreeningReview,
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
    JobPositionDeleteCommand,
    KnowledgeBaseCreate,
    KnowledgeBaseAssignmentCreate,
    KnowledgeBaseImport,
    KnowledgeBaseRebuild,
    KnowledgeBaseSpeechProfileUpdate,
    KnowledgeBaseSpeechRetry,
    ModelConfigurationCreate,
    ModelConfigurationPatch,
    ModelConfigurationTest,
    ModelRouteCreate,
    OutboxReplay,
    ProviderConnectionCreate,
    ProviderConnectionPatch,
    GeneratedQuestionDraftImport,
    GeneratedQuestionDraftPatch,
    QuestionCreate,
    QuestionGenerationCreate,
    QuestionGenerationControl,
    QuestionGenerationImport,
    QuestionPatch,
    QuestionSearchRequest,
    RetentionRun,
    ScoreCalibrationRun,
    ResumeUrlImport,
    ResumeDocumentPatch,
    ResumeReviewCreate,
    ResumeReviewRetry,
    ReviewComplete,
    RoleRequirementCreate,
    TranscriptCorrection,
    VersionedPatch,
    WebSocketTicketCreate,
)
from app.core.errors import ApiError
from app.core.readiness import deployment_readiness
from app.core.auth import (
    authenticate_interviewer_websocket,
    current_principal,
    issue_interviewer_websocket_ticket,
)
from app.services.avatar import AvatarService
from app.services.appointments import AppointmentService
from app.services.catalog import CatalogService
from app.services.knowledge_base_speech import KnowledgeBaseSpeechService
from app.services.question_generation import QuestionGenerationService
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


@router.get("/api/v1/auth/session")
async def get_auth_session() -> Dict[str, Any]:
    principal = current_principal()
    return {
        "actor_id": principal.actor_id,
        "organization_id": principal.organization_id,
        "roles": sorted(principal.roles),
        "authenticated": principal.authenticated,
    }


@router.post("/api/v1/auth/websocket-ticket")
async def create_websocket_ticket(payload: WebSocketTicketCreate) -> Dict[str, Any]:
    try:
        return issue_interviewer_websocket_ticket(payload.interview_id)
    except PermissionError as exc:
        raise ApiError("AUTHORIZATION_FORBIDDEN", str(exc), status_code=403) from exc


class LiveConnectionManager:
    def __init__(self) -> None:
        self.connections: Dict[str, Dict[WebSocket, str]] = {}
        self.bus = realtime_event_bus()

    def connect(self, interview_id: str, websocket: WebSocket, role: str) -> None:
        self.connections.setdefault(interview_id, {})[websocket] = role

    def disconnect(self, interview_id: str, websocket: WebSocket) -> None:
        interview_connections = self.connections.get(interview_id)
        if not interview_connections:
            return
        interview_connections.pop(websocket, None)
        if not interview_connections:
            self.connections.pop(interview_id, None)

    async def broadcast(self, interview_id: str, events: List[Dict[str, Any]]) -> None:
        await self._broadcast_local(interview_id, events)
        for event in events:
            await self.bus.publish(interview_id, event)

    async def _broadcast_local(self, interview_id: str, events: List[Dict[str, Any]]) -> None:
        disconnected: List[WebSocket] = []
        for websocket, role in list(self.connections.get(interview_id, {}).items()):
            try:
                for event in events:
                    await websocket.send_json(self._project_event(event, role))
            except (RuntimeError, WebSocketDisconnect):
                disconnected.append(websocket)
        for websocket in disconnected:
            self.disconnect(interview_id, websocket)

    @staticmethod
    def _project_event(event: Dict[str, Any], role: str) -> Dict[str, Any]:
        if role != "candidate" or event.get("type") != "evaluation.completed":
            return event
        payload = event.get("payload") or {}
        return {
            **event,
            "payload": {
                "turn_id": payload.get("turn_id"),
                "status": "completed",
            },
        }

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
            "knowledge_base_speech": KnowledgeBaseSpeechService,
            "question_generation": QuestionGenerationService,
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


@router.get("/readyz")
async def readyz() -> Response:
    result = await deployment_readiness(get_store())
    return JSONResponse(result, status_code=200 if result["ready"] else 503)


@router.get("/api/v1/admin/model-providers/catalog")
async def model_provider_catalog() -> Dict[str, Any]:
    return {"items": services()["model_admin"].catalog()}


@router.post("/api/v1/admin/model-provider-connections")
async def create_model_provider_connection(payload: ProviderConnectionCreate) -> Dict[str, Any]:
    return services()["model_admin"].create_provider_connection(payload.model_dump())


@router.get("/api/v1/admin/model-provider-connections")
async def list_model_provider_connections() -> Dict[str, Any]:
    return {"items": services()["model_admin"].list_provider_connections(), "next_cursor": None}


@router.get("/api/v1/admin/model-provider-connections/{connection_id}")
async def get_model_provider_connection(connection_id: str) -> Dict[str, Any]:
    return services()["model_admin"].get_provider_connection(connection_id)


@router.patch("/api/v1/admin/model-provider-connections/{connection_id}")
async def patch_model_provider_connection(connection_id: str, payload: ProviderConnectionPatch) -> Dict[str, Any]:
    return services()["model_admin"].patch_provider_connection(connection_id, payload.model_dump(exclude_unset=True))


@router.delete("/api/v1/admin/model-provider-connections/{connection_id}")
async def delete_model_provider_connection(connection_id: str, expected_version: int) -> Dict[str, Any]:
    return services()["model_admin"].delete_provider_connection(connection_id, expected_version)


@router.post("/api/v1/admin/model-provider-connections/{connection_id}/validate")
async def validate_model_provider_connection(connection_id: str) -> Dict[str, Any]:
    return await services()["model_admin"].validate_provider_connection(connection_id)


@router.get("/api/v1/admin/model-provider-connections/{connection_id}/model-catalog")
async def model_catalog_for_connection(connection_id: str, model_type: Optional[str] = None) -> Dict[str, Any]:
    return services()["model_admin"].model_catalog(connection_id, model_type)


@router.post("/api/v1/admin/model-configurations")
async def create_model_configuration(payload: ModelConfigurationCreate) -> Dict[str, Any]:
    return services()["model_admin"].create_model_configuration(payload.model_dump())


@router.get("/api/v1/admin/model-configurations")
async def list_model_configurations() -> Dict[str, Any]:
    return {"items": services()["model_admin"].list_model_configurations(), "next_cursor": None}


@router.get("/api/v1/admin/model-configurations/{configuration_id}")
async def get_model_configuration(configuration_id: str) -> Dict[str, Any]:
    return services()["model_admin"].get_model_configuration(configuration_id)


@router.get("/api/v1/admin/model-configurations/{configuration_id}/voices")
async def get_model_configuration_voices(configuration_id: str) -> Dict[str, Any]:
    return services()["model_admin"].voice_catalog(configuration_id)


@router.patch("/api/v1/admin/model-configurations/{configuration_id}")
async def patch_model_configuration(configuration_id: str, payload: ModelConfigurationPatch) -> Dict[str, Any]:
    return services()["model_admin"].patch_model_configuration(
        configuration_id, payload.model_dump(exclude_unset=True)
    )


@router.delete("/api/v1/admin/model-configurations/{configuration_id}")
async def delete_model_configuration(configuration_id: str, expected_version: int) -> Dict[str, Any]:
    return services()["model_admin"].delete_model_configuration(configuration_id, expected_version)


@router.post("/api/v1/admin/model-configurations/{configuration_id}/test")
async def test_model_configuration(
    configuration_id: str, payload: Optional[ModelConfigurationTest] = None
) -> Dict[str, Any]:
    return await services()["model_admin"].test_model_configuration(
        configuration_id, payload.model_dump(exclude_none=True) if payload else {}
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


@router.post("/api/v1/admin/evaluations/score-calibration")
async def evaluate_score_calibration(
    payload: ScoreCalibrationRun,
    x_actor_id: str = Header(default="admin_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["fairness"].score_calibration(
        payload.model_dump(), actor_id=x_actor_id
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


@router.get("/api/v1/workspace/question-catalog")
async def get_workspace_question_catalog() -> Dict[str, Any]:
    return services()["catalog"].workspace_question_catalog()


@router.get("/api/v1/workspace/question-overview")
async def get_workspace_question_overview() -> Dict[str, Any]:
    return services()["catalog"].workspace_question_overview()


@router.get("/api/v1/job-positions/{position_id}")
async def get_job_position(position_id: str) -> Dict[str, Any]:
    return services()["catalog"].get_position(position_id)


@router.patch("/api/v1/job-positions/{position_id}")
async def patch_job_position(position_id: str, payload: VersionedPatch) -> Dict[str, Any]:
    return services()["catalog"].patch_position(position_id, payload.model_dump(exclude_unset=True))


@router.get("/api/v1/job-positions/{position_id}/deletion-impact")
async def get_job_position_deletion_impact(position_id: str) -> Dict[str, Any]:
    return services()["catalog"].position_deletion_impact(position_id)


@router.delete("/api/v1/job-positions/{position_id}")
async def delete_job_position(
    position_id: str,
    payload: JobPositionDeleteCommand,
    x_actor_id: str = Header(default="admin_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["catalog"].delete_position(
        position_id,
        expected_version=payload.expected_version,
        confirmation=payload.confirmation,
        actor_id=x_actor_id,
    )


@router.post("/api/v1/job-positions/{position_id}/knowledge-bases")
async def create_position_knowledge_base(position_id: str, payload: KnowledgeBaseCreate) -> Dict[str, Any]:
    return services()["catalog"].create_knowledge_base(position_id, payload.model_dump())


@router.post("/api/v1/job-positions/{position_id}/knowledge-base-assignments")
async def assign_position_knowledge_base(
    position_id: str, payload: KnowledgeBaseAssignmentCreate
) -> Dict[str, Any]:
    return services()["catalog"].assign_knowledge_base(
        position_id,
        payload.knowledge_base_id,
        expected_position_version=payload.expected_position_version,
    )


@router.get("/api/v1/job-positions/{position_id}/knowledge-bases")
async def list_position_knowledge_bases(position_id: str) -> Dict[str, Any]:
    return {"items": services()["catalog"].list_knowledge_bases(position_id), "next_cursor": None}


@router.get("/api/v1/knowledge-bases")
async def list_knowledge_bases() -> Dict[str, Any]:
    return {"items": services()["knowledge_base_speech"].list_knowledge_bases(), "next_cursor": None}


@router.get("/api/v1/knowledge-bases/{knowledge_base_id}")
async def get_knowledge_base(knowledge_base_id: str) -> Dict[str, Any]:
    return services()["catalog"].get_knowledge_base(knowledge_base_id)


@router.patch("/api/v1/knowledge-bases/{knowledge_base_id}")
async def patch_knowledge_base(knowledge_base_id: str, payload: VersionedPatch) -> Dict[str, Any]:
    return services()["catalog"].patch_knowledge_base(
        knowledge_base_id, payload.model_dump(exclude_unset=True)
    )


@router.put("/api/v1/knowledge-bases/{knowledge_base_id}/speech-profile")
async def set_knowledge_base_speech_profile(
    knowledge_base_id: str,
    payload: KnowledgeBaseSpeechProfileUpdate,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    x_actor_id: str = Header(default="admin_local", alias="X-Actor-Id"),
) -> JSONResponse:
    result = services()["knowledge_base_speech"].set_profile(
        knowledge_base_id,
        payload.model_dump(),
        idempotency_key=idempotency_key or "",
        actor_id=x_actor_id,
    )
    return JSONResponse(result, status_code=202)


@router.get("/api/v1/knowledge-bases/{knowledge_base_id}/speech-options")
async def get_knowledge_base_speech_options(knowledge_base_id: str) -> Dict[str, Any]:
    return services()["knowledge_base_speech"].speech_options(knowledge_base_id)


@router.get("/api/v1/knowledge-bases/{knowledge_base_id}/question-generation-options")
async def get_question_generation_options(knowledge_base_id: str) -> Dict[str, Any]:
    return services()["question_generation"].options(knowledge_base_id)


@router.post("/api/v1/knowledge-bases/{knowledge_base_id}/question-generation-batches")
async def create_question_generation_batch(
    knowledge_base_id: str,
    payload: QuestionGenerationCreate,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    x_actor_id: str = Header(default="admin_local", alias="X-Actor-Id"),
) -> JSONResponse:
    result = services()["question_generation"].queue(
        knowledge_base_id,
        payload.model_dump(),
        idempotency_key=idempotency_key or "",
        actor_id=x_actor_id,
    )
    return JSONResponse(result, status_code=202)


@router.get("/api/v1/knowledge-bases/{knowledge_base_id}/question-generation-batches")
async def list_question_generation_batches(knowledge_base_id: str) -> Dict[str, Any]:
    return {
        "items": services()["question_generation"].list_batches(knowledge_base_id),
        "next_cursor": None,
    }


@router.get("/api/v1/question-generation-batches/{batch_id}")
async def get_question_generation_batch(batch_id: str) -> Dict[str, Any]:
    return services()["question_generation"].get_batch(batch_id)


@router.post("/api/v1/question-generation-batches/{batch_id}/stop")
async def stop_question_generation_batch(
    batch_id: str,
    payload: QuestionGenerationControl,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    x_actor_id: str = Header(default="admin_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["question_generation"].stop(
        batch_id,
        expected_version=payload.expected_version,
        reason=payload.reason,
        idempotency_key=idempotency_key or "",
        actor_id=x_actor_id,
    )


@router.post("/api/v1/question-generation-batches/{batch_id}/resume")
async def resume_question_generation_batch(
    batch_id: str,
    payload: QuestionGenerationControl,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    x_actor_id: str = Header(default="admin_local", alias="X-Actor-Id"),
) -> JSONResponse:
    result = services()["question_generation"].resume(
        batch_id,
        expected_version=payload.expected_version,
        reason=payload.reason,
        idempotency_key=idempotency_key or "",
        actor_id=x_actor_id,
    )
    return JSONResponse(result, status_code=202)


@router.post("/api/v1/question-generation-batches/{batch_id}/retry-failed")
async def retry_failed_question_generation_batch(
    batch_id: str,
    payload: QuestionGenerationControl,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    x_actor_id: str = Header(default="admin_local", alias="X-Actor-Id"),
) -> JSONResponse:
    result = services()["question_generation"].retry_failed(
        batch_id,
        expected_version=payload.expected_version,
        reason=payload.reason,
        idempotency_key=idempotency_key or "",
        actor_id=x_actor_id,
    )
    return JSONResponse(result, status_code=202)


@router.post("/api/v1/question-generation-batches/{batch_id}/chunks/{chunk_id}/retry")
async def retry_question_generation_chunk(
    batch_id: str,
    chunk_id: str,
    payload: QuestionGenerationControl,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    x_actor_id: str = Header(default="admin_local", alias="X-Actor-Id"),
) -> JSONResponse:
    result = services()["question_generation"].retry_chunk(
        batch_id,
        chunk_id,
        expected_version=payload.expected_version,
        reason=payload.reason,
        idempotency_key=idempotency_key or "",
        actor_id=x_actor_id,
    )
    return JSONResponse(result, status_code=202)


@router.patch("/api/v1/question-generation-batches/{batch_id}/drafts/{draft_id}")
async def patch_generated_question_draft(
    batch_id: str, draft_id: str, payload: GeneratedQuestionDraftPatch
) -> Dict[str, Any]:
    return services()["question_generation"].patch_draft(
        batch_id, draft_id, payload.model_dump(exclude_unset=True)
    )


@router.delete("/api/v1/question-generation-batches/{batch_id}/drafts/{draft_id}")
async def delete_generated_question_draft(
    batch_id: str, draft_id: str, expected_version: int
) -> Dict[str, Any]:
    return services()["question_generation"].delete_draft(
        batch_id, draft_id, expected_version=expected_version
    )


@router.post("/api/v1/question-generation-batches/{batch_id}/drafts/{draft_id}/import")
async def import_generated_question_draft(
    batch_id: str,
    draft_id: str,
    payload: GeneratedQuestionDraftImport,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    result = services()["question_generation"].confirm_draft_import(
        batch_id,
        draft_id,
        expected_version=payload.expected_version,
        expected_draft_version=payload.expected_draft_version,
        idempotency_key=idempotency_key or "",
    )
    return JSONResponse(result, status_code=202)


@router.post("/api/v1/question-generation-batches/{batch_id}/import")
async def import_question_generation_batch(
    batch_id: str,
    payload: QuestionGenerationImport,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    result = services()["question_generation"].confirm_import(
        batch_id,
        expected_version=payload.expected_version,
        idempotency_key=idempotency_key or "",
    )
    return JSONResponse(result, status_code=202)


@router.get("/api/v1/knowledge-bases/{knowledge_base_id}/speech-builds")
async def list_knowledge_base_speech_builds(knowledge_base_id: str) -> Dict[str, Any]:
    return {
        "items": services()["knowledge_base_speech"].list_builds(knowledge_base_id),
        "next_cursor": None,
    }


@router.get("/api/v1/knowledge-bases/{knowledge_base_id}/speech-builds/{job_id}")
async def get_knowledge_base_speech_build(knowledge_base_id: str, job_id: str) -> Dict[str, Any]:
    return services()["knowledge_base_speech"].get_build(knowledge_base_id, job_id)


@router.post("/api/v1/knowledge-bases/{knowledge_base_id}/speech-builds/{job_id}/retry-failed")
async def retry_knowledge_base_speech_build(
    knowledge_base_id: str,
    job_id: str,
    payload: KnowledgeBaseSpeechRetry,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    result = services()["knowledge_base_speech"].retry_failed(
        knowledge_base_id,
        job_id,
        expected_version=payload.expected_version,
        idempotency_key=idempotency_key or "",
    )
    return JSONResponse(result, status_code=202)


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


@router.post("/api/v1/knowledge-bases/{knowledge_base_id}/questions", status_code=202)
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


@router.delete("/api/v1/candidate-profiles/{candidate_id}")
async def delete_candidate_profile(
    candidate_id: str,
    expected_version: int,
    x_actor_id: str = Header(default="interviewer_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["talent"].archive_candidate(
        candidate_id,
        expected_version=expected_version,
        actor_id=x_actor_id,
    )


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
        job_position_id = str(form.get("job_position_id") or "").strip() or None
        role_requirement_id = str(form.get("role_requirement_id") or "").strip() or None
        result = services()["resume_ingestion"].queue_upload(
            candidate_id,
            file_name=display_name,
            content_type=getattr(upload, "content_type", None) or "application/octet-stream",
            content=b"".join(chunks),
            idempotency_key=idempotency_key or "",
            review_request={
                "job_position_id": job_position_id,
                "role_requirement_id": role_requirement_id,
            } if job_position_id or role_requirement_id else None,
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
        review_request={
            "job_position_id": payload.job_position_id,
            "role_requirement_id": payload.role_requirement_id,
        } if payload.job_position_id or payload.role_requirement_id else None,
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


@router.patch("/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}")
async def patch_resume_document(
    candidate_id: str,
    resume_id: str,
    payload: ResumeDocumentPatch,
    x_actor_id: str = Header(default="interviewer_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["resume_ingestion"].patch_resume(
        candidate_id,
        resume_id,
        payload.model_dump(),
        actor_id=x_actor_id,
    )


@router.delete("/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}")
async def delete_resume_document(
    candidate_id: str,
    resume_id: str,
    expected_version: int,
    x_actor_id: str = Header(default="interviewer_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["resume_ingestion"].delete_resume(
        candidate_id,
        resume_id,
        expected_version=expected_version,
        actor_id=x_actor_id,
    )


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
async def create_resume_review(candidate_id: str, payload: ResumeReviewCreate) -> JSONResponse:
    result = await services()["talent"].request_review(candidate_id, payload.model_dump())
    return JSONResponse(result, status_code=202)


@router.get("/api/v1/resume-reviews/{review_id}")
async def get_resume_review(review_id: str) -> Dict[str, Any]:
    return services()["talent"].get_review(review_id)


@router.post("/api/v1/resume-reviews/{review_id}/retry", status_code=202)
async def retry_resume_review(
    review_id: str,
    payload: ResumeReviewRetry,
    x_actor_id: str = Header(default="interviewer_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["talent"].retry_review(
        review_id,
        payload.model_dump(),
        actor_id=x_actor_id,
    )


@router.patch("/api/v1/resume-reviews/{review_id}/screening-review")
async def review_candidate_screening(
    review_id: str,
    payload: CandidateScreeningReview,
    x_actor_id: str = Header(default="interviewer_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["talent"].review_screening(
        review_id,
        payload.model_dump(),
        actor_id=x_actor_id,
    )


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


@router.delete("/api/v1/questions/{question_id}")
async def delete_question(question_id: str, expected_version: int) -> Dict[str, Any]:
    return services()["catalog"].delete_question(question_id, expected_version=expected_version)


@router.post("/api/v1/questions/{question_id}/speech/regenerate", status_code=202)
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
            approve=payload.approve,
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
    result = await services()["interviews"].submit_candidate_audio_answer(
        interview_id,
        x_candidate_session_token,
        payload.model_dump(),
    )
    return {
        "status": result["status"],
        "next_turn_id": result.get("next_turn_id"),
        "answer": {
            "id": result.get("answer", {}).get("id"),
            "turn_id": result.get("answer", {}).get("turn_id"),
            "evaluation_status": result.get("answer", {}).get("evaluation_status"),
        },
        "evaluation": {"status": "completed"},
    }


@router.post("/api/v1/public/interviews/{interview_id}/avatar/speak")
async def speak_public_candidate_question(
    interview_id: str,
    payload: AvatarSpeakCommand,
    x_candidate_session_token: str = Header(alias="X-Candidate-Session-Token"),
) -> Dict[str, Any]:
    services()["interviews"].validate_candidate_token(interview_id, x_candidate_session_token)
    return await services()["avatar"].speak(
        interview_id,
        payload.model_dump(),
        actor_id="candidate_session:%s" % interview_id,
    )


@router.post("/api/v1/public/interviews/{interview_id}/avatar/session/close")
async def close_public_candidate_avatar_session(
    interview_id: str,
    payload: AvatarSessionClose,
    x_candidate_session_token: str = Header(alias="X-Candidate-Session-Token"),
) -> Dict[str, Any]:
    services()["interviews"].validate_candidate_token(interview_id, x_candidate_session_token)
    return await services()["avatar"].close(interview_id, payload.session_id)


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
        live_connections.connect(interview_id, websocket, role)
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
