import json
import os
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.prompt.contracts import prompt_contract
from app.core.sensitive_data import SensitiveDataProtector, mask_email, mask_phone
from app.core.time import utc_now
from app.domain.candidate_screening import (
    SCREENING_SCORE_POLICY_VERSION,
    apply_screening_score_policy,
    effective_screening_outcome,
    screening_ai_recommendation,
)
from app.file_storage.interface import PrivateFileStorage
from app.file_storage.provider import private_file_storage
from app.model_gateway.errors import ProviderError
from app.model_gateway import capabilities as cap
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import ChatJSONRequest
from app.persistence.interface import Persistence, new_work_item
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.catalog import CatalogService
from app.services.resume_review import ResumeReviewPipeline, queue_resume_review


class TalentService:
    """Owns the enterprise resume library and evidence-grounded review interface."""

    def __init__(
        self,
        store: InMemoryStore,
        *,
        persistence: Optional[Persistence] = None,
        gateway: Optional[ModelGateway] = None,
        catalog: Optional[CatalogService] = None,
        storage: Optional[PrivateFileStorage] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.gateway = gateway or ModelGateway(store, persistence=self.persistence)
        self.catalog = catalog or CatalogService(store, persistence=self.persistence, gateway=self.gateway)
        self.storage = storage or private_file_storage()
        self.sensitive = SensitiveDataProtector()

    def create_candidate(self, payload: Dict[str, Any], organization_id: str = "org_default") -> Dict[str, Any]:
        normalized_email = self._normalize_email(payload["email"])
        normalized_phone = self._normalize_phone(payload["phone"])
        now = utc_now()
        item = {
            "id": new_id("candidate"),
            "organization_id": organization_id,
            "name": str(payload["name"]).strip(),
            "email_encrypted": self.sensitive.encrypt(normalized_email),
            "phone_encrypted": self.sensitive.encrypt(normalized_phone),
            "email_masked": mask_email(normalized_email),
            "phone_masked": mask_phone(normalized_phone),
            "email_lookup_hash": self.sensitive.lookup_hash(organization_id, normalized_email),
            "phone_lookup_hash": self.sensitive.lookup_hash(organization_id, normalized_phone),
            "external_ref": payload.get("external_ref"),
            "job_position_id": payload.get("job_position_id"),
            "metadata": deepcopy(payload.get("metadata", {})),
            "retention_expires_at": payload.get("retention_expires_at"),
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        with self.persistence.transaction(organization_id) as transaction:
            if item.get("job_position_id"):
                position = transaction.job_positions.get(item["job_position_id"])
                if position is None or position.get("status") == "archived":
                    raise ApiError("JOB_POSITION_NOT_FOUND", "Job position does not exist.", status_code=404)
            return self._candidate_projection(transaction.candidate_profiles.add(item))

    def list_candidates(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return [
                self._candidate_with_screening(transaction, item)
                for item in transaction.candidate_profiles.list()
                if item.get("status") not in {"archived", "retention_purged"}
            ]

    def get_candidate(self, candidate_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            candidate = transaction.candidate_profiles.get(candidate_id)
            candidate = self._required(candidate, "CANDIDATE_PROFILE_NOT_FOUND", "Candidate profile does not exist.")
            return self._candidate_with_screening(transaction, candidate)

    def patch_candidate(
        self, candidate_id: str, payload: Dict[str, Any], organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        expected_version = int(payload["expected_version"])
        with self.persistence.transaction(organization_id) as transaction:
            candidate = transaction.candidate_profiles.get(candidate_id)
            self._required(candidate, "CANDIDATE_PROFILE_NOT_FOUND", "Candidate profile does not exist.")
            if payload.get("name") is not None:
                candidate["name"] = str(payload["name"]).strip()
            if payload.get("email") is not None:
                email = self._normalize_email(payload["email"])
                candidate.pop("email", None)
                candidate["email_encrypted"] = self.sensitive.encrypt(email)
                candidate["email_masked"] = mask_email(email)
                candidate["email_lookup_hash"] = self.sensitive.lookup_hash(organization_id, email)
            if payload.get("phone") is not None:
                phone = self._normalize_phone(payload["phone"])
                candidate.pop("phone", None)
                candidate["phone_encrypted"] = self.sensitive.encrypt(phone)
                candidate["phone_masked"] = mask_phone(phone)
                candidate["phone_lookup_hash"] = self.sensitive.lookup_hash(organization_id, phone)
            if payload.get("job_position_id") is not None:
                position = transaction.job_positions.get(payload["job_position_id"])
                if position is None or position.get("status") == "archived":
                    raise ApiError("JOB_POSITION_NOT_FOUND", "Job position does not exist.", status_code=404)
                candidate["job_position_id"] = payload["job_position_id"]
            for field in ("external_ref", "metadata", "status", "retention_expires_at"):
                if field in payload and payload[field] is not None:
                    candidate[field] = deepcopy(payload[field])
            candidate["updated_at"] = utc_now()
            updated = transaction.candidate_profiles.update(candidate, expected_version=expected_version)
            return self._candidate_with_screening(transaction, updated)

    def archive_candidate(
        self,
        candidate_id: str,
        *,
        expected_version: int,
        actor_id: str = "interviewer_local",
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            candidate = transaction.candidate_profiles.get(candidate_id)
            self._required(candidate, "CANDIDATE_PROFILE_NOT_FOUND", "Candidate profile does not exist.")
            candidate["status"] = "archived"
            candidate["archived_at"] = utc_now()
            candidate["updated_at"] = candidate["archived_at"]
            updated = transaction.candidate_profiles.update(candidate, expected_version=expected_version)
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "candidate.archived",
                    "resource_type": "candidate_profile",
                    "resource_id": candidate_id,
                    "metadata": {},
                    "created_at": utc_now(),
                }
            )
            return self._candidate_with_screening(transaction, updated)

    def list_resumes(self, candidate_id: str, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        self.get_candidate(candidate_id, organization_id)
        with self.persistence.transaction(organization_id) as transaction:
            items = [
                item
                for item in transaction.resume_documents.list()
                if item["candidate_profile_id"] == candidate_id
                and item.get("status") not in {"deleted", "deleting", "retention_purged"}
            ]
        return sorted(items, key=lambda item: int(item.get("resume_version", 0)), reverse=True)

    def get_resume(
        self, candidate_id: str, resume_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            resume = transaction.resume_documents.get(resume_id)
            self._required(resume, "RESUME_DOCUMENT_NOT_FOUND", "Resume document does not exist.")
            if resume["candidate_profile_id"] != candidate_id or resume.get("status") in {"deleted", "retention_purged"}:
                raise ApiError("RESUME_CANDIDATE_MISMATCH", "Resume does not belong to this candidate.", status_code=404)
            file_object = transaction.file_objects.get(resume.get("file_object_id")) if resume.get("file_object_id") else None
        public = deepcopy(resume)
        public.pop("parsed_text", None)
        public["file"] = {
            "status": file_object.get("status"),
            "content_type": file_object.get("content_type"),
            "byte_count": file_object.get("byte_count"),
            "scan_status": file_object.get("scan_status"),
        } if file_object else None
        return public

    async def request_review(
        self, candidate_id: str, payload: Dict[str, Any], organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            candidate = transaction.candidate_profiles.get(candidate_id)
            self._required(candidate, "CANDIDATE_PROFILE_NOT_FOUND", "Candidate profile does not exist.")
            resume = transaction.resume_documents.get(payload["resume_document_id"])
            self._required(resume, "RESUME_DOCUMENT_NOT_FOUND", "Resume document does not exist.")
            if resume["candidate_profile_id"] != candidate_id:
                raise ApiError("RESUME_CANDIDATE_MISMATCH", "Resume does not belong to this candidate.", status_code=409)
            if resume.get("status") != "ready" or not resume.get("file_hash"):
                raise ApiError("RESUME_DOCUMENT_NOT_READY", "Resume ingestion must finish before review.", status_code=409)
            position = transaction.job_positions.get(payload["job_position_id"])
            self._required(position, "JOB_POSITION_NOT_FOUND", "Job position does not exist.")
            role = transaction.role_requirements.get(payload["role_requirement_id"])
            self._required(role, "ROLE_REQUIREMENT_NOT_FOUND", "Role requirement does not exist.")
            if role.get("job_position_id") not in {None, position["id"]}:
                raise ApiError("ROLE_POSITION_MISMATCH", "Role requirement belongs to another position.", status_code=409)
            review, work = queue_resume_review(
                transaction,
                candidate_id=candidate_id,
                resume=resume,
                position=position,
                role=role,
                organization_id=organization_id,
            )
        return {"review": review, "job": self._public_work(work) if work else None}

    async def process_review_work(
        self, work_item_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            # Resume review can legitimately spend longer than the generic 60-second
            # lease in map/reduce model calls. Keep the lease beyond Celery's default
            # 300-second hard limit so Beat cannot redeliver live work concurrently.
            work = transaction.outbox.start(work_item_id, lease_seconds=330)
            review = transaction.resume_reviews.get(work["payload"]["resume_review_id"])
            if review is None:
                raise RuntimeError("Resume review disappeared.")
            resume = transaction.resume_documents.get(review["resume_document_id"])
            role = transaction.role_requirements.get(review["role_requirement_id"])
            position = transaction.job_positions.get(review["job_position_id"])
            review["status"] = "processing"
            review["processing_stage"] = "preparing"
            review["error"] = None
            review["updated_at"] = utc_now()
            transaction.resume_reviews.update(review, expected_version=review["version"])

        def update_progress(stage: str, completed: int, total: int, strategy: str) -> None:
            with self.persistence.transaction(organization_id) as transaction:
                current = transaction.resume_reviews.get(review["id"])
                if current is None:
                    return
                current["status"] = "processing"
                current["processing_stage"] = stage
                current["processing_strategy"] = strategy
                current["processing_progress"] = {
                    "completed_chunks": completed,
                    "total_chunks": total,
                }
                current["updated_at"] = utc_now()
                transaction.resume_reviews.update(current, expected_version=current["version"])

        try:
            result = await ResumeReviewPipeline(self.gateway).process(
                parsed_text=self._load_resume_text(resume, organization_id),
                page_count=int(resume.get("page_count") or 1),
                position=position,
                role=role,
                organization_id=organization_id,
                on_progress=update_progress,
            )
        except Exception as exc:
            error_code = exc.code if isinstance(exc, ProviderError) else getattr(exc, "code", "resume_review_failed")
            retryable = exc.retryable if isinstance(exc, ProviderError) else False
            with self.persistence.transaction(organization_id) as transaction:
                current = transaction.resume_reviews.get(review["id"])
                failed = transaction.outbox.fail(
                    work_item_id,
                    str(exc),
                    lease_token=work["lease_token"],
                    error_code=error_code,
                    retryable=retryable,
                )
                terminal = failed.get("status") == "dead_letter"
                current["status"] = "failed" if terminal else "processing"
                current["processing_stage"] = "failed" if terminal else "retrying"
                current["error"] = {
                    "code": error_code,
                    "message": str(exc)[:500],
                    "retryable": retryable,
                }
                current["updated_at"] = utc_now()
                transaction.resume_reviews.update(current, expected_version=current["version"])
            raise
        data = result.data
        with self.persistence.transaction(organization_id) as transaction:
            current = transaction.resume_reviews.get(review["id"])
            now = utc_now()
            current["status"] = "ready_for_review"
            current["processing_stage"] = "completed"
            current["processing_strategy"] = result.processing["strategy"]
            current["processing_progress"] = {
                "completed_chunks": result.processing["chunk_count"],
                "total_chunks": result.processing["chunk_count"],
            }
            current["evidence_chunks"] = deepcopy(result.processing.get("chunks", []))
            current["project_evidence"] = deepcopy(data["project_evidence"])
            current["skill_evidence"] = deepcopy(data["skill_evidence"])
            current["warnings"] = deepcopy(data.get("warnings", []))
            current["summary"] = data["summary"]
            screening = apply_screening_score_policy(data["screening"])
            current["screening_recommendation"] = screening["recommendation"]
            current["screening_score"] = screening["score"]
            current["screening_policy_version"] = screening["policy_version"]
            current["screening_summary"] = screening["summary"]
            current["matched_requirements"] = deepcopy(screening["matched_requirements"])
            current["unmet_requirements"] = deepcopy(screening["unmet_requirements"])
            current["model_info"] = {
                "providers": deepcopy(result.processing.get("providers", [])),
                "prompt_versions": deepcopy(result.processing.get("prompt_versions", [])),
                "usage": deepcopy(result.processing.get("usage", {})),
                "estimated_input_tokens": result.processing.get("estimated_input_tokens"),
                "page_structure": result.processing.get("page_structure"),
            }
            if screening["recommendation"] == "qualified":
                question_work = self._enqueue_experience_question_generation(
                    transaction,
                    current,
                    reason="ai_screening_qualified",
                )
                current["question_generation_status"] = "queued"
                current["question_generation_work_item_id"] = question_work["id"]
                current["question_generation_error"] = None
            else:
                current["question_generation_status"] = "not_eligible"
                current["question_generation_work_item_id"] = None
                current["question_generation_error"] = None
            current["question_count"] = 0
            current["updated_at"] = now
            current = transaction.resume_reviews.update(current, expected_version=current["version"])
            self._refresh_screening_retention(transaction, current["candidate_profile_id"], now)
            transaction.outbox.complete(work_item_id, lease_token=work["lease_token"])
            return current

    async def process_experience_question_generation_work(
        self, work_item_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            work = transaction.outbox.start(work_item_id, lease_seconds=90)
            review = transaction.resume_reviews.get(work["payload"]["resume_review_id"])
            if review is None:
                raise RuntimeError("Resume review disappeared before question generation.")
            if effective_screening_outcome(review) != "qualified":
                review["question_generation_status"] = "not_eligible"
                review["question_generation_error"] = None
                review["updated_at"] = utc_now()
                updated = transaction.resume_reviews.update(review, expected_version=review["version"])
                transaction.outbox.complete(work_item_id, lease_token=work["lease_token"])
                return updated
            evidence = self._resume_evidence_catalog(review)
            position = transaction.job_positions.get(review["job_position_id"])
            review["question_generation_status"] = "processing"
            review["question_generation_error"] = None
            review["updated_at"] = utc_now()
            transaction.resume_reviews.update(review, expected_version=review["version"])

        try:
            if not evidence:
                raise ProviderError(
                    "resume_question_evidence_missing",
                    "Resume review has no evidence that can ground an experience question.",
                    retryable=False,
                )
            contract = prompt_contract(
                "resume_experience_question_generation",
                {
                    "position_name": position.get("name", "岗位") if position else "岗位",
                    "evidence_json": json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                },
            )
            response = await self.gateway.invoke(
                cap.LLM_CHAT_JSON,
                ChatJSONRequest(
                    organization_id=organization_id,
                    purpose="resume_experience_question_generation",
                    messages=contract.messages,
                    json_schema=contract.response_schema,
                    max_output_tokens=max(
                        1000,
                        int(os.getenv("INTERVIEWER_RESUME_QUESTION_OUTPUT_TOKENS", "3000")),
                    ),
                    metadata={
                        "resume_review_id": review["id"],
                        "resume_evidence": evidence,
                        "prompt_version": contract.version,
                    },
                ),
            )
            generated = self._resolve_generated_experience_questions(
                response.data.get("questions", []),
                evidence,
            )
        except Exception as exc:
            error_code = exc.code if isinstance(exc, ProviderError) else getattr(
                exc, "code", "resume_question_generation_failed"
            )
            retryable = exc.retryable if isinstance(exc, ProviderError) else False
            with self.persistence.transaction(organization_id) as transaction:
                failed = transaction.outbox.fail(
                    work_item_id,
                    str(exc),
                    lease_token=work["lease_token"],
                    error_code=error_code,
                    retryable=retryable,
                )
                terminal = failed.get("status") == "dead_letter"
                current = transaction.resume_reviews.get(review["id"])
                if current is not None:
                    current["question_generation_status"] = "failed" if terminal else "retrying"
                    current["question_generation_error"] = {
                        "code": error_code,
                        "message": str(exc)[:500],
                        "retryable": retryable,
                    }
                    current["updated_at"] = utc_now()
                    transaction.resume_reviews.update(current, expected_version=current["version"])
            raise

        with self.persistence.transaction(organization_id) as transaction:
            current = transaction.resume_reviews.get(review["id"])
            if current is None:
                raise RuntimeError("Resume review disappeared before question generation commit.")
            if effective_screening_outcome(current) != "qualified":
                current["question_generation_status"] = "not_eligible"
                current["question_generation_error"] = None
                current["updated_at"] = utc_now()
                updated = transaction.resume_reviews.update(current, expected_version=current["version"])
                transaction.outbox.complete(work_item_id, lease_token=work["lease_token"])
                return updated
            existing = [
                item
                for item in transaction.experience_questions.list()
                if item.get("resume_review_id") == current["id"]
                and item.get("source_type") == "ai_generated"
                and item.get("status") != "archived"
                and self._question_has_grounded_evidence(item, current)
            ]
            now = utc_now()
            if not existing:
                for order, item in enumerate(generated, start=1):
                    transaction.experience_questions.add(
                        self._new_ai_experience_question(
                            current,
                            item,
                            order=order,
                            now=now,
                            organization_id=organization_id,
                        )
                    )
                question_count = len(generated)
            else:
                question_count = len(existing)
            current["question_generation_status"] = "ready"
            current["question_generation_error"] = None
            current["question_count"] = question_count
            current["question_generation_model_info"] = {
                "prompt_version": contract.version,
                "provider": response.provider.model_dump(),
                "usage": response.usage.model_dump(),
            }
            current["updated_at"] = now
            updated = transaction.resume_reviews.update(current, expected_version=current["version"])
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": "system",
                    "action": "candidate_questions.generated",
                    "resource_type": "resume_review",
                    "resource_id": current["id"],
                    "metadata": {"question_count": question_count},
                    "created_at": now,
                }
            )
            transaction.outbox.complete(work_item_id, lease_token=work["lease_token"])
            return updated

    def get_review(self, review_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            review = transaction.resume_reviews.get(review_id)
        review = self._required(review, "RESUME_REVIEW_NOT_FOUND", "Resume review does not exist.")
        return self._review_with_score_policy(review)

    def retry_review(
        self,
        review_id: str,
        payload: Dict[str, Any],
        *,
        idempotency_key: str,
        actor_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        if not idempotency_key.strip():
            raise ApiError(
                "IDEMPOTENCY_KEY_REQUIRED",
                "Resume review retries require an Idempotency-Key header.",
            )
        expected_version = int(payload["expected_version"])
        reason = str(payload.get("reason") or "interviewer_requested_retry").strip()
        with self.persistence.transaction(organization_id) as transaction:
            review = transaction.resume_reviews.get(review_id)
            self._required(review, "RESUME_REVIEW_NOT_FOUND", "Resume review does not exist.")
            if review.get("last_retry_idempotency_key") == idempotency_key:
                replayed = transaction.outbox.get(review.get("last_retry_work_item_id"))
                if replayed is not None:
                    return {"review": review, "job": self._public_work(replayed)}
            if int(review.get("version", 1)) != expected_version:
                raise ApiError(
                    "RESUME_REVIEW_VERSION_CONFLICT",
                    "Resume review version changed; refresh before retrying.",
                    status_code=409,
                )
            if review.get("status") != "failed":
                raise ApiError(
                    "RESUME_REVIEW_NOT_RETRYABLE",
                    "Only a failed resume review can be retried.",
                    status_code=409,
                )
            work = next(
                (
                    item
                    for item in transaction.outbox.list()
                    if item.get("kind") == "resume.review" and item.get("aggregate_id") == review_id
                ),
                None,
            )
            if work is None or work.get("status") not in {"failed", "dead_letter"}:
                raise ApiError(
                    "RESUME_REVIEW_WORK_NOT_RETRYABLE",
                    "The resume review work item is not failed or dead-lettered.",
                    status_code=409,
                )
            resume = transaction.resume_documents.get(review.get("resume_document_id"))
            if resume is None or resume.get("status") != "ready":
                raise ApiError(
                    "RESUME_DOCUMENT_NOT_READY",
                    "The source resume is no longer ready for review.",
                    status_code=409,
                )
            if transaction.job_positions.get(review.get("job_position_id")) is None:
                raise ApiError("JOB_POSITION_NOT_FOUND", "Job position does not exist.", status_code=409)
            if transaction.role_requirements.get(review.get("role_requirement_id")) is None:
                raise ApiError("ROLE_REQUIREMENT_NOT_FOUND", "Role requirement does not exist.", status_code=409)
            now = utc_now()
            review.update(
                {
                    "status": "queued",
                    "processing_stage": "queued",
                    "processing_progress": {"completed_chunks": 0, "total_chunks": None},
                    "processing_strategy": None,
                    "error": None,
                    "last_retry_idempotency_key": idempotency_key,
                    "last_retry_work_item_id": work["id"],
                    "updated_at": now,
                }
            )
            updated = transaction.resume_reviews.update(review, expected_version=expected_version)
            replayed = transaction.outbox.replay(work["id"], reason=reason, actor_id=actor_id)
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "resume.review.retried",
                    "resource_type": "resume_review",
                    "resource_id": review_id,
                    "metadata": {"work_item_id": work["id"], "reason": reason},
                    "created_at": now,
                }
            )
        return {"review": updated, "job": self._public_work(replayed)}

    def review_screening(
        self,
        review_id: str,
        payload: Dict[str, Any],
        *,
        actor_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        expected_version = int(payload["expected_version"])
        decision = str(payload["decision"])
        with self.persistence.transaction(organization_id) as transaction:
            review = transaction.resume_reviews.get(review_id)
            self._required(review, "RESUME_REVIEW_NOT_FOUND", "Resume review does not exist.")
            if review.get("status") != "ready_for_review" or not review.get("screening_recommendation"):
                raise ApiError("CANDIDATE_SCREENING_NOT_READY", "Candidate screening is not ready for review.", status_code=409)
            now = utc_now()
            previous_outcome = effective_screening_outcome(review)
            review.update(
                {
                    "human_review_status": "reviewed",
                    "human_decision": decision,
                    "human_review_note": str(payload.get("note") or "").strip(),
                    "reviewed_by": actor_id,
                    "reviewed_at": now,
                    "updated_at": now,
                }
            )
            if decision == "qualified" and (
                previous_outcome != "qualified"
                or review.get("question_generation_status") not in {"queued", "processing", "ready"}
            ):
                question_work = self._enqueue_experience_question_generation(
                    transaction,
                    review,
                    reason="human_screening_qualified",
                )
                review["question_generation_status"] = "queued"
                review["question_generation_work_item_id"] = question_work["id"]
                review["question_generation_error"] = None
            elif decision != "qualified":
                review["question_generation_status"] = "not_eligible"
            updated = transaction.resume_reviews.update(review, expected_version=expected_version)
            self._refresh_screening_retention(transaction, updated["candidate_profile_id"], now)
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "candidate.screening.reviewed",
                    "resource_type": "resume_review",
                    "resource_id": review_id,
                    "metadata": {"decision": decision, "job_position_id": updated["job_position_id"]},
                    "created_at": now,
                }
            )
            return updated

    def list_experience_questions(
        self, review_id: str, organization_id: str = "org_default"
    ) -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            review = transaction.resume_reviews.get(review_id)
            self._required(review, "RESUME_REVIEW_NOT_FOUND", "Resume review does not exist.")
            if effective_screening_outcome(review) != "qualified":
                return []
            return sorted(
                [
                    item
                    for item in transaction.experience_questions.list()
                    if item["resume_review_id"] == review_id and item.get("status") != "archived"
                    and self._question_has_grounded_evidence(item, review)
                ],
                key=lambda item: (int(item.get("order", 0)), item.get("created_at", "")),
            )

    def list_candidate_experience_questions(
        self, candidate_id: str, organization_id: str = "org_default"
    ) -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            candidate = transaction.candidate_profiles.get(candidate_id)
            self._required(candidate, "CANDIDATE_PROFILE_NOT_FOUND", "Candidate profile does not exist.")
            eligible_reviews = {
                review["id"]: review
                for review in transaction.resume_reviews.list()
                if review.get("candidate_profile_id") == candidate_id
                and effective_screening_outcome(review) == "qualified"
            }
            return sorted(
                [
                    item
                    for item in transaction.experience_questions.list()
                    if item.get("candidate_profile_id") == candidate_id
                    and item.get("resume_review_id") in eligible_reviews
                    and item.get("status") != "archived"
                    and self._question_has_grounded_evidence(
                        item,
                        eligible_reviews[item["resume_review_id"]],
                    )
                ],
                key=lambda item: (item.get("created_at", ""), int(item.get("order", 0))),
                reverse=True,
            )

    def create_experience_question(
        self,
        candidate_id: str,
        payload: Dict[str, Any],
        *,
        actor_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            candidate = transaction.candidate_profiles.get(candidate_id)
            self._required(candidate, "CANDIDATE_PROFILE_NOT_FOUND", "Candidate profile does not exist.")
            review = transaction.resume_reviews.get(payload["resume_review_id"])
            self._required(review, "RESUME_REVIEW_NOT_FOUND", "Resume review does not exist.")
            if review.get("candidate_profile_id") != candidate_id:
                raise ApiError(
                    "EXPERIENCE_QUESTION_CANDIDATE_MISMATCH",
                    "Resume review belongs to another candidate.",
                    status_code=409,
                )
            if review.get("status") != "ready_for_review":
                raise ApiError(
                    "RESUME_REVIEW_NOT_READY",
                    "Resume review must finish before a personal question can be created.",
                    status_code=409,
                )
            if effective_screening_outcome(review) != "qualified":
                raise ApiError(
                    "CANDIDATE_QUESTION_BANK_NOT_ELIGIBLE",
                    "Resume questions can only be created after the effective screening outcome is qualified.",
                    status_code=409,
                )
            evidence_refs = self._resolve_evidence_refs(
                payload.get("evidence_refs") or [],
                self._resume_evidence_catalog(review),
                question_text=str(payload["question_text"]).strip(),
                require_label_in_question=True,
            )
            siblings = [
                item
                for item in transaction.experience_questions.list()
                if item.get("candidate_profile_id") == candidate_id and item.get("status") != "archived"
            ]
            now = utc_now()
            item = transaction.experience_questions.add(
                {
                    "id": new_id("experience_q"),
                    "organization_id": organization_id,
                    "resume_review_id": review["id"],
                    "candidate_profile_id": candidate_id,
                    "job_position_id": review["job_position_id"],
                    "source_type": "manual",
                    "order": max((int(value.get("order", 0)) for value in siblings), default=0) + 1,
                    "question_text": str(payload["question_text"]).strip(),
                    "standard_answer": str(payload["standard_answer"]).strip(),
                    "key_points": self._normalize_experience_key_points(payload["key_points"]),
                    "rubric": deepcopy(payload.get("rubric") or self._default_experience_rubric()),
                    "evidence_refs": evidence_refs,
                    "status": "draft",
                    "speech_status": "not_requested",
                    "speech_asset_id": None,
                    "language": "zh-CN",
                    "voice_profile_id": None,
                    "created_by": actor_id,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "candidate_question.created",
                    "resource_type": "experience_question",
                    "resource_id": item["id"],
                    "metadata": {"candidate_profile_id": candidate_id, "resume_review_id": review["id"]},
                    "created_at": now,
                }
            )
            return item

    async def patch_experience_question(
        self,
        question_id: str,
        payload: Dict[str, Any],
        *,
        actor_id: str = "interviewer_local",
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        expected_version = int(payload["expected_version"])
        with self.persistence.transaction(organization_id) as transaction:
            question = transaction.experience_questions.get(question_id)
            self._required(question, "EXPERIENCE_QUESTION_NOT_FOUND", "Experience question does not exist.")
            if question.get("status") == "archived":
                raise ApiError(
                    "EXPERIENCE_QUESTION_ARCHIVED",
                    "Archived experience questions cannot be edited.",
                    status_code=409,
                )
            review = transaction.resume_reviews.get(question.get("resume_review_id"))
            if review is None or effective_screening_outcome(review) != "qualified":
                raise ApiError(
                    "CANDIDATE_QUESTION_BANK_NOT_ELIGIBLE",
                    "Resume questions can only be changed while the effective screening outcome is qualified.",
                    status_code=409,
                )
            original_status = question.get("status")
            original_question_text = question.get("question_text")
            for field in ("question_text", "standard_answer"):
                if field in payload and payload[field] is not None:
                    question[field] = str(payload[field]).strip()
            if payload.get("key_points") is not None:
                question["key_points"] = self._normalize_experience_key_points(payload["key_points"])
            for field in ("rubric", "status"):
                if field in payload and payload[field] is not None:
                    question[field] = deepcopy(payload[field])
            evidence_labels = payload.get("evidence_refs")
            if evidence_labels is None:
                evidence_labels = [
                    item.get("label") if isinstance(item, dict) else item
                    for item in question.get("evidence_refs", [])
                ]
            question["evidence_refs"] = self._resolve_evidence_refs(
                evidence_labels,
                self._resume_evidence_catalog(review),
                question_text=question["question_text"],
                require_label_in_question=True,
            )
            if question["status"] == "approved" and (
                original_status != "approved" or question.get("question_text") != original_question_text
            ):
                question["speech_status"] = "deferred"
                question["speech_asset_id"] = None
                question["speech_error"] = None
            elif question["status"] != "approved":
                question["speech_status"] = "not_requested"
            question["updated_at"] = utc_now()
            question = transaction.experience_questions.update(question, expected_version=expected_version)
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "candidate_question.updated",
                    "resource_type": "experience_question",
                    "resource_id": question_id,
                    "metadata": {"status": question["status"]},
                    "created_at": question["updated_at"],
                }
            )
            return question

    def archive_experience_question(
        self,
        question_id: str,
        *,
        expected_version: int,
        actor_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            question = transaction.experience_questions.get(question_id)
            self._required(question, "EXPERIENCE_QUESTION_NOT_FOUND", "Experience question does not exist.")
            if question.get("status") == "archived":
                return question
            now = utc_now()
            question["status"] = "archived"
            question["archived_at"] = now
            question["archived_by"] = actor_id
            question["updated_at"] = now
            updated = transaction.experience_questions.update(question, expected_version=expected_version)
            for work in transaction.outbox.list():
                if (
                    work.get("aggregate_id") == question_id
                    and work.get("kind") == "question.speech.generate"
                    and not work.get("payload", {}).get("appointment_id")
                    and work.get("status") not in {"completed", "cancelled"}
                ):
                    transaction.outbox.cancel(
                        work["id"], reason="experience_question_archived", actor_id=actor_id
                    )
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "candidate_question.archived",
                    "resource_type": "experience_question",
                    "resource_id": question_id,
                    "metadata": {"candidate_profile_id": updated.get("candidate_profile_id")},
                    "created_at": now,
                }
            )
            return updated

    async def regenerate_experience_question_speech(
        self, question_id: str, *, expected_version: int, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            question = transaction.experience_questions.get(question_id)
            self._required(question, "EXPERIENCE_QUESTION_NOT_FOUND", "Experience question does not exist.")
            review = transaction.resume_reviews.get(question.get("resume_review_id"))
            if review is None or effective_screening_outcome(review) != "qualified":
                raise ApiError(
                    "CANDIDATE_QUESTION_BANK_NOT_ELIGIBLE",
                    "Resume question speech can only be generated while the effective screening outcome is qualified.",
                    status_code=409,
                )
            if question.get("status") != "approved":
                raise ApiError(
                    "EXPERIENCE_QUESTION_NOT_APPROVED",
                    "Only an approved experience question can regenerate speech.",
                    status_code=409,
                )
            if int(question["version"]) != int(expected_version):
                from app.persistence.errors import ConcurrencyConflict

                raise ConcurrencyConflict(
                    "ExperienceQuestion %s expected version %s, found %s"
                    % (question_id, expected_version, question["version"])
                )
            works = [
                item
                for item in transaction.outbox.list()
                if item.get("kind") == "question.speech.generate"
                and item.get("aggregate_id") == question_id
                and item.get("payload", {}).get("appointment_id")
                and item.get("status") in {"failed", "dead_letter"}
            ]
            if not works:
                raise ApiError(
                    "EXPERIENCE_QUESTION_SPEECH_NOT_RETRYABLE",
                    "Resume question speech is generated after a candidate confirms an appointment.",
                    status_code=409,
                )
            replayed = [
                transaction.outbox.replay(
                    item["id"],
                    reason="interviewer_requested_resume_speech_retry",
                    actor_id="interviewer_local",
                )
                for item in works
            ]
            return {**question, "speech_retry_work_item_ids": [item["id"] for item in replayed]}

    def _load_resume_text(self, resume: Dict[str, Any], organization_id: str) -> str:
        file_object_id = resume.get("parsed_text_file_object_id")
        if file_object_id:
            with self.persistence.transaction(organization_id) as transaction:
                file_object = transaction.file_objects.get(str(file_object_id))
            if file_object is None or file_object.get("status") != "ready" or not file_object.get("object_key"):
                raise ApiError("RESUME_PARSED_TEXT_NOT_READY", "Parsed resume text is not ready.", status_code=409)
            return self.storage.open(str(file_object["object_key"])).decode("utf-8")
        legacy_text = str(resume.get("parsed_text") or "")
        if legacy_text and os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() != "production":
            return legacy_text
        raise ApiError("RESUME_PARSED_TEXT_NOT_READY", "Parsed resume text is not ready.", status_code=409)

    def _lookup_hash(self, organization_id: str, value: str) -> str:
        return self.sensitive.lookup_hash(organization_id, value)

    def _enqueue_experience_question_generation(
        self,
        transaction: Any,
        review: Dict[str, Any],
        *,
        reason: str,
    ) -> Dict[str, Any]:
        generation_revision = int(review.get("version", 1)) + 1
        return transaction.outbox.enqueue(
            new_work_item(
                organization_id=review["organization_id"],
                kind="resume.experience_questions.generate",
                aggregate_id=review["id"],
                idempotency_key="resume.experience-questions:%s:%s" % (
                    review["id"],
                    generation_revision,
                ),
                payload={
                    "resume_review_id": review["id"],
                    "generation_revision": generation_revision,
                    "reason": reason,
                },
            )
        )

    @staticmethod
    def _resume_evidence_catalog(review: Dict[str, Any]) -> List[Dict[str, Any]]:
        catalog: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for evidence_type, field in (("project", "project_evidence"), ("skill", "skill_evidence")):
            for raw in review.get(field, []):
                label = str(raw.get("label") or "").strip()
                evidence = str(raw.get("evidence") or "").strip()
                if not label or not evidence or label in seen:
                    continue
                seen.add(label)
                catalog.append(
                    {
                        "label": label,
                        "evidence": evidence,
                        "source_pages": [
                            int(page)
                            for page in raw.get("source_pages", [])
                            if isinstance(page, int) and page > 0
                        ],
                        "evidence_type": evidence_type,
                    }
                )
        return catalog

    @staticmethod
    def _resolve_evidence_refs(
        values: List[Any],
        catalog: List[Dict[str, Any]],
        *,
        question_text: str,
        require_label_in_question: bool = False,
    ) -> List[Dict[str, Any]]:
        labels: List[str] = []
        for value in values:
            label = str(value.get("label") if isinstance(value, dict) else value).strip()
            if label and label not in labels:
                labels.append(label)
        if not labels:
            raise ApiError(
                "EXPERIENCE_QUESTION_EVIDENCE_REQUIRED",
                "Every resume question must reference at least one item of resume evidence.",
                status_code=422,
            )
        by_label = {item["label"]: item for item in catalog}
        unknown = [label for label in labels if label not in by_label]
        if unknown:
            raise ApiError(
                "EXPERIENCE_QUESTION_EVIDENCE_INVALID",
                "Resume question references evidence that is not present in this review.",
                status_code=422,
                details={"unknown_labels": unknown},
            )
        if require_label_in_question and not any(
            label.casefold() in question_text.casefold() for label in labels
        ):
            raise ApiError(
                "EXPERIENCE_QUESTION_NOT_GROUNDED",
                "Generated resume question must name at least one referenced resume evidence label.",
                status_code=422,
            )
        return [deepcopy(by_label[label]) for label in labels]

    @classmethod
    def _question_has_grounded_evidence(
        cls,
        question: Dict[str, Any],
        review: Dict[str, Any],
    ) -> bool:
        refs = question.get("evidence_refs") or []
        if not refs or not all(isinstance(item, dict) for item in refs):
            return False
        catalog = {item["label"]: item for item in cls._resume_evidence_catalog(review)}
        question_text = str(question.get("question_text") or "")
        for ref in refs:
            label = str(ref.get("label") or "").strip()
            evidence = str(ref.get("evidence") or "").strip()
            current = catalog.get(label)
            if (
                not current
                or not evidence
                or current["evidence"] != evidence
                or label.casefold() not in question_text.casefold()
            ):
                return False
        return True

    def _resolve_generated_experience_questions(
        self,
        values: List[Dict[str, Any]],
        evidence: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not 1 <= len(values) <= 3:
            raise ProviderError(
                "resume_question_generation_invalid",
                "Resume question generation must return between one and three questions.",
                retryable=False,
            )
        result: List[Dict[str, Any]] = []
        seen_questions: set[str] = set()
        for value in values:
            question_text = str(value.get("question_text") or "").strip()
            if not question_text or question_text in seen_questions:
                raise ProviderError(
                    "resume_question_generation_invalid",
                    "Generated resume questions must be non-empty and unique.",
                    retryable=False,
                )
            try:
                evidence_refs = self._resolve_evidence_refs(
                    value.get("evidence_refs") or [],
                    evidence,
                    question_text=question_text,
                    require_label_in_question=True,
                )
            except ApiError as exc:
                raise ProviderError(
                    "resume_question_not_grounded",
                    exc.message,
                    retryable=False,
                    details=exc.details,
                ) from exc
            verification_points = [
                str(item).strip()
                for item in value.get("verification_points", [])
                if str(item).strip()
            ]
            if not verification_points:
                raise ProviderError(
                    "resume_question_generation_invalid",
                    "Generated resume question has no verification points.",
                    retryable=False,
                )
            seen_questions.add(question_text)
            result.append(
                {
                    "question_text": question_text,
                    "verification_points": verification_points,
                    "evidence_refs": evidence_refs,
                    "evaluation_guide": str(value.get("evaluation_guide") or "").strip(),
                }
            )
        return result

    def _new_ai_experience_question(
        self,
        review: Dict[str, Any],
        generated: Dict[str, Any],
        *,
        order: int,
        now: str,
        organization_id: str,
    ) -> Dict[str, Any]:
        return {
            "id": new_id("experience_q"),
            "organization_id": organization_id,
            "resume_review_id": review["id"],
            "candidate_profile_id": review["candidate_profile_id"],
            "job_position_id": review["job_position_id"],
            "source_type": "ai_generated",
            "order": order,
            "question_text": generated["question_text"],
            "standard_answer": generated["evaluation_guide"],
            "key_points": self._normalize_experience_key_points(generated["verification_points"]),
            "rubric": self._default_experience_rubric(),
            "evidence_refs": deepcopy(generated["evidence_refs"]),
            "status": "draft",
            "speech_status": "not_requested",
            "speech_asset_id": None,
            "language": "zh-CN",
            "voice_profile_id": None,
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _default_experience_rubric() -> Dict[str, Any]:
        return {
            "dimensions": [
                "specificity",
                "technical_depth",
                "evidence_consistency",
                "reflection",
            ]
        }

    @staticmethod
    def _normalize_experience_key_points(values: List[Any]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for index, value in enumerate(values, start=1):
            raw = value.model_dump() if hasattr(value, "model_dump") else deepcopy(value)
            if isinstance(raw, str):
                text = raw.strip()
                weight = 1.0
                aliases: List[str] = []
            else:
                text = str(raw.get("text") or "").strip()
                weight = float(raw.get("weight", 1.0))
                aliases = [str(item).strip() for item in raw.get("aliases", []) if str(item).strip()]
            if not text:
                raise ApiError(
                    "EXPERIENCE_QUESTION_KEY_POINT_REQUIRED",
                    "Every experience question key point must be non-empty.",
                    status_code=422,
                )
            normalized.append(
                {
                    "id": raw.get("id") if isinstance(raw, dict) and raw.get("id") else new_id("kp"),
                    "text": text,
                    "weight": weight,
                    "aliases": aliases,
                    "order": index,
                }
            )
        if not normalized:
            raise ApiError(
                "EXPERIENCE_QUESTION_KEY_POINTS_REQUIRED",
                "At least one experience question key point is required.",
                status_code=422,
            )
        return normalized

    def _candidate_projection(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        item = deepcopy(candidate)
        item.pop("email_encrypted", None)
        item.pop("phone_encrypted", None)
        item.pop("email_lookup_hash", None)
        item.pop("phone_lookup_hash", None)
        item["email"] = item.pop("email_masked", item.get("email", ""))
        item["phone"] = item.pop("phone_masked", item.get("phone", ""))
        return item

    def _candidate_with_screening(self, transaction: Any, candidate: Dict[str, Any]) -> Dict[str, Any]:
        item = self._candidate_projection(candidate)
        reviews = [
            review
            for review in transaction.resume_reviews.list()
            if review.get("candidate_profile_id") == candidate["id"]
            and review.get("status") not in {"deleted", "retention_purged"}
        ]
        if not reviews:
            resumes = [
                resume
                for resume in transaction.resume_documents.list()
                if resume.get("candidate_profile_id") == candidate["id"]
                and resume.get("status") not in {"deleted", "deleting", "retention_purged"}
            ]
            latest_resume = max(resumes, key=lambda value: str(value.get("created_at") or "")) if resumes else None
            if latest_resume and latest_resume.get("status") in {"processing", "failed"}:
                item["screening"] = {
                    "review_id": None,
                    "review_version": None,
                    "resume_document_id": latest_resume["id"],
                    "job_position_id": candidate.get("job_position_id"),
                    "job_position_name": "岗位",
                    "ai_recommendation": None,
                    "effective_outcome": "processing" if latest_resume["status"] == "processing" else "failed",
                    "score": None,
                    "summary": "简历正在进行安全扫描与文本解析。" if latest_resume["status"] == "processing" else "简历处理失败。",
                    "matched_requirements": [],
                    "unmet_requirements": [],
                    "human_review_status": "pending",
                    "processing_stage": "ingesting" if latest_resume["status"] == "processing" else "failed",
                    "processing_progress": None,
                    "error": latest_resume.get("processing_error"),
                }
            else:
                item["screening"] = None
            return item
        review = max(reviews, key=lambda value: str(value.get("created_at") or ""))
        position = transaction.job_positions.get(review.get("job_position_id"))
        if review.get("status") in {"queued", "processing", "failed"}:
            item["screening"] = {
                "review_id": review["id"],
                "review_version": review["version"],
                "resume_document_id": review["resume_document_id"],
                "job_position_id": review["job_position_id"],
                "job_position_name": position.get("name") if position else "岗位",
                "ai_recommendation": None,
                "effective_outcome": "failed" if review["status"] == "failed" else "processing",
                "score": None,
                "summary": "简历初筛失败，请检查任务错误后重试。" if review["status"] == "failed" else "LLM 正在提取并聚合简历证据。",
                "matched_requirements": [],
                "unmet_requirements": [],
                "human_review_status": "pending",
                "processing_stage": review.get("processing_stage"),
                "processing_progress": deepcopy(review.get("processing_progress")),
                "processing_strategy": review.get("processing_strategy"),
                "error": deepcopy(review.get("error")),
            }
            return item
        ai_recommendation = self._review_ai_recommendation(review)
        effective = review.get("human_decision") or ai_recommendation
        item["screening"] = {
            "review_id": review["id"],
            "review_version": review["version"],
            "resume_document_id": review["resume_document_id"],
            "job_position_id": review["job_position_id"],
            "job_position_name": position.get("name") if position else "岗位",
            "ai_recommendation": ai_recommendation,
            "effective_outcome": effective,
            "score": review.get("screening_score"),
            "score_policy_version": review.get("screening_policy_version") or SCREENING_SCORE_POLICY_VERSION,
            "summary": review.get("screening_summary"),
            "matched_requirements": deepcopy(review.get("matched_requirements", [])),
            "unmet_requirements": deepcopy(review.get("unmet_requirements", [])),
            "human_review_status": review.get("human_review_status", "pending"),
            "human_decision": review.get("human_decision"),
            "human_review_note": review.get("human_review_note"),
            "reviewed_by": review.get("reviewed_by"),
            "reviewed_at": review.get("reviewed_at"),
            "question_generation_status": review.get("question_generation_status", "not_eligible"),
            "question_generation_error": deepcopy(review.get("question_generation_error")),
            "question_count": int(review.get("question_count", 0)),
        }
        return item

    def _public_work(self, work: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if work is None:
            return None
        return {key: value for key, value in work.items() if key not in {"lease_token", "payload"}}

    def _refresh_screening_retention(self, transaction: Any, candidate_id: str, now: str) -> None:
        candidate = transaction.candidate_profiles.get(candidate_id)
        if candidate is None:
            return
        latest_by_position: Dict[str, Dict[str, Any]] = {}
        for review in transaction.resume_reviews.list():
            if review.get("candidate_profile_id") != candidate_id or review.get("status") in {"deleted", "retention_purged"}:
                continue
            key = str(review.get("job_position_id") or "")
            previous = latest_by_position.get(key)
            if previous is None or str(review.get("created_at") or "") > str(previous.get("created_at") or ""):
                latest_by_position[key] = review
        outcomes = [effective_screening_outcome(review) for review in latest_by_position.values()]
        if outcomes and all(outcome == "unqualified" for outcome in outcomes):
            if candidate.get("retention_reason") != "screening_unqualified" or not candidate.get("retention_expires_at"):
                parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                candidate["retention_expires_at"] = (parsed + timedelta(days=7)).astimezone(timezone.utc).isoformat()
            candidate["retention_reason"] = "screening_unqualified"
        elif candidate.get("retention_reason") == "screening_unqualified":
            candidate["retention_expires_at"] = None
            candidate["retention_reason"] = None
        candidate["updated_at"] = now
        transaction.candidate_profiles.update(candidate, expected_version=candidate["version"])

    @staticmethod
    def _review_ai_recommendation(review: Dict[str, Any]) -> Optional[str]:
        return screening_ai_recommendation(review)

    def _review_with_score_policy(self, review: Dict[str, Any]) -> Dict[str, Any]:
        item = deepcopy(review)
        recommendation = self._review_ai_recommendation(item)
        if recommendation is not None:
            item["screening_recommendation"] = recommendation
            item["screening_policy_version"] = item.get("screening_policy_version") or SCREENING_SCORE_POLICY_VERSION
        return item

    def _normalize_email(self, value: str) -> str:
        email = str(value).strip().lower()
        if "@" not in email:
            raise ApiError("CANDIDATE_EMAIL_INVALID", "Candidate email is invalid.")
        return email

    def _normalize_phone(self, value: str) -> str:
        phone = re.sub(r"[^0-9+]", "", str(value))
        if len(re.sub(r"\D", "", phone)) < 7:
            raise ApiError("CANDIDATE_PHONE_INVALID", "Candidate phone is invalid.")
        return phone

    def _required(self, item: Optional[Dict[str, Any]], code: str, message: str) -> Dict[str, Any]:
        if item is None:
            raise ApiError(code, message, status_code=404)
        return item
