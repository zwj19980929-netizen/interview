import hashlib
import json
import os
from copy import deepcopy
from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.domain.appointment_speech import update_speech_preparation_item
from app.domain.speech_profile import (
    speech_asset_matches_profile,
    speech_profile_fingerprint,
)
from app.model_gateway import capabilities as cap
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import TTSSynthesizeRequest
from app.persistence.interface import Persistence, new_work_item
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.text import normalize_skill, tokenize
from app.file_storage.interface import PrivateFileStorage
from app.file_storage.provider import private_file_storage
from app.services.private_assets import PrivateAssetImporter
from app.services.retention import RetentionService
from app.services.roles import build_role_requirement_document


class CatalogService:
    """Owns the JobPosition -> KnowledgeBase -> Question build interface."""

    def __init__(
        self,
        store: InMemoryStore,
        *,
        persistence: Optional[Persistence] = None,
        gateway: Optional[ModelGateway] = None,
        storage: Optional[PrivateFileStorage] = None,
        asset_importer: Optional[PrivateAssetImporter] = None,
        retention: Optional[RetentionService] = None,
    ) -> None:
        self.store = store
        self.persistence = persistence or persistence_for(store)
        self.gateway = gateway or ModelGateway(store, persistence=self.persistence)
        self.storage = storage
        self.asset_importer = asset_importer or PrivateAssetImporter()
        self.retention = retention

    def create_position(self, payload: Dict[str, Any], organization_id: str = "org_default") -> Dict[str, Any]:
        code = str(payload["code"]).strip().lower()
        with self.persistence.transaction(organization_id) as transaction:
            if any(item["code"] == code for item in transaction.job_positions.list()):
                raise ApiError("JOB_POSITION_CODE_CONFLICT", "Job position code already exists.", status_code=409)
            now = utc_now()
            position = transaction.job_positions.add(
                {
                    "id": new_id("position"),
                    "organization_id": organization_id,
                    "code": code,
                    "name": str(payload["name"]).strip(),
                    "description": str(payload.get("description", "")).strip(),
                    "knowledge_base_ids": [],
                    "status": payload.get("status", "active"),
                    "created_at": now,
                    "updated_at": now,
                }
            )
            requirement_payload = payload.get("initial_requirement")
            if requirement_payload:
                requirement = transaction.role_requirements.add(
                    build_role_requirement_document(
                        requirement_payload,
                        organization_id=organization_id,
                        job_position_id=position["id"],
                        now=now,
                    )
                )
                position["initial_role_requirement"] = requirement
            return position

    def list_positions(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            knowledge_bases = transaction.knowledge_bases.list()
            return [
                self._project_position(item, knowledge_bases)
                for item in transaction.job_positions.list()
                if item.get("status") != "archived"
            ]

    def workspace_question_catalog(self, organization_id: str = "org_default") -> Dict[str, Any]:
        """Return the managed question hierarchy from one persistence transaction."""
        with self.persistence.transaction(organization_id) as transaction:
            knowledge_bases = transaction.knowledge_bases.list()
            return {
                "positions": [
                    self._project_position(item, knowledge_bases)
                    for item in transaction.job_positions.list()
                    if item.get("status") != "archived"
                ],
                "knowledge_bases": knowledge_bases,
                "questions": transaction.questions.list(),
            }

    def workspace_question_overview(self, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            questions = sorted(
                transaction.questions.list(),
                key=lambda item: str(item.get("created_at", "")),
                reverse=True,
            )
        return {
            "total": len(questions),
            "ready": sum(
                1
                for item in questions
                if item.get("validation_status") == "valid" and item.get("speech_status") == "ready"
            ),
            "recent": questions[:5],
        }

    def get_position(self, position_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.job_positions.get(position_id)
            knowledge_bases = transaction.knowledge_bases.list()
        item = self._required(item, "JOB_POSITION_NOT_FOUND", "Job position does not exist.")
        return self._project_position(item, knowledge_bases)

    def patch_position(
        self, position_id: str, payload: Dict[str, Any], organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        expected_version = int(payload["expected_version"])
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.job_positions.get(position_id)
            self._required(item, "JOB_POSITION_NOT_FOUND", "Job position does not exist.")
            for field in ("name", "description", "status"):
                if field in payload and payload[field] is not None:
                    item[field] = payload[field]
            item["updated_at"] = utc_now()
            return transaction.job_positions.update(item, expected_version=expected_version)

    def position_deletion_impact(
        self, position_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            position = transaction.job_positions.get(position_id)
            self._required(position, "JOB_POSITION_NOT_FOUND", "Job position does not exist.")
            candidate_ids = self._candidate_ids_for_position(transaction, position_id)
            return {
                "position_id": position_id,
                "position_name": position["name"],
                "position_version": position["version"],
                "candidate_count": len(candidate_ids),
                "role_requirement_count": sum(
                    1
                    for item in transaction.role_requirements.list()
                    if item.get("job_position_id") == position_id and item.get("status") != "archived"
                ),
                "plan_count": sum(
                    1
                    for item in transaction.interview_plans.list()
                    if item.get("job_position_id") == position_id and item.get("status") != "archived"
                ),
                "appointment_count": sum(
                    1
                    for item in transaction.interview_appointments.list()
                    if item.get("job_position_id") == position_id and item.get("status") != "cancelled"
                ),
            }

    def delete_position(
        self,
        position_id: str,
        *,
        expected_version: int,
        confirmation: str,
        actor_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        impact = self.position_deletion_impact(position_id, organization_id)
        if confirmation.strip() != impact["position_name"]:
            raise ApiError(
                "JOB_POSITION_DELETE_CONFIRMATION_INVALID",
                "Type the exact job position name to confirm deletion.",
                status_code=422,
            )
        with self.persistence.transaction(organization_id) as transaction:
            position = transaction.job_positions.get(position_id)
            self._required(position, "JOB_POSITION_NOT_FOUND", "Job position does not exist.")
            position["status"] = "deleting"
            position["updated_at"] = utc_now()
            reserved = transaction.job_positions.update(position, expected_version=expected_version)
            candidate_ids = self._candidate_ids_for_position(transaction, position_id)
        retention = self.retention or RetentionService(self.store, persistence=self.persistence)
        for candidate_id in candidate_ids:
            retention.purge_candidate(candidate_id, organization_id)
        with self.persistence.transaction(organization_id) as transaction:
            position = transaction.job_positions.get(position_id)
            position["status"] = "archived"
            position["knowledge_base_ids"] = []
            position["deleted_at"] = utc_now()
            position["deleted_by"] = actor_id
            position["updated_at"] = position["deleted_at"]
            archived = transaction.job_positions.update(position, expected_version=reserved["version"])
            for item in transaction.role_requirements.list():
                if item.get("job_position_id") == position_id and item.get("status") != "archived":
                    item.update({"status": "archived", "updated_at": position["deleted_at"]})
                    transaction.role_requirements.update(item, expected_version=item["version"])
            for item in transaction.interview_plans.list():
                if item.get("job_position_id") == position_id and item.get("status") != "archived":
                    item.update({"status": "archived", "updated_at": position["deleted_at"]})
                    transaction.interview_plans.update(item, expected_version=item["version"])
            for item in transaction.interview_appointments.list():
                if item.get("job_position_id") == position_id and item.get("status") != "cancelled":
                    item.update(
                        {
                            "status": "cancelled",
                            "invitation_status": "revoked",
                            "invitation_token_hash": None,
                            "updated_at": position["deleted_at"],
                        }
                    )
                    transaction.interview_appointments.update(item, expected_version=item["version"])
            audit = transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "job_position.delete.completed",
                    "resource_type": "job_position",
                    "resource_id": position_id,
                    "metadata": {
                        "candidate_count": len(candidate_ids),
                        "role_requirement_count": impact["role_requirement_count"],
                        "plan_count": impact["plan_count"],
                        "appointment_count": impact["appointment_count"],
                    },
                    "created_at": position["deleted_at"],
                }
            )
        return {
            "deleted": True,
            "position_id": archived["id"],
            "candidate_count": len(candidate_ids),
            "audit_event_id": audit["id"],
        }

    def create_knowledge_base(
        self, position_id: str, payload: Dict[str, Any], organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            position = transaction.job_positions.get(position_id)
            self._required(position, "JOB_POSITION_NOT_FOUND", "Job position does not exist.")
            now = utc_now()
            default_profile = self._default_speech_profile(transaction, payload, now)
            knowledge_base = transaction.knowledge_bases.add(
                {
                    "id": new_id("kb"),
                    "organization_id": organization_id,
                    "job_position_id": position_id,
                    "name": str(payload["name"]).strip(),
                    "description": str(payload.get("description", "")).strip(),
                    "positioning": str(payload.get("positioning", "")).strip(),
                    "tags": [normalize_skill(item) for item in payload.get("tags", []) if normalize_skill(item)],
                    "language": payload.get("language", "zh-CN"),
                    "voice_profile_id": payload.get("voice_profile_id", "voice_default_cn"),
                    "speech_profile": default_profile,
                    "speech_build_status": "ready" if default_profile else "configuration_required",
                    "status": "draft",
                    "readiness": {"question_count": 0, "valid_count": 0, "speech_ready_count": 0},
                    "created_at": now,
                    "updated_at": now,
                }
            )
            assigned = list(dict.fromkeys([*position.get("knowledge_base_ids", []), knowledge_base["id"]]))
            position["knowledge_base_ids"] = assigned
            position["updated_at"] = now
            transaction.job_positions.update(position, expected_version=position["version"])
            return knowledge_base

    def assign_knowledge_base(
        self,
        position_id: str,
        knowledge_base_id: str,
        *,
        expected_position_version: int,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        """Associate an existing organization bank without copying its questions or speech profile."""
        with self.persistence.transaction(organization_id) as transaction:
            position = transaction.job_positions.get(position_id)
            self._required(position, "JOB_POSITION_NOT_FOUND", "Job position does not exist.")
            knowledge_base = transaction.knowledge_bases.get(knowledge_base_id)
            self._required(knowledge_base, "KNOWLEDGE_BASE_NOT_FOUND", "Knowledge base does not exist.")
            assigned = self._position_knowledge_base_ids(position, transaction.knowledge_bases.list())
            if knowledge_base_id in assigned:
                return {
                    "position": self._project_position(position, transaction.knowledge_bases.list()),
                    "knowledge_base": deepcopy(knowledge_base),
                }
            assigned.append(knowledge_base_id)
            position["knowledge_base_ids"] = sorted(set(assigned))
            position["updated_at"] = utc_now()
            updated = transaction.job_positions.update(
                position,
                expected_version=expected_position_version,
            )
            return {
                "position": self._project_position(updated, transaction.knowledge_bases.list()),
                "knowledge_base": deepcopy(knowledge_base),
            }

    def list_knowledge_bases(
        self, position_id: str, organization_id: str = "org_default"
    ) -> List[Dict[str, Any]]:
        self.get_position(position_id, organization_id)
        with self.persistence.transaction(organization_id) as transaction:
            position = transaction.job_positions.get(position_id)
            knowledge_bases = transaction.knowledge_bases.list()
            assigned = set(self._position_knowledge_base_ids(position, knowledge_bases))
            return [
                item
                for item in knowledge_bases
                if item["id"] in assigned
            ]

    def get_knowledge_base(self, knowledge_base_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.knowledge_bases.get(knowledge_base_id)
        return self._required(item, "KNOWLEDGE_BASE_NOT_FOUND", "Knowledge base does not exist.")

    def patch_knowledge_base(
        self, knowledge_base_id: str, payload: Dict[str, Any], organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        expected_version = int(payload["expected_version"])
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.knowledge_bases.get(knowledge_base_id)
            self._required(item, "KNOWLEDGE_BASE_NOT_FOUND", "Knowledge base does not exist.")
            for field in ("name", "description", "positioning", "language", "voice_profile_id", "status"):
                if field in payload and payload[field] is not None:
                    item[field] = payload[field]
            if payload.get("tags") is not None:
                item["tags"] = [
                    normalize_skill(value)
                    for value in payload["tags"]
                    if normalize_skill(value)
                ]
            item["updated_at"] = utc_now()
            return transaction.knowledge_bases.update(item, expected_version=expected_version)

    def get_question(self, question_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.questions.get(question_id)
            self._required(item, "QUESTION_NOT_FOUND", "Question does not exist.")
            return self._question_projection(item, transaction)

    def patch_question(
        self, question_id: str, payload: Dict[str, Any], organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        expected_version = int(payload["expected_version"])
        with self.persistence.transaction(organization_id) as transaction:
            question = transaction.questions.get(question_id)
            self._required(question, "QUESTION_NOT_FOUND", "Question does not exist.")
            if question.get("status") == "archived" and payload.get("status") not in {None, "archived"}:
                raise ApiError("QUESTION_ARCHIVED", "Archived questions cannot be reactivated.", status_code=409)
            original_question_text = question.get("question_text")
            speech_fields = {"question_text"}
            for field in (
                "title",
                "question_text",
                "standard_answer",
                "difficulty",
                "type",
                "role_families",
                "rubric",
                "status",
            ):
                if field in payload and payload[field] is not None:
                    question[field] = deepcopy(payload[field])
            if payload.get("skills") is not None:
                question["skills"] = [normalize_skill(item) for item in payload["skills"] if normalize_skill(item)]
            if payload.get("key_points") is not None:
                question["key_points"] = self._normalize_key_points(payload["key_points"])
            self._validate_scoring_basis(question, question.get("key_points", []))
            question["content_hash"] = self._question_hash(question, question["key_points"])
            spoken_changed = any(
                field in payload and payload[field] != original_question_text for field in speech_fields
            )
            knowledge_base = transaction.knowledge_bases.get(question["knowledge_base_id"])
            profile = deepcopy((knowledge_base or {}).get("speech_profile"))
            if spoken_changed and question.get("status") != "archived":
                question["speech_status"] = "pending" if profile else "configuration_required"
            question["updated_at"] = utc_now()
            updated = transaction.questions.update(question, expected_version=expected_version)
            if spoken_changed and updated.get("status") != "archived":
                if profile is None:
                    self._refresh_knowledge_base(transaction, updated["knowledge_base_id"])
                    return self._question_projection(updated, transaction)
                transaction.outbox.enqueue(
                    new_work_item(
                        organization_id=organization_id,
                        kind="question.speech.generate",
                        aggregate_id=updated["id"],
                        idempotency_key="question.speech:%s:%s:%s"
                        % (updated["id"], updated["version"], profile["revision"]),
                        payload={
                            "owner_type": "question",
                            "owner_id": updated["id"],
                            "source_version": updated["version"],
                            "knowledge_base_id": updated["knowledge_base_id"],
                            "speech_profile": profile,
                            "speech_profile_revision": profile["revision"],
                        },
                    )
                )
            self._refresh_knowledge_base(transaction, updated["knowledge_base_id"])
            return self._question_projection(updated, transaction)

    def delete_question(
        self, question_id: str, *, expected_version: int, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        """Remove a question from the active bank while preserving historical snapshots/assets."""
        with self.persistence.transaction(organization_id) as transaction:
            question = transaction.questions.get(question_id)
            self._required(question, "QUESTION_NOT_FOUND", "Question does not exist.")
            if question.get("status") == "archived":
                if int(question["version"]) != int(expected_version):
                    from app.persistence.errors import ConcurrencyConflict

                    raise ConcurrencyConflict(
                        "Question %s expected version %s, found %s"
                        % (question_id, expected_version, question["version"])
                    )
                return {"id": question_id, "deleted": True, "status": "archived"}
            question["status"] = "archived"
            question["speech_status"] = "superseded"
            question["updated_at"] = utc_now()
            archived = transaction.questions.update(question, expected_version=expected_version)
            self._refresh_knowledge_base(transaction, archived["knowledge_base_id"])
            return {"id": question_id, "deleted": True, "status": "archived"}

    async def regenerate_question_speech(
        self,
        question_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        if not idempotency_key.strip():
            raise ApiError(
                "IDEMPOTENCY_KEY_REQUIRED",
                "Question speech regeneration requires an Idempotency-Key header.",
            )
        with self.persistence.transaction(organization_id) as transaction:
            question = transaction.questions.get(question_id)
            self._required(question, "QUESTION_NOT_FOUND", "Question does not exist.")
            work_idempotency_key = "question.speech.regenerate:%s:%s" % (
                question_id,
                idempotency_key,
            )
            existing = next(
                (
                    item
                    for item in transaction.outbox.list()
                    if item.get("idempotency_key") == work_idempotency_key
                    and item.get("kind") == "question.speech.generate"
                    and item.get("aggregate_id") == question_id
                ),
                None,
            )
            if existing is not None:
                return {**question, "job_id": existing["id"], "work_item_id": existing["id"]}
            if question.get("status") != "active":
                raise ApiError("QUESTION_NOT_ACTIVE", "Only an active question can regenerate speech.", status_code=409)
            knowledge_base = transaction.knowledge_bases.get(question["knowledge_base_id"])
            profile = deepcopy((knowledge_base or {}).get("speech_profile"))
            if profile is None:
                raise ApiError(
                    "KNOWLEDGE_BASE_SPEECH_PROFILE_REQUIRED",
                    "Configure the knowledge base TTS model and voice before generating speech.",
                    status_code=409,
                )
            question["speech_status"] = "pending"
            question["updated_at"] = utc_now()
            question = transaction.questions.update(question, expected_version=expected_version)
            work = transaction.outbox.enqueue(
                new_work_item(
                    organization_id=organization_id,
                    kind="question.speech.generate",
                    aggregate_id=question["id"],
                    idempotency_key=work_idempotency_key,
                    payload={
                        "owner_type": "question",
                        "owner_id": question["id"],
                        "source_version": question["version"],
                        "knowledge_base_id": question["knowledge_base_id"],
                        "speech_profile": profile,
                        "speech_profile_revision": profile["revision"],
                    },
                )
            )
        return {**question, "job_id": work["id"], "work_item_id": work["id"]}

    def queue_import(
        self,
        knowledge_base_id: str,
        questions: List[Dict[str, Any]],
        *,
        idempotency_key: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        if not idempotency_key.strip():
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Batch imports require an Idempotency-Key header.")
        with self.persistence.transaction(organization_id) as transaction:
            knowledge_base = transaction.knowledge_bases.get(knowledge_base_id)
            self._required(knowledge_base, "KNOWLEDGE_BASE_NOT_FOUND", "Knowledge base does not exist.")
            work = transaction.outbox.enqueue(
                new_work_item(
                    organization_id=organization_id,
                    kind="knowledge_base.import",
                    aggregate_id=knowledge_base_id,
                    idempotency_key="knowledge-base.import:%s:%s" % (knowledge_base_id, idempotency_key),
                    payload={"knowledge_base_id": knowledge_base_id, "questions": deepcopy(questions)},
                )
            )
            if work["status"] == "pending":
                knowledge_base["status"] = "building"
                knowledge_base["updated_at"] = utc_now()
                transaction.knowledge_bases.update(knowledge_base, expected_version=knowledge_base["version"])
        return self._build_projection(work, knowledge_base_id, organization_id)

    def queue_rebuild(
        self,
        knowledge_base_id: str,
        *,
        reason: str,
        idempotency_key: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        if not idempotency_key.strip():
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Rebuilds require an Idempotency-Key header.")
        with self.persistence.transaction(organization_id) as transaction:
            knowledge_base = transaction.knowledge_bases.get(knowledge_base_id)
            self._required(knowledge_base, "KNOWLEDGE_BASE_NOT_FOUND", "Knowledge base does not exist.")
            work = transaction.outbox.enqueue(
                new_work_item(
                    organization_id=organization_id,
                    kind="knowledge_base.rebuild",
                    aggregate_id=knowledge_base_id,
                    idempotency_key="knowledge-base.rebuild:%s:%s" % (knowledge_base_id, idempotency_key),
                    payload={"knowledge_base_id": knowledge_base_id, "reason": reason},
                )
            )
            if work["status"] == "pending":
                knowledge_base["status"] = "building"
                knowledge_base["updated_at"] = utc_now()
                transaction.knowledge_bases.update(knowledge_base, expected_version=knowledge_base["version"])
        return self._build_projection(work, knowledge_base_id, organization_id)

    async def process_build_work(
        self, work_item_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            work = transaction.outbox.start(work_item_id)
        try:
            if work["kind"] == "knowledge_base.import":
                normalized: List[Dict[str, Any]] = []
                errors: List[Dict[str, Any]] = []
                for index, raw in enumerate(work["payload"].get("questions", [])):
                    try:
                        value = deepcopy(raw)
                        key_points = self._normalize_key_points(value.get("key_points", []))
                        self._validate_scoring_basis(value, key_points)
                        value["key_points"] = key_points
                        normalized.append(value)
                    except ApiError as exc:
                        errors.append({"index": index, "code": exc.code, "message": exc.message})
                if errors:
                    raise ApiError(
                        "KNOWLEDGE_BASE_IMPORT_INVALID",
                        "Batch import contains invalid questions.",
                        status_code=422,
                        details={"items": errors},
                    )
                created = []
                for value in normalized:
                    generation_batch_id = value.get("generation_batch_id")
                    generation_draft_id = value.get("generation_draft_id")
                    if generation_batch_id and generation_draft_id:
                        with self.persistence.transaction(organization_id) as transaction:
                            existing = next(
                                (
                                    item
                                    for item in transaction.questions.list()
                                    if item.get("generation_batch_id") == generation_batch_id
                                    and item.get("generation_draft_id") == generation_draft_id
                                ),
                                None,
                            )
                        if existing is not None:
                            created.append(existing)
                            continue
                    created.append(await self.create_question(work["aggregate_id"], value, organization_id))
            elif work["kind"] == "knowledge_base.rebuild":
                with self.persistence.transaction(organization_id) as transaction:
                    knowledge_base = transaction.knowledge_bases.get(work["aggregate_id"])
                    profile = deepcopy((knowledge_base or {}).get("speech_profile"))
                    questions = [
                        item
                        for item in transaction.questions.list()
                        if item.get("knowledge_base_id") == work["aggregate_id"] and item.get("status") == "active"
                    ]
                    for question in questions:
                        if profile is None:
                            continue
                        question["speech_status"] = "pending"
                        question["updated_at"] = utc_now()
                        question = transaction.questions.update(question, expected_version=question["version"])
                        speech_work = transaction.outbox.enqueue(
                            new_work_item(
                                organization_id=organization_id,
                                kind="question.speech.generate",
                                aggregate_id=question["id"],
                                idempotency_key="question.speech:%s:%s:%s"
                                % (question["id"], question["version"], profile["revision"]),
                                payload={
                                    "owner_type": "question",
                                    "owner_id": question["id"],
                                    "source_version": question["version"],
                                    "knowledge_base_id": work["aggregate_id"],
                                    "speech_profile": deepcopy(profile),
                                    "speech_profile_revision": profile["revision"],
                                    "parent_build_id": work_item_id,
                                },
                            )
                        )
            else:
                raise RuntimeError("Unsupported knowledge base build work: %s" % work["kind"])
            with self.persistence.transaction(organization_id) as transaction:
                current = transaction.outbox.get(work_item_id)
                self._refresh_knowledge_base(transaction, work["aggregate_id"])
                generation_batch_id = work.get("payload", {}).get("generation_batch_id")
                if generation_batch_id:
                    batch = transaction.question_generation_batches.get(generation_batch_id)
                    if batch is not None:
                        batch["imported_question_ids"] = [
                            item["id"]
                            for item in transaction.questions.list()
                            if item.get("generation_batch_id") == generation_batch_id
                        ]
                        if work.get("payload", {}).get("generation_import_scope") == "single":
                            draft_id = work.get("payload", {}).get("generation_draft_id")
                            question = next(
                                (
                                    item for item in transaction.questions.list()
                                    if item.get("generation_batch_id") == generation_batch_id
                                    and item.get("generation_draft_id") == draft_id
                                ),
                                None,
                            )
                            draft = next(
                                (item for item in batch.get("drafts", []) if item.get("id") == draft_id),
                                None,
                            )
                            if draft is not None:
                                draft["import_status"] = "imported"
                                draft["imported_question_id"] = (question or {}).get("id")
                                draft["import_error"] = None
                            batch["status"] = "reviewing"
                            batch["phase"] = "reviewing"
                        else:
                            batch["status"] = "imported"
                        batch["last_error"] = None
                        batch["updated_at"] = utc_now()
                        transaction.question_generation_batches.update(
                            batch, expected_version=batch["version"]
                        )
                transaction.outbox.complete(work_item_id, lease_token=current["lease_token"])
            return self.get_build(work_item_id, organization_id)
        except Exception as exc:
            with self.persistence.transaction(organization_id) as transaction:
                current = transaction.outbox.get(work_item_id)
                failed_work = None
                if current and current.get("status") == "running":
                    failed_work = transaction.outbox.fail(
                        work_item_id, str(exc), lease_token=current["lease_token"]
                    )
                knowledge_base = transaction.knowledge_bases.get(work["aggregate_id"])
                generation_batch_id = work.get("payload", {}).get("generation_batch_id")
                if generation_batch_id:
                    batch = transaction.question_generation_batches.get(generation_batch_id)
                    if batch is not None:
                        if work.get("payload", {}).get("generation_import_scope") == "single":
                            draft_id = work.get("payload", {}).get("generation_draft_id")
                            draft = next(
                                (item for item in batch.get("drafts", []) if item.get("id") == draft_id),
                                None,
                            )
                            if draft is not None:
                                draft["import_status"] = (
                                    "failed"
                                    if (failed_work or {}).get("status") == "dead_letter"
                                    else "importing"
                                )
                                draft["import_error"] = str(exc)[:1000]
                            batch["status"] = "reviewing"
                            batch["phase"] = "reviewing"
                        else:
                            batch["status"] = (
                                "failed"
                                if (failed_work or {}).get("status") == "dead_letter"
                                else "importing"
                            )
                            batch["last_error"] = str(exc)[:1000]
                        batch["updated_at"] = utc_now()
                        transaction.question_generation_batches.update(
                            batch, expected_version=batch["version"]
                        )
                if knowledge_base and (failed_work or {}).get("status") == "dead_letter":
                    knowledge_base["status"] = "failed"
                    knowledge_base["updated_at"] = utc_now()
                    transaction.knowledge_bases.update(knowledge_base, expected_version=knowledge_base["version"])
            raise

    def get_build(self, work_item_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            work = transaction.outbox.get(work_item_id)
            if work is None or work.get("kind") not in {"knowledge_base.import", "knowledge_base.rebuild"}:
                raise ApiError("KNOWLEDGE_BASE_BUILD_NOT_FOUND", "Knowledge base build does not exist.", status_code=404)
        return self._build_projection(work, work["aggregate_id"], organization_id)

    async def create_question(
        self,
        knowledge_base_id: str,
        payload: Dict[str, Any],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            knowledge_base = transaction.knowledge_bases.get(knowledge_base_id)
            self._required(knowledge_base, "KNOWLEDGE_BASE_NOT_FOUND", "Knowledge base does not exist.")
            profile = deepcopy(knowledge_base.get("speech_profile"))
            key_points = self._normalize_key_points(payload.get("key_points", []))
            self._validate_scoring_basis(payload, key_points)
            now = utc_now()
            question = transaction.questions.add(
                {
                    "id": new_id("q"),
                    "organization_id": organization_id,
                    "job_position_id": knowledge_base["job_position_id"],
                    "knowledge_base_id": knowledge_base_id,
                    "title": str(payload.get("title") or payload["question_text"][:40]).strip(),
                    "question_text": str(payload["question_text"]).strip(),
                    "standard_answer": str(payload["standard_answer"]).strip(),
                    "key_points": key_points,
                    "rubric": deepcopy(payload["rubric"]),
                    "skills": [normalize_skill(item) for item in payload.get("skills", []) if normalize_skill(item)],
                    "difficulty": payload.get("difficulty", "mid"),
                    "type": payload.get("type", "open_ended"),
                    "role_families": list(payload.get("role_families", [])),
                    "status": "active",
                    "validation_status": "valid",
                    "speech_status": "pending" if profile else "configuration_required",
                    "index_status": "not_required",
                    "content_hash": self._question_hash(payload, key_points),
                    "speech_asset_id": None,
                    "generation_batch_id": payload.get("generation_batch_id"),
                    "generation_draft_id": payload.get("generation_draft_id"),
                    "created_at": now,
                    "updated_at": now,
                }
            )
            knowledge_base["status"] = "building"
            if profile is None:
                knowledge_base["speech_build_status"] = "configuration_required"
                knowledge_base["status"] = "draft"
            knowledge_base["updated_at"] = now
            transaction.knowledge_bases.update(knowledge_base, expected_version=knowledge_base["version"])
            work = None
            if profile is not None:
                work = transaction.outbox.enqueue(
                    new_work_item(
                        organization_id=organization_id,
                        kind="question.speech.generate",
                        aggregate_id=question["id"],
                        idempotency_key="question.speech:%s:%s:%s"
                        % (question["id"], question["version"], profile["revision"]),
                        payload={
                            "owner_type": "question",
                            "owner_id": question["id"],
                            "source_version": question["version"],
                            "knowledge_base_id": knowledge_base_id,
                            "speech_profile": profile,
                            "speech_profile_revision": profile["revision"],
                        },
                    )
                )
        return {
            **question,
            "job_id": work["id"] if work else None,
            "work_item_id": work["id"] if work else None,
            "speech_configuration_required": profile is None,
        }

    async def process_speech_work(
        self, work_item_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            work = transaction.outbox.start(work_item_id)
            payload = work["payload"]
            owner_type = payload["owner_type"]
            appointment_id = payload.get("appointment_id")
            if appointment_id:
                appointment, owner = self._appointment_speech_context(
                    transaction,
                    payload,
                    work_item_id=work_item_id,
                )
                if owner is None:
                    transaction.outbox.complete(
                        work_item_id,
                        lease_token=work["lease_token"],
                        result_status="superseded",
                    )
                    if appointment is not None and appointment.get("status") != "cancelled":
                        self._update_appointment_speech_item(
                            transaction,
                            appointment["id"],
                            payload["owner_id"],
                            int(payload["source_version"]),
                            status="cancelled",
                        )
                    return {"id": payload["owner_id"], "status": "superseded"}
                self._update_appointment_speech_item(
                    transaction,
                    appointment["id"],
                    owner["id"],
                    int(owner["version"]),
                    status="building",
                )
            else:
                repository = (
                    transaction.questions
                    if owner_type == "question"
                    else transaction.experience_questions
                )
                owner = repository.get(payload["owner_id"])
            if owner is None:
                raise RuntimeError("Speech owner disappeared: %s" % payload["owner_id"])
            if int(owner["version"]) != int(payload["source_version"]):
                transaction.outbox.complete(
                    work_item_id, lease_token=work["lease_token"], result_status="superseded"
                )
                return owner
            if owner_type == "question" and owner.get("status") != "active":
                transaction.outbox.complete(
                    work_item_id, lease_token=work["lease_token"], result_status="superseded"
                )
                return owner
            if (
                owner_type == "experience_question"
                and not appointment_id
                and owner.get("status") != "approved"
            ):
                transaction.outbox.complete(
                    work_item_id, lease_token=work["lease_token"], result_status="superseded"
                )
                return owner
            profile = deepcopy(payload.get("speech_profile"))
            profile_revision = payload.get("speech_profile_revision")
            if owner_type == "question":
                knowledge_base = transaction.knowledge_bases.get(owner["knowledge_base_id"])
                if profile is None:
                    profile = deepcopy((knowledge_base or {}).get("speech_profile"))
                    profile_revision = (profile or {}).get("revision")
                current_revision = ((knowledge_base or {}).get("speech_profile") or {}).get("revision")
                if profile is not None and int(current_revision or -1) != int(profile_revision or -2):
                    transaction.outbox.complete(
                        work_item_id, lease_token=work["lease_token"], result_status="superseded"
                    )
                    return owner
                language = (profile or {}).get("language") or (
                    knowledge_base.get("language", "zh-CN") if knowledge_base else "zh-CN"
                )
                voice = (profile or {}).get("voice_profile_id") or (
                    knowledge_base.get("voice_profile_id", "voice_default_cn") if knowledge_base else "voice_default_cn"
                )
            else:
                language = (profile or {}).get("language") or owner.get("language", "zh-CN")
                voice = (profile or {}).get("voice_profile_id") or owner.get("voice_profile_id")

            if appointment_id and profile:
                existing_asset = next(
                    (
                        asset
                        for asset in transaction.question_speech_assets.list()
                        if asset.get("owner_type") == "experience_question"
                        and asset.get("owner_id") == owner["id"]
                        and int(asset.get("source_version", 0)) == int(owner["version"])
                        and asset.get("status") == "ready"
                        and speech_asset_matches_profile(asset, profile)
                    ),
                    None,
                )
                if existing_asset is not None:
                    appointment = transaction.interview_appointments.get(appointment_id)
                    if appointment is not None:
                        self._update_appointment_speech_item(
                            transaction,
                            appointment["id"],
                            owner["id"],
                            int(owner["version"]),
                            status="ready",
                            asset_id=existing_asset["id"],
                        )
                    transaction.outbox.complete(
                        work_item_id,
                        lease_token=work["lease_token"],
                        result_status="reused",
                    )
                    return existing_asset

        route = None
        if profile and profile.get("model_configuration_id"):
            route = {
                "id": "knowledge_base_speech_profile",
                "organization_id": organization_id,
                "capability": cap.TTS_SYNTHESIZE,
                "purpose": "question_speech_generation",
                "primary": {
                    "model_configuration_id": profile["model_configuration_id"],
                    "timeout_s": 30,
                },
                "fallbacks": [],
                "policy": {"retry_count": 1, "retry_backoff_ms": 250},
            }

        try:
            if appointment_id and (
                not profile
                or not profile.get("model_configuration_id")
                or not voice
            ):
                raise ApiError(
                    "APPOINTMENT_SPEECH_PROFILE_INVALID",
                    "Appointment speech work requires a frozen model and voice profile.",
                    status_code=409,
                )
            response = await self.gateway.invoke(
                cap.TTS_SYNTHESIZE,
                TTSSynthesizeRequest(
                    organization_id=organization_id,
                    purpose="question_speech_generation",
                    text=owner["question_text"],
                    language=language,
                    voice_profile_id=voice,
                    format=(profile or {}).get("audio_format", "audio/wav"),
                    speaking_rate=float((profile or {}).get("speaking_rate", 1.0)),
                    metadata={
                        "owner_type": owner_type,
                        "owner_id": owner["id"],
                        "source_version": owner["version"],
                        "appointment_id": appointment_id,
                    },
                ),
                route=route,
            )
        except Exception as exc:
            with self.persistence.transaction(organization_id) as transaction:
                failed = transaction.outbox.fail(
                    work_item_id,
                    str(exc),
                    lease_token=work["lease_token"],
                    error_code=getattr(exc, "code", exc.__class__.__name__),
                    retryable=(
                        False
                        if isinstance(exc, ApiError)
                        else getattr(exc, "retryable", None)
                    ),
                )
                terminal = failed.get("status") == "dead_letter"
                if appointment_id:
                    self._update_appointment_speech_item(
                        transaction,
                        appointment_id,
                        owner["id"],
                        int(owner["version"]),
                        status="failed" if terminal else "building",
                        error_code=failed.get("last_error_code") if terminal else None,
                    )
                elif terminal:
                    repository = (
                        transaction.questions
                        if owner_type == "question"
                        else transaction.experience_questions
                    )
                    current = repository.get(owner["id"])
                    if current is not None and current["version"] == owner["version"]:
                        current["speech_status"] = "failed"
                        current["speech_error"] = str(exc)[:500]
                        current["updated_at"] = utc_now()
                        repository.update(current, expected_version=current["version"])
            raise

        if owner_type == "question" and profile is not None:
            with self.persistence.transaction(organization_id) as transaction:
                current = transaction.questions.get(owner["id"])
                knowledge_base = transaction.knowledge_bases.get(owner["knowledge_base_id"])
                current_revision = ((knowledge_base or {}).get("speech_profile") or {}).get("revision")
                if (
                    current is None
                    or int(current["version"]) != int(owner["version"])
                    or int(current_revision or -1) != int(profile_revision or -2)
                ):
                    transaction.outbox.complete(
                        work_item_id, lease_token=work["lease_token"], result_status="superseded"
                    )
                    return current or owner
        elif owner_type == "experience_question" and appointment_id:
            with self.persistence.transaction(organization_id) as transaction:
                _, current_snapshot = self._appointment_speech_context(
                    transaction,
                    payload,
                    work_item_id=work_item_id,
                )
                if current_snapshot is None:
                    transaction.outbox.complete(
                        work_item_id,
                        lease_token=work["lease_token"],
                        result_status="superseded",
                    )
                    return owner
        elif owner_type == "experience_question":
            with self.persistence.transaction(organization_id) as transaction:
                current = transaction.experience_questions.get(owner["id"])
                if (
                    current is None
                    or int(current["version"]) != int(owner["version"])
                    or current.get("status") != "approved"
                ):
                    transaction.outbox.complete(
                        work_item_id, lease_token=work["lease_token"], result_status="superseded"
                    )
                    return current or owner

        private_file = None
        try:
            if response.provider.provider_id != "mock":
                content, checksum = await self.asset_importer.audio(response.audio_uri, response.content_type)
                private_file = self._private_storage().store(
                    organization_id=organization_id,
                    object_id=new_id("speech_file"),
                    content=content,
                    content_type=response.content_type,
                    checksum=checksum,
                )
        except Exception as exc:
            with self.persistence.transaction(organization_id) as transaction:
                failed = transaction.outbox.fail(
                    work_item_id,
                    str(exc),
                    lease_token=work["lease_token"],
                    error_code=getattr(exc, "code", exc.__class__.__name__),
                    retryable=getattr(exc, "retryable", None),
                )
                terminal = failed.get("status") == "dead_letter"
                if appointment_id:
                    self._update_appointment_speech_item(
                        transaction,
                        appointment_id,
                        owner["id"],
                        int(owner["version"]),
                        status="failed" if terminal else "building",
                        error_code=failed.get("last_error_code") if terminal else None,
                    )
                elif terminal:
                    repository = (
                        transaction.questions
                        if owner_type == "question"
                        else transaction.experience_questions
                    )
                    current = repository.get(owner["id"])
                    if current is not None and current["version"] == owner["version"]:
                        current["speech_status"] = "failed"
                        current["speech_error"] = str(exc)[:500]
                        current["updated_at"] = utc_now()
                        repository.update(current, expected_version=current["version"])
            raise

        with self.persistence.transaction(organization_id) as transaction:
            current = None
            if appointment_id:
                _, current_snapshot = self._appointment_speech_context(
                    transaction,
                    payload,
                    work_item_id=work_item_id,
                )
                if current_snapshot is None:
                    transaction.outbox.complete(
                        work_item_id,
                        lease_token=work["lease_token"],
                        result_status="superseded",
                    )
                    return owner
            else:
                repository = (
                    transaction.questions
                    if owner_type == "question"
                    else transaction.experience_questions
                )
                current = repository.get(owner["id"])
                if current is None:
                    raise RuntimeError("Speech owner disappeared: %s" % owner["id"])
                if owner_type == "question":
                    current_work = transaction.outbox.get(work_item_id)
                    knowledge_base = transaction.knowledge_bases.get(
                        current["knowledge_base_id"]
                    )
                    current_revision = (
                        (knowledge_base or {}).get("speech_profile") or {}
                    ).get("revision")
                    if (
                        current_work.get("cancel_requested")
                        or int(current["version"]) != int(owner["version"])
                        or int(current_revision or -1) != int(profile_revision or -2)
                    ):
                        transaction.outbox.complete(
                            work_item_id,
                            lease_token=work["lease_token"],
                            result_status="superseded",
                        )
                        return current
            now = utc_now()
            file_object = None
            if private_file is not None:
                file_object = transaction.file_objects.add(
                    {
                        "id": new_id("file"),
                        "organization_id": organization_id,
                        "purpose": "question_speech",
                        "status": "ready",
                        "storage_backend": private_file.storage_backend,
                        "object_key": private_file.object_key,
                        "content_type": private_file.content_type,
                        "checksum": private_file.checksum,
                        "byte_count": private_file.byte_count,
                        "scan_status": "not_applicable",
                        "source_type": "model_provider_copy",
                        "created_at": now,
                        "updated_at": now,
                    }
                )
            asset = transaction.question_speech_assets.add(
                {
                    "id": new_id("speech"),
                    "organization_id": organization_id,
                    "owner_type": owner_type,
                    "owner_id": owner["id"],
                    "source_version": owner["version"],
                    "language": language,
                    "voice_profile_id": voice,
                    "speech_profile_revision": profile_revision,
                    "model_configuration_id": (profile or {}).get("model_configuration_id"),
                    "model_configuration_version": (profile or {}).get("model_configuration_version"),
                    "speech_profile_fingerprint": (
                        speech_profile_fingerprint(profile) if profile else None
                    ),
                    "knowledge_base_speech_profile_revisions": deepcopy(
                        (profile or {}).get("knowledge_base_revisions", {})
                    ),
                    "audio_uri": (
                        "private-file://%s" % file_object["id"] if file_object else response.audio_uri
                    ),
                    "file_object_id": file_object["id"] if file_object else None,
                    "content_type": response.content_type,
                    "audio_format": response.content_type,
                    "speaking_rate": float((profile or {}).get("speaking_rate", 1.0)),
                    "duration_ms": response.duration_ms,
                    "content_hash": private_file.checksum if private_file else response.content_hash,
                    "provider": response.provider.model_dump(),
                    "status": "ready",
                    "production_ready": file_object is not None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            if appointment_id:
                self._update_appointment_speech_item(
                    transaction,
                    appointment_id,
                    owner["id"],
                    int(owner["version"]),
                    status="ready",
                    asset_id=asset["id"],
                )
                updated = asset
            else:
                current["speech_asset_id"] = asset["id"]
                current["speech_status"] = "ready"
                if owner_type == "question":
                    current["speech_profile_revision"] = profile_revision
                current["speech_error"] = None
                current["updated_at"] = now
                updated = repository.update(current, expected_version=current["version"])
            transaction.outbox.complete(work_item_id, lease_token=work["lease_token"])
            if owner_type == "question":
                self._refresh_knowledge_base(transaction, current["knowledge_base_id"])
            return updated

    def _appointment_speech_context(
        self,
        transaction: Any,
        payload: Dict[str, Any],
        *,
        work_item_id: str,
    ) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        appointment = transaction.interview_appointments.get(payload.get("appointment_id"))
        if appointment is None or appointment.get("status") not in {"registered", "consumed"}:
            return appointment, None
        plan = transaction.interview_plans.get(appointment.get("plan_id"))
        profile = payload.get("speech_profile") or {}
        frozen_profile = (plan or {}).get("speech_profile_snapshot") or {}
        expected_fingerprint = payload.get("speech_profile_fingerprint")
        if (
            not profile
            or speech_profile_fingerprint(profile) != expected_fingerprint
            or frozen_profile.get("fingerprint") != expected_fingerprint
        ):
            return appointment, None
        snapshot = next(
            (
                item
                for item in (plan or {}).get("experience_question_snapshots", [])
                if item.get("id") == payload.get("owner_id")
                and int(item.get("version", 0)) == int(payload.get("source_version", 0))
            ),
            None,
        )
        preparation_item = next(
            (
                item
                for item in (appointment.get("speech_preparation") or {}).get("items", [])
                if item.get("question_id") == payload.get("owner_id")
                and int(item.get("source_version", 0)) == int(payload.get("source_version", 0))
            ),
            None,
        )
        if (
            snapshot is None
            or preparation_item is None
            or preparation_item.get("work_item_id") != work_item_id
            or preparation_item.get("status") == "cancelled"
        ):
            return appointment, None
        return appointment, deepcopy(snapshot)

    @staticmethod
    def _update_appointment_speech_item(
        transaction: Any,
        appointment_id: str,
        question_id: str,
        source_version: int,
        *,
        status: str,
        asset_id: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        appointment = transaction.interview_appointments.get(appointment_id)
        if appointment is None:
            return None
        if appointment.get("status") == "cancelled" and status != "cancelled":
            return appointment
        updated_preparation = update_speech_preparation_item(
            appointment.get("speech_preparation") or {},
            question_id=question_id,
            source_version=source_version,
            status=status,
            asset_id=asset_id,
            error_code=error_code,
        )
        if updated_preparation == appointment.get("speech_preparation"):
            return appointment
        appointment["speech_preparation"] = updated_preparation
        appointment["updated_at"] = utc_now()
        return transaction.interview_appointments.update(
            appointment,
            expected_version=appointment["version"],
        )

    def issue_speech_access(
        self,
        asset_id: str,
        *,
        actor_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            asset = transaction.question_speech_assets.get(asset_id)
            self._required(asset, "QUESTION_SPEECH_ASSET_NOT_FOUND", "Question speech asset does not exist.")
            file_object = transaction.file_objects.get(asset.get("file_object_id")) if asset.get("file_object_id") else None
            if file_object is None or file_object.get("status") != "ready":
                provider_id = str((asset.get("provider") or {}).get("provider_id") or "")
                if provider_id == "mock" or str(asset.get("audio_uri") or "").startswith(
                    "mock-tts://"
                ):
                    raise ApiError(
                        "QUESTION_SPEECH_PREVIEW_UNAVAILABLE",
                        "这条题目语音由开发模拟模型生成，没有实际可播放的音频。请在题库顶部点击“配置语音”，选择已测试通过的语音模型和声音，然后重新生成。",
                        status_code=409,
                        details={
                            "reason": "development_mock_asset",
                            "action": "configure_knowledge_base_speech",
                            "owner_id": asset.get("owner_id"),
                        },
                    )
                raise ApiError(
                    "QUESTION_SPEECH_ASSET_NOT_PRIVATE",
                    "这条题目语音还没有保存为可试听的私有音频。请重新生成语音；如果仍然失败，请管理员检查私有文件存储配置。",
                    status_code=409,
                    details={
                        "reason": "private_audio_missing",
                        "action": "regenerate_question_speech",
                        "owner_id": asset.get("owner_id"),
                    },
                )
            grant = self._private_storage().issue_read_access(file_object["object_key"], expires_seconds=300)
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "question_speech.access_granted",
                    "resource_type": "question_speech_asset",
                    "resource_id": asset_id,
                    "metadata": {"file_object_id": file_object["id"], "expires_seconds": 300},
                    "created_at": utc_now(),
                }
            )
        url = grant if grant.startswith(("http://", "https://")) else "/api/v1/private-files/%s" % grant
        return {"url": url, "expires_in_seconds": 300, "content_type": file_object["content_type"]}

    def list_questions(
        self, knowledge_base_id: str, organization_id: str = "org_default"
    ) -> List[Dict[str, Any]]:
        self.get_knowledge_base(knowledge_base_id, organization_id)
        with self.persistence.transaction(organization_id) as transaction:
            return [
                self._question_projection(item, transaction)
                for item in transaction.questions.list()
                if item.get("knowledge_base_id") == knowledge_base_id
                and item.get("status") != "archived"
            ]

    def _default_speech_profile(
        self, transaction: Any, payload: Dict[str, Any], now: str
    ) -> Optional[Dict[str, Any]]:
        route = next(
            (
                item
                for item in transaction.model_routes.list()
                if item.get("enabled", True)
                and item.get("capability") == cap.TTS_SYNTHESIZE
                and item.get("purpose") == "question_speech_generation"
            ),
            None,
        )
        if route is not None:
            model_id = str((route.get("primary") or {}).get("model_configuration_id") or "")
            model = transaction.model_configurations.get(model_id) if model_id else None
            connection = (
                transaction.provider_connections.get(model.get("provider_connection_id"))
                if model
                else None
            )
            if (
                model
                and model.get("enabled", True)
                and model.get("status") == "ready"
                and cap.TTS_SYNTHESIZE in model.get("supported_capabilities", [])
                and connection
                and connection.get("enabled", True)
            ):
                settings = model.get("settings") or {}
                default_voice = str(settings.get("default_voice") or "voice_default_cn").strip()
                requested_voice = str(payload.get("voice_profile_id") or "").strip()
                voice_map = settings.get("voice_map") or {}
                voice = (
                    requested_voice
                    if requested_voice
                    and requested_voice != "voice_default_cn"
                    and (requested_voice == default_voice or requested_voice in voice_map)
                    else default_voice
                )
                return {
                    "model_configuration_id": model["id"],
                    "model_configuration_version": model["version"],
                    "voice_profile_id": voice,
                    "language": payload.get("language", "zh-CN"),
                    "audio_format": "audio/wav",
                    "speaking_rate": 1.0,
                    "revision": 1,
                    "source": "model_route_default",
                    "model_route_id": route["id"],
                    "configured_by": "model_route_default",
                    "configured_at": now,
                }

        if os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production":
            return None
        return {
            "model_configuration_id": "model_cfg_mock_tts_synthesize",
            "model_configuration_version": 1,
            "voice_profile_id": payload.get("voice_profile_id", "voice_default_cn"),
            "language": payload.get("language", "zh-CN"),
            "audio_format": "audio/wav",
            "speaking_rate": 1.0,
            "revision": 1,
            "source": "development_mock",
            "configured_by": "local_development",
            "configured_at": now,
        }

    @staticmethod
    def _question_projection(question: Dict[str, Any], transaction: Any) -> Dict[str, Any]:
        item = deepcopy(question)
        knowledge_base = transaction.knowledge_bases.get(item.get("knowledge_base_id"))
        profile = (knowledge_base or {}).get("speech_profile") or {}
        asset = (
            transaction.question_speech_assets.get(item.get("speech_asset_id"))
            if item.get("speech_asset_id")
            else None
        )
        file_object = (
            transaction.file_objects.get(asset.get("file_object_id"))
            if asset and asset.get("file_object_id")
            else None
        )
        available = bool(
            asset
            and asset.get("status") == "ready"
            and asset.get("production_ready")
            and file_object
            and file_object.get("status") == "ready"
        )
        if available:
            reason = None
            message = "语音已生成，可以试听。"
        elif profile.get("source") == "development_mock" or str(
            profile.get("model_configuration_id") or ""
        ).startswith("model_cfg_mock_"):
            reason = "development_mock_asset"
            message = "当前是开发模拟语音，没有实际音频。请先配置已测试通过的语音模型和声音。"
        elif not profile:
            reason = "speech_profile_required"
            message = "题库还没有配置语音模型。请点击页面顶部的“配置语音”。"
        elif item.get("speech_status") == "failed":
            reason = "speech_generation_failed"
            message = "读题语音生成失败，请检查题库语音配置后重试。"
        elif item.get("speech_status") in {"pending", "rebuilding"}:
            reason = "speech_generation_pending"
            message = "读题语音正在生成，完成后即可试听。"
        elif asset and not file_object:
            reason = "private_audio_missing"
            message = "语音文件没有保存到私有存储，请重新生成。"
        else:
            reason = "speech_asset_missing"
            message = "这道题还没有可试听的语音，请重新生成。"
        item["speech_preview"] = {
            "available": available,
            "reason": reason,
            "message": message,
        }
        return item

    def search_questions(self, payload: Dict[str, Any], organization_id: str = "org_default") -> Dict[str, Any]:
        query_tokens = set(tokenize(payload.get("query", "")))
        filters = payload.get("filters", {}) or {}
        position_id = payload.get("job_position_id") or filters.get("job_position_id")
        knowledge_base_ids = set(payload.get("knowledge_base_ids") or filters.get("knowledge_base_ids", []))
        if not position_id:
            raise ApiError("QUESTION_SEARCH_SCOPE_REQUIRED", "job_position_id is required.", status_code=422)
        if not knowledge_base_ids:
            raise ApiError("QUESTION_SEARCH_SCOPE_REQUIRED", "knowledge_base_ids cannot be empty.", status_code=422)
        skills = {normalize_skill(item) for item in filters.get("skills", [])}
        difficulty = set(filters.get("difficulty", []))
        question_types = set(filters.get("type", filters.get("types", [])))
        with self.persistence.transaction(organization_id) as transaction:
            position = transaction.job_positions.get(position_id)
            if position is None:
                raise ApiError("JOB_POSITION_NOT_FOUND", "Job position does not exist.", status_code=404)
            knowledge_bases = [transaction.knowledge_bases.get(item) for item in knowledge_base_ids]
            if any(item is None for item in knowledge_bases):
                raise ApiError("KNOWLEDGE_BASE_NOT_FOUND", "Every selected knowledge base must exist.", status_code=404)
            assigned = set(self._position_knowledge_base_ids(position, transaction.knowledge_bases.list()))
            if any(item["id"] not in assigned for item in knowledge_bases if item):
                raise ApiError(
                    "KNOWLEDGE_BASE_POSITION_MISMATCH",
                    "Knowledge base is not assigned to this position.",
                    status_code=409,
                )
            questions = transaction.questions.search_catalog(
                job_position_id=position_id,
                knowledge_base_ids=sorted(knowledge_base_ids),
                skills=sorted(skills),
                difficulties=sorted(difficulty),
                question_types=sorted(question_types),
            )
        items = []
        for question in questions:
            question_skills = set(question.get("skills", []))
            haystack = set(tokenize(" ".join([question.get("title", ""), question["question_text"], " ".join(question_skills)])))
            matched = query_tokens.intersection(haystack)
            if query_tokens and not matched:
                continue
            item = {
                "question_id": question["id"],
                "title": question["title"],
                "skills": question_skills and sorted(question_skills) or [],
                "difficulty": question["difficulty"],
                "match_reasons": ["命中结构化边界"] + (["命中关键词: %s" % ", ".join(sorted(matched))] if matched else []),
                "score": round(len(matched) / max(1, len(query_tokens)), 4) if query_tokens else 1.0,
            }
            if payload.get("include_answer"):
                item.update({key: deepcopy(question[key]) for key in ("question_text", "standard_answer", "key_points", "rubric")})
            items.append(item)
        items.sort(key=lambda item: (-item["score"], item["question_id"]))
        return {"items": items[: int(payload.get("limit", 20))], "next_cursor": None}

    def _refresh_knowledge_base(self, transaction: Any, knowledge_base_id: str) -> None:
        knowledge_base = transaction.knowledge_bases.get(knowledge_base_id)
        if knowledge_base is None:
            return
        questions = [
            item
            for item in transaction.questions.list()
            if item.get("knowledge_base_id") == knowledge_base_id and item.get("status") != "archived"
        ]
        valid = [item for item in questions if item.get("validation_status") == "valid"]
        profile = knowledge_base.get("speech_profile")
        current_revision = (profile or {}).get("revision")
        speech_ready = [
            item
            for item in valid
            if item.get("speech_status") == "ready"
            and (
                current_revision is None
                or int(item.get("speech_profile_revision", -1)) == int(current_revision)
            )
        ]
        failed = [item for item in questions if item.get("validation_status") == "invalid" or item.get("speech_status") == "failed"]
        knowledge_base["readiness"] = {
            "question_count": len(questions),
            "valid_count": len(valid),
            "speech_ready_count": len(speech_ready),
            "failed_count": len(failed),
        }
        if profile is None:
            knowledge_base["speech_build_status"] = "configuration_required"
            knowledge_base["status"] = "draft"
        elif questions and len(speech_ready) == len(questions):
            knowledge_base["speech_build_status"] = "ready"
            knowledge_base["status"] = "ready"
        elif failed:
            knowledge_base["speech_build_status"] = "failed"
            knowledge_base["status"] = "failed"
        else:
            knowledge_base["speech_build_status"] = "running"
            knowledge_base["status"] = "building"
        knowledge_base["updated_at"] = utc_now()
        transaction.knowledge_bases.update(knowledge_base, expected_version=knowledge_base["version"])

    def _normalize_key_points(self, values: List[Any]) -> List[Dict[str, Any]]:
        result = []
        for order, raw in enumerate(values, start=1):
            item = {"text": raw, "weight": 1.0, "aliases": []} if isinstance(raw, str) else dict(raw)
            text = str(item.get("text", "")).strip()
            if not text:
                raise ApiError("QUESTION_KEY_POINT_INVALID", "Key point text cannot be empty.")
            result.append({"id": new_id("kp"), "text": text, "weight": float(item.get("weight", 1.0)), "aliases": list(item.get("aliases", [])), "order": order})
        return result

    def _validate_scoring_basis(self, payload: Dict[str, Any], key_points: List[Dict[str, Any]]) -> None:
        missing = []
        for field in ("question_text", "standard_answer", "rubric"):
            if not payload.get(field):
                missing.append(field)
        if not key_points:
            missing.append("key_points")
        if not payload.get("skills"):
            missing.append("skills")
        if missing:
            raise ApiError(
                "QUESTION_SCORING_BASIS_INCOMPLETE",
                "Question is missing required scoring fields.",
                details={"fields": missing},
            )

    def _question_hash(self, payload: Dict[str, Any], key_points: List[Dict[str, Any]]) -> str:
        canonical = json.dumps(
            {"question_text": payload.get("question_text"), "standard_answer": payload.get("standard_answer"), "key_points": key_points, "rubric": payload.get("rubric")},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:%s" % hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _position_knowledge_base_ids(
        position: Dict[str, Any], knowledge_bases: List[Dict[str, Any]]
    ) -> List[str]:
        """Read explicit assignments while preserving legacy owner-based data."""
        assigned = {str(item) for item in position.get("knowledge_base_ids", []) if item}
        assigned.update(
            item["id"]
            for item in knowledge_bases
            if item.get("job_position_id") == position["id"]
        )
        return sorted(assigned)

    def _project_position(
        self, position: Dict[str, Any], knowledge_bases: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        item = deepcopy(position)
        item["knowledge_base_ids"] = self._position_knowledge_base_ids(item, knowledge_bases)
        return item

    @staticmethod
    def _candidate_ids_for_position(transaction: Any, position_id: str) -> List[str]:
        """Resolve explicit and legacy evidence of Position Candidate Membership."""
        candidate_ids = {
            item["id"]
            for item in transaction.candidate_profiles.list()
            if item.get("job_position_id") == position_id
            or position_id in item.get("job_position_ids", [])
        }
        candidate_ids.update(
            item["candidate_profile_id"]
            for item in transaction.resume_reviews.list()
            if item.get("job_position_id") == position_id and item.get("candidate_profile_id")
        )
        candidate_ids.update(
            item["candidate_profile_id"]
            for item in transaction.interview_plans.list()
            if item.get("job_position_id") == position_id and item.get("candidate_profile_id")
        )
        candidate_ids.update(
            item["candidate_profile_id"]
            for item in transaction.interview_appointments.list()
            if item.get("job_position_id") == position_id and item.get("candidate_profile_id")
        )
        candidate_ids.update(
            item.get("plan_snapshot", {}).get("candidate_profile_id")
            for item in transaction.interview_sessions.list()
            if item.get("plan_snapshot", {}).get("job_position_id") == position_id
            and item.get("plan_snapshot", {}).get("candidate_profile_id")
        )
        active_ids = {
            item["id"]
            for item in transaction.candidate_profiles.list()
            if item.get("status") != "retention_purged"
        }
        return sorted(candidate_ids.intersection(active_ids))

    def _required(self, item: Optional[Dict[str, Any]], code: str, message: str) -> Dict[str, Any]:
        if item is None:
            raise ApiError(code, message, status_code=404)
        return item

    def _build_projection(
        self, work: Dict[str, Any], knowledge_base_id: str, organization_id: str
    ) -> Dict[str, Any]:
        knowledge_base = self.get_knowledge_base(knowledge_base_id, organization_id)
        return {
            "id": work["id"],
            "kind": work["kind"],
            "status": work["status"],
            "attempt_count": work.get("attempt_count", 0),
            "last_error": work.get("last_error"),
            "knowledge_base_id": knowledge_base_id,
            "knowledge_base_status": knowledge_base["status"],
            "readiness": deepcopy(knowledge_base.get("readiness", {})),
            "created_at": work["created_at"],
            "updated_at": work["updated_at"],
        }

    def _private_storage(self) -> PrivateFileStorage:
        if self.storage is None:
            self.storage = private_file_storage()
        return self.storage
