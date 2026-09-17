"""Optional organization defaults, saved atomically with immutable Skill prose.

The only ambient inputs for a new plan are returned by resolve_defaults. The
caller freezes this result into its own plan; future edits do not update it.
"""

from copy import deepcopy
import hmac
import json
from typing import Any, Dict, Optional

from pydantic import ValidationError

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.sensitive_data import SensitiveDataProtector
from app.core.time import utc_now
from app.persistence.errors import ConcurrencyConflict, RecordAlreadyExists
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.schemas.interview_customization import CompanyProfile, InterviewCustomizationUpdate, company_context_text
from app.services.interview_skills import InterviewSkillService


class InterviewCustomizationService:
    def __init__(self, store: InMemoryStore, *, persistence: Optional[Persistence] = None) -> None:
        self.persistence = persistence or persistence_for(store)
        self._skills = InterviewSkillService(store, persistence=self.persistence)
        self._protector_instance = None

    @property
    def _protector(self):
        if self._protector_instance is None:
            self._protector_instance = SensitiveDataProtector()
        return self._protector_instance

    def get(self, *, actor_id: str = "system", organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as tx:
            record = tx.interview_customizations.get(organization_id)
            result = self._response(tx, record)
            if record or result["skill"]:
                self._audit(tx, record, "read", actor_id, skill_id=(result["skill"] or {}).get("skill_id"))
            return result

    def update(self, payload: Dict[str, Any], *, actor_id: str,
               organization_id: str = "org_default") -> Dict[str, Any]:
        try:
            command = InterviewCustomizationUpdate.model_validate(payload)
        except ValidationError as exc:
            raise ApiError("INTERVIEW_CUSTOMIZATION_INVALID", "面试定制格式无效，请检查正文和企业资料长度。", status_code=422) from exc
        supplied = command.model_fields_set
        try:
            with self.persistence.transaction(organization_id) as tx:
                current = tx.interview_customizations.get(organization_id)
                if command.expected_version != (current["version"] if current else 0):
                    raise ConcurrencyConflict("Interview customization changed; reload before saving.")
                skill_id = self._selected_skill_id(tx, current)
                if "skill_instructions" in supplied:
                    skill_id = self._skills.save_customization_instructions(
                        tx, command.skill_instructions, skill_id=skill_id, actor_id=actor_id,
                    ) if command.skill_instructions.strip() else None
                now = utc_now()
                record = deepcopy(current) if current else {
                    "id": organization_id, "organization_id": organization_id,
                    "created_at": now, "created_by": actor_id,
                }
                if "company_profile" in supplied or current is None:
                    profile = command.company_profile.model_dump() if "company_profile" in supplied else CompanyProfile().model_dump()
                    record.update(self._seal_json(organization_id, profile))
                record.update(default_skill_id=skill_id, updated_at=now, updated_by=actor_id)
                if current:
                    record = tx.interview_customizations.update(record, expected_version=command.expected_version)
                else:
                    record = tx.interview_customizations.add(record)
                self._audit(tx, record, "updated", actor_id, skill_id=skill_id,
                            modules=sorted(supplied - {"expected_version"}))
                return self._response(tx, record)
        except RecordAlreadyExists as exc:
            # Concurrent first saves race on the unique tenant row. Roll back
            # both the Skill and config transaction; clients reload version 1.
            raise ConcurrencyConflict("Interview customization changed; reload before saving.") from exc

    def resolve_defaults(self, organization_id: str = "org_default", *,
                         include_skill: bool = True, include_company: bool = True) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as tx:
            record = tx.interview_customizations.get(organization_id)
            skill_id = self._selected_skill_id(tx, record) if include_skill else None
            summary = self._skills.customization_skill(tx, skill_id, include_instructions=False) if skill_id else None
            usable = bool(summary and summary["status"] in {"active", "approved"})
            return {
                "skill_id": skill_id if usable else None,
                "skill_snapshot": self._skills.freeze_snapshot(tx, skill_id) if usable else None,
                "company_context": company_context_text(self._open_json(record)) if include_company and record and record.get("has_company_profile") else "",
                "customization_version": record["version"] if record else 0,
            }

    def _selected_skill_id(self, tx, record):
        # The existence of a saved row is the explicit opt-out marker too.
        return record.get("default_skill_id") if record is not None else self._skills.suggest_customization_default(tx)

    def _response(self, tx, record):
        skill_id = self._selected_skill_id(tx, record)
        return {
            "version": record["version"] if record else 0,
            "skill": self._skills.customization_skill(tx, skill_id) if skill_id else None,
            "company_profile": self._open_json(record) if record else CompanyProfile().model_dump(),
            "updated_at": record["updated_at"] if record else None,
        }

    def _seal_json(self, organization_id, profile):
        value = {"organization_id": organization_id, "company_profile": profile}
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {"sealed_company_profile": self._protector.encrypt(serialized),
                "content_hash": self._protector.lookup_hash(organization_id, serialized),
                "has_company_profile": bool(company_context_text(profile))}

    def _open_json(self, record):
        try:
            serialized = self._protector.decrypt(record["sealed_company_profile"])
            digest = self._protector.lookup_hash(record["organization_id"], serialized)
            if not hmac.compare_digest(digest, record["content_hash"]):
                raise ValueError("content hash")
            value = json.loads(serialized)
            if set(value) != {"organization_id", "company_profile"} or value["organization_id"] != record["organization_id"]:
                raise ValueError("tenant envelope")
            return CompanyProfile.model_validate(value["company_profile"]).model_dump()
        except (ValueError, TypeError, KeyError, ValidationError) as exc:
            raise ApiError("INTERVIEW_CUSTOMIZATION_CONTENT_INVALID", "面试定制资料无法读取，请联系管理员检查加密配置。", status_code=409) from exc

    @staticmethod
    def _audit(tx, record, action, actor_id, *, skill_id=None, modules=None):
        metadata = {"customization_version": record["version"] if record else 0,
                    "skill_id": skill_id}
        if modules is not None:
            metadata["updated_modules"] = modules
        tx.audit_events.add({"id": new_id("audit"), "organization_id": tx.organization_id,
            "actor_id": actor_id, "action": "interview.customization." + action,
            "resource_type": "interview_customization", "resource_id": tx.organization_id,
            "metadata": metadata, "created_at": utc_now()})
