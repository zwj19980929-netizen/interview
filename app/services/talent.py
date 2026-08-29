import os
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
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
from app.model_gateway.gateway import ModelGateway
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
                current["status"] = "failed"
                current["processing_stage"] = "failed"
                current["error"] = {
                    "code": error_code,
                    "message": str(exc)[:500],
                    "retryable": retryable,
                }
                current["updated_at"] = utc_now()
                transaction.resume_reviews.update(current, expected_version=current["version"])
                transaction.outbox.fail(
                    work_item_id,
                    str(exc),
                    lease_token=work["lease_token"],
                    error_code=error_code,
                    retryable=retryable,
                )
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
            current["updated_at"] = now
            current = transaction.resume_reviews.update(current, expected_version=current["version"])
            self._refresh_screening_retention(transaction, current["candidate_profile_id"], now)
            for order, generated in enumerate(data["experience_questions"], start=1):
                transaction.experience_questions.add(
                    {
                        "id": new_id("experience_q"),
                        "organization_id": organization_id,
                        "resume_review_id": current["id"],
                        "candidate_profile_id": current["candidate_profile_id"],
                        "job_position_id": current["job_position_id"],
                        "order": order,
                        "question_text": generated["question_text"],
                        "standard_answer": generated.get("evaluation_guide") or "回答应与简历证据一致并包含具体行动与结果。",
                        "key_points": [
                            {"id": new_id("kp"), "text": value, "weight": 1.0, "aliases": [], "order": index}
                            for index, value in enumerate(generated["verification_points"], start=1)
                        ],
                        "rubric": {"dimensions": ["specificity", "technical_depth", "evidence_consistency", "reflection"]},
                        "evidence_refs": deepcopy(generated.get("evidence_refs", [])),
                        "status": "draft",
                        "speech_status": "pending",
                        "speech_asset_id": None,
                        "language": "zh-CN",
                        "voice_profile_id": "voice_default_cn",
                        "created_at": now,
                        "updated_at": now,
                    }
                )
            transaction.outbox.complete(work_item_id, lease_token=work["lease_token"])
            return current

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
        actor_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        expected_version = int(payload["expected_version"])
        reason = str(payload.get("reason") or "interviewer_requested_retry").strip()
        with self.persistence.transaction(organization_id) as transaction:
            review = transaction.resume_reviews.get(review_id)
            self._required(review, "RESUME_REVIEW_NOT_FOUND", "Resume review does not exist.")
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
        self.get_review(review_id, organization_id)
        with self.persistence.transaction(organization_id) as transaction:
            return [item for item in transaction.experience_questions.list() if item["resume_review_id"] == review_id]

    async def patch_experience_question(
        self, question_id: str, payload: Dict[str, Any], organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        expected_version = int(payload["expected_version"])
        with self.persistence.transaction(organization_id) as transaction:
            question = transaction.experience_questions.get(question_id)
            self._required(question, "EXPERIENCE_QUESTION_NOT_FOUND", "Experience question does not exist.")
            for field in ("question_text", "standard_answer", "key_points", "rubric", "status"):
                if field in payload and payload[field] is not None:
                    question[field] = deepcopy(payload[field])
            if question["status"] == "approved":
                question["speech_status"] = "pending"
            question["updated_at"] = utc_now()
            question = transaction.experience_questions.update(question, expected_version=expected_version)
            if question["status"] != "approved":
                return question
            work = transaction.outbox.enqueue(
                new_work_item(
                    organization_id=organization_id,
                    kind="question.speech.generate",
                    aggregate_id=question["id"],
                    idempotency_key="experience-question.speech:%s:%s" % (question["id"], question["version"]),
                    payload={"owner_type": "experience_question", "owner_id": question["id"], "source_version": question["version"]},
                )
            )
        return await self.catalog.process_speech_work(work["id"], organization_id)

    async def regenerate_experience_question_speech(
        self, question_id: str, *, expected_version: int, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            question = transaction.experience_questions.get(question_id)
            self._required(question, "EXPERIENCE_QUESTION_NOT_FOUND", "Experience question does not exist.")
            if question.get("status") != "approved":
                raise ApiError(
                    "EXPERIENCE_QUESTION_NOT_APPROVED",
                    "Only an approved experience question can regenerate speech.",
                    status_code=409,
                )
            question["speech_status"] = "pending"
            question["updated_at"] = utc_now()
            question = transaction.experience_questions.update(question, expected_version=expected_version)
            work = transaction.outbox.enqueue(
                new_work_item(
                    organization_id=organization_id,
                    kind="question.speech.generate",
                    aggregate_id=question["id"],
                    idempotency_key="experience-question.speech:%s:%s" % (question["id"], question["version"]),
                    payload={
                        "owner_type": "experience_question",
                        "owner_id": question["id"],
                        "source_version": question["version"],
                    },
                )
            )
        return await self.catalog.process_speech_work(work["id"], organization_id)

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
