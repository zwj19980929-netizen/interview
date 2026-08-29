import hashlib
import re
from copy import deepcopy
from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.prompt.contracts import prompt_contract
from app.core.time import utc_now
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import ChatJSONRequest
from app.persistence.errors import ConcurrencyConflict
from app.persistence.interface import Persistence, new_work_item
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.text import normalize_skill


DEFAULT_RUBRIC = {
    "semantic_weight": 0.45,
    "key_point_weight": 0.35,
    "communication_weight": 0.2,
}


class QuestionGenerationService:
    """Owns AI-authored question drafts until a human confirms their import."""

    PLAN_KIND = "question_generation.plan"
    LEGACY_GENERATE_KIND = "question_generation.generate"
    CHUNK_KIND = "question_generation.generate_chunk"
    MERGE_KIND = "question_generation.merge"
    CHUNK_SIZE = 2
    OUTPUT_TOKENS_PER_SLOT = 4000
    MAX_CHUNK_OUTPUT_TOKENS = 8000
    MAX_REFILL_ROUNDS = 2
    # Blueprint uniqueness carries the semantic diversity guarantee for sibling
    # questions. Text comparison is intentionally stricter so a shared question
    # template does not make different blueprint topics look like duplicates.
    SIMILARITY_THRESHOLD = 0.90

    def __init__(
        self,
        store: InMemoryStore,
        *,
        persistence: Optional[Persistence] = None,
        gateway: Optional[ModelGateway] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.gateway = gateway or ModelGateway(store, persistence=self.persistence)

    def options(
        self, knowledge_base_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            knowledge_base = transaction.knowledge_bases.get(knowledge_base_id)
            self._required_bank(knowledge_base)
            position = transaction.job_positions.get(knowledge_base["job_position_id"])
            questions = [
                item
                for item in transaction.questions.list()
                if item.get("knowledge_base_id") == knowledge_base_id
                and item.get("status") == "active"
            ]
            models = [
                self._model_projection(item)
                for item in transaction.model_configurations.list()
                if cap.LLM_CHAT_JSON in item.get("supported_capabilities", [])
            ]
        inferred_tags = sorted(
            {
                skill
                for question in questions
                for skill in question.get("skills", [])
                if str(skill).strip()
            }
        )
        positioning = str(
            knowledge_base.get("positioning")
            or knowledge_base.get("description")
            or (position or {}).get("description")
            or (position or {}).get("name")
            or ""
        ).strip()
        tags = self._normalize_tags(knowledge_base.get("tags") or inferred_tags)
        candidates = sorted(models, key=lambda item: (not item["selectable"], item["display_name"]))
        return {
            "knowledge_base_id": knowledge_base_id,
            "positioning": positioning,
            "tags": tags,
            "position": deepcopy(position),
            "items": [item for item in candidates if item["selectable"]],
            "candidates": candidates,
        }

    def queue(
        self,
        knowledge_base_id: str,
        payload: Dict[str, Any],
        *,
        idempotency_key: str,
        actor_id: str = "admin_local",
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        if not idempotency_key.strip():
            raise ApiError(
                "IDEMPOTENCY_KEY_REQUIRED",
                "Question generation requires an Idempotency-Key header.",
            )
        target_count = min(30, max(1, int(payload.get("target_count", 10))))
        with self.persistence.transaction(organization_id) as transaction:
            for existing in transaction.question_generation_batches.list():
                if (
                    existing.get("knowledge_base_id") == knowledge_base_id
                    and existing.get("idempotency_key") == idempotency_key
                ):
                    return self._projection(existing, transaction)
            knowledge_base = transaction.knowledge_bases.get(knowledge_base_id)
            self._required_bank(knowledge_base)
            position = transaction.job_positions.get(knowledge_base["job_position_id"])
            model = transaction.model_configurations.get(payload["model_configuration_id"])
            self._require_generation_model(model)
            positioning = str(
                payload.get("positioning")
                or knowledge_base.get("positioning")
                or knowledge_base.get("description")
                or (position or {}).get("description")
                or (position or {}).get("name")
                or ""
            ).strip()
            tags = self._normalize_tags(payload.get("tags") or knowledge_base.get("tags") or [])
            if not positioning and not tags:
                raise ApiError(
                    "QUESTION_GENERATION_CONTEXT_REQUIRED",
                    "Provide knowledge base positioning or at least one tag before generating questions.",
                    status_code=422,
                )
            now = utc_now()
            batch_id = new_id("question_gen")
            generation_work = new_work_item(
                organization_id=organization_id,
                kind=self.PLAN_KIND,
                aggregate_id=batch_id,
                idempotency_key="question-generation:%s:%s" % (knowledge_base_id, idempotency_key),
                payload={"batch_id": batch_id, "execution_revision": 1},
            )
            generation_work["retry_base_seconds"] = max(
                5, int(generation_work.get("retry_base_seconds", 0))
            )
            work = transaction.outbox.enqueue(generation_work)
            batch = transaction.question_generation_batches.add(
                {
                    "id": batch_id,
                    "organization_id": organization_id,
                    "knowledge_base_id": knowledge_base_id,
                    "job_position_id": knowledge_base["job_position_id"],
                    "model_configuration_id": model["id"],
                    "model_configuration_version": model["version"],
                    "target_count": target_count,
                    "context_snapshot": {
                        "knowledge_base_name": knowledge_base["name"],
                        "position_name": (position or {}).get("name", ""),
                        "positioning": positioning,
                        "tags": tags,
                        "requirements": str(payload.get("requirements") or "").strip(),
                    },
                    "status": "queued",
                    "phase": "queued",
                    "execution_revision": 1,
                    "stop_requested_at": None,
                    "stop_requested_by": None,
                    "stop_reason": None,
                    "control_history": [],
                    "blueprints": [],
                    "generation_chunks": [],
                    "merge_work_item_ids": [],
                    "refill_round": 0,
                    "rejections": [],
                    "drafts": [],
                    "generated_count": 0,
                    "prompt_version": None,
                    "prompt_versions": {},
                    "provider_runs": [],
                    "generation_warning": None,
                    "generation_work_item_id": work["id"],
                    "import_work_item_id": None,
                    "draft_import_work_item_ids": [],
                    "imported_question_ids": [],
                    "idempotency_key": idempotency_key,
                    "created_by": actor_id,
                    "last_error": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            if positioning != knowledge_base.get("positioning") or tags != knowledge_base.get("tags"):
                knowledge_base["positioning"] = positioning
                knowledge_base["tags"] = tags
                knowledge_base["updated_at"] = now
                transaction.knowledge_bases.update(
                    knowledge_base, expected_version=knowledge_base["version"]
                )
            return self._projection(batch, transaction)

    def list_batches(
        self, knowledge_base_id: str, organization_id: str = "org_default"
    ) -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            self._required_bank(transaction.knowledge_bases.get(knowledge_base_id))
            items = [
                self._projection(item, transaction)
                for item in transaction.question_generation_batches.list()
                if item.get("knowledge_base_id") == knowledge_base_id
            ]
        return sorted(items, key=lambda item: str(item.get("created_at", "")), reverse=True)

    def get_batch(
        self, batch_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            batch = transaction.question_generation_batches.get(batch_id)
            self._required_batch(batch)
            return self._projection(batch, transaction)

    def stop(
        self,
        batch_id: str,
        *,
        expected_version: int,
        reason: str,
        idempotency_key: str,
        actor_id: str = "admin_local",
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        self._require_command_key(idempotency_key)
        with self.persistence.transaction(organization_id) as transaction:
            batch = transaction.question_generation_batches.get(batch_id)
            self._required_batch(batch)
            if self._control_replayed(batch, "stop", idempotency_key):
                return self._projection(batch, transaction)
            if batch.get("status") not in {"queued", "generating", "failed", "stopping", "stopped"}:
                raise ApiError(
                    "QUESTION_GENERATION_BATCH_NOT_STOPPABLE",
                    "Only a queued, generating or failed generation batch can be stopped.",
                    status_code=409,
                )
            self._require_version(batch, expected_version)
            if batch.get("status") not in {"stopping", "stopped"}:
                batch["execution_revision"] = int(batch.get("execution_revision", 1)) + 1
                now = utc_now()
                batch["stop_requested_at"] = now
                batch["stop_requested_by"] = actor_id
                batch["stop_reason"] = reason
                running = False
                for work_id in self._generation_work_ids(batch):
                    work = transaction.outbox.get(work_id)
                    if not work or work.get("status") in {"completed", "cancelled"}:
                        continue
                    cancelled = transaction.outbox.cancel(
                        work_id, reason=reason, actor_id=actor_id
                    )
                    running = running or cancelled.get("status") == "running"
                for chunk in batch.get("generation_chunks", []):
                    if chunk.get("status") == "completed":
                        continue
                    work = transaction.outbox.get(chunk.get("work_item_id"))
                    chunk["status"] = "stopping" if work and work.get("status") == "running" else "cancelled"
                    chunk["cancelled_at"] = now
                batch["status"] = "stopping" if running else "stopped"
                batch["phase"] = batch["status"]
            self._record_control(batch, "stop", idempotency_key, actor_id, reason)
            batch["updated_at"] = utc_now()
            updated = transaction.question_generation_batches.update(
                batch, expected_version=expected_version
            )
            return self._projection(updated, transaction)

    def resume(
        self,
        batch_id: str,
        *,
        expected_version: int,
        reason: str,
        idempotency_key: str,
        actor_id: str = "admin_local",
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        self._require_command_key(idempotency_key)
        with self.persistence.transaction(organization_id) as transaction:
            batch = transaction.question_generation_batches.get(batch_id)
            self._required_batch(batch)
            if self._control_replayed(batch, "resume", idempotency_key):
                return self._projection(batch, transaction)
            if batch.get("status") != "stopped":
                raise ApiError(
                    "QUESTION_GENERATION_BATCH_NOT_RESUMABLE",
                    "Only a stopped generation batch can continue.",
                    status_code=409,
                )
            self._require_version(batch, expected_version)
            batch["execution_revision"] = int(batch.get("execution_revision", 1)) + 1
            batch["stop_requested_at"] = None
            batch["stop_requested_by"] = None
            batch["stop_reason"] = None
            batch["last_error"] = None
            revision = batch["execution_revision"]
            if not batch.get("blueprints"):
                work = self._enqueue_plan(transaction, batch, idempotency_key)
                batch["generation_work_item_id"] = work["id"]
                batch["status"] = "queued"
                batch["phase"] = "queued"
            else:
                completed_slots = {
                    candidate.get("blueprint_slot_id")
                    for chunk in batch.get("generation_chunks", [])
                    if chunk.get("status") == "completed"
                    for candidate in chunk.get("candidates", [])
                }
                missing = [
                    blueprint
                    for blueprint in batch["blueprints"]
                    if blueprint.get("slot_id") not in completed_slots
                ]
                for chunk in batch.get("generation_chunks", []):
                    if chunk.get("status") != "completed":
                        chunk["status"] = "superseded"
                        chunk["superseded_at"] = utc_now()
                if missing:
                    batch.setdefault("generation_chunks", []).extend(
                        self._enqueue_chunks(
                            transaction,
                            batch,
                            missing,
                            round_number=int(batch.get("refill_round", 0)),
                            command_token=idempotency_key,
                        )
                    )
                    batch["phase"] = "generating"
                else:
                    merge = self._enqueue_merge_work(
                        transaction, batch, command_token=idempotency_key
                    )
                    batch.setdefault("merge_work_item_ids", []).append(merge["id"])
                    batch["phase"] = "merging"
                batch["status"] = "generating"
            self._record_control(batch, "resume", idempotency_key, actor_id, reason)
            batch["updated_at"] = utc_now()
            updated = transaction.question_generation_batches.update(
                batch, expected_version=expected_version
            )
            return self._projection(updated, transaction)

    def retry_failed(
        self,
        batch_id: str,
        *,
        expected_version: int,
        reason: str,
        idempotency_key: str,
        actor_id: str = "admin_local",
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        self._require_command_key(idempotency_key)
        with self.persistence.transaction(organization_id) as transaction:
            batch = transaction.question_generation_batches.get(batch_id)
            self._required_batch(batch)
            if self._control_replayed(batch, "retry_failed", idempotency_key):
                return self._projection(batch, transaction)
            if batch.get("status") != "failed":
                raise ApiError(
                    "QUESTION_GENERATION_BATCH_NOT_RETRYABLE",
                    "Only a failed generation batch can retry failed work.",
                    status_code=409,
                )
            self._require_version(batch, expected_version)
            queued = 0
            if not batch.get("blueprints"):
                work = self._enqueue_plan(transaction, batch, idempotency_key)
                batch["generation_work_item_id"] = work["id"]
                batch["phase"] = "planning"
                queued = 1
            else:
                failed_chunks = [
                    chunk
                    for chunk in batch.get("generation_chunks", [])
                    if chunk.get("status") == "failed"
                ]
                for chunk in failed_chunks:
                    chunk["status"] = "superseded"
                    chunk["superseded_at"] = utc_now()
                    batch.setdefault("generation_chunks", []).extend(
                        self._enqueue_chunks(
                            transaction,
                            batch,
                            chunk.get("blueprints", []),
                            round_number=int(chunk.get("round", 0)),
                            command_token="%s:%s" % (idempotency_key, chunk["id"]),
                        )
                    )
                    queued += 1
                if not failed_chunks and self._merge_retryable(batch, transaction):
                    merge = self._enqueue_merge_work(
                        transaction, batch, command_token=idempotency_key
                    )
                    batch.setdefault("merge_work_item_ids", []).append(merge["id"])
                    batch["phase"] = "merging"
                    queued = 1
            if not queued:
                raise ApiError(
                    "QUESTION_GENERATION_NO_FAILED_WORK",
                    "This batch has no failed generation work to retry.",
                    status_code=409,
                )
            batch["status"] = "generating"
            if batch.get("phase") not in {"planning", "merging"}:
                batch["phase"] = "generating"
            batch["last_error"] = None
            self._record_control(batch, "retry_failed", idempotency_key, actor_id, reason)
            batch["updated_at"] = utc_now()
            updated = transaction.question_generation_batches.update(
                batch, expected_version=expected_version
            )
            return self._projection(updated, transaction)

    def retry_chunk(
        self,
        batch_id: str,
        chunk_id: str,
        *,
        expected_version: int,
        reason: str,
        idempotency_key: str,
        actor_id: str = "admin_local",
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        self._require_command_key(idempotency_key)
        action = "retry_chunk:%s" % chunk_id
        with self.persistence.transaction(organization_id) as transaction:
            batch = transaction.question_generation_batches.get(batch_id)
            self._required_batch(batch)
            if self._control_replayed(batch, action, idempotency_key):
                return self._projection(batch, transaction)
            if batch.get("status") != "failed":
                raise ApiError(
                    "QUESTION_GENERATION_CHUNK_NOT_RETRYABLE",
                    "A chunk can be retried only after the batch has failed.",
                    status_code=409,
                )
            self._require_version(batch, expected_version)
            chunk = self._chunk(batch, chunk_id)
            if chunk.get("status") != "failed":
                raise ApiError(
                    "QUESTION_GENERATION_CHUNK_NOT_RETRYABLE",
                    "Only a failed generation chunk can be retried.",
                    status_code=409,
                )
            chunk["status"] = "superseded"
            chunk["superseded_at"] = utc_now()
            batch.setdefault("generation_chunks", []).extend(
                self._enqueue_chunks(
                    transaction,
                    batch,
                    chunk.get("blueprints", []),
                    round_number=int(chunk.get("round", 0)),
                    command_token=idempotency_key,
                )
            )
            batch["status"] = "generating"
            batch["phase"] = "generating"
            batch["last_error"] = None
            self._record_control(batch, action, idempotency_key, actor_id, reason)
            batch["updated_at"] = utc_now()
            updated = transaction.question_generation_batches.update(
                batch, expected_version=expected_version
            )
            return self._projection(updated, transaction)

    def patch_draft(
        self,
        batch_id: str,
        draft_id: str,
        payload: Dict[str, Any],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        expected_version = int(payload["expected_version"])
        with self.persistence.transaction(organization_id) as transaction:
            batch = transaction.question_generation_batches.get(batch_id)
            self._required_batch(batch)
            self._require_reviewing(batch)
            if batch["version"] != expected_version:
                raise ConcurrencyConflict(
                    "QuestionGenerationBatch %s expected version %s, found %s"
                    % (batch_id, expected_version, batch["version"])
                )
            index = next(
                (idx for idx, item in enumerate(batch["drafts"]) if item["id"] == draft_id),
                None,
            )
            if index is None:
                raise ApiError(
                    "GENERATED_QUESTION_DRAFT_NOT_FOUND",
                    "Generated question draft does not exist.",
                    status_code=404,
                )
            if batch["drafts"][index].get("import_status") in {"importing", "imported"}:
                raise ApiError(
                    "GENERATED_QUESTION_DRAFT_FROZEN",
                    "A generated question cannot be edited after its import has started.",
                    status_code=409,
                )
            merged = {**batch["drafts"][index]}
            for field in (
                "title",
                "question_text",
                "standard_answer",
                "key_points",
                "difficulty",
                "type",
                "skills",
            ):
                if field in payload and payload[field] is not None:
                    merged[field] = deepcopy(payload[field])
            normalized = self._normalize_draft(merged, draft_id=draft_id)
            normalized["version"] = int(batch["drafts"][index].get("version", 1)) + 1
            normalized["updated_at"] = utc_now()
            batch["drafts"][index] = normalized
            batch["updated_at"] = utc_now()
            updated = transaction.question_generation_batches.update(
                batch, expected_version=expected_version
            )
            return self._projection(updated, transaction)

    def delete_draft(
        self,
        batch_id: str,
        draft_id: str,
        *,
        expected_version: int,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            batch = transaction.question_generation_batches.get(batch_id)
            self._required_batch(batch)
            self._require_reviewing(batch)
            if batch["version"] != expected_version:
                raise ConcurrencyConflict(
                    "QuestionGenerationBatch %s expected version %s, found %s"
                    % (batch_id, expected_version, batch["version"])
                )
            current = next((item for item in batch["drafts"] if item["id"] == draft_id), None)
            if current is None:
                raise ApiError(
                    "GENERATED_QUESTION_DRAFT_NOT_FOUND",
                    "Generated question draft does not exist.",
                    status_code=404,
                )
            if current.get("import_status") in {"importing", "imported"}:
                raise ApiError(
                    "GENERATED_QUESTION_DRAFT_FROZEN",
                    "A generated question cannot be deleted after its import has started.",
                    status_code=409,
                )
            drafts = [item for item in batch["drafts"] if item["id"] != draft_id]
            batch["drafts"] = drafts
            batch["generated_count"] = len(drafts)
            batch["updated_at"] = utc_now()
            updated = transaction.question_generation_batches.update(
                batch, expected_version=expected_version
            )
            return self._projection(updated, transaction)

    def confirm_import(
        self,
        batch_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        if not idempotency_key.strip():
            raise ApiError(
                "IDEMPOTENCY_KEY_REQUIRED",
                "Question generation import requires an Idempotency-Key header.",
            )
        with self.persistence.transaction(organization_id) as transaction:
            batch = transaction.question_generation_batches.get(batch_id)
            self._required_batch(batch)
            if batch.get("status") in {"importing", "imported"}:
                return self._projection(batch, transaction)
            self._require_reviewing(batch)
            if batch["version"] != expected_version:
                raise ConcurrencyConflict(
                    "QuestionGenerationBatch %s expected version %s, found %s"
                    % (batch_id, expected_version, batch["version"])
                )
            importing_drafts = [
                draft for draft in batch.get("drafts", [])
                if draft.get("import_status") == "importing"
            ]
            if importing_drafts:
                raise ApiError(
                    "QUESTION_DRAFT_IMPORT_IN_PROGRESS",
                    "Wait for the in-progress single question import before importing the remaining batch.",
                    status_code=409,
                )
            remaining_drafts = [
                draft for draft in batch.get("drafts", [])
                if draft.get("import_status") != "imported"
            ]
            if not remaining_drafts:
                raise ApiError(
                    "QUESTION_GENERATION_BATCH_EMPTY",
                    "There are no remaining drafts to import.",
                    status_code=409,
                )
            questions = []
            for draft in remaining_drafts:
                questions.append(
                    {
                        **deepcopy(draft),
                        "knowledge_base_id": batch["knowledge_base_id"],
                        "generation_batch_id": batch["id"],
                        "generation_draft_id": draft["id"],
                    }
                )
            work = transaction.outbox.enqueue(
                new_work_item(
                    organization_id=organization_id,
                    kind="knowledge_base.import",
                    aggregate_id=batch["knowledge_base_id"],
                    idempotency_key="question-generation.import:%s:%s" % (batch_id, idempotency_key),
                    payload={
                        "knowledge_base_id": batch["knowledge_base_id"],
                        "generation_batch_id": batch_id,
                        "questions": questions,
                    },
                )
            )
            batch["status"] = "importing"
            batch["import_work_item_id"] = work["id"]
            batch["import_idempotency_key"] = idempotency_key
            batch["updated_at"] = utc_now()
            batch = transaction.question_generation_batches.update(
                batch, expected_version=batch["version"]
            )
            knowledge_base = transaction.knowledge_bases.get(batch["knowledge_base_id"])
            if knowledge_base is not None:
                knowledge_base["status"] = "building"
                knowledge_base["updated_at"] = utc_now()
                transaction.knowledge_bases.update(
                    knowledge_base, expected_version=knowledge_base["version"]
                )
            return self._projection(batch, transaction)

    def confirm_draft_import(
        self,
        batch_id: str,
        draft_id: str,
        *,
        expected_version: int,
        expected_draft_version: Optional[int] = None,
        idempotency_key: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        if not idempotency_key.strip():
            raise ApiError(
                "IDEMPOTENCY_KEY_REQUIRED",
                "Single question import requires an Idempotency-Key header.",
            )
        with self.persistence.transaction(organization_id) as transaction:
            batch = transaction.question_generation_batches.get(batch_id)
            self._required_batch(batch)
            self._require_reviewing(batch)
            draft = next(
                (item for item in batch.get("drafts", []) if item["id"] == draft_id),
                None,
            )
            if draft is None:
                raise ApiError(
                    "GENERATED_QUESTION_DRAFT_NOT_FOUND",
                    "Generated question draft does not exist.",
                    status_code=404,
                )
            if (
                draft.get("import_idempotency_key") == idempotency_key
                and draft.get("import_work_item_id")
            ):
                return self._projection(batch, transaction)
            current_draft_version = int(draft.get("version", 1))
            if (
                expected_draft_version is not None
                and current_draft_version != int(expected_draft_version)
            ):
                raise ConcurrencyConflict(
                    "GeneratedQuestionDraft %s expected version %s, found %s"
                    % (draft_id, expected_draft_version, current_draft_version)
                )
            if draft.get("import_status") == "imported":
                raise ApiError(
                    "GENERATED_QUESTION_DRAFT_ALREADY_IMPORTED",
                    "This generated question has already been imported.",
                    status_code=409,
                )
            if draft.get("import_status") == "importing":
                raise ApiError(
                    "GENERATED_QUESTION_DRAFT_IMPORT_IN_PROGRESS",
                    "This generated question is already being imported.",
                    status_code=409,
                )
            question = {
                **deepcopy(draft),
                "knowledge_base_id": batch["knowledge_base_id"],
                "generation_batch_id": batch["id"],
                "generation_draft_id": draft["id"],
            }
            work = transaction.outbox.enqueue(
                new_work_item(
                    organization_id=organization_id,
                    kind="knowledge_base.import",
                    aggregate_id=batch["knowledge_base_id"],
                    idempotency_key="question-generation.import-draft:%s:%s:%s"
                    % (batch_id, draft_id, idempotency_key),
                    payload={
                        "knowledge_base_id": batch["knowledge_base_id"],
                        "generation_batch_id": batch_id,
                        "generation_draft_id": draft_id,
                        "generation_import_scope": "single",
                        "questions": [question],
                    },
                )
            )
            draft["import_status"] = "importing"
            draft["import_work_item_id"] = work["id"]
            draft["import_idempotency_key"] = idempotency_key
            draft["import_error"] = None
            batch.setdefault("draft_import_work_item_ids", []).append(work["id"])
            batch["updated_at"] = utc_now()
            updated = transaction.question_generation_batches.update(
                batch, expected_version=batch["version"]
            )
            return self._projection(updated, transaction)

    async def process_plan_work(
        self, work_item_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            work = transaction.outbox.start(work_item_id, lease_seconds=150)
            batch = transaction.question_generation_batches.get(work["aggregate_id"])
            self._required_batch(batch)
            if self._work_superseded(batch, work):
                batch = self._complete_superseded(transaction, batch, work)
                return self._projection(batch, transaction)
            if batch.get("blueprints") or batch.get("status") in {"reviewing", "importing", "imported"}:
                transaction.outbox.complete(work_item_id, lease_token=work["lease_token"], result_status="superseded")
                return self._projection(batch, transaction)
            batch["status"] = "generating"
            batch["phase"] = "planning"
            batch["last_error"] = None
            batch["updated_at"] = utc_now()
            batch = transaction.question_generation_batches.update(batch, expected_version=batch["version"])

        request = self._planning_request(batch, organization_id)
        try:
            response = await self.gateway.invoke(
                cap.LLM_CHAT_JSON,
                request,
                route=self._route(batch, organization_id, "question_blueprint_planning", timeout_s=60),
            )
            blueprints = self._normalize_blueprints(response.data, batch["target_count"])
            with self.persistence.transaction(organization_id) as transaction:
                current = transaction.question_generation_batches.get(batch["id"])
                self._required_batch(current)
                running = transaction.outbox.get(work_item_id)
                if self._work_superseded(current, running):
                    current = self._complete_superseded(transaction, current, running)
                    return self._projection(current, transaction)
                if current.get("blueprints"):
                    running = transaction.outbox.get(work_item_id)
                    transaction.outbox.complete(work_item_id, lease_token=running["lease_token"], result_status="superseded")
                    return self._projection(current, transaction)
                chunks = self._enqueue_chunks(transaction, current, blueprints, round_number=0)
                current["blueprints"] = blueprints
                current["generation_chunks"] = chunks
                current["status"] = "generating"
                current["phase"] = "generating"
                current["prompt_versions"] = {"planning": request.metadata["prompt_version"]}
                current["provider_runs"] = [self._provider_run("planning", response)]
                current["last_error"] = None
                current["updated_at"] = utc_now()
                current = transaction.question_generation_batches.update(current, expected_version=current["version"])
                running = transaction.outbox.get(work_item_id)
                transaction.outbox.complete(work_item_id, lease_token=running["lease_token"], result_status="chunks_queued")
                return self._projection(current, transaction)
        except Exception as exc:
            self._fail_pipeline_work(work_item_id, batch["id"], exc, organization_id, phase="planning")
            raise

    async def process_chunk_work(
        self, work_item_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            work = transaction.outbox.start(work_item_id, lease_seconds=220)
            batch = transaction.question_generation_batches.get(work["aggregate_id"])
            self._required_batch(batch)
            chunk = self._chunk(batch, work["payload"]["chunk_id"])
            if self._work_superseded(batch, work):
                batch = self._complete_superseded(transaction, batch, work, chunk=chunk)
                return self._projection(batch, transaction)
            if chunk.get("status") == "completed" or batch.get("status") in {"reviewing", "importing", "imported"}:
                transaction.outbox.complete(work_item_id, lease_token=work["lease_token"], result_status="superseded")
                return self._projection(batch, transaction)
            chunk["status"] = "running"
            chunk["last_error"] = None
            batch["status"] = "generating"
            batch["phase"] = "refilling" if int(chunk.get("round", 0)) else "generating"
            batch["updated_at"] = utc_now()
            batch = transaction.question_generation_batches.update(batch, expected_version=batch["version"])

        request = self._chunk_request(batch, chunk, organization_id)
        try:
            response = await self.gateway.invoke(
                cap.LLM_CHAT_JSON,
                request,
                route=self._route(
                    batch,
                    organization_id,
                    "question_blueprint_generation",
                    timeout_s=120,
                ),
            )
            candidates = self._normalize_chunk_candidates(response.data, chunk)
            with self.persistence.transaction(organization_id) as transaction:
                current = transaction.question_generation_batches.get(batch["id"])
                self._required_batch(current)
                current_chunk = self._chunk(current, chunk["id"])
                running = transaction.outbox.get(work_item_id)
                if self._work_superseded(current, running):
                    current = self._complete_superseded(
                        transaction, current, running, chunk=current_chunk
                    )
                    return self._projection(current, transaction)
                if current_chunk.get("status") != "completed":
                    current_chunk["status"] = "completed"
                    current_chunk["candidates"] = candidates
                    current_chunk["prompt_version"] = request.metadata["prompt_version"]
                    current_chunk["provider"] = response.provider.model_dump()
                    current_chunk["usage"] = response.usage.model_dump()
                    current_chunk["last_error"] = None
                    current.setdefault("provider_runs", []).append(
                        self._provider_run("chunk", response, chunk_id=chunk["id"])
                    )
                    current.setdefault("prompt_versions", {})["generation"] = request.metadata["prompt_version"]
                    current["updated_at"] = utc_now()
                    current = transaction.question_generation_batches.update(current, expected_version=current["version"])
                running = transaction.outbox.get(work_item_id)
                transaction.outbox.complete(work_item_id, lease_token=running["lease_token"], result_status="candidates_ready")
                current = self._enqueue_merge_if_ready(transaction, current)
                return self._projection(current, transaction)
        except ProviderError as exc:
            if (
                exc.code == "provider_output_truncated"
                and len(chunk.get("blueprints", [])) > 1
            ):
                return self._split_truncated_chunk(
                    work_item_id,
                    batch["id"],
                    chunk["id"],
                    exc,
                    organization_id,
                )
            self._fail_chunk_work(work_item_id, batch["id"], chunk["id"], exc, organization_id)
            raise
        except Exception as exc:
            self._fail_chunk_work(work_item_id, batch["id"], chunk["id"], exc, organization_id)
            raise

    def process_merge_work(
        self, work_item_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            work = transaction.outbox.start(work_item_id, lease_seconds=60)
            batch = transaction.question_generation_batches.get(work["aggregate_id"])
            self._required_batch(batch)
            if self._work_superseded(batch, work):
                batch = self._complete_superseded(transaction, batch, work)
                return self._projection(batch, transaction)
            if batch.get("status") in {"reviewing", "importing", "imported"}:
                transaction.outbox.complete(work_item_id, lease_token=work["lease_token"], result_status="superseded")
                return self._projection(batch, transaction)
            batch["phase"] = "merging"
            batch["updated_at"] = utc_now()
            batch = transaction.question_generation_batches.update(batch, expected_version=batch["version"])

        try:
            drafts, rejections, missing = self._merge_candidates(batch, organization_id)
            with self.persistence.transaction(organization_id) as transaction:
                current = transaction.question_generation_batches.get(batch["id"])
                self._required_batch(current)
                running = transaction.outbox.get(work_item_id)
                if self._work_superseded(current, running):
                    current = self._complete_superseded(transaction, current, running)
                    return self._projection(current, transaction)
                current["drafts"] = drafts
                current["generated_count"] = len(drafts)
                current["rejections"] = rejections
                if missing and int(current.get("refill_round", 0)) < self.MAX_REFILL_ROUNDS:
                    round_number = int(current.get("refill_round", 0)) + 1
                    new_chunks = self._enqueue_chunks(transaction, current, missing, round_number=round_number)
                    current.setdefault("generation_chunks", []).extend(new_chunks)
                    current["refill_round"] = round_number
                    current["status"] = "generating"
                    current["phase"] = "refilling"
                    result_status = "refill_queued"
                else:
                    current["status"] = "reviewing" if drafts else "failed"
                    current["phase"] = "reviewing" if drafts else "failed"
                    current["generation_warning"] = (
                        None if len(drafts) == int(current["target_count"])
                        else "目标 %s 道，去重和补生成后得到 %s 道。" % (current["target_count"], len(drafts))
                    )
                    current["prompt_version"] = current.get("prompt_versions", {}).get("generation")
                    current["usage"] = self._aggregate_usage(current.get("provider_runs", []))
                    current["provider"] = self._last_provider(current.get("provider_runs", []))
                    result_status = current["status"]
                current["last_error"] = None
                current["updated_at"] = utc_now()
                current = transaction.question_generation_batches.update(current, expected_version=current["version"])
                running = transaction.outbox.get(work_item_id)
                transaction.outbox.complete(work_item_id, lease_token=running["lease_token"], result_status=result_status)
                return self._projection(current, transaction)
        except Exception as exc:
            self._fail_pipeline_work(work_item_id, batch["id"], exc, organization_id, phase="merging")
            raise

    def _planning_request(self, batch: Dict[str, Any], organization_id: str) -> ChatJSONRequest:
        context = batch["context_snapshot"]
        contract = prompt_contract(
            "question_blueprint_planning",
            {**context, "target_count": batch["target_count"], "existing_questions": self._existing_summaries(batch, organization_id)},
        )
        return ChatJSONRequest(
            organization_id=organization_id,
            purpose="question_blueprint_planning",
            messages=contract.messages,
            json_schema=contract.response_schema,
            temperature=0.35,
            max_output_tokens=min(6000, max(1800, int(batch["target_count"]) * 350)),
            metadata={
                "batch_id": batch["id"], "target_count": batch["target_count"],
                "positioning": context.get("positioning", ""), "tags": context.get("tags", []),
                "prompt_version": contract.version,
            },
        )

    def _chunk_request(self, batch: Dict[str, Any], chunk: Dict[str, Any], organization_id: str) -> ChatJSONRequest:
        context = batch["context_snapshot"]
        contract = prompt_contract(
            "question_blueprint_generation",
            {
                **context,
                "blueprints": chunk["blueprints"],
                "excluded_questions": self._exclusion_summaries(batch, organization_id),
            },
        )
        return ChatJSONRequest(
            organization_id=organization_id,
            purpose="question_blueprint_generation",
            messages=contract.messages,
            json_schema=contract.response_schema,
            temperature=0.45,
            max_output_tokens=min(
                self.MAX_CHUNK_OUTPUT_TOKENS,
                max(
                    self.OUTPUT_TOKENS_PER_SLOT,
                    len(chunk["blueprints"]) * self.OUTPUT_TOKENS_PER_SLOT,
                ),
            ),
            metadata={
                "batch_id": batch["id"],
                "chunk_id": chunk["id"],
                "blueprints": chunk["blueprints"],
                "positioning": context.get("positioning", ""),
                "tags": context.get("tags", []),
                "requirements": context.get("requirements", ""),
                "prompt_version": contract.version,
            },
        )

    def _normalize_blueprints(self, data: Dict[str, Any], target_count: int) -> List[Dict[str, Any]]:
        raw_items = data.get("blueprints") if isinstance(data, dict) else None
        if not isinstance(raw_items, list) or len(raw_items) != int(target_count):
            raise ApiError(
                "QUESTION_BLUEPRINTS_INCOMPLETE",
                "The model did not return the requested number of question blueprints.",
                status_code=502,
            )
        result: List[Dict[str, Any]] = []
        seen = set()
        for index, raw in enumerate(raw_items, start=1):
            topic = str(raw.get("topic") or "").strip()
            scenario = str(raw.get("scenario") or "").strip()
            focus = self._normalize_tags(raw.get("focus") or [])
            key = self._blueprint_key({"topic": topic, "scenario": scenario, "focus": focus})
            if not topic or not scenario or not focus or key in seen:
                raise ApiError(
                    "QUESTION_BLUEPRINTS_DUPLICATE",
                    "Question blueprints must have unique topic, scenario and focus combinations.",
                    status_code=502,
                )
            seen.add(key)
            result.append(
                {
                    "slot_id": "slot_%02d" % index,
                    "topic": topic,
                    "scenario": scenario,
                    "focus": focus,
                    "difficulty": raw.get("difficulty") if raw.get("difficulty") in {"junior", "mid", "senior", "expert"} else "mid",
                    "question_type": "open_ended",
                    "dedupe_key": key,
                }
            )
        return result

    def _normalize_chunk_candidates(self, data: Dict[str, Any], chunk: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw_items = data.get("questions") if isinstance(data, dict) else None
        expected = {item["slot_id"]: item for item in chunk["blueprints"]}
        if not isinstance(raw_items, list) or len(raw_items) != len(expected):
            raise ApiError(
                "QUESTION_CHUNK_INCOMPLETE",
                "The model did not return exactly one question for every blueprint slot.",
                status_code=502,
            )
        result = []
        seen = set()
        for raw in raw_items:
            slot_id = str(raw.get("slot_id") or "")
            if slot_id not in expected or slot_id in seen:
                raise ApiError(
                    "QUESTION_CHUNK_SLOT_INVALID",
                    "Generated questions must preserve each assigned blueprint slot exactly once.",
                    status_code=502,
                )
            seen.add(slot_id)
            normalized = self._normalize_draft(raw)
            normalized["blueprint_slot_id"] = slot_id
            normalized["blueprint_key"] = expected[slot_id]["dedupe_key"]
            result.append(normalized)
        return result

    def _enqueue_chunks(
        self,
        transaction: Any,
        batch: Dict[str, Any],
        blueprints: List[Dict[str, Any]],
        *,
        round_number: int,
        command_token: Optional[str] = None,
        chunk_size: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        chunks = []
        size = max(1, int(chunk_size or self.CHUNK_SIZE))
        for offset in range(0, len(blueprints), size):
            assigned = deepcopy(blueprints[offset : offset + size])
            chunk_id = new_id("question_chunk")
            slot_key = "-".join(item["slot_id"] for item in assigned)
            revision = int(batch.get("execution_revision", 1))
            work = new_work_item(
                organization_id=batch["organization_id"],
                kind=self.CHUNK_KIND,
                aggregate_id=batch["id"],
                idempotency_key="question-generation.chunk:%s:%s:%s:%s:%s"
                % (batch["id"], revision, round_number, slot_key, command_token or chunk_id),
                payload={
                    "batch_id": batch["id"],
                    "chunk_id": chunk_id,
                    "execution_revision": revision,
                },
            )
            work["retry_base_seconds"] = max(5, int(work.get("retry_base_seconds", 0)))
            work = transaction.outbox.enqueue(work)
            chunks.append(
                {
                    "id": chunk_id,
                    "round": round_number,
                    "blueprints": assigned,
                    "slot_ids": [item["slot_id"] for item in assigned],
                    "work_item_id": work["id"],
                    "status": "queued",
                    "execution_revision": revision,
                    "candidates": [],
                    "last_error": None,
                    "created_at": utc_now(),
                }
            )
        return chunks

    def _enqueue_merge_if_ready(self, transaction: Any, batch: Dict[str, Any]) -> Dict[str, Any]:
        active_chunks = [
            chunk
            for chunk in batch.get("generation_chunks", [])
            if chunk.get("status") not in {"superseded", "cancelled"}
        ]
        if not active_chunks or any(chunk.get("status") != "completed" for chunk in active_chunks):
            return batch
        work = self._enqueue_merge_work(transaction, batch)
        if work["id"] not in batch.setdefault("merge_work_item_ids", []):
            batch["merge_work_item_ids"].append(work["id"])
            batch["phase"] = "merging"
            batch["updated_at"] = utc_now()
            batch = transaction.question_generation_batches.update(batch, expected_version=batch["version"])
        return batch

    def _enqueue_merge_work(
        self, transaction: Any, batch: Dict[str, Any], *, command_token: Optional[str] = None
    ) -> Dict[str, Any]:
        round_number = int(batch.get("refill_round", 0))
        revision = int(batch.get("execution_revision", 1))
        token = command_token or "automatic"
        work = new_work_item(
            organization_id=batch["organization_id"],
            kind=self.MERGE_KIND,
            aggregate_id=batch["id"],
            idempotency_key="question-generation.merge:%s:%s:%s:%s"
            % (batch["id"], revision, round_number, token),
            payload={
                "batch_id": batch["id"],
                "round": round_number,
                "execution_revision": revision,
            },
        )
        return transaction.outbox.enqueue(work)

    def _enqueue_plan(
        self, transaction: Any, batch: Dict[str, Any], command_token: str
    ) -> Dict[str, Any]:
        revision = int(batch.get("execution_revision", 1))
        work = new_work_item(
            organization_id=batch["organization_id"],
            kind=self.PLAN_KIND,
            aggregate_id=batch["id"],
            idempotency_key="question-generation.plan:%s:%s:%s"
            % (batch["id"], revision, command_token),
            payload={"batch_id": batch["id"], "execution_revision": revision},
        )
        work["retry_base_seconds"] = max(5, int(work.get("retry_base_seconds", 0)))
        return transaction.outbox.enqueue(work)

    def _merge_candidates(
        self, batch: Dict[str, Any], organization_id: str
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        existing = self._existing_question_texts(batch["knowledge_base_id"], organization_id)
        accepted: Dict[str, Dict[str, Any]] = {}
        accepted_texts: List[str] = []
        rejections: List[Dict[str, Any]] = []
        chunks = sorted(batch.get("generation_chunks", []), key=lambda item: (int(item.get("round", 0)), item.get("created_at", "")))
        for chunk in chunks:
            for candidate in chunk.get("candidates", []):
                slot_id = candidate["blueprint_slot_id"]
                if slot_id in accepted:
                    rejections.append({"slot_id": slot_id, "reason": "duplicate_slot", "title": candidate["title"]})
                    continue
                comparison = existing + accepted_texts
                duplicate = next(
                    (text for text in comparison if self._is_similar(candidate["question_text"], text)),
                    None,
                )
                if duplicate is not None:
                    rejections.append({"slot_id": slot_id, "reason": "similar_question", "title": candidate["title"]})
                    continue
                accepted[slot_id] = deepcopy(candidate)
                accepted_texts.append(candidate["question_text"])
        blueprint_order = {item["slot_id"]: index for index, item in enumerate(batch.get("blueprints", []))}
        drafts = sorted(accepted.values(), key=lambda item: blueprint_order.get(item["blueprint_slot_id"], 999))
        missing = [item for item in batch.get("blueprints", []) if item["slot_id"] not in accepted]
        return drafts, rejections, missing

    def _fail_pipeline_work(
        self, work_item_id: str, batch_id: str, exc: Exception, organization_id: str, *, phase: str
    ) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            running = transaction.outbox.get(work_item_id)
            if not running or running.get("status") != "running":
                return
            current = transaction.question_generation_batches.get(batch_id)
            if current is not None and self._work_superseded(current, running):
                self._complete_superseded(transaction, current, running)
                return
            failed = transaction.outbox.fail(
                work_item_id,
                self._error_message(exc),
                lease_token=running["lease_token"],
                error_code=self._error_code(exc),
                retryable=getattr(exc, "retryable", None),
            )
            if current is not None:
                current["status"] = "failed" if failed["status"] == "dead_letter" else "generating"
                current["phase"] = "failed" if failed["status"] == "dead_letter" else phase
                current["last_error"] = self._error_message(exc)
                current["updated_at"] = utc_now()
                transaction.question_generation_batches.update(current, expected_version=current["version"])

    def _fail_chunk_work(
        self, work_item_id: str, batch_id: str, chunk_id: str, exc: Exception, organization_id: str
    ) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            running = transaction.outbox.get(work_item_id)
            if not running or running.get("status") != "running":
                return
            current = transaction.question_generation_batches.get(batch_id)
            if current is not None and self._work_superseded(current, running):
                chunk = self._chunk(current, chunk_id)
                self._complete_superseded(transaction, current, running, chunk=chunk)
                return
            failed = transaction.outbox.fail(
                work_item_id,
                self._error_message(exc),
                lease_token=running["lease_token"],
                error_code=self._error_code(exc),
                retryable=getattr(exc, "retryable", None),
            )
            if current is not None:
                chunk = self._chunk(current, chunk_id)
                chunk["status"] = "failed" if failed["status"] == "dead_letter" else "retrying"
                chunk["last_error"] = self._error_message(exc)
                chunk["last_error_code"] = self._error_code(exc)
                current["status"] = "failed" if failed["status"] == "dead_letter" else "generating"
                current["phase"] = "failed" if failed["status"] == "dead_letter" else "generating"
                current["last_error"] = self._error_message(exc)
                current["updated_at"] = utc_now()
                transaction.question_generation_batches.update(current, expected_version=current["version"])

    def _split_truncated_chunk(
        self,
        work_item_id: str,
        batch_id: str,
        chunk_id: str,
        exc: ProviderError,
        organization_id: str,
    ) -> Dict[str, Any]:
        """Adapt a multi-slot truncation into isolated single-slot work."""
        with self.persistence.transaction(organization_id) as transaction:
            running = transaction.outbox.get(work_item_id)
            current = transaction.question_generation_batches.get(batch_id)
            self._required_batch(current)
            current_chunk = self._chunk(current, chunk_id)
            if not running or running.get("status") != "running":
                return self._projection(current, transaction)
            if self._work_superseded(current, running):
                current = self._complete_superseded(
                    transaction, current, running, chunk=current_chunk
                )
                return self._projection(current, transaction)

            replacements = self._enqueue_chunks(
                transaction,
                current,
                current_chunk.get("blueprints", []),
                round_number=int(current_chunk.get("round", 0)),
                command_token="output-truncated:%s" % current_chunk["id"],
                chunk_size=1,
            )
            now = utc_now()
            current_chunk["status"] = "superseded"
            current_chunk["superseded_at"] = now
            current_chunk["last_error"] = self._error_message(exc)
            current_chunk["last_error_code"] = exc.code
            current_chunk["recovery"] = {
                "strategy": "split_into_single_slot_chunks",
                "reason": exc.code,
                "replacement_chunk_ids": [item["id"] for item in replacements],
                "diagnostics": {
                    key: exc.details[key]
                    for key in (
                        "finish_reason",
                        "requested_max_output_tokens",
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                        "reasoning_tokens",
                        "content_length",
                    )
                    if key in exc.details
                },
                "created_at": now,
            }
            current.setdefault("generation_chunks", []).extend(replacements)
            current["status"] = "generating"
            current["phase"] = (
                "refilling" if int(current_chunk.get("round", 0)) else "generating"
            )
            current["last_error"] = None
            current["updated_at"] = now
            current = transaction.question_generation_batches.update(
                current, expected_version=current["version"]
            )
            transaction.outbox.complete(
                work_item_id,
                lease_token=running["lease_token"],
                result_status="split_into_single_slot_chunks",
            )
            return self._projection(current, transaction)

    @staticmethod
    def _require_command_key(idempotency_key: str) -> None:
        if not idempotency_key.strip():
            raise ApiError(
                "IDEMPOTENCY_KEY_REQUIRED",
                "Question generation control commands require an Idempotency-Key header.",
            )

    @staticmethod
    def _require_version(batch: Dict[str, Any], expected_version: int) -> None:
        if int(batch.get("version", 1)) != int(expected_version):
            raise ConcurrencyConflict(
                "QuestionGenerationBatch %s expected version %s, found %s"
                % (batch["id"], expected_version, batch.get("version", 1))
            )

    @staticmethod
    def _control_replayed(batch: Dict[str, Any], action: str, idempotency_key: str) -> bool:
        return any(
            item.get("action") == action and item.get("idempotency_key") == idempotency_key
            for item in batch.get("control_history", [])
        )

    @staticmethod
    def _record_control(
        batch: Dict[str, Any], action: str, idempotency_key: str, actor_id: str, reason: str
    ) -> None:
        batch.setdefault("control_history", []).append(
            {
                "action": action,
                "idempotency_key": idempotency_key,
                "actor_id": actor_id,
                "reason": str(reason)[:500],
                "execution_revision": int(batch.get("execution_revision", 1)),
                "created_at": utc_now(),
            }
        )
        batch["control_history"] = batch["control_history"][-100:]

    @staticmethod
    def _error_code(exc: Exception) -> str:
        return str(getattr(exc, "code", None) or exc.__class__.__name__).lower()

    @staticmethod
    def _error_message(exc: Exception) -> str:
        return str(getattr(exc, "message", None) or str(exc))[:1000]

    @staticmethod
    def _generation_work_ids(batch: Dict[str, Any]) -> List[str]:
        values = [batch.get("generation_work_item_id")]
        values.extend(chunk.get("work_item_id") for chunk in batch.get("generation_chunks", []))
        values.extend(batch.get("merge_work_item_ids", []))
        result = []
        for value in values:
            if value and value not in result:
                result.append(value)
        return result

    @staticmethod
    def _work_superseded(batch: Dict[str, Any], work: Optional[Dict[str, Any]]) -> bool:
        if work is None:
            return True
        work_revision = int(work.get("payload", {}).get("execution_revision", 1))
        batch_revision = int(batch.get("execution_revision", 1))
        return batch.get("status") in {"stopping", "stopped"} or work_revision != batch_revision

    def _complete_superseded(
        self,
        transaction: Any,
        batch: Dict[str, Any],
        work: Dict[str, Any],
        *,
        chunk: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if chunk is not None and chunk.get("status") != "completed":
            chunk["status"] = "superseded"
            chunk["superseded_at"] = utc_now()
        transaction.outbox.complete(
            work["id"], lease_token=work["lease_token"], result_status="superseded"
        )
        if batch.get("status") == "stopping":
            batch = self._settle_stop(transaction, batch)
        elif chunk is not None:
            batch["updated_at"] = utc_now()
            batch = transaction.question_generation_batches.update(
                batch, expected_version=batch["version"]
            )
        return batch

    def _settle_stop(self, transaction: Any, batch: Dict[str, Any]) -> Dict[str, Any]:
        running = any(
            (transaction.outbox.get(work_id) or {}).get("status") == "running"
            for work_id in self._generation_work_ids(batch)
        )
        if not running:
            batch["status"] = "stopped"
            batch["phase"] = "stopped"
        batch["updated_at"] = utc_now()
        return transaction.question_generation_batches.update(
            batch, expected_version=batch["version"]
        )

    @staticmethod
    def _merge_retryable(batch: Dict[str, Any], transaction: Any) -> bool:
        for work_id in reversed(batch.get("merge_work_item_ids", [])):
            work = transaction.outbox.get(work_id)
            if work and work.get("status") in {"failed", "dead_letter"}:
                return True
        return bool(batch.get("generation_chunks")) and all(
            chunk.get("status") in {"completed", "superseded", "cancelled"}
            for chunk in batch.get("generation_chunks", [])
        )

    def _route(
        self, batch: Dict[str, Any], organization_id: str, purpose: str, *, timeout_s: int
    ) -> Dict[str, Any]:
        return {
            "id": "question_generation_batch",
            "organization_id": organization_id,
            "capability": cap.LLM_CHAT_JSON,
            "purpose": purpose,
            "primary": {"model_configuration_id": batch["model_configuration_id"], "timeout_s": timeout_s},
            "fallbacks": [],
            # Durable work owns backoff/retry. One work attempt must make at most
            # one provider call so expensive structured output is never doubled
            # by nested retry loops.
            "policy": {"retry_count": 0},
        }

    @staticmethod
    def _chunk(batch: Dict[str, Any], chunk_id: str) -> Dict[str, Any]:
        chunk = next((item for item in batch.get("generation_chunks", []) if item["id"] == chunk_id), None)
        if chunk is None:
            raise ApiError("QUESTION_GENERATION_CHUNK_NOT_FOUND", "Generation chunk does not exist.", status_code=404)
        return chunk

    def _existing_summaries(self, batch: Dict[str, Any], organization_id: str) -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            items = [
                item for item in transaction.questions.list()
                if item.get("knowledge_base_id") == batch["knowledge_base_id"] and item.get("status") == "active"
            ]
        return [
            {"title": item.get("title", "")[:120], "question": item.get("question_text", "")[:240], "skills": item.get("skills", [])[:8]}
            for item in items[:100]
        ]

    def _exclusion_summaries(self, batch: Dict[str, Any], organization_id: str) -> List[Dict[str, Any]]:
        summaries = self._existing_summaries(batch, organization_id)
        summaries.extend(
            {"title": item.get("title", "")[:120], "question": item.get("question_text", "")[:240]}
            for item in batch.get("drafts", [])
        )
        return summaries[:150]

    def _existing_question_texts(self, knowledge_base_id: str, organization_id: str) -> List[str]:
        with self.persistence.transaction(organization_id) as transaction:
            return [
                str(item.get("question_text") or "") for item in transaction.questions.list()
                if item.get("knowledge_base_id") == knowledge_base_id and item.get("status") == "active"
            ]

    @classmethod
    def _is_similar(cls, left: str, right: str) -> bool:
        if cls._text_hash(left) == cls._text_hash(right):
            return True
        left_tokens = cls._text_shingles(left)
        right_tokens = cls._text_shingles(right)
        if not left_tokens or not right_tokens:
            return False
        return len(left_tokens.intersection(right_tokens)) / len(left_tokens.union(right_tokens)) >= cls.SIMILARITY_THRESHOLD

    @staticmethod
    def _text_shingles(value: str) -> set[str]:
        normalized = re.sub(r"[^a-z0-9_+#\u4e00-\u9fff]", "", str(value).lower())
        return {normalized[index : index + 2] for index in range(max(0, len(normalized) - 1))}

    @staticmethod
    def _blueprint_key(blueprint: Dict[str, Any]) -> str:
        raw = "|".join(
            [str(blueprint.get("topic", "")), str(blueprint.get("scenario", "")), *[str(item) for item in blueprint.get("focus", [])]]
        )
        return hashlib.sha256(" ".join(raw.lower().split()).encode("utf-8")).hexdigest()

    @staticmethod
    def _provider_run(stage: str, response: Any, *, chunk_id: Optional[str] = None) -> Dict[str, Any]:
        return {
            "stage": stage,
            "chunk_id": chunk_id,
            "provider": response.provider.model_dump(),
            "usage": response.usage.model_dump(),
        }

    @staticmethod
    def _aggregate_usage(runs: List[Dict[str, Any]]) -> Dict[str, int]:
        return {
            name: sum(int(run.get("usage", {}).get(name, 0)) for run in runs)
            for name in ("input_tokens", "output_tokens", "total_tokens")
        }

    @staticmethod
    def _last_provider(runs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return deepcopy(runs[-1].get("provider")) if runs else None

    def _normalize_generated(
        self, data: Dict[str, Any], batch: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        raw_items = data.get("questions") if isinstance(data, dict) else None
        if not isinstance(raw_items, list):
            return []
        existing_hashes = self._existing_question_hashes(
            batch["knowledge_base_id"], batch["organization_id"]
        )
        result: List[Dict[str, Any]] = []
        seen = set(existing_hashes)
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            try:
                normalized = self._normalize_draft(raw)
            except ApiError:
                continue
            content_hash = self._text_hash(normalized["question_text"])
            if content_hash in seen:
                continue
            seen.add(content_hash)
            result.append(normalized)
            if len(result) >= int(batch["target_count"]):
                break
        return result

    def _normalize_draft(
        self, raw: Dict[str, Any], *, draft_id: Optional[str] = None
    ) -> Dict[str, Any]:
        question_text = str(raw.get("question_text") or "").strip()
        standard_answer = str(raw.get("standard_answer") or "").strip()
        title = str(raw.get("title") or question_text[:40]).strip()
        skills = self._normalize_tags(raw.get("skills") or [])
        key_points = []
        for item in raw.get("key_points") or []:
            if isinstance(item, str):
                text = item.strip()
                weight = 1.0
                aliases: List[str] = []
            elif isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                weight = float(item.get("weight", 1.0))
                aliases = [str(value).strip() for value in item.get("aliases", []) if str(value).strip()]
            else:
                continue
            if text:
                key_points.append({"text": text, "weight": max(0.0, weight), "aliases": aliases})
        missing = []
        if not title:
            missing.append("title")
        if not question_text:
            missing.append("question_text")
        if not standard_answer:
            missing.append("standard_answer")
        if not key_points:
            missing.append("key_points")
        if not skills:
            missing.append("skills")
        if missing:
            raise ApiError(
                "QUESTION_SCORING_BASIS_INCOMPLETE",
                "Generated question is missing required scoring fields.",
                status_code=422,
                details={"fields": missing},
            )
        return {
            "id": draft_id or new_id("question_draft"),
            "version": int(raw.get("version", 1)),
            "title": title[:200],
            "question_text": question_text,
            "standard_answer": standard_answer,
            "key_points": key_points,
            "rubric": deepcopy(DEFAULT_RUBRIC),
            "skills": skills,
            "difficulty": raw.get("difficulty") if raw.get("difficulty") in {"junior", "mid", "senior", "expert"} else "mid",
            "type": str(raw.get("type") or "open_ended"),
            "role_families": [],
        }

    def _existing_question_hashes(
        self, knowledge_base_id: str, organization_id: str
    ) -> set[str]:
        with self.persistence.transaction(organization_id) as transaction:
            return {
                self._text_hash(item.get("question_text", ""))
                for item in transaction.questions.list()
                if item.get("knowledge_base_id") == knowledge_base_id
                and item.get("status") == "active"
            }

    @staticmethod
    def _model_projection(model: Dict[str, Any]) -> Dict[str, Any]:
        selectable = bool(model.get("enabled", True) and model.get("status") == "ready")
        return {
            "id": model["id"],
            "version": model["version"],
            "display_name": model.get("display_name") or model.get("provider_model_id"),
            "provider_id": model.get("provider_id"),
            "provider_model_id": model.get("provider_model_id"),
            "status": model.get("status", "untested"),
            "enabled": model.get("enabled", True),
            "selectable": selectable,
            "unavailable_reason": None if selectable else ("disabled" if not model.get("enabled", True) else model.get("status", "untested")),
        }

    @staticmethod
    def _normalize_tags(values: Any) -> List[str]:
        if isinstance(values, str):
            values = values.replace("，", ",").split(",")
        result = []
        seen = set()
        for item in values or []:
            value = normalize_skill(str(item))
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result[:30]

    @staticmethod
    def _text_hash(value: str) -> str:
        normalized = " ".join(str(value).lower().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _required_bank(knowledge_base: Optional[Dict[str, Any]]) -> None:
        if knowledge_base is None:
            raise ApiError(
                "KNOWLEDGE_BASE_NOT_FOUND", "Knowledge base does not exist.", status_code=404
            )

    @staticmethod
    def _required_batch(batch: Optional[Dict[str, Any]]) -> None:
        if batch is None:
            raise ApiError(
                "QUESTION_GENERATION_BATCH_NOT_FOUND",
                "Question generation batch does not exist.",
                status_code=404,
            )

    @staticmethod
    def _require_reviewing(batch: Dict[str, Any]) -> None:
        if batch.get("status") != "reviewing":
            raise ApiError(
                "QUESTION_GENERATION_BATCH_NOT_REVIEWABLE",
                "Only a reviewing generation batch can be edited or imported.",
                status_code=409,
            )

    @staticmethod
    def _require_generation_model(model: Optional[Dict[str, Any]]) -> None:
        if model is None:
            raise ApiError(
                "MODEL_CONFIGURATION_NOT_FOUND",
                "Model configuration does not exist.",
                status_code=404,
            )
        if not model.get("enabled", True) or model.get("status") != "ready":
            raise ApiError(
                "MODEL_CONFIGURATION_NOT_READY",
                "Question generation model must be enabled and ready.",
                status_code=409,
            )
        if cap.LLM_CHAT_JSON not in model.get("supported_capabilities", []):
            raise ApiError(
                "MODEL_CONFIGURATION_CAPABILITY_MISMATCH",
                "Selected model does not support structured LLM output.",
                status_code=409,
            )

    @staticmethod
    def _projection(batch: Dict[str, Any], transaction: Any) -> Dict[str, Any]:
        item = deepcopy(batch)
        item.setdefault("execution_revision", 1)
        item.setdefault("control_history", [])
        item.setdefault("stop_requested_at", None)
        item.setdefault("stop_requested_by", None)
        item.setdefault("stop_reason", None)
        item.setdefault("draft_import_work_item_ids", [])
        for draft in item.get("drafts", []):
            draft.setdefault("version", 1)
            draft.setdefault("import_status", "pending")
            draft.setdefault("import_work_item_id", None)
            draft.setdefault("imported_question_id", None)
            draft.setdefault("import_error", None)
        work = (
            transaction.outbox.get(item.get("generation_work_item_id"))
            if item.get("generation_work_item_id")
            else None
        )
        import_work = (
            transaction.outbox.get(item.get("import_work_item_id"))
            if item.get("import_work_item_id")
            else None
        )
        item["work"] = deepcopy(work)
        item["import_work"] = deepcopy(import_work)
        tasks: List[Dict[str, Any]] = []
        if work:
            tasks.append(QuestionGenerationService._task_projection(work, task_type="planning"))
        for chunk in item.get("generation_chunks", []):
            chunk_work = transaction.outbox.get(chunk.get("work_item_id"))
            if not chunk_work:
                continue
            tasks.append(
                QuestionGenerationService._task_projection(
                    chunk_work,
                    task_type="generate_chunk",
                    status=chunk.get("status"),
                    chunk_id=chunk.get("id"),
                    slot_ids=chunk.get("slot_ids", []),
                    round_number=int(chunk.get("round", 0)),
                )
            )
        for merge_work_id in item.get("merge_work_item_ids", []):
            merge_work = transaction.outbox.get(merge_work_id)
            if merge_work:
                tasks.append(
                    QuestionGenerationService._task_projection(
                        merge_work, task_type="merge"
                    )
                )
        if import_work:
            tasks.append(
                QuestionGenerationService._task_projection(
                    import_work, task_type="import"
                )
            )
        for draft in item.get("drafts", []):
            draft_work_id = draft.get("import_work_item_id")
            draft_work = transaction.outbox.get(draft_work_id) if draft_work_id else None
            if draft_work:
                tasks.append(
                    QuestionGenerationService._task_projection(
                        draft_work,
                        task_type="import_draft",
                        status=draft.get("import_status"),
                        chunk_id=draft.get("id"),
                    )
                )
        item["tasks"] = sorted(
            tasks, key=lambda task: str(task.get("created_at") or "")
        )
        actions = []
        if item.get("status") in {"queued", "generating"}:
            actions.append("stop")
        if item.get("status") == "stopped":
            actions.append("resume")
        if item.get("status") == "failed" and any(
            task.get("retryable") for task in item["tasks"]
        ):
            actions.append("retry_failed")
        if item.get("status") == "reviewing" and any(
            draft.get("import_status") not in {"importing", "imported"}
            for draft in item.get("drafts", [])
        ) and not any(
            draft.get("import_status") == "importing" for draft in item.get("drafts", [])
        ):
            actions.append("import")
        item["available_actions"] = actions
        chunks = [
            chunk
            for chunk in item.get("generation_chunks", [])
            if chunk.get("status") not in {"superseded", "cancelled"}
        ]
        completed_chunks = sum(1 for chunk in chunks if chunk.get("status") == "completed")
        completed_slots = sum(
            len(chunk.get("slot_ids", []))
            for chunk in chunks
            if chunk.get("status") == "completed"
        )
        item["generation_progress"] = {
            "phase": item.get("phase", item.get("status")),
            "planned_count": len(item.get("blueprints", [])),
            "completed_chunks": completed_chunks,
            "total_chunks": len(chunks),
            "completed_slots": min(completed_slots, int(item.get("target_count", 0))),
            "target_count": int(item.get("target_count", 0)),
            "accepted_count": len(item.get("drafts", [])),
            "rejected_count": len(item.get("rejections", [])),
            "refill_round": int(item.get("refill_round", 0)),
        }
        return item

    @staticmethod
    def _task_projection(
        work: Dict[str, Any],
        *,
        task_type: str,
        status: Optional[str] = None,
        chunk_id: Optional[str] = None,
        slot_ids: Optional[List[str]] = None,
        round_number: Optional[int] = None,
    ) -> Dict[str, Any]:
        work_status = str(work.get("status") or "pending")
        projected_status = status or work_status
        retryable = work_status in {"failed", "dead_letter"} and projected_status not in {
            "superseded",
            "cancelled",
        }
        error = None
        if work.get("last_error"):
            error = {
                "code": work.get("last_error_code") or "work_failed",
                "message": work.get("last_error"),
                "retryable": bool(
                    work.get("error_retryable")
                    if work.get("error_retryable") is not None
                    else retryable
                ),
            }
        return {
            "id": work["id"],
            "type": task_type,
            "status": projected_status,
            "work_status": work_status,
            "chunk_id": chunk_id,
            "slot_ids": list(slot_ids or []),
            "round": round_number,
            "execution_revision": int(
                work.get("payload", {}).get("execution_revision", 1)
            ),
            "attempt_count": int(work.get("attempt_count", 0)),
            "max_attempts": int(work.get("max_attempts", 1)),
            "retryable": retryable,
            "created_at": work.get("created_at"),
            "started_at": work.get("started_at"),
            "finished_at": work.get("finished_at"),
            "available_at": work.get("available_at"),
            "result_status": work.get("result_status"),
            "error": error,
        }
