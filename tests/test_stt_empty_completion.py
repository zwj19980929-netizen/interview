"""Normal empty ASR completion is distinct from missing or lost evidence."""

import asyncio
import copy
import json

import pytest

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import StreamingSTTEvent
from app.model_gateway.streaming import ValidatedSTTStream
from app.providers.dashscope.provider import DashScopeProvider
from test_stt_stable_preview_gateway import preview_data
from test_stt_stable_preview_providers import PreviewSocket, provider_context, request


def completion(kind="transcript.empty", **overrides):
    result = {
        "stream_id": "synthetic-stream", "sequence": 2, "type": kind,
        "text": "", "segments": [], "is_final": True,
        "provider": preview_data()["provider"],
    }
    if kind == "transcript.final":
        result.update(text="合成文本。", segments=[{"text": "合成文本。"}])
    result.update(overrides)
    return result


class CompletionStream:
    stream_id = "synthetic-stream"

    def __init__(self, events):
        self.events = events
        self.ready_events = [{"stream_id": self.stream_id, "sequence": 1,
                              "type": "stream.ready", "provider": preview_data()["provider"]}]
        self.preview_value = None
        self.partial = []
        self.finishes = 0

    async def send_audio(self, chunk):
        return self.partial

    async def preview(self):
        return self.preview_value

    async def finish(self):
        self.finishes += 1
        return self.events

    async def abort(self):
        pass


@pytest.mark.anyio
async def test_gateway_accepts_one_complete_empty_result_without_creating_a_transcript():
    raw = CompletionStream([completion(), {
        "stream_id": "synthetic-stream", "sequence": 3, "type": "stream.closed",
    }])
    stream = ValidatedSTTStream(raw, request())
    result = await stream.finish()
    assert [event.type for event in result] == ["transcript.empty", "stream.closed"]
    assert result[0].is_final and not result[0].text and not result[0].segments
    assert not stream._final_received
    assert await stream.finish() == [] and raw.finishes == 1


@pytest.mark.anyio
@pytest.mark.parametrize("first,second", [
    ("transcript.empty", "transcript.empty"), ("transcript.empty", "transcript.final"),
    ("transcript.final", "transcript.empty"), ("transcript.final", "transcript.final"),
])
async def test_gateway_rejects_all_duplicate_completion_combinations(first, second):
    stream = ValidatedSTTStream(CompletionStream([
        completion(first), completion(second, sequence=3),
    ]), request())
    with pytest.raises(ProviderError) as error:
        await stream.finish()
    assert error.value.code == "provider_schema_invalid"


@pytest.mark.anyio
@pytest.mark.parametrize("override", [
    {"is_final": False}, {"is_final": "true"}, {"text": " "},
    {"text": "不得丢失的文字"}, {"segments": [{"text": ""}]}, {"provider": None},
    {"provider": {**preview_data()["provider"], "provider_id": " "}},
    {"provider": {**preview_data()["provider"], "model": ""}},
    {"provider": {**preview_data()["provider"], "latency_ms": -1}},
    {"provider": {**preview_data()["provider"], "request_id": ""}},
    {"provider": {**preview_data()["provider"], "model": "changed-model"}},
    {"provider": {**preview_data()["provider"], "extra": "forbidden"}},
    {"error_code": "provider_timeout"}, {"extra": "forbidden"},
])
async def test_gateway_rejects_invalid_empty_contract_without_exposing_input(override):
    stream = ValidatedSTTStream(CompletionStream([completion(**override)]), request())
    with pytest.raises(ProviderError) as error:
        await stream.finish()
    assert error.value.code == "provider_schema_invalid"
    assert "不得丢失" not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["partial", "preview"])
async def test_gateway_cannot_complete_empty_after_nonempty_recognition(source):
    raw = CompletionStream([completion(sequence=3)])
    stream = ValidatedSTTStream(raw, request())
    if source == "partial":
        raw.partial = [completion("transcript.partial", is_final=False, text="已有文字")]
        await stream.send_audio(b"\x00\x01")
    else:
        raw.preview_value = preview_data()
        await stream.preview()
    with pytest.raises(ProviderError) as error:
        await stream.finish()
    assert error.value.code == "provider_schema_invalid"


@pytest.mark.anyio
async def test_gateway_observed_text_fact_is_sticky_and_only_vetoes_empty():
    raw = CompletionStream([completion()])
    raw.has_observed_text = True
    stream = ValidatedSTTStream(raw, request())
    assert stream.has_observed_text
    raw.has_observed_text = False
    assert stream.has_observed_text
    with pytest.raises(ProviderError) as error:
        await stream.finish()
    assert error.value.code == "provider_schema_invalid"
    assert not stream._final_received and not stream._completion_received


@pytest.mark.anyio
async def test_gateway_missing_completion_is_still_a_recoverable_failure():
    stream = ValidatedSTTStream(CompletionStream([]), request())
    with pytest.raises(ProviderError) as error:
        await stream.finish()
    assert error.value.code == "provider_final_transcript_missing" and error.value.retryable


@pytest.mark.anyio
async def test_gateway_cannot_empty_complete_before_finishing_audio():
    raw = CompletionStream([])
    raw.partial = [completion()]
    stream = ValidatedSTTStream(raw, request())
    with pytest.raises(ProviderError) as error:
        await stream.send_audio(bytes(640))
    assert error.value.code == "provider_schema_invalid"


@pytest.mark.anyio
async def test_gateway_revalidates_mutated_empty_event_instances():
    value = StreamingSTTEvent(**completion()).model_copy(update={"text": "丢失文字"})
    stream = ValidatedSTTStream(CompletionStream([value]), request())
    with pytest.raises(ProviderError) as error:
        await stream.finish()
    assert error.value.code == "provider_schema_invalid"


class EmptySocket(PreviewSocket):
    def __init__(self, sentences=(), *, acknowledge=True, failed=False, block_audio=False):
        super().__init__()
        self.sentences = copy.deepcopy(sentences)
        self.acknowledge = acknowledge
        self.failed = failed
        self.audio_started = asyncio.Event()
        self.audio_release = asyncio.Event()
        if not block_audio:
            self.audio_release.set()

    async def send(self, data):
        if isinstance(data, bytes):
            self.audio_started.set()
            await self.audio_release.wait()
        self.sent.append(data)
        if isinstance(data, str) and json.loads(data)["header"]["action"] == "finish-task":
            for sentence in self.sentences:
                self.push("result-generated", sentence)
            if self.failed:
                self.push("task-failed")
            elif self.acknowledge:
                self.push("task-finished")


async def open_socket(socket, *, partial=True):
    async def connect(*args, **kwargs):
        return socket

    req = request(enable_partial=partial)
    raw = await DashScopeProvider(websocket_connect=connect).open_stream(req, provider_context())
    return ValidatedSTTStream(raw, req)


@pytest.mark.anyio
@pytest.mark.parametrize("sentences", [[], [{"heartbeat": True}], [
    {"text": "", "sentence_end": True, "begin_time": 0, "end_time": 200},
]])
async def test_dashscope_normal_empty_end_requires_no_text_or_pending_hypothesis(sentences):
    socket = EmptySocket(sentences)
    stream = await open_socket(socket)
    await stream.send_audio(bytes(640))
    events = await stream.finish()
    assert [event.type for event in events] == ["transcript.empty", "stream.closed"]
    assert sum(len(data) for data in socket.sent if isinstance(data, bytes)) == 640
    assert socket.closed


@pytest.mark.anyio
@pytest.mark.parametrize("sentences", [
    [{"text": "", "sentence_end": False, "begin_time": 0}],
    [{"sentence_id": 1, "sentence_begin": True}],
    [{"text": "既有文字", "sentence_end": False, "begin_time": 0},
     {"text": "", "sentence_end": True, "begin_time": 0, "end_time": 200}],
    [{"text": "既有文字", "sentence_end": False, "begin_time": 0},
     {"text": "", "sentence_end": False, "begin_time": 0}],
])
async def test_dashscope_never_turns_lost_text_or_pending_hypotheses_into_empty(sentences):
    socket = EmptySocket(sentences)
    # The adapter must remember raw vendor text even when partial is disabled.
    stream = await open_socket(socket, partial=False)
    await stream.send_audio(bytes(640))
    with pytest.raises(ProviderError) as error:
        await stream.finish()
    assert error.value.code == "provider_final_transcript_missing" and error.value.retryable
    assert socket.closed


@pytest.mark.anyio
@pytest.mark.parametrize("failed", [False, True])
async def test_dashscope_timeout_or_task_failure_never_becomes_empty(failed):
    socket = EmptySocket(acknowledge=False, failed=failed)
    stream = await open_socket(socket)
    await stream.send_audio(bytes(640))
    with pytest.raises(ProviderError) as error:
        await stream.finish()
    assert error.value.code == ("provider_server_error" if failed else "provider_timeout")
    assert socket.closed


@pytest.mark.anyio
@pytest.mark.parametrize("text", [None, False, 0])
async def test_dashscope_malformed_text_cannot_certify_empty_completion(text):
    socket = EmptySocket([{"text": text, "sentence_end": True, "begin_time": 0, "end_time": 20}])
    stream = await open_socket(socket)
    with pytest.raises(ProviderError) as error:
        await stream.finish()
    assert error.value.code == "provider_schema_invalid"
    assert socket.closed


@pytest.mark.anyio
async def test_dashscope_empty_completion_waits_for_all_accepted_audio():
    socket = EmptySocket(block_audio=True)
    stream = await open_socket(socket)
    await stream.send_audio(bytes(640))
    finishing = asyncio.create_task(stream.finish())
    await asyncio.wait_for(socket.audio_started.wait(), timeout=1)
    assert not finishing.done() and socket.actions == ["run-task"]
    socket.audio_release.set()
    events = await asyncio.wait_for(finishing, timeout=1)
    assert events[0].type == "transcript.empty"
    assert socket.actions == ["run-task", "finish-task"]
