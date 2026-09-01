from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response

from app.core.errors import ApiError
from app.schemas.api import (
    CandidateProfileCreate,
    CandidateProfilePatch,
    CandidateScreeningReview,
    ExperienceQuestionCreate,
    ExperienceQuestionPatch,
    QuestionPatch,
    QuestionSearchRequest,
    ResumeDocumentPatch,
    ResumeReviewCreate,
    ResumeReviewRetry,
    ResumeUrlImport,
    VersionedPatch,
)
from app.transport.http.responses import (
    ApiJSONResponse,
    accepted_response,
    collection_response,
)
from app.transport.service_locator import services


router = APIRouter(default_response_class=ApiJSONResponse)

@router.post("/api/v1/candidate-profiles")
async def create_candidate_profile(payload: CandidateProfileCreate) -> Dict[str, Any]:
    return services()["talent"].create_candidate(payload.model_dump())


@router.get("/api/v1/candidate-profiles")
async def list_candidate_profiles() -> Dict[str, Any]:
    return collection_response(services()["talent"].list_candidates())


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
        return accepted_response(
            {
                "resume_document_id": result["resume_document"]["id"],
                "ingestion_job_id": result["job"]["id"],
                "source_type": "local_upload",
                "ingestion_status": result["job"]["status"],
            }
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
    return accepted_response(
        {
            "resume_document_id": result["resume_document"]["id"],
            "ingestion_job_id": result["job"]["id"],
            "source_type": "url_import",
            "ingestion_status": result["job"]["status"],
        }
    )


@router.get("/api/v1/candidate-profiles/{candidate_id}/resumes")
async def list_resume_documents(candidate_id: str) -> Dict[str, Any]:
    return collection_response(services()["talent"].list_resumes(candidate_id))


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
    return accepted_response(result)


@router.get("/api/v1/resume-reviews/{review_id}")
async def get_resume_review(review_id: str) -> Dict[str, Any]:
    return services()["talent"].get_review(review_id)


@router.post("/api/v1/resume-reviews/{review_id}/retry", status_code=202)
async def retry_resume_review(
    review_id: str,
    payload: ResumeReviewRetry,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    x_actor_id: str = Header(default="interviewer_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["talent"].retry_review(
        review_id,
        payload.model_dump(),
        idempotency_key=idempotency_key or "",
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
    return collection_response(services()["talent"].list_experience_questions(review_id))


@router.get("/api/v1/candidate-profiles/{candidate_id}/experience-questions")
async def list_candidate_experience_questions(candidate_id: str) -> Dict[str, Any]:
    return collection_response(services()["talent"].list_candidate_experience_questions(candidate_id))


@router.post("/api/v1/candidate-profiles/{candidate_id}/experience-questions", status_code=201)
async def create_candidate_experience_question(
    candidate_id: str,
    payload: ExperienceQuestionCreate,
    x_actor_id: str = Header(default="interviewer_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["talent"].create_experience_question(
        candidate_id,
        payload.model_dump(),
        actor_id=x_actor_id,
    )


@router.patch("/api/v1/experience-questions/{question_id}")
async def patch_experience_question(
    question_id: str,
    payload: ExperienceQuestionPatch,
    x_actor_id: str = Header(default="interviewer_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return await services()["talent"].patch_experience_question(
        question_id,
        payload.model_dump(exclude_unset=True),
        actor_id=x_actor_id,
    )


@router.delete("/api/v1/experience-questions/{question_id}")
async def delete_experience_question(
    question_id: str,
    expected_version: int,
    x_actor_id: str = Header(default="interviewer_local", alias="X-Actor-Id"),
) -> Dict[str, Any]:
    return services()["talent"].archive_experience_question(
        question_id,
        expected_version=expected_version,
        actor_id=x_actor_id,
    )


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
async def regenerate_question_speech(
    question_id: str,
    payload: VersionedPatch,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> Dict[str, Any]:
    return await services()["catalog"].regenerate_question_speech(
        question_id,
        expected_version=payload.expected_version,
        idempotency_key=idempotency_key or "",
    )
