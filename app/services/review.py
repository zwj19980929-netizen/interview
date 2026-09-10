from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.domain.scoring_quality import evaluation_answers, project_evaluation, project_report
from app.domain.speech_quality import transcript_is_verified
from app.domain.interview_lifecycle import LifecycleCommand, LifecycleCommandType
from app.file_storage.signing import FileAccessSigner
from app.file_storage.provider import private_file_storage
from app.file_storage.interface import PrivateFileStorage
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
        storage: Optional[PrivateFileStorage] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.interviews = interviews or InterviewService(store, persistence=self.persistence)
        self.media_root = Path(os.getenv("INTERVIEWER_MEDIA_PATH", "data/media")).resolve()
        self.media_signer = FileAccessSigner(
            os.getenv("INTERVIEWER_MEDIA_SIGNING_SECRET", "")
            or os.getenv("INTERVIEWER_FILE_SIGNING_SECRET", "")
        )
        self.private_storage = storage or private_file_storage()

    def get_review(self, interview_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        interview = self.interviews.get_interview(interview_id, organization_id)
        evaluations = {item["id"]: item for item in interview.get("evaluation_revisions", [])}
        answers = {item["turn_id"]: item for item in interview.get("answers", [])}
        with self.persistence.transaction(organization_id) as transaction:
            capture = next((item for item in transaction.interview_media_captures.list()
                            if item.get("interview_id") == interview_id), None)
            works = [item for item in transaction.outbox.list()
                     if item.get("payload", {}).get("interview_id") == interview_id]
        recording = self._recording_projection(capture)
        turns = []
        for turn in sorted(interview.get("turns", []), key=lambda item: item["order"]):
            answer = answers.get(turn["id"])
            original = evaluations.get(answer.get("current_evaluation_id")) if answer else None
            evaluation = project_evaluation(original, evaluation_answers(interview, original)) if original else None
            answer_projection = deepcopy(answer)
            if answer_projection:
                source = answer_projection.get("stt_confidence_source", "legacy_unverified")
                answer_projection["stt_confidence_source"] = source
                if source == "legacy_unverified":
                    answer_projection["stt_confidence"] = None
                answer_projection["transcript_verified"] = transcript_is_verified(answer)
            turns.append(
                {
                    "turn_id": turn["id"],
                    "phase": turn.get("phase", "position_bank"),
                    "status": turn["status"],
                    "skip_reason": turn.get("skip_reason"),
                    "question": deepcopy(turn["question_snapshot"]),
                    "answer": answer_projection,
                    "evaluation": deepcopy(evaluation),
                    "is_followup": bool(turn.get("is_followup")),
                    "order": turn.get("order"),
                    "playback": self._turn_playback(turn, answer, capture),
                }
            )
        report = next(
            (item for item in interview.get("report_revisions", []) if item["id"] == interview.get("current_report_id")),
            None,
        )
        if report:
            report = project_report(report, interview)
        processing = self._processing_projection(interview, works)
        return {
            "interview_id": interview["id"],
            "candidate": deepcopy(interview["candidate"]),
            "position_id": interview.get("plan_snapshot", {}).get("job_position_id"),
            "status": interview["status"],
            "turns": turns,
            "report": deepcopy(report),
            "review_completion": deepcopy(interview.get("review_completion")),
            "recording": recording,
            "processing": processing,
            "human_decision_required": True,
        }

    @staticmethod
    def _processing_projection(interview, works):
        answers = interview.get("answers", [])
        completed, failed, pending = 0, 0, 0
        for answer in answers:
            if answer.get("evaluation_status") == "completed":
                completed += 1
                continue
            revision = max((item["revision"] for item in interview.get("evaluation_revisions", [])
                            if item["answer_id"] == answer["id"]), default=0) + 1
            active = any(item["kind"] == "answer.evaluate"
                         and item.get("payload", {}).get("answer_id") == answer["id"]
                         and int(item.get("payload", {}).get("revision", 0)) == revision
                         and (item["status"] in {"pending", "running"}
                              or item["status"] == "failed" and item.get("error_retryable") is not False)
                         for item in works)
            if active:
                pending += 1
            else:
                failed += 1
        submitted = bool(interview.get("candidate_input_completed_at") or interview.get("completed_at"))
        status = "ready" if interview.get("current_report_id") and not pending and not failed else (
            "failed" if failed or interview.get("status") == "report_failed" else
            "processing" if submitted or pending else "awaiting_submission")
        return {"submitted_at": interview.get("candidate_input_completed_at") or interview.get("completed_at"),
                "total": len(answers), "completed": completed, "failed": failed, "pending": pending,
                "report_status": status, "can_retry": bool(failed or interview.get("status") == "report_failed")}

    @staticmethod
    def _recording_projection(capture):
        if not capture:
            return {"status": "unavailable", "available": False, "consented_scopes": []}
        return {"status": capture["status"], "available": bool(capture.get("status") == "completed"
                and capture.get("content_hash") and capture.get("byte_count") and capture.get("storage_protection")),
                "consented_scopes": capture.get("consented_scopes", []),
                "hash_verified": bool(capture.get("status") == "completed" and capture.get("content_hash")),
                "storage_protection": capture.get("storage_protection"), "failure_code": capture.get("failure_code")}

    @staticmethod
    def _turn_playback(turn, answer, capture):
        result = {"audio_available": bool(answer and str(answer.get("audio_uri") or "").startswith(("/media/", "private-file://"))),
                  "video_available": False, "start_seconds": None, "end_seconds": None, "timing_source": None}
        if not answer or not capture or capture.get("status") != "completed" or "video_recording" not in capture.get("consented_scopes", []):
            return result
        info = capture.get("provider_result") or {}
        files = info.get("file_results") or []
        file_info = files[0] if files else {}
        try:
            origin = int(file_info.get("started_at") or info.get("started_at") or 0) / 1e9
            if not origin:
                origin = datetime.fromisoformat(capture["started_at"].replace("Z", "+00:00")).timestamp()
            start = datetime.fromisoformat((answer.get("recording_started_at") or turn["started_at"]).replace("Z", "+00:00")).timestamp() - origin
            end = datetime.fromisoformat((answer.get("recording_finished_at") or answer["created_at"]).replace("Z", "+00:00")).timestamp() - origin
            duration = int(file_info.get("duration") or 0) / 1e9
            if duration > 0:
                end = min(end, duration)
            start = max(0, start)
            if end <= start:
                return result
            result.update(video_available=True, start_seconds=round(start, 3), end_seconds=round(end, 3),
                          timing_source="server_capture_window" if answer.get("recording_started_at") and answer.get("recording_finished_at") else "server_turn_window")
        except (ValueError, TypeError, KeyError):
            pass
        return result

    def recording_url(self, interview_id: str, organization_id: str = "org_default", *, reviewer_id: str = "reviewer_local"):
        self.interviews.get_interview(interview_id, organization_id)
        with self.persistence.transaction(organization_id) as transaction:
            capture = next((item for item in transaction.interview_media_captures.list()
                            if item.get("interview_id") == interview_id), None)
            if not self._recording_projection(capture)["available"]:
                raise ApiError("RECORDING_NOT_READY", "录像正在封存、不可用或已清除，请稍后刷新。", status_code=409)
            token = self.media_signer.issue("private-media-capture://" + capture["id"], expires_seconds=300,
                claims={"organization_id": organization_id, "interview_id": interview_id,
                        "capture_hash": capture["content_hash"], "reviewer_id": reviewer_id})
            self._audit_recording_access(transaction, capture, reviewer_id, "granted")
        return {"url": "/api/v1/private-media/" + token, "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "content_type": "video/mp4", "access_mode": "signed_private"}

    def open_playback_grant(self, token: str):
        payload = self.media_signer.verify(token)
        reference = str(payload["object_key"])
        if not reference.startswith("private-media-capture://"):
            return self.open_audio_grant(token)
        claims = payload.get("claims") or {}
        with self.persistence.transaction(str(claims.get("organization_id") or "")) as transaction:
            capture = transaction.interview_media_captures.get(reference.removeprefix("private-media-capture://"))
            if (not self._recording_projection(capture)["available"]
                or capture.get("interview_id") != claims.get("interview_id")
                or capture.get("content_hash") != claims.get("capture_hash")):
                raise ApiError("RECORDING_NOT_FOUND", "录像不可用或已清除。", status_code=404)
            self._audit_recording_access(transaction, capture, str(claims.get("reviewer_id") or "signed_grant"), "downloaded")
        return {"byte_count": capture["byte_count"], "content_type": "video/mp4",
                "read_range": lambda start, end: self.private_storage.iter_bytes(capture["object_key"], start=start, end=end)}

    @staticmethod
    def _audit_recording_access(transaction, capture, actor_id, action):
        transaction.audit_events.add({"id": new_id("audit"), "organization_id": capture["organization_id"],
            "actor_id": actor_id, "action": "interview.recording." + action, "resource_type": "interview_media_capture",
            "resource_id": capture["id"], "metadata": {"interview_id": capture["interview_id"]}, "created_at": utc_now()})

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
            root = answer.get("root_turn_id") or answer["turn_id"]
            if any(item.get("evaluation_status") == "pending" for item in interview.get("answers", [])
                   if (item.get("root_turn_id") or item["turn_id"]) == root):
                raise ApiError("TRANSCRIPTION_REVIEW_BUSY", "本题正在评分，请等待后刷新。", status_code=409)
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
            interview.pop("review_completion", None)
            _, work = self.interviews._decide_and_persist(transaction, interview, LifecycleCommand(
                LifecycleCommandType.REGRADE_REQUESTED,
                {"answer_id": answer_id, "trigger_reason": "transcript_corrected"}), organization_id)
        evaluation_work = self.interviews._work_by_kind(work, "answer.evaluate")
        evaluation = await self.interviews._process_evaluation_work(evaluation_work["id"], organization_id)
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
        if audio_uri.startswith("private-file://"):
            file_id = audio_uri.removeprefix("private-file://")
            with self.persistence.transaction(organization_id) as transaction:
                file_object = transaction.file_objects.get(file_id)
            if (
                file_object is None or file_object.get("purpose") != "candidate_answer_audio"
                or file_object.get("interview_id") != interview_id or file_object.get("status") != "ready"
            ):
                raise ApiError("ANSWER_AUDIO_NOT_FOUND", "Answer audio does not exist.", status_code=404)
            signed_object = audio_uri
        elif audio_uri.startswith("/media/"):
            signed_object = audio_uri.removeprefix("/media/")
        else:
            raise ApiError("ANSWER_AUDIO_UNMANAGED", "Answer audio is not managed by private media storage.", status_code=409)
        token = self.media_signer.issue(
            signed_object,
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
        claims = payload.get("claims") or {}
        organization_id = str(claims.get("organization_id") or "org_default")
        signed_object = str(payload["object_key"])
        interview = self.interviews.get_interview(str(claims.get("interview_id") or ""), organization_id)
        answer = next((item for item in interview.get("answers", []) if item["id"] == claims.get("answer_id")), None)
        if not answer or str(answer.get("audio_uri") or "").removeprefix("/media/") != signed_object:
            raise ApiError("ANSWER_AUDIO_NOT_FOUND", "回答录音已清除或不属于该许可。", status_code=404)
        if signed_object.startswith("private-file://"):
            file_id = signed_object.removeprefix("private-file://")
            with self.persistence.transaction(organization_id) as transaction:
                file_object = transaction.file_objects.get(file_id)
            if (
                file_object is None or file_object.get("purpose") != "candidate_answer_audio"
                or file_object.get("status") != "ready" or not file_object.get("object_key")
            ):
                raise ApiError("ANSWER_AUDIO_NOT_FOUND", "Answer audio does not exist.", status_code=404)
            content = self.private_storage.open(str(file_object["object_key"]))
            content_type = str(file_object.get("content_type") or "application/octet-stream")
        else:
            path = (self.media_root / signed_object).resolve()
            if self.media_root not in path.parents:
                raise ApiError("MEDIA_ACCESS_INVALID", "Media access token is invalid.", status_code=403)
            if not path.is_file():
                raise ApiError("ANSWER_AUDIO_NOT_FOUND", "Answer audio does not exist.", status_code=404)
            content_types = {".webm": "audio/webm", ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".wav": "audio/wav"}
            content = path.read_bytes()
            content_type = content_types.get(path.suffix.lower(), "application/octet-stream")
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
        return {"content": content, "content_type": content_type}

    def complete_review(
        self, interview_id: str, payload: Dict[str, Any], organization_id: str = "org_default"
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            interview = transaction.interview_sessions.get(interview_id)
            if interview is None:
                raise ApiError("INTERVIEW_NOT_FOUND", "Interview does not exist.", status_code=404)
            evaluations = {item["id"]: item for item in interview.get("evaluation_revisions", [])}
            if any(answer.get("evaluation_status") != "completed"
                or (evaluation := evaluations.get(answer.get("current_evaluation_id"))) is None
                or project_evaluation(evaluation, evaluation_answers(interview, evaluation))["score"] is None
                for answer in interview.get("answers", [])):
                raise ApiError("REVIEW_EVIDENCE_UNRESOLVED", "请先完成后台评分，再完成复核。", status_code=409)
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
