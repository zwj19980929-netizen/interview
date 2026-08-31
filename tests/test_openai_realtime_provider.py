import asyncio
import base64
import json

import pytest

from app.model_gateway import capabilities as cap
from app.model_gateway.dialogue import ValidatedSpeechDialogueStream
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    ProviderContext,
    RealtimeSpeechDialogueRequest,
    RealtimeSpeechResponseCommand,
    StreamingAudioConfig,
)
from app.providers.openai.provider import OpenAIProvider


class FakeOpenAIRealtimeSocket:
    def __init__(self, *, emit_transcript: bool = True, fail_response_create: bool = False) -> None:
        self.received = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.emit_transcript = emit_transcript
        self.fail_response_create = fail_response_create
        self.received.put_nowait(json.dumps({"type": "session.created", "event_id": "evt_1"}))

    async def send(self, raw):
        payload = json.loads(raw)
        self.sent.append(payload)
        if payload["type"] == "session.update":
            self.received.put_nowait(json.dumps({"type": "session.updated", "event_id": "evt_2"}))
        elif payload["type"] == "response.create":
            if self.fail_response_create:
                raise RuntimeError("socket closed")
            if self.emit_transcript:
                self.received.put_nowait(json.dumps({"type": "response.output_audio_transcript.done", "transcript": "请说明幂等恢复策略。"}))
            self.received.put_nowait(json.dumps({"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x01\x00" * 24).decode("ascii")}))
            self.received.put_nowait(json.dumps({"type": "response.output_audio.done"}))
            self.received.put_nowait(json.dumps({"type": "response.done", "event_id": "evt_3"}))

    async def recv(self):
        return await self.received.get()

    async def close(self):
        self.closed = True


def context() -> ProviderContext:
    return ProviderContext(
        organization_id="org_default",
        invocation_id="inv_openai_realtime",
        route_id="route_openai_realtime",
        provider_connection_id="provider_conn_openai",
        model_configuration_id="model_cfg_openai_realtime",
        model_type="realtime_speech",
        capability=cap.SPEECH_DIALOGUE_REALTIME,
        purpose="candidate_followup_dialogue",
        model="gpt-realtime-2.1-mini",
        timeout_s=5,
        attempt=1,
        fallback_index=0,
        connection_config={
            "realtime_websocket_url": "wss://api.openai.com/v1/realtime",
            "default_voice": "marin",
        },
        credentials={"api_key": "openai-test-key"},
    )


@pytest.mark.anyio
async def test_openai_realtime_uses_ga_session_and_streams_audio() -> None:
    socket = FakeOpenAIRealtimeSocket()

    async def connect(url, **kwargs):
        assert url == "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1-mini"
        assert kwargs["additional_headers"]["Authorization"] == "Bearer openai-test-key"
        return socket

    stream = await OpenAIProvider(websocket_connect=connect).open_dialogue(
        RealtimeSpeechDialogueRequest(
            interview_id="iv_1",
            turn_id="turn_1",
            input_audio=StreamingAudioConfig(
                content_type="audio/pcm", sample_rate_hz=16000, channels=1
            ),
            session_instructions="只播报策略批准的追问",
        ),
        context(),
    )
    await stream.send_audio(b"\x00\x00" * 160)
    events = await stream.commit(
        RealtimeSpeechResponseCommand(
            spoken_text="请说明幂等恢复策略。",
            response_instructions="只逐字朗读：请说明幂等恢复策略。",
        )
    )

    session = next(item for item in socket.sent if item["type"] == "session.update")
    assert session["session"]["type"] == "realtime"
    assert session["session"]["audio"]["input"]["turn_detection"] is None
    assert session["session"]["output_modalities"] == ["audio"]
    append = next(item for item in socket.sent if item["type"] == "input_audio_buffer.append")
    assert len(base64.b64decode(append["audio"])) > 160 * 2
    assert [item.type for item in events] == [
        "output.transcript.final",
        "output.audio.delta",
        "output.audio.done",
        "dialogue.closed",
    ]
    assert socket.closed is True


@pytest.mark.anyio
async def test_validated_realtime_rejects_audio_without_final_transcript() -> None:
    socket = FakeOpenAIRealtimeSocket(emit_transcript=False)

    async def connect(url, **kwargs):
        return socket

    request = RealtimeSpeechDialogueRequest(
        interview_id="iv_1",
        turn_id="turn_1",
        input_audio=StreamingAudioConfig(
            content_type="audio/pcm", sample_rate_hz=16000, channels=1
        ),
        session_instructions="只播报策略批准的追问",
    )
    raw = await OpenAIProvider(websocket_connect=connect).open_dialogue(request, context())
    stream = ValidatedSpeechDialogueStream(raw, request)
    await stream.send_audio(b"\x00\x00" * 160)

    with pytest.raises(ProviderError) as raised:
        await stream.commit(
            RealtimeSpeechResponseCommand(
                spoken_text="请说明幂等恢复策略。",
                response_instructions="只逐字朗读：请说明幂等恢复策略。",
            )
        )

    assert raised.value.code == "provider_final_transcript_missing"


@pytest.mark.anyio
async def test_openai_realtime_maps_commit_socket_failure() -> None:
    socket = FakeOpenAIRealtimeSocket(fail_response_create=True)

    async def connect(url, **kwargs):
        return socket

    stream = await OpenAIProvider(websocket_connect=connect).open_dialogue(
        RealtimeSpeechDialogueRequest(
            interview_id="iv_1",
            turn_id="turn_1",
            input_audio=StreamingAudioConfig(
                content_type="audio/pcm", sample_rate_hz=16000, channels=1
            ),
            session_instructions="只播报策略批准的追问",
        ),
        context(),
    )
    await stream.send_audio(b"\x00\x00" * 160)

    with pytest.raises(ProviderError) as raised:
        await stream.commit(
            RealtimeSpeechResponseCommand(
                spoken_text="请说明幂等恢复策略。",
                response_instructions="只逐字朗读：请说明幂等恢复策略。",
            )
        )

    assert raised.value.code == "provider_stream_interrupted"
