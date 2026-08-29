from typing import Any, Dict, List, Optional

from app.adapters.private_media import media_recording_storage
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.repositories.memory import InMemoryStore
from app.persistence.provider import persistence_for
from app.services.interviews import InterviewService


class RealtimeInterviewSession:
    def __init__(self, store: InMemoryStore, interview_id: str, *, participant_role: str = "interviewer") -> None:
        self.store = store
        self.interview_id = interview_id
        self.participant_role = participant_role
        self.interviews = InterviewService(store)
        self.persistence = persistence_for(store)
        self.media = media_recording_storage(self.persistence)
        self.recording: Optional[Any] = None
        self.recording_turn_id: Optional[str] = None

    def validate_candidate_token(self, token: Optional[str]) -> None:
        self.interviews.validate_candidate_token(self.interview_id, token)

    def initial_events(self) -> List[Dict[str, Any]]:
        interview = self.interviews.get_interview(self.interview_id)
        return self._state_events(interview, include_completion=False)

    def state_change_events(self) -> List[Dict[str, Any]]:
        interview = self.interviews.get_interview(self.interview_id)
        return self._state_events(interview, include_completion=True)

    def _state_events(self, interview: Dict[str, Any], *, include_completion: bool) -> List[Dict[str, Any]]:
        events = [
            self._event(
                "session.state.changed",
                {
                    "status": interview["status"],
                    "current_turn_id": interview.get("current_turn_id"),
                },
            )
        ]
        current_turn = self._current_turn(interview)
        if current_turn:
            events.append(self._question_event(current_turn))
        elif include_completion and interview["status"] == "report_ready":
            events.append(self._event("interview.completed", {"status": "report_ready", "report_ready": True}))
        return events

    async def handle_event(self, message: Dict[str, Any]) -> List[Dict[str, Any]]:
        event_type = message.get("type")
        payload = message.get("payload") or {}
        if event_type == "session.ready":
            interview = self.interviews.mark_participant_ready(
                self.interview_id,
                participant=self.participant_role,
                source=payload.get("source", "web"),
            )
            return [
                self._event(
                    "session.participant.ready",
                    {"participant": self.participant_role, "source": payload.get("source", "web")},
                ),
                self._event(
                    "session.state.changed",
                    {"status": interview["status"], "current_turn_id": interview.get("current_turn_id")},
                ),
            ]
        if event_type.startswith("interviewer.control."):
            return self._handle_interviewer_control(event_type, payload)
        if event_type == "ping":
            heartbeat = self.interviews.record_heartbeat(
                self.interview_id, participant=self.participant_role
            )
            return [self._event("pong", {"received_at": heartbeat["received_at"]})]
        if event_type == "candidate.media.start":
            return [self._start_recording(message.get("turn_id"), payload)]
        if event_type == "candidate.media.stop":
            return [self._stop_recording()]
        if event_type == "candidate.transcript.partial":
            return [self._transcript_event("stt.transcript.partial", message.get("turn_id"), payload)]
        raise ApiError("REALTIME_EVENT_UNSUPPORTED", "Realtime event is not supported.")

    def _handle_interviewer_control(self, event_type: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        if self.participant_role != "interviewer":
            raise ApiError(
                "INTERVIEWER_CONTROL_FORBIDDEN",
                "Only an interviewer connection can control the interview lifecycle.",
                status_code=403,
            )
        reason = str(payload.get("reason") or "interviewer_control")
        controls = {
            "interviewer.control.pause": lambda: self.interviews.pause_interview(self.interview_id, reason),
            "interviewer.control.resume": lambda: self.interviews.resume_interview(self.interview_id, reason),
            "interviewer.control.recover": lambda: self.interviews.recover_interview(self.interview_id, reason),
            "interviewer.control.next_question": lambda: self.interviews.skip_current_turn(self.interview_id, reason),
            "interviewer.control.complete": lambda: self.interviews.complete_interview(self.interview_id),
            "interviewer.control.cancel": lambda: self.interviews.cancel_interview(self.interview_id, reason),
        }
        control = controls.get(event_type)
        if control is None:
            raise ApiError("REALTIME_EVENT_UNSUPPORTED", "Realtime lifecycle control is not supported.")
        control()
        return self.state_change_events()

    def handle_binary(self, chunk: bytes) -> None:
        if not self.recording:
            raise ApiError("MEDIA_RECORDING_NOT_STARTED", "Start media recording before sending audio chunks.", status_code=409)
        self.recording.append(chunk)

    def close(self) -> None:
        if self.recording:
            self.recording.abort()
            self.recording = None
            self.recording_turn_id = None

    def _start_recording(self, turn_id: Optional[str], payload: Dict[str, Any]) -> Dict[str, Any]:
        active_turn = self.interviews.require_active_turn(self.interview_id, turn_id)
        if self.recording:
            raise ApiError("MEDIA_RECORDING_ALREADY_STARTED", "Media recording is already active.", status_code=409)
        mime_type = str(payload.get("mime_type") or "")
        self.recording = self.media.start_recording(self.interview_id, active_turn["id"], mime_type)
        self.recording_turn_id = active_turn["id"]
        return self._event(
            "media.recording.started",
            {"mime_type": mime_type, "timeslice_ms": int(payload.get("timeslice_ms", 400))},
            turn_id=active_turn["id"],
        )

    def _stop_recording(self) -> Dict[str, Any]:
        if not self.recording:
            raise ApiError("MEDIA_RECORDING_NOT_STARTED", "No active media recording exists.", status_code=409)
        result = self.recording.finish()
        turn_id = self.recording_turn_id
        self.recording = None
        self.recording_turn_id = None
        return self._event(
            "media.recording.stopped",
            {
                "audio_uri": result.audio_uri,
                "mime_type": result.mime_type,
                "byte_count": result.byte_count,
            },
            turn_id=turn_id,
        )

    def _transcript_event(self, event_type: str, turn_id: Optional[str], payload: Dict[str, Any]) -> Dict[str, Any]:
        self._require_active_turn(turn_id)
        text = str(payload.get("text") or "").strip()
        return self._event(
            event_type,
            {
                "text": text,
                "confidence": float(payload.get("confidence", 0.0)),
                "language": payload.get("language", "zh-CN"),
                "source": payload.get("source", "browser_speech_fallback"),
            },
            turn_id=turn_id,
        )

    def _require_active_turn(self, turn_id: Optional[str]) -> None:
        self.interviews.require_active_turn(self.interview_id, turn_id)

    def _interview(self) -> Dict[str, Any]:
        return self.interviews.get_interview(self.interview_id)

    def _current_turn(self, interview: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        current_turn_id = interview.get("current_turn_id")
        return next((turn for turn in interview.get("turns", []) if turn["id"] == current_turn_id), None)

    def _question_event(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        return self._event(
            "question.selected",
            {
                "order": turn["order"],
                "question_text": turn["question_spoken_text"],
                "status": turn["status"],
            },
            turn_id=turn["id"],
        )

    def _event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        *,
        turn_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "type": event_type,
            "request_id": new_id("evt"),
            "interview_id": self.interview_id,
            "turn_id": turn_id,
            "ts": utc_now(),
            "payload": payload,
        }
