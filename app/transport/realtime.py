from copy import deepcopy
from typing import Any, Dict, List, Optional

from fastapi import WebSocket, WebSocketDisconnect

from app.realtime_bus import realtime_event_bus
from app.core.ids import new_id
from app.core.time import utc_now
from app.repositories.provider import get_store
from app.services.realtime import RealtimeInterviewSession


class LiveConnectionManager:
    """Own local sockets, cross-instance fan-out, and role-safe events."""

    def __init__(self) -> None:
        self.connections: Dict[str, Dict[WebSocket, str]] = {}
        self.bus = realtime_event_bus()

    def connect(self, interview_id: str, websocket: WebSocket, role: str) -> None:
        self.connections.setdefault(interview_id, {})[websocket] = role

    def disconnect(self, interview_id: str, websocket: WebSocket) -> None:
        interview_connections = self.connections.get(interview_id)
        if not interview_connections:
            return
        interview_connections.pop(websocket, None)
        if not interview_connections:
            self.connections.pop(interview_id, None)

    async def broadcast(self, interview_id: str, events: List[Dict[str, Any]]) -> None:
        await self._broadcast_local(interview_id, events)
        for event in events:
            await self.bus.publish(interview_id, event)

    async def _broadcast_local(self, interview_id: str, events: List[Dict[str, Any]]) -> None:
        disconnected: List[WebSocket] = []
        for websocket, role in list(self.connections.get(interview_id, {}).items()):
            try:
                for event in events:
                    await websocket.send_json(self._project_event(event, role))
            except (RuntimeError, WebSocketDisconnect):
                disconnected.append(websocket)
        for websocket in disconnected:
            self.disconnect(interview_id, websocket)

    @staticmethod
    def _project_event(event: Dict[str, Any], role: str) -> Dict[str, Any]:
        if role != "candidate":
            return event
        if event.get("type") == "followup.selected":
            payload = event.get("payload") or {}
            return {
                **event,
                "payload": {
                    "turn_id": payload.get("turn_id"),
                    "parent_turn_id": payload.get("parent_turn_id"),
                    "root_turn_id": payload.get("root_turn_id"),
                    "followup_depth": payload.get("followup_depth", 1),
                    "question_text": payload.get("question_text"),
                },
            }
        if event.get("type") == "followup.requested":
            payload = event.get("payload") or {}
            return {
                **event,
                "payload": {
                    "root_turn_id": payload.get("root_turn_id"),
                    "parent_turn_id": payload.get("parent_turn_id"),
                    "status": "selected",
                },
            }
        if event.get("type") != "evaluation.completed":
            return event
        payload = event.get("payload") or {}
        return {
            **event,
            "payload": {
                "turn_id": payload.get("turn_id"),
                "status": "completed",
            },
        }

    async def consume_remote(self) -> None:
        await self.bus.subscribe(
            lambda interview_id, event: self._broadcast_local(interview_id, [event])
        )


live_connections = LiveConnectionManager()


async def broadcast_interview_state(interview_id: str) -> None:
    projection = RealtimeInterviewSession(get_store(), interview_id)
    await live_connections.broadcast(interview_id, projection.state_change_events())


def realtime_event(
    interview_id: str,
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    turn_id: Optional[str] = None,
    request_id: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "type": event_type,
        "request_id": request_id or new_id("evt"),
        "interview_id": interview_id,
        "turn_id": turn_id,
        "ts": occurred_at or utc_now(),
        "payload": deepcopy(payload or {}),
    }


def streaming_event_projection(
    interview_id: str,
    turn_id: Optional[str],
    event: Dict[str, Any],
) -> Dict[str, Any]:
    raw_type = str(event.get("type") or "stream.error")
    mapped_type = {
        "transcript.partial": "stt.transcript.partial",
        "transcript.final": "stt.transcript.final",
        "stream.ready": "stt.stream.ready",
        "stream.closed": "stt.stream.closed",
        "stream.error": "stt.stream.error",
    }.get(raw_type, raw_type)
    payload = {
        key: deepcopy(value)
        for key, value in event.items()
        if key not in {"type", "provider"} and value is not None
    }
    provider = event.get("provider") or {}
    if provider:
        payload["provider"] = {
            "provider_id": provider.get("provider_id"),
            "model": provider.get("model"),
            "request_id": provider.get("request_id"),
        }
    return realtime_event(interview_id, mapped_type, payload, turn_id=turn_id)


def answer_result_events(interview_id: str, result: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for item in result.get("events") or []:
        payload = deepcopy(item.get("payload") or {})
        events.append(
            realtime_event(
                interview_id,
                str(item.get("type") or "interview.event"),
                payload,
                turn_id=payload.get("turn_id"),
                request_id=item.get("id"),
                occurred_at=item.get("occurred_at"),
            )
        )
    return events


async def broadcast_answer_result(interview_id: str, result: Dict[str, Any]) -> None:
    projection = RealtimeInterviewSession(get_store(), interview_id)
    await live_connections.broadcast(
        interview_id,
        answer_result_events(interview_id, result) + projection.state_change_events(),
    )
