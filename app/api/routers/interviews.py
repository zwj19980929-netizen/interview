from typing import Any, Dict

from fastapi import APIRouter, Header
from fastapi.responses import Response

from app.core.auth import current_principal
from app.schemas.api import (
    InterviewControlCommand,
    ReviewComplete,
    TakeoverMediaPermitCreate,
    TranscriptCorrection,
)
from app.transport.http.responses import ApiJSONResponse, collection_response
from app.transport.service_locator import services


router = APIRouter(default_response_class=ApiJSONResponse)


async def _publish_interview_snapshot(interview_id: str) -> None:
    principal = current_principal()
    await services()["agent_runtime"].publish_snapshot(
        interview_id, principal.organization_id
    )

@router.get("/api/v1/interviews")
async def list_interviews() -> Dict[str, Any]:
    return collection_response(services()["interviews"].list_interviews())


@router.post("/api/v1/interviews/{interview_id}/pause")
async def pause_interview(interview_id: str, payload: InterviewControlCommand) -> Dict[str, Any]:
    result = services()["interviews"].pause_interview(interview_id, payload.reason)
    await _publish_interview_snapshot(interview_id)
    return result


@router.post("/api/v1/interviews/{interview_id}/resume")
async def resume_interview(interview_id: str, payload: InterviewControlCommand) -> Dict[str, Any]:
    result = services()["interviews"].resume_interview(interview_id, payload.reason)
    await _publish_interview_snapshot(interview_id)
    return result


@router.post("/api/v1/interviews/{interview_id}/timeout")
async def timeout_interview(interview_id: str, payload: InterviewControlCommand) -> Dict[str, Any]:
    result = services()["interviews"].timeout_interview(interview_id, payload.reason)
    await _publish_interview_snapshot(interview_id)
    return result


@router.post("/api/v1/interviews/{interview_id}/recover")
async def recover_interview(interview_id: str, payload: InterviewControlCommand) -> Dict[str, Any]:
    result = services()["interviews"].recover_interview(interview_id, payload.reason)
    await _publish_interview_snapshot(interview_id)
    return result


@router.post("/api/v1/interviews/{interview_id}/cancel")
async def cancel_interview(interview_id: str, payload: InterviewControlCommand) -> Dict[str, Any]:
    result = services()["interviews"].cancel_interview(interview_id, payload.reason)
    await services()["media_captures"].stop_for_interview(
        interview_id, actor_id=current_principal().actor_id
    )
    await _publish_interview_snapshot(interview_id)
    return result


@router.post("/api/v1/interviews/{interview_id}/skip")
async def skip_interview_turn(interview_id: str, payload: InterviewControlCommand) -> Dict[str, Any]:
    result = services()["interviews"].skip_current_turn(interview_id, payload.reason)
    await _publish_interview_snapshot(interview_id)
    return result


@router.get("/api/v1/interviews/{interview_id}")
async def get_interview(interview_id: str) -> Dict[str, Any]:
    return services()["interviews"].get_interview(interview_id)


@router.post("/api/v1/interviews/{interview_id}/agent-ticket")
async def issue_enterprise_agent_ticket(interview_id: str) -> Dict[str, Any]:
    return services()["agent_tickets"].issue_enterprise(
        interview_id, current_principal()
    )


@router.post("/api/v1/interviews/{interview_id}/takeover/media-permit")
async def issue_takeover_media_permit(
    interview_id: str, payload: TakeoverMediaPermitCreate
) -> Dict[str, Any]:
    return services()["agent_tickets"].issue_takeover_media_permit(
        interview_id,
        current_principal(),
        lease_id=payload.lease_id,
        expected_version=payload.expected_version,
    )


@router.get("/api/v1/interviews/{interview_id}/events")
async def list_interview_lifecycle_events(interview_id: str) -> Dict[str, Any]:
    return collection_response(services()["interviews"].list_lifecycle_events(interview_id))


@router.post("/api/v1/interviews/{interview_id}/answers/{answer_id}/regrade")
async def regrade_answer(interview_id: str, answer_id: str) -> Dict[str, Any]:
    return await services()["interviews"].regrade_answer(interview_id, answer_id)


@router.get("/api/v1/interviews/{interview_id}/answers/{answer_id}/evaluations")
async def list_answer_evaluations(interview_id: str, answer_id: str) -> Dict[str, Any]:
    return collection_response(services()["interviews"].list_answer_evaluations(interview_id, answer_id))


@router.post("/api/v1/interviews/{interview_id}/complete")
async def complete_interview(interview_id: str) -> Dict[str, Any]:
    result = services()["interviews"].complete_interview(interview_id)
    capture = await services()["media_captures"].stop_for_interview(
        interview_id, actor_id=current_principal().actor_id
    )
    result["media_capture"] = capture
    await _publish_interview_snapshot(interview_id)
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
    return collection_response(services()["reports"].list_report_revisions(interview_id))


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
