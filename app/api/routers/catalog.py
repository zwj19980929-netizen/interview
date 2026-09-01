from typing import Any, Dict, Optional

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from app.core.errors import ApiError
from app.schemas.api import (
    GeneratedQuestionDraftImport,
    GeneratedQuestionDraftPatch,
    JobPositionCreate,
    JobPositionDeleteCommand,
    KnowledgeBaseAssignmentCreate,
    KnowledgeBaseCreate,
    KnowledgeBaseImport,
    KnowledgeBaseRebuild,
    KnowledgeBaseSpeechProfileUpdate,
    KnowledgeBaseSpeechRetry,
    QuestionCreate,
    QuestionGenerationControl,
    QuestionGenerationCreate,
    QuestionGenerationImport,
    VersionedPatch,
)
from app.transport.http.responses import (
    ApiJSONResponse,
    accepted_response,
    collection_response,
)
from app.transport.service_locator import services


router = APIRouter(default_response_class=ApiJSONResponse)

@router.post("/api/v1/job-positions")
async def create_job_position(payload: JobPositionCreate) -> Dict[str, Any]:
    return services()["catalog"].create_position(payload.model_dump())


@router.get("/api/v1/job-positions")
async def list_job_positions() -> Dict[str, Any]:
    return collection_response(services()["catalog"].list_positions())


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
    return collection_response(services()["catalog"].list_knowledge_bases(position_id))


@router.get("/api/v1/knowledge-bases")
async def list_knowledge_bases() -> Dict[str, Any]:
    return collection_response(services()["knowledge_base_speech"].list_knowledge_bases())


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
    command = payload.model_dump()
    command["speech_profile_guard_provided"] = (
        "expected_speech_profile_revision" in payload.model_fields_set
    )
    result = services()["knowledge_base_speech"].set_profile(
        knowledge_base_id,
        command,
        idempotency_key=idempotency_key or "",
        actor_id=x_actor_id,
    )
    return accepted_response(result)


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
    return accepted_response(result)


@router.get("/api/v1/knowledge-bases/{knowledge_base_id}/question-generation-batches")
async def list_question_generation_batches(knowledge_base_id: str) -> Dict[str, Any]:
    return collection_response(services()["question_generation"].list_batches(knowledge_base_id))


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
    return accepted_response(result)


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
    return accepted_response(result)


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
    return accepted_response(result)


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
    return accepted_response(result)


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
    return accepted_response(result)


@router.get("/api/v1/knowledge-bases/{knowledge_base_id}/speech-builds")
async def list_knowledge_base_speech_builds(knowledge_base_id: str) -> Dict[str, Any]:
    return collection_response(services()["knowledge_base_speech"].list_builds(knowledge_base_id))


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
    return accepted_response(result)


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
    return accepted_response(
        {
            "job_id": build["id"],
            "knowledge_base_id": knowledge_base_id,
            "status": build["status"],
            "tasks": ["parse", "validate_candidate_pool", "question_speech"],
        }
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
    return accepted_response(build)


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
    return collection_response(services()["catalog"].list_questions(knowledge_base_id))
