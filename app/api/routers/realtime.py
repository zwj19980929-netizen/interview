import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.auth import authenticate_interviewer_websocket
from app.core.errors import ApiError
from app.repositories.provider import get_store
from app.services.realtime import RealtimeInterviewSession
from app.services.streaming_stt import StreamingInterviewSTT
from app.transport.http.responses import ApiJSONResponse
from app.transport.realtime import (
    broadcast_answer_result,
    live_connections,
    streaming_event_projection,
)


router = APIRouter(default_response_class=ApiJSONResponse)

@router.websocket("/api/v1/interviews/{interview_id}/live")
async def interview_live(websocket: WebSocket, interview_id: str) -> None:
    await websocket.accept()
    store = get_store()
    role = websocket.query_params.get("role", "interviewer")
    session = RealtimeInterviewSession(store, interview_id, participant_role=role)
    try:
        try:
            if role != "candidate" and authenticate_interviewer_websocket(websocket) is not None:
                raise ApiError(
                    "AUTHORIZATION_FORBIDDEN",
                    "Interviewer WebSocket authorization failed.",
                    status_code=403,
                )
            if role == "candidate":
                session.validate_candidate_token(websocket.query_params.get("token"))
            initial_events = session.initial_events()
        except ApiError as exc:
            await websocket.send_json(
                {
                    "type": "error",
                    "interview_id": interview_id,
                    "payload": {"code": exc.code, "message": exc.message, "details": exc.details},
                }
            )
            await websocket.close(code=4403 if exc.status_code == 403 else 4404)
            return
        live_connections.connect(interview_id, websocket, role)
        for event in initial_events:
            await websocket.send_json(event)
        while True:
            packet = await websocket.receive()
            if packet["type"] == "websocket.disconnect":
                break
            try:
                if packet.get("bytes") is not None:
                    session.handle_binary(packet["bytes"])
                    continue
                if packet.get("text") is None:
                    continue
                message = json.loads(packet["text"])
                events = await session.handle_event(message)
                await live_connections.broadcast(interview_id, events)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {
                        "type": "error",
                        "interview_id": interview_id,
                        "payload": {"code": "REALTIME_MESSAGE_INVALID", "message": "Realtime message must be valid JSON."},
                    }
                )
            except ApiError as exc:
                await websocket.send_json(
                    {
                        "type": "error",
                        "interview_id": interview_id,
                        "payload": {"code": exc.code, "message": exc.message, "details": exc.details},
                    }
                )
    except WebSocketDisconnect:
        return
    finally:
        session.close()
        live_connections.disconnect(interview_id, websocket)


@router.websocket("/api/v1/interviews/{interview_id}/stt-stream")
async def interview_stt_stream(websocket: WebSocket, interview_id: str) -> None:
    await websocket.accept()
    stream = StreamingInterviewSTT(get_store(), interview_id)
    result_broadcasted = False
    try:
        stream.validate_candidate_token(websocket.query_params.get("token"))
        while True:
            packet = await websocket.receive()
            if packet["type"] == "websocket.disconnect":
                break
            try:
                if packet.get("bytes") is not None:
                    streamed = await stream.send_audio(packet["bytes"])
                    for event in streamed:
                        await websocket.send_json(event)
                    if streamed:
                        await live_connections.broadcast(
                            interview_id,
                            [streaming_event_projection(interview_id, stream.turn_id, event) for event in streamed],
                        )
                    continue
                if packet.get("text") is None:
                    continue
                message = json.loads(packet["text"])
                event_type = message.get("type")
                if event_type == "stream.open":
                    events = await stream.open(message.get("payload") or {})
                elif event_type == "stream.finish":
                    async def deliver_dialogue(event: dict) -> None:
                        # Dialogue audio is delivered on the candidate's STT
                        # socket only. Broadcasting it again on /live would make
                        # the same browser play every PCM delta twice.
                        await websocket.send_json(event)

                    events = await stream.finish(
                        message.get("payload") or {},
                        on_dialogue_event=deliver_dialogue,
                    )
                else:
                    raise ApiError("STT_STREAM_EVENT_UNSUPPORTED", "STT stream event is not supported.")
                for event in events:
                    await websocket.send_json(event)
                transport_events = [
                    streaming_event_projection(interview_id, stream.turn_id, event)
                    for event in events
                    if str(event.get("type") or "").startswith(
                        ("stream.", "transcript.", "dialogue.", "input.", "output.")
                    )
                ]
                if transport_events:
                    await live_connections.broadcast(interview_id, transport_events)
                if event_type == "stream.finish" and stream.last_result:
                    await broadcast_answer_result(interview_id, stream.last_result)
                    result_broadcasted = True
            except json.JSONDecodeError:
                await websocket.send_json(
                    {"type": "stream.error", "error_code": "STT_STREAM_MESSAGE_INVALID"}
                )
            except ApiError as exc:
                await websocket.send_json(
                    {
                        "type": "stream.error",
                        "error_code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    }
                )
    except ApiError as exc:
        await websocket.send_json(
            {"type": "stream.error", "error_code": exc.code, "message": exc.message}
        )
        await websocket.close(code=4403 if exc.status_code == 403 else 4404)
    except WebSocketDisconnect:
        return
    finally:
        recovered = await stream.close(repair_disconnect=True)
        if recovered:
            await live_connections.broadcast(
                interview_id,
                [streaming_event_projection(interview_id, stream.turn_id, event) for event in recovered],
            )
        if stream.last_result and not result_broadcasted:
            await broadcast_answer_result(interview_id, stream.last_result)
