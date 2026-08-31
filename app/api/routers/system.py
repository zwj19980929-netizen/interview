from typing import Any, Dict

from fastapi import APIRouter
from fastapi.responses import Response

from app.core.errors import ApiError
from app.core.readiness import deployment_readiness
from app.core.auth import (
    current_principal,
    issue_interviewer_websocket_ticket,
)
from app.repositories.provider import get_store
from app.schemas.api import WebSocketTicketCreate
from app.transport.http.responses import ApiJSONResponse, api_response


router = APIRouter(default_response_class=ApiJSONResponse)


# Authentication and process health

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


@router.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> Response:
    result = await deployment_readiness(get_store())
    return api_response(result, status_code=200 if result["ready"] else 503)
