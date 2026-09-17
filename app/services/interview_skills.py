"""Tenant-scoped Skill lifecycle and immutable content revisions.

Use verify_current_authorization in the effect's own transaction. Loading a
prompt is not an authorization lease and can race an emergency revocation.
"""

from copy import deepcopy
from hashlib import sha256
import json
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pydantic import ValidationError

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.prompt.interview_skills import (
    LEGACY_SKILL_SCHEMA_VERSION, SKILL_SCHEMA_VERSION, SUPPORTED_SKILL_COMPILERS,
    SkillPolicyConflict, canonical_package, compile_package, compiler_version_for,
    effective_tools, register_package,
)
from app.core.sensitive_data import SensitiveDataProtector
from app.core.time import utc_now
from app.domain.interview_lifecycle import InterviewSessionLifecycle, LifecycleCommand, LifecycleCommandType
from app.persistence.errors import ConcurrencyConflict
from app.persistence.interface import Persistence, PersistenceTransaction
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore


_SNAPSHOT_FIELDS = frozenset({
    "skill_id", "revision_id", "revision", "content_hash", "compiler_version",
    "authorization_epoch", "allowed_tools",
})
_REVISION_FIELDS = (
    "id", "skill_id", "revision", "status", "name", "description", "language", "style",
    "interview_method", "content_hash", "compiler_version", "authorization_epoch",
    "allowed_tools", "created_at", "created_by", "validated_at", "approved_at",
    "approved_by", "retired_at", "revoked_at", "validation_report", "schema_version", "activated_at",
)


def _live_or_expressing(session):
    state = session.get("agent_runtime") or {}
    return (session.get("status") in {"scheduled", "waiting", "in_progress", "paused"}
            or bool(state.get("active_performance_id") or state.get("completion_closing_performance_id")))


class InterviewSkillService:
    def __init__(self, store: InMemoryStore, *, persistence: Optional[Persistence] = None,
                 revocation_publisher: Optional[Callable[[str, str], Awaitable[Dict[str, Any]]]] = None) -> None:
        self.persistence = persistence or persistence_for(store)
        self._store = store
        self._revocation_publisher = revocation_publisher
        self._protector_instance = None

    @property
    def _protector(self) -> SensitiveDataProtector:
        # Constructing a session/readiness service must not load unused Skill
        # secrets. Actual private content access still fails closed in production.
        if self._protector_instance is None:
            self._protector_instance = SensitiveDataProtector()
        return self._protector_instance

    def create(self, payload: Dict[str, Any], *, actor_id: str,
               organization_id: str = "org_default") -> Dict[str, Any]:
        package = self._register_payload(payload)
        now, skill_id = utc_now(), new_id("iskill")
        with self.persistence.transaction(organization_id) as tx:
            if len(tx.interview_skills.list()) >= 100:
                raise ApiError("SKILL_QUOTA_EXCEEDED", "The organization Skill limit is 100.", status_code=409)
            revision = self._new_revision(tx, skill_id, 1, package, actor_id)
            skill = tx.interview_skills.add({
                "id": skill_id, "organization_id": organization_id,
                "active_revision_id": revision["id"], "revision_count": 1,
                "created_at": now, "updated_at": now,
            })
            self._audit(tx, skill, revision, "created", actor_id)
            return self._detail(tx, skill, package=package)

    def list(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as tx:
            return [self._summary(tx, skill) for skill in tx.interview_skills.list()]

    def get(self, skill_id: str, *, actor_id: str = "system",
            organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as tx:
            skill = self._required(tx, skill_id)
            revision = self._revision(tx, skill)
            result = self._detail(tx, skill, allow_unavailable=True)
            self._audit(tx, skill, revision, "content_read", actor_id)
            return result

    def revise(self, skill_id: str, payload: Dict[str, Any], *, expected_version: int,
               actor_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        package = self._register_payload(payload)
        with self.persistence.transaction(organization_id) as tx:
            skill = self._required(tx, skill_id, expected_version)
            count = int(skill["revision_count"])
            if count >= 50:
                raise ApiError("SKILL_REVISION_LIMIT", "The Skill revision limit is 50.", status_code=409)
            revision = self._new_revision(tx, skill_id, count + 1, package, actor_id)
            skill.update(active_revision_id=revision["id"], revision_count=count + 1, updated_at=utc_now())
            skill = tx.interview_skills.update(skill, expected_version=expected_version)
            self._audit(tx, skill, revision, "revised", actor_id)
            return self._detail(tx, skill, package=package)

    def get_revision(self, skill_id: str, revision_id: str, *, actor_id: str = "system",
                     organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as tx:
            skill = self._required(tx, skill_id)
            revision = self._revision(tx, skill, revision_id)
            self._audit(tx, skill, revision, "content_read", actor_id)
            return self._detail(tx, skill, revision=revision, allow_unavailable=True)

    def customization_skill(self, tx: PersistenceTransaction, skill_id: str,
                            *, include_instructions: bool = True) -> Optional[Dict[str, Any]]:
        """Read one current revision without opening a nested transaction."""
        skill = tx.interview_skills.get(skill_id)
        if skill is None:
            return None
        revision = self._revision(tx, skill)
        result = {"skill_id": skill["id"], "name": revision["name"],
                  "revision": revision["revision"], "status": revision["status"]}
        if include_instructions:
            result["instructions"] = self._package(revision)["instructions"]
        return result

    def suggest_customization_default(self, tx: PersistenceTransaction) -> Optional[str]:
        """Compatibility suggestion only; callers never use it after saving a default."""
        ordered = sorted(tx.interview_skills.list(),
                         key=lambda item: (item.get("updated_at") or item.get("created_at") or "", item["id"]),
                         reverse=True)
        for item in ordered:
            value = self.customization_skill(tx, item["id"], include_instructions=False)
            if value and value["status"] in {"active", "approved"}:
                return item["id"]
        return None

    def save_customization_instructions(self, tx: PersistenceTransaction, instructions: str,
                                       *, skill_id: Optional[str], actor_id: str) -> str:
        """Compose an immutable v2 revision with the caller's configuration CAS.

        Existing resources and explicit tool restrictions survive prose edits.
        Transaction ownership stays with the customization aggregate so a
        failing config write cannot leave an orphan revision or audit event.
        """
        skill = self._required(tx, skill_id) if skill_id else None
        package = {"schema_version": SKILL_SCHEMA_VERSION, "name": "面试定制", "instructions": instructions}
        if skill:
            current = self._revision(tx, skill)
            previous = self._package(current)
            if (current["status"] == "active" and previous["schema_version"] == SKILL_SCHEMA_VERSION
                    and previous["instructions"] == instructions):
                return skill["id"]
            package.update({key: deepcopy(previous[key]) for key in ("name", "description", "resources", "allowed_tools") if key in previous})
        package = self._register_payload({"package": package})
        now = utc_now()
        if skill:
            count = int(skill["revision_count"])
            if count >= 50:
                raise ApiError("SKILL_REVISION_LIMIT", "The Skill revision limit is 50.", status_code=409)
            revision = self._new_revision(tx, skill["id"], count + 1, package, actor_id)
            skill.update(active_revision_id=revision["id"], revision_count=count + 1, updated_at=now)
            skill = tx.interview_skills.update(skill, expected_version=skill["version"])
            self._audit(tx, skill, revision, "revised", actor_id)
        else:
            if len(tx.interview_skills.list()) >= 100:
                raise ApiError("SKILL_QUOTA_EXCEEDED", "The organization Skill limit is 100.", status_code=409)
            identity = new_id("iskill")
            revision = self._new_revision(tx, identity, 1, package, actor_id)
            skill = tx.interview_skills.add({"id": identity, "organization_id": tx.organization_id,
                "active_revision_id": revision["id"], "revision_count": 1, "created_at": now, "updated_at": now})
            self._audit(tx, skill, revision, "created", actor_id)
        return skill["id"]

    def validate(self, skill_id: str, *, expected_version: int, actor_id: str,
                 revision_id: Optional[str] = None,
                 organization_id: str = "org_default") -> Dict[str, Any]:
        return self._transition(skill_id, "validated", expected_version=expected_version,
                                actor_id=actor_id, revision_id=revision_id, organization_id=organization_id)

    def approve(self, skill_id: str, *, expected_version: int, actor_id: str, reason: str,
                review_confirmed: bool, revision_id: Optional[str] = None,
                organization_id: str = "org_default") -> Dict[str, Any]:
        if review_confirmed is not True:
            raise ApiError("SKILL_REVIEW_REQUIRED", "Explicit human content review is required.", status_code=422)
        return self._transition(skill_id, "approved", expected_version=expected_version,
                                actor_id=actor_id, reason=reason, revision_id=revision_id,
                                organization_id=organization_id)

    def retire(self, skill_id: str, *, expected_version: int, actor_id: str, reason: str,
               revision_id: Optional[str] = None,
               organization_id: str = "org_default") -> Dict[str, Any]:
        return self._transition(skill_id, "retired", expected_version=expected_version,
                                actor_id=actor_id, reason=reason, revision_id=revision_id,
                                organization_id=organization_id)

    def revoke(self, skill_id: str, *, expected_version: int, actor_id: str, reason: str,
               revision_id: Optional[str] = None,
               organization_id: str = "org_default") -> Dict[str, Any]:
        return self._transition(skill_id, "revoked", expected_version=expected_version,
                                actor_id=actor_id, reason=reason, revision_id=revision_id,
                                organization_id=organization_id)

    def delete(self, skill_id: str, *, expected_version: int, actor_id: str,
               organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as tx:
            skill = self._required(tx, skill_id, expected_version)
            revisions = self._revisions(tx, skill_id)
            if any(item["status"] not in {"draft", "validated"} for item in revisions):
                raise ApiError("SKILL_HISTORY_RETAINED", "Published Skill history must be retained; retire or revoke it.", status_code=409)
            for revision in revisions:
                tx.interview_skill_revisions.delete(revision["id"], expected_version=revision["version"])
            tx.interview_skills.delete(skill_id, expected_version=expected_version)
            self._audit(tx, skill, None, "deleted", actor_id)
            return {"id": skill_id, "deleted": True}

    async def revoke_with_delivery(self, skill_id: str, *, expected_version: int, actor_id: str,
                                   reason: str, revision_id: Optional[str] = None,
                                   organization_id: str = "org_default") -> Dict[str, Any]:
        result = self.revoke(skill_id, expected_version=expected_version, actor_id=actor_id,
                             reason=reason, revision_id=revision_id, organization_id=organization_id)
        return await self._deliver_revocation(result, actor_id, organization_id)

    async def retry_revocation_with_delivery(self, skill_id: str, *, expected_version: int, actor_id: str,
                                             revision_id: Optional[str] = None,
                                             organization_id: str = "org_default") -> Dict[str, Any]:
        result = self.retry_revocation_delivery(skill_id, expected_version=expected_version,
            actor_id=actor_id, revision_id=revision_id, organization_id=organization_id)
        return await self._deliver_revocation(result, actor_id, organization_id)

    async def _deliver_revocation(self, result: Dict[str, Any], actor_id: str,
                                 organization_id: str) -> Dict[str, Any]:
        delivery = []
        publisher = self._revocation_publisher
        for interview_id in result.get("affected_session_ids", []):
            try:
                if publisher is None:
                    # Runtime construction is deferred until authorization has
                    # committed. Provider/readiness failures cannot undo revoke.
                    from app.services.interview_agent import InterviewAgentRuntime
                    publisher = InterviewAgentRuntime(self._store).publish_skill_revocation
                delivery.append(await publisher(interview_id, organization_id))
            except Exception:
                delivery.append({"interview_id": interview_id, "local_cleanup_complete": False,
                                 "cross_instance": "delivery_failed"})
        result["revocation_delivery"] = delivery
        try:
            self.record_revocation_delivery(result["id"], delivery, actor_id=actor_id,
                                            organization_id=organization_id)
        except Exception:
            result["delivery_audit_status"] = "failed"
        else:
            result["delivery_audit_status"] = "recorded"
        return result

    def retry_revocation_delivery(self, skill_id: str, *, expected_version: int, actor_id: str,
                                  revision_id: Optional[str] = None,
                                  organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as tx:
            skill = self._required(tx, skill_id, expected_version)
            revision = self._revision(tx, skill, revision_id)
            if revision["status"] != "revoked":
                raise ApiError("SKILL_STATE_CONFLICT", "Only a revoked Skill revision can retry delivery.", status_code=409)
            affected = [session["id"] for session in tx.interview_sessions.list()
                        if (session.get("skill_authorization_blocked") or {}).get("revision_id") == revision["id"]]
            self._audit(tx, skill, revision, "revocation_delivery_retried", actor_id)
            return {**self._detail(tx, skill, allow_unavailable=True), "changed_revision": self._revision_summary(revision),
                    "affected_session_ids": sorted(affected)}

    def record_revocation_delivery(self, skill_id: str, results: List[Dict[str, Any]], *, actor_id: str,
                                   organization_id: str = "org_default") -> None:
        with self.persistence.transaction(organization_id) as tx:
            self._required(tx, skill_id)
            for result in results:
                tx.audit_events.add({
                    "id": new_id("audit"), "organization_id": organization_id, "actor_id": actor_id,
                    "action": "interview.skill.revocation_delivery_result", "resource_type": "interview_skill",
                    "resource_id": skill_id, "metadata": {key: deepcopy(result[key]) for key in (
                        "interview_id", "local_cleanup_complete", "cross_instance", "local_channels_notified",
                        "runtime_audit_status",
                    ) if key in result}, "created_at": utc_now(),
                })

    def freeze_snapshot(self, tx: PersistenceTransaction, skill_id: str,
                        revision_id: Optional[str] = None) -> Dict[str, Any]:
        skill = self._required(tx, skill_id)
        revision = self._revision(tx, skill, revision_id)
        if revision["status"] not in {"active", "approved"}:
            raise ApiError("SKILL_NOT_APPROVED", "A new plan requires an active Skill revision or an approved legacy revision.", status_code=409)
        self._compile(self._package(revision))
        return self._snapshot(revision)

    def verify_current_authorization(self, tx: PersistenceTransaction, snapshot: Dict[str, Any],
                                     *, for_new_plan: bool = False) -> Dict[str, Any]:
        if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_FIELDS:
            raise ApiError("SKILL_SNAPSHOT_INVALID", "Skill snapshot identity is invalid.", status_code=409)
        if (any(not isinstance(snapshot[key], str) or not snapshot[key] for key in (
                "skill_id", "revision_id", "content_hash", "compiler_version"))
                or any(type(snapshot[key]) is not int or snapshot[key] < 1 for key in (
                    "revision", "authorization_epoch"))
                or not isinstance(snapshot["allowed_tools"], list)
                or any(not isinstance(tool, str) for tool in snapshot["allowed_tools"])):
            raise ApiError("SKILL_SNAPSHOT_INVALID", "Skill snapshot identity is invalid.", status_code=409)
        # get_document locks the same revision row as revoke in PostgreSQL;
        # Memory/SQLite serialize the enclosing transaction.
        skill = self._required(tx, snapshot["skill_id"])
        revision = self._revision(tx, skill, snapshot["revision_id"])
        allowed_states = {"active", "approved"} if for_new_plan else {"active", "approved", "retired"}
        if revision["status"] not in allowed_states:
            raise ApiError("SKILL_AUTHORIZATION_REVOKED", "The frozen Skill revision is no longer authorized.", status_code=409)
        if canonical_package(snapshot) != canonical_package(self._snapshot(revision)):
            raise ApiError("SKILL_SNAPSHOT_STALE", "Skill content or authorization epoch does not match.", status_code=409)
        if revision["compiler_version"] not in SUPPORTED_SKILL_COMPILERS:
            raise ApiError("SKILL_COMPILER_UNAVAILABLE", "The frozen Skill compiler is unavailable.", status_code=409)
        return self._revision_summary(revision)

    def load_compiled(self, snapshot: Dict[str, Any],
                      organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as tx:
            self.verify_current_authorization(tx, snapshot)
            revision = tx.interview_skill_revisions.get(snapshot["revision_id"])
            compiled = self._compile(self._package(revision))
            if (compiled["content_hash"] != snapshot["content_hash"]
                    or compiled["compiler_version"] != snapshot["compiler_version"]):
                raise ApiError("SKILL_CONTENT_INVALID", "Skill content integrity check failed.", status_code=409)
            return compiled

    def _transition(self, skill_id, status, *, expected_version, actor_id,
                    revision_id=None, organization_id, reason=None):
        if status in {"approved", "retired", "revoked"} and (
            not isinstance(reason, str) or not reason.strip() or len(reason) > 500
        ):
            raise ApiError("SKILL_REASON_REQUIRED", "A bounded nonempty review reason is required.", status_code=422)
        with self.persistence.transaction(organization_id) as tx:
            locked_sessions = self._lock_skill_sessions(tx, skill_id) if status == "revoked" else []
            skill = self._required(tx, skill_id, expected_version)
            revision = self._revision(tx, skill, revision_id)
            if status == "revoked":
                # A create transaction may have completed CREATE+START between
                # our first scan and acquiring the Skill lock. Do not acquire
                # a newly seen session lock in reverse order: retry atomically.
                locked_ids = {item["id"] for item in locked_sessions}
                for observed in tx.interview_sessions.list():
                    snapshot = (observed.get("plan_snapshot") or {}).get("enterprise_skill_snapshot") or {}
                    if (snapshot.get("skill_id") == skill_id and observed["id"] not in locked_ids
                            and _live_or_expressing(observed)):
                        raise ConcurrencyConflict("Skill session bindings changed during revocation; retry with current state.")
            allowed = {"validated": {"draft"}, "approved": {"validated"},
                       "retired": {"active", "approved"}, "revoked": {"active", "draft", "validated", "approved", "retired"}}
            if revision["status"] not in allowed[status]:
                raise ApiError("SKILL_STATE_CONFLICT", "The Skill lifecycle transition is not allowed.", status_code=409)
            if status in {"validated", "approved"}:
                compiled = self._compile(self._package(revision))
                if compiled["content_hash"] != revision["content_hash"]:
                    raise ApiError("SKILL_CONTENT_INVALID", "Skill content integrity check failed.", status_code=409)
                revision["compiler_version"] = compiled["compiler_version"]
            now = utc_now()
            revision.update(status=status, updated_at=now)
            revision[status + "_at"] = now
            if status == "validated":
                revision["validation_report"] = {
                    "schema": "passed", "resource_bounds": "passed", "static_policy": "passed",
                    "behavioral_evaluation": "requires_enterprise_acceptance",
                    "compiler_version": compiled["compiler_version"],
                }
            if status == "approved":
                revision["approved_by"] = actor_id
            if status == "revoked":
                revision["authorization_epoch"] += 1
            if reason:
                revision["last_reason_sealed"] = self._protector.encrypt(reason.strip())
            revision = tx.interview_skill_revisions.update(revision, expected_version=revision["version"])
            skill["updated_at"] = now
            skill = tx.interview_skills.update(skill, expected_version=expected_version)
            self._audit(tx, skill, revision, status, actor_id, reason=reason)
            affected_session_ids = self._pause_revoked_sessions(tx, revision, actor_id, locked_sessions) if status == "revoked" else []
            result = self._detail(tx, skill, allow_unavailable=status == "revoked")
            result["changed_revision"] = self._revision_summary(revision)
            if status == "revoked":
                result["affected_session_ids"] = affected_session_ids
            return result

    @staticmethod
    def _lock_skill_sessions(tx, skill_id):
        """Match the runtime's session -> Skill -> revision lock order."""
        locked = []
        for observed in sorted(tx.interview_sessions.list(), key=lambda item: item["id"]):
            snapshot = (observed.get("plan_snapshot") or {}).get("enterprise_skill_snapshot") or {}
            if (snapshot.get("skill_id") == skill_id
                    and _live_or_expressing(observed)):
                current = tx.interview_sessions.get(observed["id"])
                if current is not None:
                    locked.append(current)
        return locked

    @staticmethod
    def _pause_revoked_sessions(tx, revision, actor_id, locked_sessions):
        """Revoke current effects with the authorization change, without media I/O.

        Session rows were locked before the Skill in stable ID order, matching
        runtime commits. New session creation serializes on the Skill row and
        admission/effect checks still recheck the epoch. Database conflicts
        abort the whole operation; no epoch, pause or audit commits partially.
        """
        affected = []
        for current in locked_sessions:
            snapshot = (current.get("plan_snapshot") or {}).get("enterprise_skill_snapshot") or {}
            if (snapshot.get("revision_id") != revision["id"]
                    or snapshot.get("skill_id") != revision["skill_id"]
                    or not _live_or_expressing(current)):
                continue
            now = utc_now()
            if current["status"] == "in_progress":
                current = InterviewSessionLifecycle().execute(
                    current, LifecycleCommand(LifecycleCommandType.PAUSE, {"reason": "enterprise_skill_revoked"}), now=now,
                ).session
            current["skill_authorization_blocked"] = {
                "revision_id": revision["id"], "authorization_epoch": revision["authorization_epoch"],
                "reason": "enterprise_skill_revoked", "occurred_at": now,
            }
            state = current.setdefault("agent_runtime", {})
            state.update(floor="none", floor_reason="enterprise_skill_revoked", floor_changed_at=now,
                         active_performance_id=None, active_output_id=None,
                         active_expression_act_event_id=None, expression_replay_act_event_id=None,
                         completion_closing_performance_id=None)
            current["updated_at"] = now
            tx.interview_sessions.update(current, expected_version=current["version"])
            tx.audit_events.add({
                "id": new_id("audit"), "organization_id": tx.organization_id, "actor_id": actor_id,
                "action": "interview.skill.authorization_blocked", "resource_type": "interview_session",
                "resource_id": current["id"], "metadata": {"revision_id": revision["id"],
                    "authorization_epoch": revision["authorization_epoch"]}, "created_at": now,
            })
            affected.append(current["id"])
        return affected

    def _new_revision(self, tx, skill_id, number, package, actor_id):
        now = utc_now()
        active = package["schema_version"] == SKILL_SCHEMA_VERSION
        return tx.interview_skill_revisions.add({
            "id": new_id("iskrev"), "organization_id": tx.organization_id,
            "skill_id": skill_id, "revision": number, "status": "active" if active else "draft",
            "schema_version": package["schema_version"], "activated_at": now if active else None,
            **{key: package[key] for key in ("name", "description", "language", "style", "interview_method") if key in package},
            "allowed_tools": effective_tools(package),
            "content_hash": sha256(canonical_package(package).encode("utf-8")).hexdigest(),
            "compiler_version": compiler_version_for(package), "authorization_epoch": 1,
            "sealed_package": self._protector.encrypt(canonical_package(package)),
            "created_at": now, "updated_at": now, "created_by": actor_id,
            "validated_at": None, "approved_at": None, "approved_by": None,
            "retired_at": None, "revoked_at": None, "validation_report": None,
        })

    def _register_payload(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"package"}:
            raise ApiError("SKILL_PACKAGE_INVALID", "Only a Skill package is accepted.", status_code=422)
        try:
            return register_package(payload["package"])
        except (ValueError, TypeError, ValidationError) as exc:
            raise ApiError("SKILL_PACKAGE_INVALID", "Skill package schema or resource bounds are invalid.", status_code=422) from exc

    @staticmethod
    def _compile(package):
        try:
            return compile_package(package)
        except SkillPolicyConflict as exc:
            raise ApiError("SKILL_POLICY_CONFLICT", "Skill content conflicts with platform interview policy.", status_code=422) from exc
        except (ValueError, TypeError, ValidationError) as exc:
            raise ApiError("SKILL_PACKAGE_INVALID", "Skill package schema or resource bounds are invalid.", status_code=422) from exc

    def _package(self, revision):
        try:
            package = register_package(json.loads(self._protector.decrypt(revision["sealed_package"])))
            digest = sha256(canonical_package(package).encode("utf-8")).hexdigest()
            if digest != revision["content_hash"]:
                raise ValueError("hash")
            return package
        except (ValueError, KeyError, TypeError) as exc:
            raise ApiError("SKILL_CONTENT_INVALID", "Skill content integrity check failed.", status_code=409) from exc

    @staticmethod
    def _required(tx, skill_id, expected_version=None):
        if not isinstance(skill_id, str):
            raise ApiError("SKILL_NOT_FOUND", "Skill does not exist.", status_code=404)
        skill = tx.interview_skills.get(skill_id)
        if skill is None:
            raise ApiError("SKILL_NOT_FOUND", "Skill does not exist.", status_code=404)
        if expected_version is not None and (type(expected_version) is not int or skill["version"] != expected_version):
            raise ConcurrencyConflict("Skill version changed.")
        return skill

    @staticmethod
    def _revision(tx, skill, revision_id=None):
        revision = tx.interview_skill_revisions.get(revision_id or skill["active_revision_id"])
        if revision is None or revision["skill_id"] != skill["id"]:
            raise ApiError("SKILL_REVISION_NOT_FOUND", "Skill revision does not exist.", status_code=404)
        return revision

    @staticmethod
    def _revisions(tx, skill_id):
        return sorted((item for item in tx.interview_skill_revisions.list() if item["skill_id"] == skill_id), key=lambda item: item["revision"])

    @staticmethod
    def _snapshot(revision):
        return {
            "skill_id": revision["skill_id"], "revision_id": revision["id"],
            **{key: deepcopy(revision[key]) for key in (
                "revision", "content_hash", "compiler_version", "authorization_epoch", "allowed_tools",
            )},
        }

    @staticmethod
    def _revision_summary(revision):
        result = {key: deepcopy(revision.get(key)) for key in _REVISION_FIELDS if key in revision}
        result["schema_version"] = revision.get("schema_version", LEGACY_SKILL_SCHEMA_VERSION)
        return result

    def _summary(self, tx, skill):
        revision = self._revision(tx, skill)
        return {**self._revision_summary(revision), **deepcopy(skill), "revision_id": revision["id"]}

    def _detail(self, tx, skill, package=None, *, revision=None, allow_unavailable=False):
        revision = revision or self._revision(tx, skill)
        content_status = "available"
        if package is None:
            try:
                package = self._package(revision)
            except ApiError as exc:
                if not allow_unavailable or exc.code != "SKILL_CONTENT_INVALID":
                    raise
                # Integrity failures block reading/approval/use, but must not
                # roll back emergency revocation of this or an older revision.
                content_status = "unavailable"
        return {**self._revision_summary(revision), **deepcopy(skill), "revision_id": revision["id"],
                "package": package, "content_status": content_status,
                "revisions": [self._revision_summary(item) for item in self._revisions(tx, skill["id"])]}

    @staticmethod
    def _audit(tx, skill, revision, action, actor_id, reason=None):
        metadata = {"skill_version": skill["version"]}
        if revision:
            metadata.update(revision_id=revision["id"], revision=revision["revision"],
                            content_hash=revision["content_hash"], status=revision["status"],
                            authorization_epoch=revision["authorization_epoch"])
        if reason:
            metadata["reason_hash"] = sha256(reason.encode("utf-8")).hexdigest()
        tx.audit_events.add({
            "id": new_id("audit"), "organization_id": tx.organization_id,
            "actor_id": actor_id, "action": "interview.skill." + action,
            "resource_type": "interview_skill", "resource_id": skill["id"],
            "metadata": metadata, "created_at": utc_now(),
        })
