import hashlib
import os
import re
from copy import deepcopy
from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.sensitive_data import SensitiveDataProtector, mask_email, mask_phone
from app.core.time import utc_now
from app.file_storage.interface import PrivateFileStorage
from app.file_storage.provider import private_file_storage
from app.model_gateway import capabilities as cap
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import ChatJSONRequest, ChatMessage
from app.persistence.interface import Persistence, new_work_item
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.catalog import CatalogService


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
            "metadata": deepcopy(payload.get("metadata", {})),
            "retention_expires_at": payload.get("retention_expires_at"),
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        with self.persistence.transaction(organization_id) as transaction:
            return self._candidate_projection(transaction.candidate_profiles.add(item))

    def list_candidates(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return [self._candidate_projection(item) for item in transaction.candidate_profiles.list()]

    def get_candidate(self, candidate_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            candidate = transaction.candidate_profiles.get(candidate_id)
        candidate = self._required(candidate, "CANDIDATE_PROFILE_NOT_FOUND", "Candidate profile does not exist.")
        return self._candidate_projection(candidate)

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
            for field in ("external_ref", "metadata", "status", "retention_expires_at"):
                if field in payload and payload[field] is not None:
                    candidate[field] = deepcopy(payload[field])
            candidate["updated_at"] = utc_now()
            return self._candidate_projection(
                transaction.candidate_profiles.update(candidate, expected_version=expected_version)
            )

    def list_resumes(self, candidate_id: str, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        self.get_candidate(candidate_id, organization_id)
        with self.persistence.transaction(organization_id) as transaction:
            items = [item for item in transaction.resume_documents.list() if item["candidate_profile_id"] == candidate_id]
        return sorted(items, key=lambda item: int(item.get("resume_version", 0)), reverse=True)

    def get_resume(
        self, candidate_id: str, resume_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            resume = transaction.resume_documents.get(resume_id)
            self._required(resume, "RESUME_DOCUMENT_NOT_FOUND", "Resume document does not exist.")
            if resume["candidate_profile_id"] != candidate_id:
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
            position = transaction.job_positions.get(payload["job_position_id"])
            self._required(position, "JOB_POSITION_NOT_FOUND", "Job position does not exist.")
            role = transaction.role_requirements.get(payload["role_requirement_id"])
            self._required(role, "ROLE_REQUIREMENT_NOT_FOUND", "Role requirement does not exist.")
            if role.get("job_position_id") not in {None, position["id"]}:
                raise ApiError("ROLE_POSITION_MISMATCH", "Role requirement belongs to another position.", status_code=409)
            existing = next(
                (
                    item
                    for item in transaction.resume_reviews.list()
                    if item["resume_document_id"] == resume["id"]
                    and item["job_position_id"] == position["id"]
                    and item["role_requirement_id"] == role["id"]
                    and item["input_hash"] == self._review_hash(resume, position, role)
                ),
                None,
            )
            if existing:
                return existing
            now = utc_now()
            review = transaction.resume_reviews.add(
                {
                    "id": new_id("resume_review"),
                    "organization_id": organization_id,
                    "candidate_profile_id": candidate_id,
                    "resume_document_id": resume["id"],
                    "job_position_id": position["id"],
                    "role_requirement_id": role["id"],
                    "input_hash": self._review_hash(resume, position, role),
                    "status": "queued",
                    "project_evidence": [],
                    "skill_evidence": [],
                    "warnings": [],
                    "summary": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            work = transaction.outbox.enqueue(
                new_work_item(
                    organization_id=organization_id,
                    kind="resume.review",
                    aggregate_id=review["id"],
                    idempotency_key="resume.review:%s" % review["input_hash"],
                    payload={"resume_review_id": review["id"]},
                )
            )
        return await self.process_review_work(work["id"], organization_id)

    async def process_review_work(
        self, work_item_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            work = transaction.outbox.start(work_item_id)
            review = transaction.resume_reviews.get(work["payload"]["resume_review_id"])
            if review is None:
                raise RuntimeError("Resume review disappeared.")
            resume = transaction.resume_documents.get(review["resume_document_id"])
            role = transaction.role_requirements.get(review["role_requirement_id"])
            position = transaction.job_positions.get(review["job_position_id"])
        sanitized = self._redact_resume(self._load_resume_text(resume, organization_id))
        try:
            response = await self.gateway.invoke(
                cap.LLM_CHAT_JSON,
                ChatJSONRequest(
                    organization_id=organization_id,
                    purpose="resume_review",
                    messages=[
                        ChatMessage(role="system", content="只依据简历项目证据生成核验问题，不作录用结论。"),
                        ChatMessage(role="user", content="岗位：%s\n要求：%s\n脱敏简历：%s" % (position["name"], role["description"], sanitized)),
                    ],
                    json_schema=self._review_schema(),
                    metadata={
                        "resume_text": sanitized,
                        "position_name": position["name"],
                        "must_have_skills": role.get("must_have_skills", []),
                    },
                ),
            )
        except Exception as exc:
            with self.persistence.transaction(organization_id) as transaction:
                current = transaction.resume_reviews.get(review["id"])
                current["status"] = "failed"
                current["error"] = str(exc)[:500]
                current["updated_at"] = utc_now()
                transaction.resume_reviews.update(current, expected_version=current["version"])
                transaction.outbox.fail(work_item_id, str(exc), lease_token=work["lease_token"])
            raise
        data = response.data
        with self.persistence.transaction(organization_id) as transaction:
            current = transaction.resume_reviews.get(review["id"])
            now = utc_now()
            current["status"] = "ready_for_review"
            current["project_evidence"] = deepcopy(data["project_evidence"])
            current["skill_evidence"] = deepcopy(data["skill_evidence"])
            current["warnings"] = deepcopy(data.get("warnings", []))
            current["summary"] = data["summary"]
            current["model_info"] = response.provider.model_dump()
            current["updated_at"] = now
            current = transaction.resume_reviews.update(current, expected_version=current["version"])
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
        return self._required(review, "RESUME_REVIEW_NOT_FOUND", "Resume review does not exist.")

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

    def _review_schema(self) -> Dict[str, Any]:
        question = {
            "type": "object",
            "required": ["question_text", "verification_points", "evidence_refs", "evaluation_guide"],
            "properties": {
                "question_text": {"type": "string", "minLength": 1},
                "verification_points": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "evaluation_guide": {"type": "string"},
            },
        }
        evidence = {
            "type": "object",
            "required": ["label", "evidence"],
            "properties": {"label": {"type": "string"}, "evidence": {"type": "string"}},
        }
        return {
            "type": "object",
            "required": ["summary", "project_evidence", "skill_evidence", "experience_questions"],
            "properties": {
                "summary": {"type": "string"},
                "project_evidence": {"type": "array", "items": evidence},
                "skill_evidence": {"type": "array", "items": evidence},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "experience_questions": {"type": "array", "minItems": 1, "items": question},
            },
        }

    def _redact_resume(self, text: str) -> str:
        text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL_REDACTED]", text)
        text = re.sub(r"(?<!\d)(?:\+?86[- ]?)?1\d{10}(?!\d)", "[PHONE_REDACTED]", text)
        return re.sub(r"(?im)^(性别|年龄|婚姻状况|照片)\s*[:：].*$", "", text).strip()

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

    def _review_hash(self, resume: Dict[str, Any], position: Dict[str, Any], role: Dict[str, Any]) -> str:
        value = "%s:%s:%s:%s" % (resume["file_hash"], position["id"], position["version"], role["version"])
        return "sha256:%s" % hashlib.sha256(value.encode("utf-8")).hexdigest()

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
