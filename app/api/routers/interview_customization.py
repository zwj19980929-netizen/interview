from fastapi import APIRouter

from app.core.auth import current_principal
from app.core.errors import ApiError
from app.schemas.interview_customization import InterviewCustomizationResponse, InterviewCustomizationUpdate
from app.transport.http.responses import ApiJSONResponse
from app.transport.service_locator import services


router = APIRouter(prefix="/api/v1/interview-customization", default_response_class=ApiJSONResponse)


def _actor():
    principal = current_principal()
    if not principal.roles.intersection({"admin", "interviewer"}):
        raise ApiError("AUTHORIZATION_FORBIDDEN", "需要管理员或面试官权限。", status_code=403)
    return principal.actor_id


@router.get("", response_model=InterviewCustomizationResponse)
async def get_customization():
    return services()["interview_customization"].get(actor_id=_actor())


@router.patch("", response_model=InterviewCustomizationResponse)
async def update_customization(payload: InterviewCustomizationUpdate):
    return services()["interview_customization"].update(payload.model_dump(exclude_unset=True), actor_id=_actor())
