from copy import deepcopy
from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.model_gateway import capabilities as cap
from app.persistence.interface import Persistence, new_work_item
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.model_admin import ModelAdminService


class KnowledgeBaseSpeechService:
    """Deep module for a bank speech profile and its durable rebuild lifecycle."""

    BUILD_KIND = "knowledge_base.speech.rebuild"
    CHILD_KIND = "question.speech.generate"

    def __init__(
        self,
        store: InMemoryStore,
        *,
        persistence: Optional[Persistence] = None,
        model_admin: Optional[ModelAdminService] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.model_admin = model_admin or ModelAdminService(store, persistence=self.persistence)

    def list_knowledge_bases(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            positions = {item["id"]: item for item in transaction.job_positions.list()}
            questions = transaction.questions.list()
            work_items = transaction.outbox.list()
            result = []
            for knowledge_base in transaction.knowledge_bases.list():
                bank_questions = [
                    item
                    for item in questions
                    if item.get("knowledge_base_id") == knowledge_base["id"]
                    and item.get("status") != "archived"
                ]
                current_build = self._latest_build(work_items, knowledge_base["id"])
                item = deepcopy(knowledge_base)
                if not item.get("speech_profile"):
                    item["speech_build_status"] = "configuration_required"
                item["job_position"] = deepcopy(positions.get(item.get("job_position_id")))
                item["question_count"] = len(bank_questions)
                item["speech_ready_count"] = sum(
                    1
                    for question in bank_questions
                    if question.get("speech_status") == "ready"
                    and self._question_matches_profile(question, item.get("speech_profile"))
                )
                item["current_speech_build"] = (
                    self._project_build(current_build, work_items) if current_build else None
                )
                result.append(item)
            return result

    def speech_options(
        self, knowledge_base_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            knowledge_base = transaction.knowledge_bases.get(knowledge_base_id)
            if knowledge_base is None:
                raise ApiError("KNOWLEDGE_BASE_NOT_FOUND", "Knowledge base does not exist.", status_code=404)
            models = [
                item
                for item in transaction.model_configurations.list()
                if cap.TTS_SYNTHESIZE in item.get("supported_capabilities", [])
            ]
        candidates = []
        for model in models:
            voices = self.model_admin.voice_catalog(model["id"], organization_id)["items"]
            selectable = bool(model.get("enabled", True) and model.get("status") == "ready" and voices)
            if not model.get("enabled", True):
                unavailable_reason = "disabled"
            elif model.get("status") != "ready":
                unavailable_reason = str(model.get("status") or "untested")
            elif not voices:
                unavailable_reason = "voice_required"
            else:
                unavailable_reason = None
            candidates.append(
                {
                    "id": model["id"],
                    "version": model["version"],
                    "display_name": model["display_name"],
                    "provider_id": model["provider_id"],
                    "provider_model_id": model["provider_model_id"],
                    "status": model.get("status", "untested"),
                    "enabled": model.get("enabled", True),
                    "selectable": selectable,
                    "unavailable_reason": unavailable_reason,
                    "voices": voices,
                }
            )
        options = [deepcopy(item) for item in candidates if item["selectable"]]
        return {
            "current": deepcopy(knowledge_base.get("speech_profile")),
            "items": options,
            "candidates": candidates,
        }

    def set_profile(
        self,
        knowledge_base_id: str,
        payload: Dict[str, Any],
        *,
        idempotency_key: str,
        actor_id: str = "admin_local",
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        if not idempotency_key.strip():
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Speech profile updates require an Idempotency-Key header.")
        model = self.model_admin.get_model_configuration(payload["model_configuration_id"], organization_id)
        if not model.get("enabled", True) or model.get("status") != "ready":
            raise ApiError(
                "MODEL_CONFIGURATION_NOT_READY",
                "TTS model configuration must be enabled and ready.",
                status_code=409,
            )
        if cap.TTS_SYNTHESIZE not in model.get("supported_capabilities", []):
            raise ApiError(
                "MODEL_CONFIGURATION_CAPABILITY_MISMATCH",
                "Selected model does not support TTS synthesis.",
                status_code=409,
            )
        catalog = self.model_admin.voice_catalog(model["id"], organization_id)
        allowed_voices = {item["voice_profile_id"] for item in catalog["items"]}
        if payload["voice_profile_id"] not in allowed_voices:
            raise ApiError(
                "TTS_VOICE_NOT_AVAILABLE",
                "Selected voice is not available for this TTS model.",
                status_code=422,
                details={"voice_profile_ids": sorted(allowed_voices)},
            )

        with self.persistence.transaction(organization_id) as transaction:
            knowledge_base = transaction.knowledge_bases.get(knowledge_base_id)
            if knowledge_base is None:
                raise ApiError("KNOWLEDGE_BASE_NOT_FOUND", "Knowledge base does not exist.", status_code=404)
            previous = knowledge_base.get("speech_profile") or {}
            current_profile_revision = previous.get("revision")
            version_matches = int(knowledge_base["version"]) == int(payload["expected_version"])
            profile_guard_matches = (
                payload.get("speech_profile_guard_provided") is True
                and current_profile_revision == payload.get("expected_speech_profile_revision")
            )
            if not version_matches and not profile_guard_matches:
                from app.persistence.errors import ConcurrencyConflict

                raise ConcurrencyConflict(
                    "KnowledgeBase %s expected version %s, found %s"
                    % (knowledge_base_id, payload["expected_version"], knowledge_base["version"])
                )
            comparable = {
                "model_configuration_id": model["id"],
                "model_configuration_version": model["version"],
                "voice_profile_id": payload["voice_profile_id"],
                "language": payload["language"],
                "audio_format": payload["audio_format"],
                "speaking_rate": float(payload["speaking_rate"]),
            }
            if all(previous.get(key) == value for key, value in comparable.items()):
                existing = self._latest_build(transaction.outbox.list(), knowledge_base_id)
                if existing and int(existing.get("payload", {}).get("speech_profile_revision", -1)) == int(
                    previous.get("revision", -2)
                ):
                    return self._project_build(existing, transaction.outbox.list())
                question_count = sum(
                    1
                    for item in transaction.questions.list()
                    if item.get("knowledge_base_id") == knowledge_base_id and item.get("status") == "active"
                )
                return {
                    "id": None,
                    "job_id": None,
                    "knowledge_base_id": knowledge_base_id,
                    "speech_profile_revision": previous.get("revision"),
                    "status": knowledge_base.get("speech_build_status", "ready"),
                    "total": question_count,
                    "pending": 0,
                    "running": 0,
                    "ready": int((knowledge_base.get("readiness") or {}).get("speech_ready_count", 0)),
                    "failed": 0,
                    "superseded": 0,
                }

            revision = int(previous.get("revision", 0)) + 1
            now = utc_now()
            profile = {
                **comparable,
                "revision": revision,
                "source": "knowledge_base_explicit",
                "configured_by": actor_id,
                "configured_at": now,
            }
            cancelled_work_ids = []
            for candidate in transaction.outbox.list():
                candidate_payload = candidate.get("payload", {})
                same_revision = int(
                    candidate_payload.get("speech_profile_revision", -1)
                ) == int(previous.get("revision", -2))
                belongs_to_bank = (
                    candidate.get("kind") == self.BUILD_KIND
                    and candidate.get("aggregate_id") == knowledge_base_id
                ) or (
                    candidate.get("kind") == self.CHILD_KIND
                    and candidate_payload.get("knowledge_base_id") == knowledge_base_id
                )
                if (
                    belongs_to_bank
                    and same_revision
                    and candidate.get("status") in {"pending", "failed", "running"}
                ):
                    transaction.outbox.cancel(
                        candidate["id"],
                        reason="Superseded by speech profile revision %s" % revision,
                        actor_id=actor_id,
                    )
                    cancelled_work_ids.append(candidate["id"])
            manifest = []
            for question in transaction.questions.list():
                if question.get("knowledge_base_id") != knowledge_base_id or question.get("status") != "active":
                    continue
                question["speech_status"] = "pending"
                question["speech_error"] = None
                question["updated_at"] = now
                question = transaction.questions.update(question, expected_version=question["version"])
                manifest.append({"question_id": question["id"], "question_version": question["version"]})

            knowledge_base["speech_profile"] = profile
            knowledge_base["language"] = profile["language"]
            knowledge_base["voice_profile_id"] = profile["voice_profile_id"]
            knowledge_base["speech_build_status"] = "pending"
            knowledge_base["status"] = "building" if manifest else "draft"
            knowledge_base["updated_at"] = now
            updated = transaction.knowledge_bases.update(
                knowledge_base, expected_version=int(knowledge_base["version"])
            )
            work = transaction.outbox.enqueue(
                new_work_item(
                    organization_id=organization_id,
                    kind=self.BUILD_KIND,
                    aggregate_id=knowledge_base_id,
                    idempotency_key="knowledge-base.speech:%s:%s:%s"
                    % (knowledge_base_id, revision, idempotency_key),
                    payload={
                        "knowledge_base_id": knowledge_base_id,
                        "speech_profile": deepcopy(profile),
                        "speech_profile_revision": revision,
                        "question_manifest": manifest,
                    },
                )
            )
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "knowledge_base.speech_profile_changed",
                    "resource_type": "knowledge_base",
                    "resource_id": knowledge_base_id,
                    "metadata": {
                        "previous_revision": previous.get("revision"),
                        "speech_profile_revision": revision,
                        "model_configuration_id": model["id"],
                        "voice_profile_id": profile["voice_profile_id"],
                        "question_count": len(manifest),
                        "superseded_work_count": len(cancelled_work_ids),
                        "superseded_work_ids": cancelled_work_ids,
                    },
                    "created_at": now,
                }
            )
            projection = self._project_build(work, transaction.outbox.list())
            projection["knowledge_base_version"] = updated["version"]
            return projection

    def list_builds(
        self, knowledge_base_id: str, organization_id: str = "org_default"
    ) -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            if transaction.knowledge_bases.get(knowledge_base_id) is None:
                raise ApiError("KNOWLEDGE_BASE_NOT_FOUND", "Knowledge base does not exist.", status_code=404)
            work_items = transaction.outbox.list()
            builds = [
                self._project_build(item, work_items)
                for item in work_items
                if item.get("kind") == self.BUILD_KIND and item.get("aggregate_id") == knowledge_base_id
            ]
        return sorted(builds, key=lambda item: str(item.get("created_at", "")), reverse=True)

    def get_build(
        self, knowledge_base_id: str, build_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        builds = self.list_builds(knowledge_base_id, organization_id)
        build = next((item for item in builds if item["id"] == build_id), None)
        if build is None:
            raise ApiError("KNOWLEDGE_BASE_SPEECH_BUILD_NOT_FOUND", "Speech build does not exist.", status_code=404)
        return build

    def retry_failed(
        self,
        knowledge_base_id: str,
        build_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        if not idempotency_key.strip():
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Speech build retries require an Idempotency-Key header.")
        with self.persistence.transaction(organization_id) as transaction:
            knowledge_base = transaction.knowledge_bases.get(knowledge_base_id)
            if knowledge_base is None:
                raise ApiError("KNOWLEDGE_BASE_NOT_FOUND", "Knowledge base does not exist.", status_code=404)
            retry_idempotency_key = "knowledge-base.speech.retry:%s:%s" % (
                build_id,
                idempotency_key,
            )
            existing = next(
                (
                    item
                    for item in transaction.outbox.list()
                    if item.get("idempotency_key") == retry_idempotency_key
                    and item.get("kind") == self.BUILD_KIND
                    and item.get("aggregate_id") == knowledge_base_id
                ),
                None,
            )
            if existing is not None:
                return self._project_build(existing, transaction.outbox.list())
            if int(knowledge_base["version"]) != int(expected_version):
                from app.persistence.errors import ConcurrencyConflict

                raise ConcurrencyConflict("KnowledgeBase version changed before failed speech retry.")
            parent = transaction.outbox.get(build_id)
            if parent is None or parent.get("kind") != self.BUILD_KIND or parent.get("aggregate_id") != knowledge_base_id:
                raise ApiError("KNOWLEDGE_BASE_SPEECH_BUILD_NOT_FOUND", "Speech build does not exist.", status_code=404)
            revision = int((knowledge_base.get("speech_profile") or {}).get("revision", 0))
            if revision != int(parent.get("payload", {}).get("speech_profile_revision", -1)):
                raise ApiError("KNOWLEDGE_BASE_SPEECH_BUILD_SUPERSEDED", "Only the current speech build can be retried.", status_code=409)
            # Current Question state is authoritative for a manual retry. Older
            # workers could advance the Question version on a transient failure
            # and then complete the successful retry as `superseded`; those
            # children no longer look failed even though the current question is.
            failed_question_ids = {
                item.get("question_id")
                for item in parent.get("payload", {}).get("question_manifest", [])
            }
            manifest = []
            for question_id in failed_question_ids:
                question = transaction.questions.get(question_id)
                if (
                    question is None
                    or question.get("knowledge_base_id") != knowledge_base_id
                    or question.get("status") != "active"
                    or question.get("speech_status") != "failed"
                ):
                    continue
                manifest.append(
                    {
                        "question_id": question["id"],
                        "question_version": question["version"],
                    }
                )
            if not manifest:
                raise ApiError(
                    "KNOWLEDGE_BASE_SPEECH_BUILD_NOT_FAILED",
                    "Speech build has no current failed questions.",
                    status_code=409,
                )
            work = transaction.outbox.enqueue(
                new_work_item(
                    organization_id=organization_id,
                    kind=self.BUILD_KIND,
                    aggregate_id=knowledge_base_id,
                    idempotency_key=retry_idempotency_key,
                    payload={
                        "knowledge_base_id": knowledge_base_id,
                        "speech_profile": deepcopy(knowledge_base["speech_profile"]),
                        "speech_profile_revision": revision,
                        "question_manifest": manifest,
                        "retry_of": build_id,
                    },
                )
            )
            knowledge_base["speech_build_status"] = "pending"
            knowledge_base["status"] = "building"
            knowledge_base["updated_at"] = utc_now()
            transaction.knowledge_bases.update(knowledge_base, expected_version=expected_version)
            return self._project_build(work, transaction.outbox.list())

    def process_build_work(
        self, work_item_id: str, organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            work = transaction.outbox.start(work_item_id)
            profile = work["payload"]["speech_profile"]
            revision = int(work["payload"]["speech_profile_revision"])
            knowledge_base = transaction.knowledge_bases.get(work["aggregate_id"])
            if knowledge_base is None:
                transaction.outbox.fail(work_item_id, "Knowledge base disappeared.", lease_token=work["lease_token"])
                raise ApiError("KNOWLEDGE_BASE_NOT_FOUND", "Knowledge base does not exist.", status_code=404)
            if int((knowledge_base.get("speech_profile") or {}).get("revision", -1)) != revision:
                transaction.outbox.complete(work_item_id, lease_token=work["lease_token"])
                return self._project_build(transaction.outbox.get(work_item_id), transaction.outbox.list())
            for item in work["payload"].get("question_manifest", []):
                question = transaction.questions.get(item["question_id"])
                if question is None or int(question["version"]) != int(item["question_version"]):
                    continue
                is_retry = bool(work["payload"].get("retry_of"))
                child_idempotency_key = (
                    "question.speech.retry:%s:%s:%s:%s"
                    % (question["id"], question["version"], revision, work_item_id)
                    if is_retry
                    else "question.speech:%s:%s:%s"
                    % (question["id"], question["version"], revision)
                )
                transaction.outbox.enqueue(
                    new_work_item(
                        organization_id=organization_id,
                        kind=self.CHILD_KIND,
                        aggregate_id=question["id"],
                        idempotency_key=child_idempotency_key,
                        payload={
                            "owner_type": "question",
                            "owner_id": question["id"],
                            "source_version": question["version"],
                            "knowledge_base_id": knowledge_base["id"],
                            "speech_profile": deepcopy(profile),
                            "speech_profile_revision": revision,
                            "parent_build_id": work_item_id,
                        },
                    )
                )
            if work["payload"].get("question_manifest"):
                knowledge_base["speech_build_status"] = "running"
                knowledge_base["updated_at"] = utc_now()
                transaction.knowledge_bases.update(knowledge_base, expected_version=knowledge_base["version"])
            transaction.outbox.complete(work_item_id, lease_token=work["lease_token"])
            return self._project_build(transaction.outbox.get(work_item_id), transaction.outbox.list())

    def _project_build(self, work: Dict[str, Any], work_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        children = [item for item in work_items if item.get("payload", {}).get("parent_build_id") == work["id"]]
        manifest_count = len(work.get("payload", {}).get("question_manifest", []))
        counts = {"pending": 0, "running": 0, "ready": 0, "failed": 0, "superseded": 0}
        for child in children:
            status = child.get("status")
            if (
                child.get("result_status") == "superseded"
                or status == "cancelled"
                or child.get("cancel_requested")
            ):
                counts["superseded"] += 1
            elif status == "completed":
                counts["ready"] += 1
            elif status == "dead_letter":
                counts["failed"] += 1
            elif status == "failed":
                # `failed` is the durable outbox's retry-waiting state.  Only a
                # dead letter is terminal; presenting a claimable retry as a
                # completed build failure makes operators race the worker with
                # a manual replay.
                counts["pending"] += 1
            elif status == "running":
                counts["running"] += 1
            else:
                counts["pending"] += 1
        if work.get("status") in {"failed", "dead_letter"}:
            status = "failed"
        elif not children and work.get("status") != "completed":
            status = "pending"
        elif counts["failed"]:
            status = "failed" if counts["pending"] == 0 and counts["running"] == 0 else "running"
        elif manifest_count == counts["ready"] + counts["superseded"] and counts["superseded"]:
            status = "superseded"
        elif manifest_count == counts["ready"]:
            status = "ready"
        else:
            status = "running"
        failed_items = [
            {
                "question_id": child.get("payload", {}).get("owner_id"),
                "error_code": child.get("last_error_code") or "speech_generation_failed",
                "retryable": child.get("error_retryable") is not False,
            }
            for child in children
            if child.get("status") == "dead_letter"
        ]
        return {
            "id": work["id"],
            "job_id": work["id"],
            "knowledge_base_id": work["aggregate_id"],
            "speech_profile_revision": work.get("payload", {}).get("speech_profile_revision"),
            "status": status,
            "total": manifest_count,
            **counts,
            "failed_items": failed_items,
            "last_error": work.get("last_error"),
            "created_at": work.get("created_at"),
            "updated_at": work.get("updated_at"),
        }

    def _latest_build(self, work_items: List[Dict[str, Any]], knowledge_base_id: str) -> Optional[Dict[str, Any]]:
        builds = [
            item
            for item in work_items
            if item.get("kind") == self.BUILD_KIND and item.get("aggregate_id") == knowledge_base_id
        ]
        return max(builds, key=lambda item: str(item.get("created_at", "")), default=None)

    def _question_matches_profile(
        self, question: Dict[str, Any], profile: Optional[Dict[str, Any]]
    ) -> bool:
        if not profile:
            return False
        return int(question.get("speech_profile_revision", -1)) == int(profile.get("revision", -2))
