import hashlib
import json
from copy import deepcopy
from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
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
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.gateway = gateway or ModelGateway(store, persistence=self.persistence)
        self.storage = storage
        self.asset_importer = asset_importer or PrivateAssetImporter()

    def create_position(self, payload: Dict[str, Any], organization_id: str = "org_default") -> Dict[str, Any]:
        code = str(payload["code"]).strip().lower()
        with self.persistence.transaction(organization_id) as transaction:
            if any(item["code"] == code for item in transaction.job_positions.list()):
                raise ApiError("JOB_POSITION_CODE_CONFLICT", "Job position code already exists.", status_code=409)
            now = utc_now()
            return transaction.job_positions.add(
                {
                    "id": new_id("position"),
                    "organization_id": organization_id,
                    "code": code,
                    "name": str(payload["name"]).strip(),
                    "description": str(payload.get("description", "")).strip(),
                    "status": payload.get("status", "active"),
                    "created_at": now,
                    "updated_at": now,
                }
            )

    def list_positions(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return transaction.job_positions.list()

    def get_position(self, position_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.job_positions.get(position_id)
        return self._required(item, "JOB_POSITION_NOT_FOUND", "Job position does not exist.")

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

    def create_knowledge_base(
        self, position_id: str, payload: Dict[str, Any], organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            position = transaction.job_positions.get(position_id)
            self._required(position, "JOB_POSITION_NOT_FOUND", "Job position does not exist.")
            now = utc_now()
            return transaction.knowledge_bases.add(
                {
                    "id": new_id("kb"),
                    "organization_id": organization_id,
                    "job_position_id": position_id,
                    "name": str(payload["name"]).strip(),
                    "description": str(payload.get("description", "")).strip(),
                    "language": payload.get("language", "zh-CN"),
                    "voice_profile_id": payload.get("voice_profile_id", "voice_default_cn"),
                    "status": "draft",
                    "readiness": {"question_count": 0, "valid_count": 0, "speech_ready_count": 0},
                    "created_at": now,
                    "updated_at": now,
                }
            )

    def list_knowledge_bases(
        self, position_id: str, organization_id: str = "org_default"
    ) -> List[Dict[str, Any]]:
        self.get_position(position_id, organization_id)
        with self.persistence.transaction(organization_id) as transaction:
            return [
                item
                for item in transaction.knowledge_bases.list()
                if item["job_position_id"] == position_id
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
            for field in ("name", "description", "language", "voice_profile_id", "status"):
                if field in payload and payload[field] is not None:
                    item[field] = payload[field]
            item["updated_at"] = utc_now()
            return transaction.knowledge_bases.update(item, expected_version=expected_version)

    def get_question(self, question_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            item = transaction.questions.get(question_id)
        return self._required(item, "QUESTION_NOT_FOUND", "Question does not exist.")

    def patch_question(
        self, question_id: str, payload: Dict[str, Any], organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        expected_version = int(payload["expected_version"])
        with self.persistence.transaction(organization_id) as transaction:
            question = transaction.questions.get(question_id)
            self._required(question, "QUESTION_NOT_FOUND", "Question does not exist.")
            if question.get("status") == "archived" and payload.get("status") not in {None, "archived"}:
                raise ApiError("QUESTION_ARCHIVED", "Archived questions cannot be reactivated.", status_code=409)
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
            spoken_changed = any(field in payload for field in speech_fields)
            if spoken_changed and question.get("status") != "archived":
                question["speech_status"] = "pending"
            question["updated_at"] = utc_now()
            updated = transaction.questions.update(question, expected_version=expected_version)
            if spoken_changed and updated.get("status") != "archived":
                transaction.outbox.enqueue(
                    new_work_item(
                        organization_id=organization_id,
                        kind="question.speech.generate",
                        aggregate_id=updated["id"],
                        idempotency_key="question.speech:%s:%s" % (updated["id"], updated["version"]),
                        payload={"owner_type": "question", "owner_id": updated["id"], "source_version": updated["version"]},
                    )
                )
            self._refresh_knowledge_base(transaction, updated["knowledge_base_id"])
            return updated

    async def regenerate_question_speech(
        self, question_id: str, *, expected_version: int, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            question = transaction.questions.get(question_id)
            self._required(question, "QUESTION_NOT_FOUND", "Question does not exist.")
            if question.get("status") != "active":
                raise ApiError("QUESTION_NOT_ACTIVE", "Only an active question can regenerate speech.", status_code=409)
            question["speech_status"] = "pending"
            question["updated_at"] = utc_now()
            question = transaction.questions.update(question, expected_version=expected_version)
            work = transaction.outbox.enqueue(
                new_work_item(
                    organization_id=organization_id,
                    kind="question.speech.generate",
                    aggregate_id=question["id"],
                    idempotency_key="question.speech:%s:%s" % (question["id"], question["version"]),
                    payload={"owner_type": "question", "owner_id": question["id"], "source_version": question["version"]},
                )
            )
        return await self.process_speech_work(work["id"], organization_id)

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
                    created.append(await self.create_question(work["aggregate_id"], value, organization_id))
            elif work["kind"] == "knowledge_base.rebuild":
                with self.persistence.transaction(organization_id) as transaction:
                    questions = [
                        item
                        for item in transaction.questions.list()
                        if item.get("knowledge_base_id") == work["aggregate_id"] and item.get("status") == "active"
                    ]
                    speech_work_ids = []
                    for question in questions:
                        question["speech_status"] = "pending"
                        question["updated_at"] = utc_now()
                        question = transaction.questions.update(question, expected_version=question["version"])
                        speech_work = transaction.outbox.enqueue(
                            new_work_item(
                                organization_id=organization_id,
                                kind="question.speech.generate",
                                aggregate_id=question["id"],
                                idempotency_key="question.speech:%s:%s" % (question["id"], question["version"]),
                                payload={
                                    "owner_type": "question",
                                    "owner_id": question["id"],
                                    "source_version": question["version"],
                                },
                            )
                        )
                        speech_work_ids.append(speech_work["id"])
                for speech_work_id in speech_work_ids:
                    await self.process_speech_work(speech_work_id, organization_id)
            else:
                raise RuntimeError("Unsupported knowledge base build work: %s" % work["kind"])
            with self.persistence.transaction(organization_id) as transaction:
                current = transaction.outbox.get(work_item_id)
                self._refresh_knowledge_base(transaction, work["aggregate_id"])
                transaction.outbox.complete(work_item_id, lease_token=current["lease_token"])
            return self.get_build(work_item_id, organization_id)
        except Exception as exc:
            with self.persistence.transaction(organization_id) as transaction:
                current = transaction.outbox.get(work_item_id)
                knowledge_base = transaction.knowledge_bases.get(work["aggregate_id"])
                if knowledge_base:
                    knowledge_base["status"] = "failed"
                    knowledge_base["updated_at"] = utc_now()
                    transaction.knowledge_bases.update(knowledge_base, expected_version=knowledge_base["version"])
                if current and current.get("status") == "running":
                    transaction.outbox.fail(work_item_id, str(exc), lease_token=current["lease_token"])
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
                    "speech_status": "pending",
                    "index_status": "not_required",
                    "content_hash": self._question_hash(payload, key_points),
                    "speech_asset_id": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            knowledge_base["status"] = "building"
            knowledge_base["updated_at"] = now
            transaction.knowledge_bases.update(knowledge_base, expected_version=knowledge_base["version"])
            work = transaction.outbox.enqueue(
                new_work_item(
                    organization_id=organization_id,
                    kind="question.speech.generate",
                    aggregate_id=question["id"],
                    idempotency_key="question.speech:%s:%s" % (question["id"], question["version"]),
                    payload={"owner_type": "question", "owner_id": question["id"], "source_version": question["version"]},
                )
            )
        return await self.process_speech_work(work["id"], organization_id)

    async def process_speech_work(
        self, work_item_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            work = transaction.outbox.start(work_item_id)
            payload = work["payload"]
            owner_type = payload["owner_type"]
            repository = (
                transaction.questions if owner_type == "question" else transaction.experience_questions
            )
            owner = repository.get(payload["owner_id"])
            if owner is None:
                raise RuntimeError("Speech owner disappeared: %s" % payload["owner_id"])
            if int(owner["version"]) != int(payload["source_version"]):
                transaction.outbox.complete(work_item_id, lease_token=work["lease_token"])
                return owner
            if owner_type == "question":
                knowledge_base = transaction.knowledge_bases.get(owner["knowledge_base_id"])
                language = knowledge_base.get("language", "zh-CN") if knowledge_base else "zh-CN"
                voice = knowledge_base.get("voice_profile_id", "voice_default_cn") if knowledge_base else "voice_default_cn"
            else:
                language = owner.get("language", "zh-CN")
                voice = owner.get("voice_profile_id", "voice_default_cn")

        try:
            response = await self.gateway.invoke(
                cap.TTS_SYNTHESIZE,
                TTSSynthesizeRequest(
                    organization_id=organization_id,
                    purpose="question_speech_generation",
                    text=owner["question_text"],
                    language=language,
                    voice_profile_id=voice,
                    metadata={"owner_type": owner_type, "owner_id": owner["id"], "source_version": owner["version"]},
                ),
            )
        except Exception as exc:
            with self.persistence.transaction(organization_id) as transaction:
                repository = transaction.questions if owner_type == "question" else transaction.experience_questions
                current = repository.get(owner["id"])
                if current is not None and current["version"] == owner["version"]:
                    current["speech_status"] = "failed"
                    current["speech_error"] = str(exc)[:500]
                    current["updated_at"] = utc_now()
                    repository.update(current, expected_version=current["version"])
                transaction.outbox.fail(work_item_id, str(exc), lease_token=work["lease_token"])
            raise

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
                repository = transaction.questions if owner_type == "question" else transaction.experience_questions
                current = repository.get(owner["id"])
                if current is not None and current["version"] == owner["version"]:
                    current["speech_status"] = "failed"
                    current["speech_error"] = str(exc)[:500]
                    current["updated_at"] = utc_now()
                    repository.update(current, expected_version=current["version"])
                transaction.outbox.fail(work_item_id, str(exc), lease_token=work["lease_token"])
            raise

        with self.persistence.transaction(organization_id) as transaction:
            repository = transaction.questions if owner_type == "question" else transaction.experience_questions
            current = repository.get(owner["id"])
            if current is None:
                raise RuntimeError("Speech owner disappeared: %s" % owner["id"])
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
                    "audio_uri": (
                        "private-file://%s" % file_object["id"] if file_object else response.audio_uri
                    ),
                    "file_object_id": file_object["id"] if file_object else None,
                    "content_type": response.content_type,
                    "duration_ms": response.duration_ms,
                    "content_hash": private_file.checksum if private_file else response.content_hash,
                    "provider": response.provider.model_dump(),
                    "status": "ready",
                    "production_ready": file_object is not None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            current["speech_asset_id"] = asset["id"]
            current["speech_status"] = "ready"
            current["speech_error"] = None
            current["updated_at"] = now
            updated = repository.update(current, expected_version=current["version"])
            transaction.outbox.complete(work_item_id, lease_token=work["lease_token"])
            if owner_type == "question":
                self._refresh_knowledge_base(transaction, current["knowledge_base_id"])
            return updated

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
                raise ApiError(
                    "QUESTION_SPEECH_ASSET_NOT_PRIVATE",
                    "Question speech is not available as a private production asset.",
                    status_code=409,
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
            return [item for item in transaction.questions.list() if item.get("knowledge_base_id") == knowledge_base_id]

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
            if any(item["job_position_id"] != position_id for item in knowledge_bases if item):
                raise ApiError(
                    "KNOWLEDGE_BASE_POSITION_MISMATCH",
                    "Knowledge base belongs to another position.",
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
        speech_ready = [item for item in valid if item.get("speech_status") == "ready"]
        failed = [item for item in questions if item.get("validation_status") == "invalid" or item.get("speech_status") == "failed"]
        knowledge_base["readiness"] = {
            "question_count": len(questions),
            "valid_count": len(valid),
            "speech_ready_count": len(speech_ready),
            "failed_count": len(failed),
        }
        knowledge_base["status"] = "ready" if questions and len(speech_ready) == len(questions) else ("failed" if failed else "building")
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
