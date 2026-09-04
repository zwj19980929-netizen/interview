import asyncio
import json
import os
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.domain.interview_agent import ClientCapabilities, ClientSignal, OpenAgentSession
from app.repositories.provider import get_store
from app.services.agent_ticket import InterviewAgentTicketService
from app.services.interview_agent import InterviewAgentRuntime
from app.transport.http.responses import ApiJSONResponse


router = APIRouter(default_response_class=ApiJSONResponse)


@router.websocket("/api/v1/interviews/{interview_id}/agent")
async def interview_agent(websocket: WebSocket, interview_id: str) -> None:
    """Unified control/event channel; audio/video media remains on WebRTC."""

    await websocket.accept()
    store = get_store()
    channel = None
    pump_task: Optional[asyncio.Task] = None
    try:
        consumed = InterviewAgentTicketService(store).consume(
            interview_id,
            websocket.query_params.get("ticket", ""),
            organization_id=os.getenv(
                "INTERVIEWER_ORGANIZATION_ID", "org_default"
            ),
        )
        first = await websocket.receive_json()
        if not isinstance(first, dict) or first.get("type") != "session.open":
            raise ApiError(
                "AGENT_SESSION_OPEN_REQUIRED",
                "The first agent message must be session.open.",
                status_code=422,
            )
        opened = OpenAgentSession(
            interview_id=interview_id,
            principal=consumed.principal,
            connection_id=consumed.connection_id,
            recovery_cursor=int((first.get("payload") or {}).get("recovery_cursor", 0)),
            capabilities=ClientCapabilities.model_validate(
                (first.get("payload") or {}).get("capabilities") or {}
            ),
        )
        channel = await InterviewAgentRuntime(store).open(opened)

        async def send_events() -> None:
            assert channel is not None
            async for event in channel.events():
                await websocket.send_json(event.model_dump(mode="json"))
            try:
                await websocket.close(
                    code=1013,
                    reason="agent event transport requires reconnect",
                )
            except RuntimeError:
                pass

        pump_task = asyncio.create_task(send_events())
        while True:
            packet = await websocket.receive()
            if packet["type"] == "websocket.disconnect":
                break
            if packet.get("bytes") is not None:
                raise ApiError(
                    "LIVEKIT_EVIDENCE_AUDIO_REQUIRED",
                    "Audio and video evidence must use the authorized WebRTC media plane; the Agent WebSocket accepts control events only.",
                    status_code=409,
                )
            if packet.get("text") is None:
                continue
            if len(packet["text"].encode("utf-8")) > 64 * 1024:
                raise ApiError(
                    "AGENT_MESSAGE_TOO_LARGE",
                    "Agent JSON messages are limited to 65536 bytes.",
                    status_code=422,
                )
            message = json.loads(packet["text"])
            if not isinstance(message, dict):
                raise ApiError(
                    "AGENT_MESSAGE_INVALID",
                    "Agent messages must be JSON objects.",
                    status_code=422,
                )
            await channel.send(
                ClientSignal(
                    type=message.get("type"),
                    idempotency_key=(
                        message.get("idempotency_key")
                        or message.get("idempotencyKey")
                        or new_id("signal")
                    ),
                    turn_id=message.get("turn_id") or message.get("turnId"),
                    causation_id=message.get("causation_id") or message.get("causationId"),
                    payload=message.get("payload") or {},
                )
            )
    except (ApiError, ValidationError, json.JSONDecodeError, TypeError, ValueError) as exc:
        code = getattr(exc, "code", "AGENT_MESSAGE_INVALID")
        message = getattr(exc, "message", str(exc))
        try:
            await websocket.send_json(
                {
                    "event_id": new_id("agent_problem"),
                    "session_sequence": 1,
                    "type": "problem",
                    "turn_id": None,
                    "causation_id": None,
                    "occurred_at": utc_now(),
                    "replayability": "transient",
                    "payload": {
                        "code": code,
                        "message": message,
                        "recoverable": False,
                    },
                }
            )
        except RuntimeError:
            pass
        try:
            await websocket.close(
                code=4403 if getattr(exc, "status_code", 422) == 403 else 4400
            )
        except RuntimeError:
            pass
    except WebSocketDisconnect:
        pass
    finally:
        if channel is not None:
            try:
                await channel.close("websocket_disconnected")
            except asyncio.CancelledError:
                raise
            except BaseException:
                # The channel already completed fail-closed cleanup; a provider
                # shutdown failure must not mask WebSocket teardown.
                pass
        if pump_task is not None:
            pump_task.cancel()
            try:
                await pump_task
            except (asyncio.CancelledError, RuntimeError, WebSocketDisconnect):
                pass
