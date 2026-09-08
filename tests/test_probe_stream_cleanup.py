"""Synthetic probes only: no vendor calls, candidate audio, or real credentials."""

import asyncio
import json

import pytest

from app.core.prompt.realtime_dialogue import session_instruction
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.streaming import ValidatedSTTStream
from app.model_gateway.schemas import (
    ProviderContext,
    RealtimeSpeechDialogueEvent,
    RealtimeSpeechDialogueRequest,
    StreamingAudioConfig,
    StreamingSTTEvent,
    StreamingSTTRequest,
)
from app.providers.dashscope.provider import DashScopeProvider, DashScopeSTTStream
from app.providers.mock.provider import MockProvider
from app.repositories.memory import InMemoryStore


def stt_request():
    return StreamingSTTRequest(
        interview_id="synthetic_probe", turn_id="synthetic_turn",
        audio=StreamingAudioConfig(content_type="audio/pcm", sample_rate_hz=16000, channels=1),
    )


def stt_context(timeout_s=0.02, **config):
    return ProviderContext(
        organization_id="org_default", invocation_id="synthetic_invocation", route_id="synthetic_route",
        provider_connection_id="synthetic_connection", model_configuration_id="synthetic_model", model_type="stt",
        capability=cap.STT_STREAMING, purpose="synthetic_probe", model="qwen-audio-3.0-asr-flash-streaming",
        timeout_s=timeout_s, attempt=1, fallback_index=0,
        connection_config={"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", **config},
        credentials={"api_key": "synthetic-not-real"},
    )


class ProbeSocket:
    def __init__(self, *, fault="recv_wait", slow_close=False, bad_close=False):
        self.fault = fault
        self.slow_close = slow_close
        self.bad_close = bad_close
        self.waiting = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_finished = asyncio.Event()
        self.close_calls = 0
        self.task_id = ""

    async def send(self, value):
        self.task_id = json.loads(value)["header"]["task_id"]
        if self.fault == "send_error":
            raise RuntimeError("synthetic send failure")
        if self.fault == "send_wait":
            self.waiting.set()
            await asyncio.sleep(60)

    async def recv(self):
        if self.fault == "recv_error":
            raise RuntimeError("synthetic receive failure")
        if self.fault == "invalid_json":
            return "not-json"
        if self.fault == "unexpected_event":
            return json.dumps({"header": {"event": "task-finished", "task_id": self.task_id}})
        self.waiting.set()
        await asyncio.sleep(60)

    async def close(self):
        self.close_calls += 1
        self.close_started.set()
        try:
            if self.bad_close:
                raise RuntimeError("synthetic cleanup failure")
            if self.slow_close:
                await asyncio.sleep(60)
        finally:
            self.close_finished.set()


async def open_socket(socket, context=None):
    async def connect(*args, **kwargs):
        return socket

    return await DashScopeProvider(websocket_connect=connect).open_stream(stt_request(), context or stt_context())


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["send_wait", "recv_wait"])
async def test_dashscope_cancelled_handshake_closes_connected_socket(stage):
    socket = ProbeSocket(fault=stage)
    task = asyncio.create_task(open_socket(socket))
    await socket.waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.close_calls == 1 and socket.close_finished.is_set()


@pytest.mark.anyio
async def test_dashscope_outer_timeout_closes_socket_before_gateway_reports_timeout():
    socket = ProbeSocket()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(open_socket(socket), timeout=0.02)
    assert socket.close_calls == 1 and socket.close_finished.is_set()


@pytest.mark.anyio
@pytest.mark.parametrize("fault", ["send_error", "recv_error", "invalid_json", "unexpected_event"])
async def test_dashscope_handshake_errors_close_without_masking_original(fault):
    socket = ProbeSocket(fault=fault, bad_close=True)
    with pytest.raises((RuntimeError, ProviderError)) as error:
        await open_socket(socket)
    assert "cleanup failure" not in str(error.value)
    assert socket.close_calls == 1 and socket.close_finished.is_set()


@pytest.mark.anyio
async def test_dashscope_constructor_failure_also_closes_acquired_socket(monkeypatch):
    socket = ProbeSocket()
    def invalid_constructor(*args, **kwargs):
        raise ValueError("synthetic initialization error")

    monkeypatch.setattr(DashScopeSTTStream, "__init__", invalid_constructor)
    with pytest.raises(ValueError):
        await open_socket(socket)
    assert socket.close_calls == 1


@pytest.mark.anyio
async def test_dashscope_cleanup_timeout_is_bounded_and_preserves_error():
    socket = ProbeSocket(fault="send_error", slow_close=True)
    with pytest.raises(RuntimeError, match="send failure"):
        await asyncio.wait_for(open_socket(socket), timeout=0.2)
    assert socket.close_calls == 1 and socket.close_finished.is_set()


class OpenedProbeStream:
    stream_id = "synthetic_stream"

    def __init__(self, capability, *, invalid=False, slow_abort=False, abort_error=False):
        event_type = StreamingSTTEvent if capability == cap.STT_STREAMING else RealtimeSpeechDialogueEvent
        self.ready_events = [event_type(
            stream_id="wrong_stream" if invalid else self.stream_id,
            sequence=1, type="stream.ready" if capability == cap.STT_STREAMING else "dialogue.ready",
        )]
        self.slow_abort = slow_abort
        self.abort_error = abort_error
        self.abort_calls = 0
        self.abort_started = asyncio.Event()
        self.abort_finished = asyncio.Event()
        self.abort_gate = None

    async def abort(self):
        self.abort_calls += 1
        self.abort_started.set()
        try:
            if self.abort_error:
                raise RuntimeError("synthetic abort failure")
            if self.abort_gate is not None:
                await self.abort_gate.wait()
            if self.slow_abort:
                await asyncio.sleep(60)
        finally:
            self.abort_finished.set()


def gateway_open(capability, source, monkeypatch=None, failure=None):
    class Adapter(MockProvider):
        async def open_stream(self, request, context):
            return source

        async def open_dialogue(self, request, context):
            return source

    gateway = ModelGateway(InMemoryStore(), {"mock": Adapter()})
    route = gateway._resolve_route("org_default", capability, "synthetic_probe")
    route["primary"]["timeout_s"] = 0.02
    if failure is not None:
        def broken_audit(**kwargs):
            raise failure

        monkeypatch.setattr(gateway, "_log_invocation", broken_audit)
    if capability == cap.STT_STREAMING:
        return gateway.open_stream(stt_request(), route=route)
    return gateway.open_speech_dialogue(RealtimeSpeechDialogueRequest(
        interview_id="synthetic_probe", turn_id="synthetic_turn", session_instructions=session_instruction(),
    ), route=route)


@pytest.mark.anyio
@pytest.mark.parametrize("capability", [cap.STT_STREAMING, cap.SPEECH_DIALOGUE_REALTIME])
async def test_gateway_invalid_ready_aborts_before_error_or_fallback(capability):
    source = OpenedProbeStream(capability, invalid=True, abort_error=True)
    with pytest.raises(ProviderError) as error:
        await gateway_open(capability, source)
    assert error.value.code == "provider_schema_invalid"
    assert source.abort_calls == 1 and source.abort_finished.is_set()


@pytest.mark.anyio
@pytest.mark.parametrize("capability", [cap.STT_STREAMING, cap.SPEECH_DIALOGUE_REALTIME])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_gateway_post_open_audit_failure_or_cancel_aborts(capability, cancelled, monkeypatch):
    source = OpenedProbeStream(capability)
    failure = asyncio.CancelledError() if cancelled else RuntimeError("synthetic audit failure")
    with pytest.raises(type(failure)):
        await gateway_open(capability, source, monkeypatch, failure)
    assert source.abort_calls == 1 and source.abort_finished.is_set()


@pytest.mark.anyio
@pytest.mark.parametrize("capability", [cap.STT_STREAMING, cap.SPEECH_DIALOGUE_REALTIME])
async def test_gateway_abort_timeout_does_not_hold_probe_open(capability):
    source = OpenedProbeStream(capability, invalid=True, slow_abort=True)
    with pytest.raises(ProviderError):
        await asyncio.wait_for(gateway_open(capability, source), timeout=0.2)
    assert source.abort_calls == 1 and source.abort_finished.is_set()


@pytest.mark.anyio
async def test_gateway_second_cancellation_does_not_orphan_bounded_cleanup(monkeypatch):
    source = OpenedProbeStream(cap.STT_STREAMING)
    source.abort_gate = asyncio.Event()
    task = asyncio.create_task(gateway_open(cap.STT_STREAMING, source, monkeypatch, asyncio.CancelledError()))
    await source.abort_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    source.abort_gate.set()
    await asyncio.wait_for(source.abort_finished.wait(), timeout=0.2)
    assert source.abort_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("capability", [cap.STT_STREAMING, cap.SPEECH_DIALOGUE_REALTIME])
async def test_gateway_success_keeps_stream_owned_by_caller(capability):
    source = OpenedProbeStream(capability)
    managed = await gateway_open(capability, source)
    assert source.abort_calls == 0
    await managed.abort()
    assert source.abort_calls == 1


class AbortableTransport:
    def __init__(self, *, auto_notify=True, broken=False):
        self.abort_calls = 0
        self.released = asyncio.Event()
        self.physically_closed = False
        self.auto_notify = auto_notify
        self.broken = broken

    def abort(self):
        self.abort_calls += 1
        if self.broken:
            raise RuntimeError("synthetic transport abort failure")
        self.physically_closed = True
        if self.auto_notify:
            asyncio.get_running_loop().call_soon(self.released.set)


class CloseHandshakeSocket(ProbeSocket):
    def __init__(self, *, close_error=False, auto_notify=True, broken_transport=False):
        super().__init__()
        self.transport = AbortableTransport(auto_notify=auto_notify, broken=broken_transport)
        self.peer_acknowledged_close = asyncio.Event()
        self.close_error = close_error
        self.sent = []
        self._task_started = False

    async def send(self, value):
        self.sent.append(value)
        if isinstance(value, str):
            await super().send(value)

    async def recv(self):
        if not self._task_started:
            self._task_started = True
            return json.dumps({"header": {"event": "task-started", "task_id": self.task_id}})
        self.waiting.set()
        await asyncio.sleep(60)

    async def close(self):
        self.close_calls += 1
        self.close_started.set()
        try:
            if self.close_error:
                raise RuntimeError("synthetic close handshake failure")
            await self.peer_acknowledged_close.wait()
            self.transport.physically_closed = True
            self.transport.released.set()
        finally:
            self.close_finished.set()

    async def wait_closed(self):
        await self.transport.released.wait()


@pytest.mark.anyio
async def test_dashscope_abort_forces_transport_after_bounded_graceful_close():
    socket = CloseHandshakeSocket()
    stream = await open_socket(socket)
    await stream.send_audio(b"\x01\x00" * 320)
    workers = [stream._sender_task, stream._reader_task]
    await asyncio.wait_for(stream.abort(), timeout=0.3)
    assert socket.close_calls == 1 and socket.transport.abort_calls == 1
    assert socket.transport.physically_closed and socket.transport.released.is_set()
    assert all(task.done() for task in workers)
    assert stream._pending_audio_bytes == 0 and not stream._audio_send_buffer
    assert not any(isinstance(value, str) and "finish-task" in value for value in socket.sent)
    await stream.abort()
    assert socket.close_calls == 1 and socket.transport.abort_calls == 1


@pytest.mark.anyio
async def test_dashscope_repeat_abort_joins_same_graceful_cleanup():
    socket = CloseHandshakeSocket()
    stream = await open_socket(socket, stt_context(timeout_s=1))
    first = asyncio.create_task(stream.abort())
    await socket.close_started.wait()
    cleanup = stream._abort_task
    second = asyncio.create_task(stream.abort())
    await asyncio.sleep(0)
    assert not first.done() and not second.done() and stream._abort_task is cleanup
    socket.peer_acknowledged_close.set()
    await asyncio.gather(first, second)
    assert socket.close_calls == 1 and socket.transport.abort_calls == 0


@pytest.mark.anyio
async def test_dashscope_abort_caller_cancel_forces_socket_but_preserves_cancellation():
    socket = CloseHandshakeSocket()
    stream = await open_socket(socket)
    caller = asyncio.create_task(stream.abort())
    await socket.close_started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    # Resource reclamation must already have happened before cancellation exits.
    assert socket.transport.physically_closed
    await asyncio.wait_for(stream.abort(), timeout=0.3)
    assert socket.close_calls == 1 and socket.transport.abort_calls == 1


@pytest.mark.anyio
async def test_dashscope_repeated_caller_cancel_cannot_cancel_shared_cleanup():
    socket = CloseHandshakeSocket(auto_notify=False)
    stream = await open_socket(socket)
    first = asyncio.create_task(stream.abort())
    await socket.close_started.wait()
    cleanup = stream._abort_task
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(stream.abort())
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert not cleanup.cancelled() and stream._abort_task is cleanup
    assert socket.transport.physically_closed and socket.transport.abort_calls == 1
    socket.transport.released.set()
    await asyncio.wait_for(stream.abort(), timeout=0.3)
    assert cleanup.done() and not cleanup.cancelled()


@pytest.mark.anyio
async def test_dashscope_abort_close_error_recovers_transport_without_false_failure():
    socket = CloseHandshakeSocket(close_error=True)
    stream = await open_socket(socket)
    await stream.abort()
    assert socket.transport.physically_closed and socket.transport.abort_calls == 1


@pytest.mark.anyio
async def test_dashscope_abort_cleanup_failure_does_not_replace_original_cancel():
    socket = CloseHandshakeSocket(broken_transport=True)
    stream = await open_socket(socket)
    caller = asyncio.create_task(stream.abort())
    await socket.close_started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    # A later join observes the cleanup failure instead of silently accepting
    # ``closed=True`` as evidence that the underlying transport was reclaimed.
    with pytest.raises(ProviderError) as error:
        await asyncio.wait_for(stream.abort(), timeout=0.3)
    assert error.value.code == "provider_stream_close_failed"


@pytest.mark.anyio
async def test_dashscope_abort_forced_close_does_not_wait_forever_for_closed_notification():
    socket = CloseHandshakeSocket(auto_notify=False)
    stream = await open_socket(socket)
    await asyncio.wait_for(stream.abort(), timeout=0.3)
    assert socket.transport.physically_closed and socket.transport.abort_calls == 1


class FinalCloseHandshakeSocket(CloseHandshakeSocket):
    """Complete synthetic ASR protocol with independently delayed TCP close."""

    def __init__(self, *, respond_final=True, empty_final=False, **kwargs):
        super().__init__(**kwargs)
        self.received = asyncio.Queue()
        self.finish_received = asyncio.Event()
        self.respond_final = respond_final
        self.empty_final = empty_final

    async def recv(self):
        if not self._task_started:
            return await super().recv()
        return await self.received.get()

    async def send(self, value):
        await super().send(value)
        if not isinstance(value, str) or json.loads(value)["header"]["action"] != "finish-task":
            return
        self.finish_received.set()
        if not self.respond_final:
            return
        if not self.empty_final:
            for text, ended, start, end in [
                ("合成前段。", True, 0, 60),
                ("尾字不能丢", False, 60, 120),
            ]:
                self.received.put_nowait(json.dumps({
                    "header": {"event": "result-generated", "task_id": self.task_id},
                    "payload": {"output": {"sentence": {
                        "text": text, "sentence_end": ended, "begin_time": start, "end_time": end,
                    }}},
                }))
        self.received.put_nowait(json.dumps({
            "header": {"event": "task-finished", "task_id": self.task_id},
        }))


def finish_commands(socket):
    return [value for value in socket.sent if isinstance(value, str)
            and json.loads(value)["header"]["action"] == "finish-task"]


async def observe_cancel(operation, cancelled):
    try:
        return await operation
    except asyncio.CancelledError as error:
        # Python 3.9 drops the cancellation message when awaiting a cancelled
        # Task. Observe inside the owner coroutine, before that runtime seam.
        cancelled.append(error)
        raise


@pytest.mark.anyio
@pytest.mark.parametrize("close_error", [False, True])
async def test_dashscope_final_survives_slow_or_failed_close_and_preserves_pcm_tail(close_error):
    socket = FinalCloseHandshakeSocket(close_error=close_error)
    # A long route timeout must not enlarge the independent 0.5 s close bound.
    raw = await open_socket(socket, stt_context(timeout_s=30))
    stream = ValidatedSTTStream(raw, stt_request())
    pcm = b"\x01\x00" * 1920  # 100 ms packet plus a 20 ms final remainder.
    await stream.send_audio(pcm)
    events = await asyncio.wait_for(stream.finish(), timeout=1)
    finals = [event for event in events if event.type == "transcript.final"]
    assert len(finals) == 1 and finals[0].text == "合成前段。尾字不能丢"
    assert [segment.text for segment in finals[0].segments] == ["合成前段。", "尾字不能丢"]
    assert b"".join(value for value in socket.sent if isinstance(value, bytes)) == pcm
    assert len(finish_commands(socket)) == 1
    assert socket.transport.physically_closed and socket.transport.abort_calls == 1
    assert socket.close_calls == 1 and raw._abort_task.done()
    assert await stream.finish() == []
    await raw.abort()
    assert socket.close_calls == 1


@pytest.mark.anyio
async def test_dashscope_finish_and_abort_share_cleanup_concurrent_finish_has_one_final():
    socket = FinalCloseHandshakeSocket()
    raw = await open_socket(socket, stt_context(timeout_s=2))
    first = asyncio.create_task(raw.finish())
    await socket.close_started.wait()
    cleanup = raw._abort_task
    second = asyncio.create_task(raw.finish())
    aborting = asyncio.create_task(raw.abort())
    await asyncio.sleep(0)
    assert not first.done() and not second.done() and not aborting.done()
    assert raw._abort_task is cleanup and cleanup is not None
    socket.peer_acknowledged_close.set()
    first_events, second_events, _ = await asyncio.gather(first, second, aborting)
    assert sum(event.type == "transcript.final" for event in first_events) == 1
    assert second_events == [] and len(finish_commands(socket)) == 1
    assert socket.close_calls == 1 and socket.transport.abort_calls == 0


@pytest.mark.anyio
async def test_dashscope_cancelled_duplicate_finish_cannot_cancel_final_owner():
    socket = FinalCloseHandshakeSocket()
    raw = await open_socket(socket, stt_context(timeout_s=2))
    owner = asyncio.create_task(raw.finish())
    await socket.close_started.wait()
    duplicate = asyncio.create_task(raw.finish())
    await asyncio.sleep(0)
    duplicate.cancel()
    with pytest.raises(asyncio.CancelledError):
        await duplicate
    assert not owner.done() and socket.transport.abort_calls == 0
    socket.peer_acknowledged_close.set()
    events = await owner
    assert sum(event.type == "transcript.final" for event in events) == 1
    assert len(finish_commands(socket)) == 1 and socket.close_calls == 1


@pytest.mark.anyio
async def test_dashscope_cancelled_validated_finish_reclaims_socket_and_preserves_cancellation():
    socket = FinalCloseHandshakeSocket()
    raw = await open_socket(socket)
    stream = ValidatedSTTStream(raw, stt_request())
    cancelled = []
    caller = asyncio.create_task(observe_cancel(stream.finish(), cancelled))
    await socket.close_started.wait()
    cleanup = raw._abort_task
    caller.cancel("synthetic final cancellation")
    with pytest.raises(asyncio.CancelledError) as error:
        await caller
    assert cancelled[0].args == ("synthetic final cancellation",)
    assert socket.transport.physically_closed and socket.transport.abort_calls == 1
    await asyncio.wait_for(stream.abort(), timeout=0.3)
    assert raw._abort_task is cleanup and cleanup.done()
    assert socket.close_calls == 1 and await stream.finish() == []


@pytest.mark.anyio
async def test_dashscope_finish_repeated_cancel_does_not_orphan_shared_cleanup():
    socket = FinalCloseHandshakeSocket(auto_notify=False)
    raw = await open_socket(socket)
    cancelled = []
    caller = asyncio.create_task(observe_cancel(raw.finish(), cancelled))
    await socket.close_started.wait()
    cleanup = raw._abort_task
    caller.cancel("original cancellation")
    await asyncio.sleep(0)
    caller.cancel("second cancellation")
    with pytest.raises(asyncio.CancelledError) as error:
        await caller
    assert cancelled[0].args == ("original cancellation",)
    assert raw._abort_task is cleanup and not cleanup.cancelled()
    assert socket.transport.physically_closed and socket.transport.abort_calls == 1
    await asyncio.wait_for(raw.abort(), timeout=0.3)
    assert cleanup.done() and socket.close_calls == 1


@pytest.mark.anyio
async def test_dashscope_cancel_before_final_still_reclaims_socket_without_returning_final():
    socket = FinalCloseHandshakeSocket(respond_final=False)
    raw = await open_socket(socket)
    cancelled = []
    caller = asyncio.create_task(observe_cancel(raw.finish(), cancelled))
    await socket.finish_received.wait()
    caller.cancel("before final")
    with pytest.raises(asyncio.CancelledError) as error:
        await caller
    assert cancelled[0].args == ("before final",)
    assert socket.transport.physically_closed and socket.transport.abort_calls == 1
    assert await raw.finish() == []


@pytest.mark.anyio
async def test_dashscope_final_error_survives_cleanup_error_and_later_abort_exposes_cleanup():
    socket = FinalCloseHandshakeSocket(empty_final=True, broken_transport=True)
    raw = await open_socket(socket)
    # A normal empty task is now valid recognition completion. Keep the
    # original error-precedence scenario by leaving a vendor hypothesis open.
    socket.received.put_nowait(json.dumps({
        "header": {"event": "result-generated", "task_id": socket.task_id},
        "payload": {"output": {"sentence": {
            "text": "", "sentence_begin": True, "sentence_end": False, "sentence_id": 1,
        }}},
    }))
    with pytest.raises(ProviderError) as original:
        await asyncio.wait_for(raw.finish(), timeout=0.3)
    assert original.value.code == "provider_final_transcript_missing"
    with pytest.raises(ProviderError) as cleanup:
        await raw.abort()
    assert cleanup.value.code == "provider_stream_close_failed"
    assert socket.close_calls == 1


@pytest.mark.anyio
async def test_dashscope_final_cancel_and_cleanup_error_preserve_original_cancellation():
    socket = FinalCloseHandshakeSocket(broken_transport=True)
    raw = await open_socket(socket)
    cancelled = []
    caller = asyncio.create_task(observe_cancel(raw.finish(), cancelled))
    await socket.close_started.wait()
    caller.cancel("original final cancellation")
    with pytest.raises(asyncio.CancelledError) as original:
        await caller
    assert cancelled[0].args == ("original final cancellation",)
    with pytest.raises(ProviderError) as cleanup:
        await raw.abort()
    assert cleanup.value.code == "provider_stream_close_failed"


@pytest.mark.anyio
async def test_dashscope_finishing_one_socket_cannot_close_another_stream():
    first_socket, other_socket = FinalCloseHandshakeSocket(), FinalCloseHandshakeSocket()
    first = await open_socket(first_socket)
    other = await open_socket(other_socket)
    assert first.stream_id != other.stream_id
    await first.finish()
    assert first_socket.transport.physically_closed
    assert not other.closed and other_socket.close_calls == 0
    assert not other_socket.transport.physically_closed
    other_socket.peer_acknowledged_close.set()
    events = await other.finish()
    assert sum(event.type == "transcript.final" for event in events) == 1
