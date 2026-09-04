import hashlib
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import FileResponse, Response

from app.core.errors import ApiError
from app.adapters.livekit_media import LiveKitMediaPlane
from app.core.readiness import deployment_readiness
from app.core.interview_agent_metrics import interview_agent_metrics
from app.core.auth import (
    current_principal,
    issue_interviewer_websocket_ticket,
)
from app.repositories.provider import get_store
from app.schemas.api import WebSocketTicketCreate
from app.services.media_capture import InterviewMediaCaptureService
from app.services.avatar_asset_access import AvatarAssetAccessService
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


@router.get("/api/v1/admin/interview-agent-metrics")
async def interview_agent_metric_snapshot() -> Dict[str, Any]:
    """Label-free bounded histograms; candidate or interview IDs never enter."""

    return {
        "schema": "interview-agent-metrics.v1",
        "metrics": interview_agent_metrics().snapshot(),
    }


@router.post("/api/v1/public/livekit/egress-webhook")
async def livekit_egress_webhook(
    request: Request,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> Dict[str, Any]:
    body = await request.body()
    plane = LiveKitMediaPlane()
    event = plane.verify_webhook(body, authorization or "")
    capture = await InterviewMediaCaptureService(
        get_store(), media_plane=plane
    ).handle_provider_webhook(
        event, event_fingerprint=hashlib.sha256(body).hexdigest()
    )
    return {
        "accepted": True,
        "event": event.get("event"),
        "capture_id": (capture or {}).get("id"),
        "capture_status": (capture or {}).get("status"),
    }


@router.get("/api/v1/public/interviews/{interview_id}/avatar-config")
async def public_avatar_config(
    interview_id: str,
    x_candidate_session_token: str = Header(alias="X-Candidate-Session-Token"),
) -> Dict[str, Any]:
    principal = current_principal()
    return AvatarAssetAccessService(get_store()).issue_config(
        interview_id,
        x_candidate_session_token,
        organization_id=principal.organization_id,
    )


@router.get("/api/v1/public/interviews/{interview_id}/avatar-model")
async def public_avatar_model(
    interview_id: str,
    grant: str = Query(min_length=32, max_length=4096),
) -> Response:
    asset = AvatarAssetAccessService(get_store()).authorize(interview_id, grant)
    return FileResponse(
        path=asset.asset_path,
        media_type="model/gltf-binary",
        filename="interviewer.vrm",
        headers={
            "Cache-Control": "private, no-store, max-age=0",
            "ETag": '"sha256:%s"' % asset.asset_sha256,
            "X-Content-Type-Options": "nosniff",
        },
    )
