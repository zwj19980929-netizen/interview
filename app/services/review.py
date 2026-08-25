from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.file_storage.signing import FileAccessSigner
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.interviews import InterviewService


class EnterpriseReviewService:
    """Read/revise interface for human review without changing historical revisions."""

    def __init__(
        self,
        store: InMemoryStore,
        *,
        persistence: Optional[Persistence] = None,
        interviews: Optional[InterviewService] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.interviews = interviews or InterviewService(store, persistence=self.persistence)
        self.media_root = Path(os.getenv("INTERVIEWER_MEDIA_PATH", "data/media")).resolve()
        self.media_signer = FileAccessSigner(
            os.getenv("INTERVIEWER_MEDIA_SIGNING_SECRET", "")
            or os.getenv("INTERVIEWER_FILE_SIGNING_SECRET", "")
        )

    def get_review(self, interview_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        interview = self.interviews.get_interview(interview_id, organization_id)
        evaluations = {item["id"]: item for item in interview.get("evaluation_revisions", [])}
        answers = {item["turn_id"]: item for item in interview.get("answers", [])}
        turns = []
        for turn in sorted(interview.get("turns", []), key=lambda item: item["order"]):
            answer = answers.get(turn["id"])
            evaluation = evaluations.get(answer.get("current_evaluation_id")) if answer else None
            turns.append(
                {
                    "turn_id": turn["id"],
                    "phase": turn.get("phase", "position_bank"),
                    "status": turn["status"],
                    "question": deepcopy(turn["question_snapshot"]),
                    "answer": deepcopy(answer),
                    "evaluation": deepcopy(evaluation),
                }
            )
        report = next(
            (item for item in interview.get("report_revisions", []) if item["id"] == interview.get("current_report_id")),
            None,
        )
        return {
            "interview_id": interview["id"],
            "candidate": deepcopy(interview["candidate"]),
            "position_id": interview.get("plan_snapshot", {}).get("job_position_id"),
            "status": interview["status"],
            "turns": turns,
            "report": deepcopy(report),
            "review_completion": deepcopy(interview.get("review_completion")),
            "human_decision_required": True,
        }

    async def correct_transcript(
        self,
        interview_id: str,
        answer_id: str,
        payload: Dict[str, Any],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        text = str(payload["final_transcript"]).strip()
        if not text:
            raise ApiError("TRANSCRIPT_TEXT_REQUIRED", "Corrected transcript cannot be empty.")
        with self.persistence.transaction(organization_id) as transaction:
            interview = transaction.interview_sessions.get(interview_id)
            if interview is None:
                raise ApiError("INTERVIEW_NOT_FOUND", "Interview does not exist.", status_code=404)
            answer = next((item for item in interview.get("answers", []) if item["id"] == answer_id), None)
            if answer is None:
                raise ApiError("ANSWER_NOT_FOUND", "Answer does not exist.", status_code=404)
            revisions = answer.setdefault("transcript_revisions", [])
            revision = max([int(item["revision"]) for item in revisions], default=0) + 1
            now = utc_now()
            revisions.append(
                {
                    "revision": revision,
                    "text": text,
                    "source": "human_correction",
                    "reason": payload.get("reason", "enterprise_review"),
                    "reviewer_id": payload.get("reviewer_id", "reviewer_local"),
                    "created_at": now,
                }
            )
            answer["final_transcript"] = text
            answer["current_transcript_revision"] = revision
            answer["transcript_source"] = "human_correction"
            answer["updated_at"] = now
            interview.setdefault("review_audit", []).append(
                {"id": new_id("audit"), "type": "transcript.corrected", "answer_id": answer_id, "revision": revision, "occurred_at": now}
            )
            interview["updated_at"] = now
            transaction.interview_sessions.update(interview, expected_version=interview["version"])
        evaluation = await self.interviews.regrade_answer(interview_id, answer_id, organization_id)
        return {"answer_id": answer_id, "transcript_revision": revision, "evaluation": evaluation}

    def audio_url(
        self,
        interview_id: str,
        answer_id: str,
        organization_id: str = "org_default",
        *,
        reviewer_id: str = "reviewer_local",
    ) -> Dict[str, Any]:
        interview = self.interviews.get_interview(interview_id, organization_id)
        answer = next((item for item in interview.get("answers", []) if item["id"] == answer_id), None)
        if answer is None:
            raise ApiError("ANSWER_NOT_FOUND", "Answer does not exist.", status_code=404)
        if not answer.get("audio_uri"):
            raise ApiError("ANSWER_AUDIO_NOT_FOUND", "Answer has no audio recording.", status_code=404)
        audio_uri = str(answer["audio_uri"])
        if not audio_uri.startswith("/media/"):
            raise ApiError("ANSWER_AUDIO_UNMANAGED", "Answer audio is not managed by private media storage.", status_code=409)
        relative_path = audio_uri.removeprefix("/media/")
        token = self.media_signer.issue(
            relative_path,
            expires_seconds=300,
            claims={
                "organization_id": organization_id,
                "interview_id": interview_id,
                "answer_id": answer_id,
                "reviewer_id": reviewer_id,
            },
        )
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        with self.persistence.transaction(organization_id) as transaction:
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": reviewer_id,
                    "action": "answer.audio.access_granted",
                    "resource_type": "candidate_answer",
                    "resource_id": answer_id,
                    "metadata": {"interview_id": interview_id, "expires_seconds": 300},
                    "created_at": utc_now(),
                }
            )
        return {
            "answer_id": answer_id,
            "url": "/api/v1/private-media/%s" % token,
            "expires_at": expires.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "access_mode": "signed_private",
        }

    def open_audio_grant(self, token: str) -> Dict[str, Any]:
        payload = self.media_signer.verify(token)
        path = (self.media_root / str(payload["object_key"])).resolve()
        if self.media_root not in path.parents:
            raise ApiError("MEDIA_ACCESS_INVALID", "Media access token is invalid.", status_code=403)
        if not path.is_file():
            raise ApiError("ANSWER_AUDIO_NOT_FOUND", "Answer audio does not exist.", status_code=404)
        claims = payload.get("claims") or {}
        organization_id = str(claims.get("organization_id") or "org_default")
        with self.persistence.transaction(organization_id) as transaction:
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": str(claims.get("reviewer_id") or "signed_media_grant"),
                    "action": "answer.audio.downloaded",
                    "resource_type": "candidate_answer",
                    "resource_id": str(claims.get("answer_id") or payload["object_key"]),
                    "metadata": {
                        "interview_id": claims.get("interview_id"),
                        "signed_access": True,
                    },
                    "created_at": utc_now(),
                }
            )
        content_types = {".webm": "audio/webm", ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".wav": "audio/wav"}
        return {"content": path.read_bytes(), "content_type": content_types.get(path.suffix.lower(), "application/octet-stream")}

    def complete_review(
        self, interview_id: str, payload: Dict[str, Any], organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            interview = transaction.interview_sessions.get(interview_id)
            if interview is None:
                raise ApiError("INTERVIEW_NOT_FOUND", "Interview does not exist.", status_code=404)
            now = utc_now()
            interview["review_completion"] = {
                "reviewer_id": payload.get("reviewer_id", "reviewer_local"),
                "notes": payload.get("notes", ""),
                "completed_at": now,
                "ai_decision_used": False,
            }
            interview.setdefault("review_audit", []).append(
                {"id": new_id("audit"), "type": "review.completed", "occurred_at": now}
            )
            interview["updated_at"] = now
            updated = transaction.interview_sessions.update(interview, expected_version=interview["version"])
            return deepcopy(updated["review_completion"])
