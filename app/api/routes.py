import json
from typing import Any, Dict, List, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.repositories.provider import get_store
from app.schemas.api import (
    AnswerSubmit,
    AvatarSpeakCommand,
    InterviewCreate,
    InterviewControlCommand,
    InterviewPlanGenerateRequest,
    InterviewPlanPatch,
    ModelRouteCreate,
    ProviderConfigCreate,
    ProviderConfigPatch,
    QuestionCreate,
    QuestionSearchRequest,
    RoleRequirementCreate,
)
from app.core.errors import ApiError
from app.services.avatar import AvatarService
from app.services.interviews import InterviewService
from app.services.model_admin import ModelAdminService
from app.services.plan_assembly import (
    InterviewPlanAssembly,
    PlanAssemblyPolicy,
    PlanAssemblyRequest,
)
from app.services.plans import InterviewPlanService
from app.services.questions import QuestionService
from app.services.reports import ReportService
from app.services.roles import RoleRequirementService
from app.services.realtime import RealtimeInterviewSession


router = APIRouter()


class LiveConnectionManager:
    def __init__(self) -> None:
        self.connections: Dict[str, Set[WebSocket]] = {}

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
        disconnected: List[WebSocket] = []
        for websocket in list(self.connections.get(interview_id, set())):
            try:
                for event in events:
                    await websocket.send_json(event)
            except (RuntimeError, WebSocketDisconnect):
                disconnected.append(websocket)
        for websocket in disconnected:
            self.disconnect(interview_id, websocket)


live_connections = LiveConnectionManager()


def services() -> Dict[str, Any]:
    store = get_store()
    return {
        "model_admin": ModelAdminService(store),
        "questions": QuestionService(store),
        "roles": RoleRequirementService(store),
        "plan_assembly": InterviewPlanAssembly(store),
        "plans": InterviewPlanService(store),
        "interviews": InterviewService(store),
        "avatar": AvatarService(store),
        "reports": ReportService(store),
    }


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
async def test_model_provider_config(config_id: str) -> Dict[str, Any]:
    return await services()["model_admin"].test_provider_config(config_id)


@router.post("/api/v1/admin/model-routes")
async def create_model_route(payload: ModelRouteCreate) -> Dict[str, Any]:
    return services()["model_admin"].create_route(payload.model_dump())


@router.get("/api/v1/admin/model-routes")
async def list_model_routes() -> Dict[str, Any]:
    return {"items": services()["model_admin"].list_routes(), "next_cursor": None}


@router.post("/api/v1/admin/model-routes/{route_id}/test")
async def test_model_route(route_id: str) -> Dict[str, Any]:
    return await services()["model_admin"].test_route(route_id)


@router.post("/api/v1/questions")
async def create_question(payload: QuestionCreate) -> Dict[str, Any]:
    return await services()["questions"].create_question(payload.model_dump())


@router.get("/api/v1/questions")
async def list_questions() -> Dict[str, Any]:
    return {"items": services()["questions"].list_questions(), "next_cursor": None}


@router.post("/api/v1/questions/search")
async def search_questions(payload: QuestionSearchRequest) -> Dict[str, Any]:
    return await services()["questions"].search_questions(payload.model_dump())


@router.get("/api/v1/questions/{question_id}")
async def get_question(question_id: str) -> Dict[str, Any]:
    return services()["questions"].get_question(question_id)


@router.post("/api/v1/role-requirements")
async def create_role_requirement(payload: RoleRequirementCreate) -> Dict[str, Any]:
    return services()["roles"].create_role_requirement(payload.model_dump())


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
        )
    )


@router.get("/api/v1/interview-plans")
async def list_interview_plans() -> Dict[str, Any]:
    return {"items": services()["plans"].list_plans(), "next_cursor": None}


@router.patch("/api/v1/interview-plans/{plan_id}")
async def patch_interview_plan(plan_id: str, payload: InterviewPlanPatch) -> Dict[str, Any]:
    return services()["plans"].patch_plan(plan_id, payload.model_dump(exclude_unset=True))


@router.post("/api/v1/interviews")
async def create_interview(payload: InterviewCreate) -> Dict[str, Any]:
    return services()["interviews"].create_interview(payload.model_dump())


@router.get("/api/v1/interviews")
async def list_interviews() -> Dict[str, Any]:
    return {"items": services()["interviews"].list_interviews(), "next_cursor": None}


@router.post("/api/v1/interviews/{interview_id}/start")
async def start_interview(interview_id: str) -> Dict[str, Any]:
    result = services()["interviews"].start_interview(interview_id)
    await broadcast_interview_state(interview_id)
    return result


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


@router.post("/api/v1/interviews/{interview_id}/answers")
async def submit_answer(interview_id: str, payload: AnswerSubmit) -> Dict[str, Any]:
    return await services()["interviews"].submit_answer(interview_id, payload.model_dump())


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


@router.get("/api/v1/interviews/{interview_id}/reports")
async def list_report_revisions(interview_id: str) -> Dict[str, Any]:
    return {
        "items": services()["reports"].list_report_revisions(interview_id),
        "next_cursor": None,
    }


@router.websocket("/api/v1/interviews/{interview_id}/live")
async def interview_live(websocket: WebSocket, interview_id: str) -> None:
    await websocket.accept()
    store = get_store()
    role = websocket.query_params.get("role", "interviewer")
    session = RealtimeInterviewSession(store, interview_id, participant_role=role)
    try:
        try:
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
