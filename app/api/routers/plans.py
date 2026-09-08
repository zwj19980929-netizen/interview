from typing import Any, Dict, Optional

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from app.schemas.api import (
    CandidateIntakeCreate,
    CandidateReadinessCreate,
    CandidateRuntimeProblemReport,
    InterviewAppointmentCreate,
    InterviewAppointmentPatch,
    InterviewInvitationCreate,
    InterviewPlanGenerateRequest,
    InterviewPlanPatch,
    RoleRequirementCreate,
)
from app.services.plan_assembly import PlanAssemblyPolicy, PlanAssemblyRequest
from app.transport.http.fields.public_interview import PUBLIC_INTERVIEW_FIELDS
from app.transport.http.responses import ApiJSONResponse, api_response, collection_response
from app.transport.service_locator import services


router = APIRouter(default_response_class=ApiJSONResponse)

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
    return collection_response(items)


@router.get("/api/v1/role-requirements")
async def list_role_requirements() -> Dict[str, Any]:
    return collection_response(services()["roles"].list_role_requirements())


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
    return collection_response(services()["plans"].list_plans())


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
    return collection_response(services()["appointments"].list())


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
    return await services()["appointments"].invite_with_refresh(appointment_id, payload.model_dump())


@router.post("/api/v1/interview-appointments/{appointment_id}/readiness/refresh")
async def refresh_appointment_readiness(appointment_id: str) -> Dict[str, Any]:
    return await services()["appointments"].refresh_readiness(appointment_id)


@router.post("/api/v1/interview-appointments/{appointment_id}/cancel")
async def cancel_interview_appointment(appointment_id: str) -> Dict[str, Any]:
    return services()["appointments"].cancel(appointment_id)


@router.get("/api/v1/public/interview-invitations/{token}")
async def get_public_interview_invitation(token: str) -> Dict[str, Any]:
    return services()["appointments"].public_invitation(token)


@router.post("/api/v1/public/interview-invitations/{token}/intake")
async def submit_candidate_intake(token: str, payload: CandidateIntakeCreate) -> Dict[str, Any]:
    return services()["appointments"].intake(
        token, payload.model_dump(exclude_unset=True)
    )


@router.post("/api/v1/public/interview-invitations/{token}/readiness")
async def get_candidate_readiness(
    token: str,
    payload: Optional[CandidateReadinessCreate] = None,
) -> Dict[str, Any]:
    return await services()["appointments"].readiness_with_refresh(
        token,
        payload.model_dump(exclude_unset=True) if payload is not None else None,
    )


@router.post("/api/v1/public/interview-invitations/{token}/start")
async def start_public_interview(token: str) -> Dict[str, Any]:
    return await services()["appointments"].start_with_refresh(token)


@router.get("/api/v1/public/interviews/{interview_id}")
async def get_public_candidate_interview(
    interview_id: str,
    x_candidate_session_token: str = Header(alias="X-Candidate-Session-Token"),
) -> JSONResponse:
    result = services()["interviews"].get_candidate_interview(interview_id, x_candidate_session_token)
    return api_response(result, fields=PUBLIC_INTERVIEW_FIELDS)


@router.post("/api/v1/public/interviews/{interview_id}/agent-ticket")
async def issue_public_candidate_agent_ticket(
    interview_id: str,
    x_candidate_session_token: str = Header(alias="X-Candidate-Session-Token"),
) -> Dict[str, Any]:
    return services()["agent_tickets"].issue_candidate(
        interview_id, x_candidate_session_token
    )


@router.post("/api/v1/public/interviews/{interview_id}/runtime-problems")
async def report_public_candidate_runtime_problem(
    interview_id: str,
    payload: CandidateRuntimeProblemReport,
    x_candidate_session_token: str = Header(alias="X-Candidate-Session-Token"),
) -> Dict[str, Any]:
    result = services()["interviews"].report_candidate_runtime_problem(
        interview_id,
        x_candidate_session_token,
        payload.code,
    )
    try:
        await services()["agent_runtime"].publish_snapshot(interview_id)
        result["snapshot_broadcasted"] = True
    except Exception:
        # The lifecycle pause was already committed. A transient event-bus
        # failure must not turn a confirmed fail-closed result into a false
        # negative for the candidate; reconnect reads the persisted snapshot.
        result["snapshot_broadcasted"] = False
    return result
