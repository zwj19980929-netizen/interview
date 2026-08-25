from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.file_storage.interface import PrivateFileStorage
from app.file_storage.provider import private_file_storage
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore


class RetentionService:
    """Explicit, audited purge boundary for expired candidate data."""

    def __init__(
        self,
        store: InMemoryStore,
        *,
        persistence: Optional[Persistence] = None,
        storage: Optional[PrivateFileStorage] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.storage = storage or private_file_storage()
        self.media_root = Path(os.getenv("INTERVIEWER_MEDIA_PATH", "data/media")).resolve()

    def run(
        self,
        *,
        dry_run: bool,
        actor_id: str,
        now: Optional[str] = None,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        cutoff = self._parse_time(now or utc_now())
        with self.persistence.transaction(organization_id) as transaction:
            candidates = [
                item
                for item in transaction.candidate_profiles.list()
                if item.get("status") != "retention_purged"
                and item.get("retention_expires_at")
                and self._parse_time(str(item["retention_expires_at"])) <= cutoff
            ]
        candidate_ids = [item["id"] for item in candidates]
        if not dry_run:
            for candidate_id in candidate_ids:
                self._purge_candidate(candidate_id, organization_id)
        with self.persistence.transaction(organization_id) as transaction:
            event = transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "retention.preview" if dry_run else "retention.purge.completed",
                    "resource_type": "candidate_profile",
                    "resource_id": "retention_batch",
                    "metadata": {
                        "candidate_count": len(candidate_ids),
                        "candidate_ids": candidate_ids,
                        "cutoff": cutoff.isoformat(),
                        "dry_run": dry_run,
                    },
                    "created_at": utc_now(),
                }
            )
        return {
            "dry_run": dry_run,
            "cutoff": cutoff.isoformat(),
            "candidate_count": len(candidate_ids),
            "candidate_ids": candidate_ids,
            "audit_event_id": event["id"],
        }

    def _purge_candidate(self, candidate_id: str, organization_id: str) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            candidate = transaction.candidate_profiles.get(candidate_id)
            if candidate is None:
                return
            resumes = [item for item in transaction.resume_documents.list() if item.get("candidate_profile_id") == candidate_id]
            experience_ids = {
                item["id"]
                for item in transaction.experience_questions.list()
                if item.get("candidate_profile_id") == candidate_id
            }
            speech_assets = [
                item
                for item in transaction.question_speech_assets.list()
                if item.get("owner_type") == "experience_question" and item.get("owner_id") in experience_ids
            ]
            file_ids = {
                str(file_id)
                for resume in resumes
                for file_id in (resume.get("file_object_id"), resume.get("parsed_text_file_object_id"))
                if file_id
            }
            file_ids.update(
                str(item["file_object_id"])
                for item in speech_assets
                if item.get("file_object_id")
            )
            files = [transaction.file_objects.get(file_id) for file_id in file_ids]
            interviews = [
                item
                for item in transaction.interview_sessions.list()
                if item.get("candidate_id") == candidate_id
                or item.get("plan_snapshot", {}).get("candidate_profile_id") == candidate_id
            ]
        for file_object in files:
            if file_object and file_object.get("object_key"):
                self.storage.delete(str(file_object["object_key"]))
        for interview in interviews:
            for answer in interview.get("answers", []):
                self._delete_local_audio(answer.get("audio_uri"))
        with self.persistence.transaction(organization_id) as transaction:
            current = transaction.candidate_profiles.get(candidate_id)
            now = utc_now()
            current.update(
                {
                    "name": "[retention_purged]",
                    "email_encrypted": None,
                    "phone_encrypted": None,
                    "email_masked": "",
                    "phone_masked": "",
                    "email_lookup_hash": None,
                    "phone_lookup_hash": None,
                    "external_ref": None,
                    "metadata": {},
                    "status": "retention_purged",
                    "retention_purged_at": now,
                    "updated_at": now,
                }
            )
            transaction.candidate_profiles.update(current, expected_version=current["version"])
            for resume in transaction.resume_documents.list():
                if resume.get("candidate_profile_id") != candidate_id:
                    continue
                resume.update({"status": "retention_purged", "processing_error": None, "updated_at": now})
                transaction.resume_documents.update(resume, expected_version=resume["version"])
            for file_id in file_ids:
                file_object = transaction.file_objects.get(file_id)
                if file_object is None:
                    continue
                file_object.update({"status": "deleted", "object_key": None, "deleted_at": now, "updated_at": now})
                transaction.file_objects.update(file_object, expected_version=file_object["version"])
            for review in transaction.resume_reviews.list():
                if review.get("candidate_profile_id") != candidate_id:
                    continue
                review.update(
                    {
                        "status": "retention_purged",
                        "project_evidence": [],
                        "skill_evidence": [],
                        "warnings": [],
                        "summary": None,
                        "updated_at": now,
                    }
                )
                transaction.resume_reviews.update(review, expected_version=review["version"])
            for question in transaction.experience_questions.list():
                if question.get("candidate_profile_id") != candidate_id:
                    continue
                question.update(
                    {
                        "question_text": "[retention_purged]",
                        "standard_answer": "[retention_purged]",
                        "key_points": [],
                        "evidence_refs": [],
                        "status": "archived",
                        "updated_at": now,
                    }
                )
                transaction.experience_questions.update(question, expected_version=question["version"])
            for asset in transaction.question_speech_assets.list():
                if asset.get("owner_type") != "experience_question" or asset.get("owner_id") not in experience_ids:
                    continue
                asset.update(
                    {
                        "audio_uri": None,
                        "status": "deleted",
                        "production_ready": False,
                        "deleted_at": now,
                        "updated_at": now,
                    }
                )
                transaction.question_speech_assets.update(asset, expected_version=asset["version"])
            for intake in transaction.candidate_intakes.list():
                if intake.get("matched_candidate_profile_id") != candidate_id:
                    continue
                for key in ("name", "email", "phone", "email_hash", "phone_hash"):
                    intake.pop(key, None)
                intake["retention_purged_at"] = now
                intake["updated_at"] = now
                transaction.candidate_intakes.update(intake, expected_version=intake["version"])
            for interview in transaction.interview_sessions.list():
                if interview.get("candidate_id") != candidate_id and interview.get("plan_snapshot", {}).get("candidate_profile_id") != candidate_id:
                    continue
                interview["candidate"].update({"name": "[retention_purged]", "email": None, "phone": None, "metadata": {}})
                for answer in interview.get("answers", []):
                    answer.update(
                        {
                            "audio_uri": None,
                            "raw_transcript": "[retention_purged]",
                            "final_transcript": "[retention_purged]",
                            "transcript_revisions": [],
                        }
                    )
                interview["evaluation_revisions"] = []
                interview["report_revisions"] = []
                interview["current_report_id"] = None
                interview["retention_purged_at"] = now
                interview["updated_at"] = now
                transaction.interview_sessions.update(interview, expected_version=interview["version"])

    def _delete_local_audio(self, audio_uri: Any) -> None:
        if not isinstance(audio_uri, str) or not audio_uri.startswith("/media/"):
            return
        path = (self.media_root / audio_uri.removeprefix("/media/")).resolve()
        if self.media_root not in path.parents:
            raise ApiError("MEDIA_PATH_INVALID", "Retention media path is invalid.")
        if path.is_file():
            path.unlink()

    def _parse_time(self, value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ApiError("RETENTION_TIME_INVALID", "Retention time must be ISO 8601.") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
