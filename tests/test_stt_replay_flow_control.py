"""Exercise the real adapter queue with a socket that drains over time."""
import asyncio
import json

import pytest

from app.model_gateway.errors import ProviderError

from app.model_gateway.schemas import ProviderContext, StreamingAudioConfig, StreamingSTTRequest
from app.model_gateway.streaming import ValidatedSTTStream
from app.providers.dashscope.provider import DashScopeProvider
from app.services.continuous_stt import ContinuousSTT


class SlowSocket:
    def __init__(self, *, broken=False):
        self.broken = broken
        self.received = asyncio.Queue()
        self.audio = []
        self.closed = False
        self.received.put_nowait(json.dumps({"header": {"event": "task-started"}}))

    async def send(self, value):
        if isinstance(value, bytes):
            if self.broken:
                raise OSError("synthetic disconnected transport")
            # Every 100 ms packet takes actual time to send. sleep(0) in the
            # replay producer cannot make an entire queue instantly disappear.
            await asyncio.sleep(0.002)
            self.audio.append(value)
        elif json.loads(value)["header"]["action"] == "finish-task":
            self.received.put_nowait(json.dumps({
                "header": {"event": "result-generated"},
                "payload": {"output": {"sentence": {
                    "sentence_id": 1, "sentence_end": True,
                    "text": "恢复后完整回答。", "begin_time": 0, "end_time": 500,
                }}},
            }))
            self.received.put_nowait(json.dumps({"header": {"event": "task-finished"}}))

    async def recv(self):
        return await self.received.get()

    async def close(self):
        self.closed = True


@pytest.mark.anyio
async def test_slow_network_replays_long_backlog_while_recording_new_audio_once():
    request = StreamingSTTRequest(interview_id="synthetic-replay", turn_id="turn",
        audio=StreamingAudioConfig(content_type="audio/pcm", sample_rate_hz=16000))
    context = ProviderContext(organization_id="org_default", invocation_id="test",
        route_id="test", provider_connection_id="test", model_configuration_id="test",
        model_type="stt", capability="stt.streaming", purpose="test",
        model="qwen-audio-3.0-asr-flash-streaming", timeout_s=2, attempt=1,
        fallback_index=0, credentials={"api_key": "synthetic"},
        connection_config={"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"})
    sockets = [SlowSocket(broken=True), SlowSocket()]
    opened, raw_streams, recorded = [], [], []

    async def connect(*args, **kwargs):
        socket = sockets[len(opened)]
        opened.append(socket)
        return socket

    provider = DashScopeProvider(websocket_connect=connect)

    async def reopen():
        raw = await provider.open_stream(request, context)
        raw_streams.append(raw)
        return ValidatedSTTStream(raw, request)

    capture = ContinuousSTT(await reopen(), reopen=reopen, record=recorded.append,
                            bytes_per_second=32000)
    frames = [(i + 1).to_bytes(2, "little") * 320 for i in range(1200)]
    arrivals = [b"\x02\x01" * 320] * 120
    try:
        for frame in frames:
            await capture.send_audio(frame)
        assert capture.recovery_required
        repair = asyncio.create_task(capture.recover())
        for frame in arrivals:
            await capture.send_audio(frame)
            await asyncio.sleep(0.001)
        await asyncio.wait_for(repair, 4)
        assert not capture.recovery_required
        final = await capture.snapshot(resume=False)
        assert final and final.is_final
        expected = b"".join(frames + arrivals)
        assert b"".join(recorded) == expected
        assert b"".join(sockets[1].audio) == expected
        assert len(opened) == 2
        assert capture.forwarded_bytes == len(expected)
    finally:
        await capture.abort()


@pytest.mark.anyio
async def test_replay_capacity_wait_is_cancel_safe_and_abort_wakes_waiters():
    from test_dashscope_provider import BlockingSendDashScopeSocket, context
    socket = BlockingSendDashScopeSocket()
    async def connect(*args, **kwargs):
        return socket
    request = StreamingSTTRequest(interview_id="synthetic", turn_id="turn",
        audio=StreamingAudioConfig(content_type="audio/pcm", sample_rate_hz=16000))
    raw = await DashScopeProvider(websocket_connect=connect).open_stream(request,
        context("stt.streaming", "qwen-audio-3.0-asr-flash-streaming"))
    stream = ValidatedSTTStream(raw, request)
    try:
        for _ in range(250):
            await stream.send_audio(bytes(640))
        await socket.send_started.wait()
        sent_before = stream.byte_count
        waiting = asyncio.create_task(stream.send_audio(bytes(640), wait_for_capacity=True))
        await asyncio.sleep(.01)
        assert not waiting.done() and stream.byte_count == sent_before
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert stream.byte_count == sent_before
        waiting = asyncio.create_task(stream.send_audio(bytes(640), wait_for_capacity=True))
        await asyncio.sleep(.01)
        await stream.abort()
        with pytest.raises(ProviderError, match="already closed"):
            await asyncio.wait_for(waiting, .2)
    finally:
        await stream.abort()
