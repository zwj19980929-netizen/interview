from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.domain.candidate_screening import effective_screening_outcome
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
                self._purge_candidate(
                    candidate_id,
                    organization_id,
                    actor_id=actor_id,
                )
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

    def purge_candidate(
        self,
        candidate_id: str,
        organization_id: str = "org_default",
        *,
        actor_id: str = "system:retention",
    ) -> Dict[str, Any]:
        """Purge one candidate through the same privacy boundary used by scheduled retention."""
        return self._purge_candidate(
            candidate_id,
            organization_id,
            actor_id=actor_id,
        )

    def run_screening_retention(
        self,
        *,
        actor_id: str = "system:screening-retention",
        now: Optional[str] = None,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        """Automatically purge only candidates whose seven-day screening deadline elapsed."""
        cutoff = self._parse_time(now or utc_now())
        with self.persistence.transaction(organization_id) as transaction:
            reconciled_candidate_ids = self._reconcile_screening_deadlines(transaction, cutoff)
            candidates = [
                item
                for item in transaction.candidate_profiles.list()
                if item.get("status") != "retention_purged"
                and item.get("retention_reason") == "screening_unqualified"
                and item.get("retention_expires_at")
                and self._parse_time(str(item["retention_expires_at"])) <= cutoff
            ]
        candidate_ids = [item["id"] for item in candidates]
        for candidate_id in candidate_ids:
            self._purge_candidate(
                candidate_id,
                organization_id,
                actor_id=actor_id,
            )
        evidence_gc = self.run_evidence_media_gc(
            actor_id=actor_id,
            organization_id=organization_id,
        )
        with self.persistence.transaction(organization_id) as transaction:
            event = transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "retention.screening_auto_purge.completed",
                    "resource_type": "candidate_profile",
                    "resource_id": "screening_retention_batch",
                    "metadata": {
                        "candidate_count": len(candidate_ids),
                        "candidate_ids": candidate_ids,
                        "reconciled_candidate_ids": reconciled_candidate_ids,
                        "evidence_gc": {
                            "segment_count": evidence_gc["segment_count"],
                            "file_object_count": evidence_gc["file_object_count"],
                        },
                        "cutoff": cutoff.isoformat(),
                    },
                    "created_at": utc_now(),
                }
            )
        return {
            "cutoff": cutoff.isoformat(),
            "candidate_count": len(candidate_ids),
            "candidate_ids": candidate_ids,
            "reconciled_candidate_ids": reconciled_candidate_ids,
            "evidence_gc": evidence_gc,
            "audit_event_id": event["id"],
        }

    def _reconcile_screening_deadlines(self, transaction: Any, now: datetime) -> List[str]:
        """Apply the current score policy to legacy reviews before selecting expired candidates."""
        reconciled_candidate_ids: List[str] = []
        for candidate in transaction.candidate_profiles.list():
            if candidate.get("status") in {"archived", "retention_purged"}:
                continue
            latest_by_position: Dict[str, Dict[str, Any]] = {}
            for review in transaction.resume_reviews.list():
                if review.get("candidate_profile_id") != candidate["id"]:
                    continue
                if review.get("status") in {"deleted", "retention_purged"}:
                    continue
                position_id = str(review.get("job_position_id") or "")
                previous = latest_by_position.get(position_id)
                if previous is None or str(review.get("created_at") or "") > str(previous.get("created_at") or ""):
                    latest_by_position[position_id] = review
            outcomes = [effective_screening_outcome(review) for review in latest_by_position.values()]
            changed = False
            if outcomes and all(outcome == "unqualified" for outcome in outcomes):
                if candidate.get("retention_reason") != "screening_unqualified":
                    candidate["retention_reason"] = "screening_unqualified"
                    changed = True
                if not candidate.get("retention_expires_at"):
                    candidate["retention_expires_at"] = (now + timedelta(days=7)).astimezone(timezone.utc).isoformat()
                    changed = True
            elif candidate.get("retention_reason") == "screening_unqualified":
                candidate["retention_reason"] = None
                candidate["retention_expires_at"] = None
                changed = True
            if changed:
                candidate["updated_at"] = now.astimezone(timezone.utc).isoformat()
                transaction.candidate_profiles.update(candidate, expected_version=candidate["version"])
                reconciled_candidate_ids.append(candidate["id"])
        return reconciled_candidate_ids

    def run_evidence_media_gc(
        self,
        *,
        actor_id: str = "system:evidence-media-gc",
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        """Delete sealed objects belonging to abandoned capture revisions.

        Reset advances ``capture_revision`` in the database before this worker
        runs.  A segment from any smaller revision is therefore permanently
        unreachable by authoritative repair and can be removed without racing
        the current writer.  Object deletion precedes database tombstoning so
        a storage error never produces a false deletion fact.
        """

        with self.persistence.transaction(organization_id) as transaction:
            streams = {
                str(item["id"]): item
                for item in transaction.evidence_media_streams.list()
            }
            segments = [
                item
                for item in transaction.evidence_media_segments.list()
                if self._segment_is_abandoned(item, streams.get(str(item.get("stream_id"))))
            ]
            file_ids = {
                str(item["file_id"])
                for item in segments
                if item.get("file_id")
            }
            file_objects = [
                item
                for item in (transaction.file_objects.get(file_id) for file_id in file_ids)
                if item is not None
                and item.get("purpose") == "candidate_evidence_segment"
            ]
        object_keys = {
            str(item["object_key"])
            for item in file_objects
            if item.get("object_key")
        }
        for object_key in sorted(object_keys):
            self.storage.delete(object_key)

        with self.persistence.transaction(organization_id) as transaction:
            current_streams = {
                str(item["id"]): item
                for item in transaction.evidence_media_streams.list()
            }
            deleted_segment_ids: List[str] = []
            deleted_file_ids: Set[str] = set()
            tombstoned_file_ids: List[str] = []
            now = utc_now()
            for segment in segments:
                current = transaction.evidence_media_segments.get(str(segment["id"]))
                if current is None or not self._segment_is_abandoned(
                    current,
                    current_streams.get(str(current.get("stream_id"))),
                ):
                    continue
                transaction.evidence_media_segments.delete(
                    current["id"], expected_version=current["version"]
                )
                deleted_segment_ids.append(str(current["id"]))
                if current.get("file_id"):
                    deleted_file_ids.add(str(current["file_id"]))
            for file_id in sorted(deleted_file_ids):
                current = transaction.file_objects.get(file_id)
                if current is None or current.get("purpose") != "candidate_evidence_segment":
                    continue
                if current.get("object_key") not in {None, *object_keys}:
                    raise ApiError(
                        "RETENTION_OBJECT_CHANGED",
                        "An Evidence object changed while its abandoned revision was being collected.",
                        status_code=409,
                    )
                if self._update_document(
                    transaction.file_objects,
                    current,
                    {
                        "status": "deleted",
                        "object_key": None,
                        "checksum": None,
                        "byte_count": 0,
                        "deleted_at": current.get("deleted_at") or now,
                    },
                    now,
                ):
                    tombstoned_file_ids.append(file_id)
            audit_event_id = None
            if deleted_segment_ids or tombstoned_file_ids:
                event = transaction.audit_events.add(
                    {
                        "id": new_id("audit"),
                        "organization_id": organization_id,
                        "actor_id": actor_id,
                        "action": "retention.evidence_media_gc.completed",
                        "resource_type": "evidence_media_segment",
                        "resource_id": "abandoned_revision_batch",
                        "metadata": {
                            "segment_count": len(deleted_segment_ids),
                            "file_object_count": len(tombstoned_file_ids),
                            "stream_ids": sorted(
                                {
                                    str(item.get("stream_id"))
                                    for item in segments
                                    if item.get("stream_id")
                                }
                            ),
                            "object_key_hashes": self._object_key_hashes(object_keys),
                        },
                        "created_at": now,
                    }
                )
                audit_event_id = event["id"]
        return {
            "segment_count": len(deleted_segment_ids),
            "file_object_count": len(tombstoned_file_ids),
            "audit_event_id": audit_event_id,
        }

    def _purge_candidate(
        self,
        candidate_id: str,
        organization_id: str,
        *,
        actor_id: str,
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            scope = self._candidate_scope(transaction, candidate_id)
        if scope is None:
            return {
                "candidate_id": candidate_id,
                "changed": False,
                "audit_event_id": None,
            }

        object_keys = set(scope["object_keys"])
        local_audio_uris = set(scope["local_audio_uris"])
        for object_key in sorted(object_keys):
            self.storage.delete(object_key)
        for audio_uri in sorted(local_audio_uris):
            self._delete_local_audio(audio_uri)

        with self.persistence.transaction(organization_id) as transaction:
            current_scope = self._candidate_scope(transaction, candidate_id)
            if current_scope is None:
                return {
                    "candidate_id": candidate_id,
                    "changed": False,
                    "audit_event_id": None,
                }
            late_object_keys = set(current_scope["object_keys"]) - object_keys
            late_local_audio = set(current_scope["local_audio_uris"]) - local_audio_uris
            if late_object_keys or late_local_audio:
                raise ApiError(
                    "RETENTION_SCOPE_CHANGED",
                    "Candidate media changed while retention deletion was in progress; retry the purge.",
                    status_code=409,
                )

            now = utc_now()
            changed = False
            candidate = current_scope["candidate"]
            changed |= self._update_document(
                transaction.candidate_profiles,
                candidate,
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
                    "retention_purged_at": candidate.get("retention_purged_at") or now,
                },
                now,
            )
            for resume in current_scope["resumes"]:
                changed |= self._update_document(
                    transaction.resume_documents,
                    resume,
                    {"status": "retention_purged", "processing_error": None},
                    now,
                )
            for file_id in sorted(current_scope["file_ids"]):
                file_object = transaction.file_objects.get(file_id)
                if file_object is None:
                    continue
                changed |= self._update_document(
                    transaction.file_objects,
                    file_object,
                    {
                        "status": "deleted",
                        "object_key": None,
                        "checksum": None,
                        "byte_count": 0,
                        "deleted_at": file_object.get("deleted_at") or now,
                    },
                    now,
                )
            for review in current_scope["reviews"]:
                changed |= self._update_document(
                    transaction.resume_reviews,
                    review,
                    {
                        "status": "retention_purged",
                        "project_evidence": [],
                        "skill_evidence": [],
                        "warnings": [],
                        "summary": None,
                        "screening_summary": None,
                        "matched_requirements": [],
                        "unmet_requirements": [],
                        "human_review_note": None,
                    },
                    now,
                )
            for question in current_scope["experience_questions"]:
                changed |= self._update_document(
                    transaction.experience_questions,
                    question,
                    {
                        "question_text": "[retention_purged]",
                        "standard_answer": "[retention_purged]",
                        "key_points": [],
                        "evidence_refs": [],
                        "status": "archived",
                    },
                    now,
                )
            for asset in current_scope["speech_assets"]:
                changed |= self._update_document(
                    transaction.question_speech_assets,
                    asset,
                    {
                        "audio_uri": None,
                        "status": "deleted",
                        "production_ready": False,
                        "deleted_at": asset.get("deleted_at") or now,
                    },
                    now,
                )
            for intake in current_scope["intakes"]:
                sanitized = dict(intake)
                for key in ("name", "email", "phone", "email_hash", "phone_hash"):
                    sanitized.pop(key, None)
                sanitized["retention_purged_at"] = (
                    intake.get("retention_purged_at") or now
                )
                changed |= self._replace_document_if_changed(
                    transaction.candidate_intakes,
                    intake,
                    sanitized,
                    now,
                )
            for interview in current_scope["interviews"]:
                sanitized = self._sanitize_interview(interview, now)
                changed |= self._replace_document_if_changed(
                    transaction.interview_sessions,
                    interview,
                    sanitized,
                    now,
                )
            for capture in current_scope["media_captures"]:
                changed |= self._update_document(
                    transaction.interview_media_captures,
                    capture,
                    {
                        "status": "retention_purged",
                        "participant_identity": None,
                        "connection_id": None,
                        "requested_scopes": [],
                        "consented_scopes": [],
                        "egress_id": None,
                        "object_key": None,
                        "private_uri": None,
                        "content_hash": None,
                        "byte_count": 0,
                        "provider_start": None,
                        "provider_result": None,
                        "failure_code": None,
                        "failure_type": None,
                        "retention_purged_at": capture.get("retention_purged_at") or now,
                    },
                    now,
                )
            for stream in current_scope["media_streams"]:
                changed |= self._update_document(
                    transaction.evidence_media_streams,
                    stream,
                    {
                        "status": "retention_purged",
                        "complete": False,
                        "last_sealed_ordinal": 0,
                        "last_sealed_frame_sequence": 0,
                        "sealed_byte_count": 0,
                        "recovered_audio_uri": None,
                        "recovered_byte_count": 0,
                        "recovered_source_pcm_byte_count": 0,
                        "abandoned_captures": [],
                        "retention_purged_at": stream.get("retention_purged_at") or now,
                    },
                    now,
                )
            deleted_segment_ids: List[str] = []
            for segment in current_scope["media_segments"]:
                current = transaction.evidence_media_segments.get(segment["id"])
                if current is None:
                    continue
                transaction.evidence_media_segments.delete(
                    current["id"], expected_version=current["version"]
                )
                deleted_segment_ids.append(str(current["id"]))
                changed = True

            audit_event_id = None
            if changed:
                event = transaction.audit_events.add(
                    {
                        "id": new_id("audit"),
                        "organization_id": organization_id,
                        "actor_id": actor_id,
                        "action": "retention.candidate_evidence_purged",
                        "resource_type": "candidate_profile",
                        "resource_id": candidate_id,
                        "metadata": {
                            "interview_count": len(current_scope["interviews"]),
                            "media_capture_count": len(current_scope["media_captures"]),
                            "evidence_stream_count": len(current_scope["media_streams"]),
                            "evidence_segment_count": len(deleted_segment_ids),
                            "file_object_count": len(current_scope["file_ids"]),
                            "external_object_count": len(object_keys),
                            "local_audio_count": len(local_audio_uris),
                            "object_key_hashes": self._object_key_hashes(object_keys),
                        },
                        "created_at": now,
                    }
                )
                audit_event_id = event["id"]
        return {
            "candidate_id": candidate_id,
            "changed": changed,
            "audit_event_id": audit_event_id,
            "external_object_count": len(object_keys),
            "evidence_segment_count": len(deleted_segment_ids),
        }

    def _candidate_scope(self, transaction: Any, candidate_id: str) -> Optional[Dict[str, Any]]:
        candidate = transaction.candidate_profiles.get(candidate_id)
        if candidate is None:
            return None
        resumes = [
            item
            for item in transaction.resume_documents.list()
            if item.get("candidate_profile_id") == candidate_id
        ]
        experience_questions = [
            item
            for item in transaction.experience_questions.list()
            if item.get("candidate_profile_id") == candidate_id
        ]
        experience_ids = {str(item["id"]) for item in experience_questions}
        speech_assets = [
            item
            for item in transaction.question_speech_assets.list()
            if item.get("owner_type") == "experience_question"
            and str(item.get("owner_id")) in experience_ids
        ]
        interviews = [
            item
            for item in transaction.interview_sessions.list()
            if item.get("candidate_id") == candidate_id
            or item.get("plan_snapshot", {}).get("candidate_profile_id") == candidate_id
        ]
        interview_ids = {str(item["id"]) for item in interviews}
        media_captures = [
            item
            for item in transaction.interview_media_captures.list()
            if item.get("candidate_id") == candidate_id
            or str(item.get("interview_id")) in interview_ids
        ]
        media_streams = [
            item
            for item in transaction.evidence_media_streams.list()
            if str(item.get("interview_id")) in interview_ids
        ]
        stream_ids = {str(item["id"]) for item in media_streams}
        media_segments = [
            item
            for item in transaction.evidence_media_segments.list()
            if str(item.get("stream_id")) in stream_ids
            or str(item.get("interview_id")) in interview_ids
        ]
        file_ids: Set[str] = {
            str(file_id)
            for resume in resumes
            for file_id in (
                resume.get("file_object_id"),
                resume.get("parsed_text_file_object_id"),
            )
            if file_id
        }
        file_ids.update(
            str(item["file_object_id"])
            for item in speech_assets
            if item.get("file_object_id")
        )
        file_ids.update(
            str(item["file_id"])
            for item in media_segments
            if item.get("file_id")
        )
        local_audio_uris: Set[str] = set()
        for interview in interviews:
            for answer in interview.get("answers", []):
                audio_uri = answer.get("audio_uri")
                if isinstance(audio_uri, str) and audio_uri.startswith("private-file://"):
                    file_ids.add(audio_uri.removeprefix("private-file://"))
                elif isinstance(audio_uri, str) and audio_uri.startswith("/media/"):
                    local_audio_uris.add(audio_uri)
        for file_object in transaction.file_objects.list():
            if (
                str(file_object.get("interview_id")) in interview_ids
                and file_object.get("purpose")
                in {
                    "candidate_answer_audio",
                    "candidate_evidence_segment",
                    "agent_expression_audio",
                }
            ):
                file_ids.add(str(file_object["id"]))
        file_objects = [
            item
            for item in (transaction.file_objects.get(file_id) for file_id in file_ids)
            if item is not None
        ]
        object_keys = {
            str(item["object_key"])
            for item in file_objects
            if item.get("object_key")
        }
        object_keys.update(
            str(item["object_key"])
            for item in media_captures
            if item.get("object_key")
        )
        return {
            "candidate": candidate,
            "resumes": resumes,
            "reviews": [
                item
                for item in transaction.resume_reviews.list()
                if item.get("candidate_profile_id") == candidate_id
            ],
            "experience_questions": experience_questions,
            "speech_assets": speech_assets,
            "intakes": [
                item
                for item in transaction.candidate_intakes.list()
                if item.get("matched_candidate_profile_id") == candidate_id
            ],
            "interviews": interviews,
            "interview_ids": interview_ids,
            "media_captures": media_captures,
            "media_streams": media_streams,
            "media_segments": media_segments,
            "file_ids": file_ids,
            "object_keys": object_keys,
            "local_audio_uris": local_audio_uris,
        }

    @staticmethod
    def _sanitize_interview(interview: Dict[str, Any], now: str) -> Dict[str, Any]:
        sanitized = dict(interview)
        candidate = dict(sanitized.get("candidate") or {})
        candidate.update(
            {"name": "[retention_purged]", "email": None, "phone": None, "metadata": {}}
        )
        sanitized["candidate"] = candidate
        answers = []
        for answer in sanitized.get("answers", []):
            item = dict(answer)
            item.update(
                {
                    "audio_uri": None,
                    "raw_transcript": "[retention_purged]",
                    "final_transcript": "[retention_purged]",
                    "transcript_revisions": [],
                }
            )
            answers.append(item)
        sanitized["answers"] = answers
        turns = []
        for turn in sanitized.get("turns", []):
            item = dict(turn)
            item["utterances"] = []
            item["current_understanding"] = None
            item["conversation_acts"] = []
            turns.append(item)
        sanitized["turns"] = turns
        runtime = dict(sanitized.get("agent_runtime") or {})
        runtime["authoritative_media_binding"] = None
        runtime["takeover"] = None
        runtime["processed_signal_keys"] = []
        runtime["active_performance_id"] = None
        sanitized["agent_runtime"] = runtime
        sanitized["agent_events"] = []
        sanitized["evaluation_revisions"] = []
        sanitized["report_revisions"] = []
        sanitized["current_report_id"] = None
        sanitized["report_id"] = None
        sanitized["retention_purged_at"] = interview.get("retention_purged_at") or now
        return sanitized

    @staticmethod
    def _update_document(
        repository: Any,
        current: Dict[str, Any],
        updates: Dict[str, Any],
        now: str,
    ) -> bool:
        if all(current.get(key) == value for key, value in updates.items()):
            return False
        updated = dict(current)
        updated.update(updates)
        updated["updated_at"] = now
        repository.update(updated, expected_version=current["version"])
        return True

    @staticmethod
    def _replace_document_if_changed(
        repository: Any,
        current: Dict[str, Any],
        replacement: Dict[str, Any],
        now: str,
    ) -> bool:
        comparable = dict(replacement)
        comparable["updated_at"] = current.get("updated_at")
        if comparable == current:
            return False
        updated = dict(replacement)
        updated["updated_at"] = now
        repository.update(updated, expected_version=current["version"])
        return True

    @staticmethod
    def _segment_is_abandoned(
        segment: Dict[str, Any], stream: Optional[Dict[str, Any]]
    ) -> bool:
        if stream is None or stream.get("status") == "retention_purged":
            return True
        return int(segment.get("capture_revision", 1)) < int(
            stream.get("capture_revision", 1)
        )

    @staticmethod
    def _object_key_hashes(object_keys: Set[str]) -> List[str]:
        return [
            "sha256:%s" % hashlib.sha256(value.encode("utf-8")).hexdigest()
            for value in sorted(object_keys)
        ]

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
