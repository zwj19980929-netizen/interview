from typing import Any, Dict

from fastapi import APIRouter, Header, Request
from starlette.concurrency import run_in_threadpool
from app.transport.http.media import private_media_response
from fastapi.responses import Response

from app.core.auth import current_principal
from app.domain.scoring_quality import project_interview_scores, project_report
from app.schemas.api import (
    InterviewControlCommand,
    ReviewComplete,
    TakeoverMediaPermitCreate,
    TranscriptCorrection,
    TranscriptionVerification,
)
from app.transport.http.responses import ApiJSONResponse, collection_response, accepted_response
from app.transport.service_locator import services


router = APIRouter(default_response_class=ApiJSONResponse)


async def _publish_interview_snapshot(interview_id: str) -> None:
    principal = current_principal()
    await services()["agent_runtime"].publish_snapshot(
        interview_id, principal.organization_id
    )

@router.get("/api/v1/interviews")
async def list_interviews() -> Dict[str, Any]:
    return collection_response(project_interview_scores(item) for item in services()["interviews"].list_interviews())


@router.delete("/api/v1/interviews/{interview_id}")
async def remove_interview_from_list(
    interview_id: str, expected_version: int
) -> Dict[str, Any]:
    principal = current_principal()
    return project_interview_scores(
        services()["interviews"].remove_from_list(
            interview_id,
            expected_version=expected_version,
            actor_id=principal.actor_id,
            organization_id=principal.organization_id,
        )
    )


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
    return project_interview_scores(services()["interviews"].get_interview(interview_id))


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
    session = project_interview_scores(services()["interviews"].get_interview(interview_id))
    return collection_response(item for item in session["evaluation_revisions"] if item["answer_id"] == answer_id)


@router.post("/api/v1/interviews/{interview_id}/complete")
async def complete_interview(interview_id: str) -> Dict[str, Any]:
    result = services()["interviews"].complete_interview(interview_id)
    capture = await services()["media_captures"].stop_for_interview(
        interview_id, actor_id=current_principal().actor_id
    )
    result["media_capture"] = capture
    if result.get("report"):
        result["report"] = project_report(result["report"], result["interview"])
    result["interview"] = project_interview_scores(result["interview"])
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
        interview_id, export_format=format, actor_id=current_principal().actor_id
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
    return services()["review"].audio_url(interview_id, answer_id, reviewer_id=current_principal().actor_id)


@router.api_route("/api/v1/private-media/{token}", methods=["GET", "HEAD"], include_in_schema=False)
async def get_private_media(token: str, request: Request) -> Response:
    opened = await run_in_threadpool(services()["review"].open_playback_grant, token)
    return private_media_response(opened, request)


@router.post("/api/v1/interviews/{interview_id}/recording-url")
async def get_recording_url(interview_id: str) -> Dict[str, Any]:
    return services()["review"].recording_url(interview_id, reviewer_id=current_principal().actor_id)


@router.post("/api/v1/interviews/{interview_id}/processing/retry")
async def retry_interview_processing(interview_id: str):
    result = services()["interviews"].retry_processing(interview_id, actor_id=current_principal().actor_id)
    services()["media_captures"].request_finalization(interview_id, actor_id=current_principal().actor_id)
    return accepted_response(result)


@router.patch("/api/v1/interviews/{interview_id}/answers/{answer_id}/transcript")
async def correct_answer_transcript(
    interview_id: str, answer_id: str, payload: TranscriptCorrection
) -> Dict[str, Any]:
    return await services()["review"].correct_transcript(
        interview_id, answer_id, {**payload.model_dump(), "reviewer_id": current_principal().actor_id}
    )


@router.post("/api/v1/interviews/{interview_id}/answers/{answer_id}/transcription-verification")
async def verify_answer_transcription(interview_id: str, answer_id: str, payload: TranscriptionVerification):
    return accepted_response(services()["interviews"].verify_transcription(
        interview_id, answer_id, payload.model_dump(), actor_id=current_principal().actor_id))


@router.post("/api/v1/interviews/{interview_id}/review-complete")
async def complete_enterprise_review(interview_id: str, payload: ReviewComplete) -> Dict[str, Any]:
    return services()["review"].complete_review(interview_id, {**payload.model_dump(), "reviewer_id": current_principal().actor_id})
