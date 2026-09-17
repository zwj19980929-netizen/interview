from typing import Any, Dict

from fastapi import APIRouter, Query

from app.core.auth import current_principal
from app.core.errors import ApiError
from app.schemas.interview_skills import (
    SkillApprovalCommand, SkillCommand, SkillCreate, SkillLifecycleCommand, SkillRevise,
)
from app.transport.http.responses import ApiJSONResponse, collection_response
from app.transport.service_locator import services


router = APIRouter(prefix="/api/v1/interview-skills", default_response_class=ApiJSONResponse)


def _actor():
    principal = current_principal()
    # Explicit route protection also applies to local X-Roles and standalone
    # router mounting; the production bearer boundary stays in middleware.
    if not principal.roles.intersection({"admin", "interviewer"}):
        raise ApiError("AUTHORIZATION_FORBIDDEN", "An administrator or interviewer role is required.", status_code=403)
    return principal.actor_id


@router.get("")
async def list_skills() -> Dict[str, Any]:
    _actor()
    return collection_response(services()["interview_skills"].list())


@router.post("")
async def create_skill(payload: SkillCreate) -> Dict[str, Any]:
    return services()["interview_skills"].create(payload.model_dump(), actor_id=_actor())


@router.get("/{skill_id}")
async def get_skill(skill_id: str) -> Dict[str, Any]:
    return services()["interview_skills"].get(skill_id, actor_id=_actor())


@router.post("/{skill_id}/revisions")
async def revise_skill(skill_id: str, payload: SkillRevise) -> Dict[str, Any]:
    return services()["interview_skills"].revise(
        skill_id, {"package": payload.package.model_dump()},
        expected_version=payload.expected_version, actor_id=_actor(),
    )


@router.get("/{skill_id}/revisions/{revision_id}")
async def get_skill_revision(skill_id: str, revision_id: str) -> Dict[str, Any]:
    return services()["interview_skills"].get_revision(skill_id, revision_id, actor_id=_actor())


@router.post("/{skill_id}/validate")
async def validate_skill(skill_id: str, payload: SkillCommand) -> Dict[str, Any]:
    return services()["interview_skills"].validate(skill_id, **payload.model_dump(), actor_id=_actor())


@router.post("/{skill_id}/approve")
async def approve_skill(skill_id: str, payload: SkillApprovalCommand) -> Dict[str, Any]:
    return services()["interview_skills"].approve(skill_id, **payload.model_dump(), actor_id=_actor())


@router.post("/{skill_id}/retire")
async def retire_skill(skill_id: str, payload: SkillLifecycleCommand) -> Dict[str, Any]:
    return services()["interview_skills"].retire(skill_id, **payload.model_dump(), actor_id=_actor())


@router.post("/{skill_id}/revoke")
async def revoke_skill(skill_id: str, payload: SkillLifecycleCommand) -> Dict[str, Any]:
    return await services()["interview_skills"].revoke_with_delivery(
        skill_id, **payload.model_dump(), actor_id=_actor(),
    )


@router.post("/{skill_id}/revoke-delivery")
async def retry_revoke_delivery(skill_id: str, payload: SkillCommand) -> Dict[str, Any]:
    return await services()["interview_skills"].retry_revocation_with_delivery(
        skill_id, **payload.model_dump(), actor_id=_actor(),
    )


@router.delete("/{skill_id}")
async def delete_skill(skill_id: str, expected_version: int = Query(ge=1)) -> Dict[str, Any]:
    return services()["interview_skills"].delete(skill_id, expected_version=expected_version, actor_id=_actor())
