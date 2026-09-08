"""Non-closing ASR previews through real adapters and synthetic transports."""

import asyncio
import json

import pytest

from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ProviderContext, StreamingAudioConfig, StreamingSTTRequest
from app.providers.dashscope.provider import DashScopeProvider
from app.providers.mock.provider import MockProvider


class PreviewSocket:
    def __init__(self):
        self.received = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.push("task-started")

    def push(self, event, sentence=None):
        self.received.put_nowait(json.dumps({
            "header": {"event": event, "task_id": "synthetic-preview-task"},
            "payload": {"output": {"sentence": sentence}} if sentence is not None else {},
        }))

    async def send(self, data):
        self.sent.append(data)
        if isinstance(data, str) and json.loads(data)["header"]["action"] == "finish-task":
            self.push("task-finished")

    async def recv(self):
        return await self.received.get()

    async def close(self):
        self.closed = True

    @property
    def actions(self):
        return [json.loads(data)["header"]["action"] for data in self.sent if isinstance(data, str)]


def provider_context(provider="dashscope"):
    return ProviderContext(
        organization_id="org_default", invocation_id="synthetic_preview_invocation",
        route_id="synthetic_preview_route", provider_connection_id="synthetic_connection",
        model_configuration_id="synthetic_model", model_type="stt", capability=cap.STT_STREAMING,
        purpose="synthetic_preview_test", model="qwen-audio-3.0-asr-flash-streaming" if provider == "dashscope" else "mock-stt",
        timeout_s=1, attempt=1, fallback_index=0, credentials={"api_key": "synthetic-not-real"},
        connection_config={"base_url": "https://dashscope.aliyuncs.com"},
    )


def request(*, enable_partial=True, metadata=None):
    return StreamingSTTRequest(
        interview_id="synthetic-preview", turn_id="synthetic-turn", enable_partial=enable_partial,
        audio=StreamingAudioConfig(content_type="audio/pcm", sample_rate_hz=16000, channels=1),
        metadata=metadata or {},
    )


async def open_dashscope(*, enable_partial=True):
    socket = PreviewSocket()

    async def connect(*args, **kwargs):
        return socket

    stream = await DashScopeProvider(websocket_connect=connect).open_stream(
        request(enable_partial=enable_partial), provider_context(),
    )
    return stream, socket


async def sentence(socket, **values):
    socket.push("result-generated", values)
    for _ in range(4):
        await asyncio.sleep(0)


@pytest.mark.anyio
@pytest.mark.parametrize("enable_partial", [True, False])
async def test_preview_reads_stable_sentence_without_finish_or_stopping_audio(enable_partial):
    stream, socket = await open_dashscope(enable_partial=enable_partial)
    try:
        assert await stream.preview() is None
        await stream.send_audio(b"\x00\x01" * 16000)
        await sentence(socket, sentence_id=1, sentence_end=False, begin_time=0, text="初稿")
        assert await stream.preview() is None
        await sentence(socket, sentence_id=1, sentence_end=True, begin_time=20, end_time=900, text="完整句子。")
        first = await stream.preview()
        assert first.text == "完整句子。" and not first.has_unstable_tail
        assert not hasattr(first, "is_final")
        sequence = stream.sequence
        for _ in range(10):
            assert (await stream.preview()).model_dump() == first.model_dump()
        assert stream.sequence == sequence
        assert socket.actions == ["run-task"] and not socket.closed
        await stream.send_audio(b"\x00\x02" * 1600)
        await asyncio.sleep(0)
        assert sum(len(data) for data in socket.sent if isinstance(data, bytes)) == 35200
        final = next(item for item in await stream.finish() if item.type == "transcript.final")
        assert final.text == first.text and final.segments == first.segments
        assert socket.actions == ["run-task", "finish-task"]
    finally:
        await stream.abort()


@pytest.mark.anyio
async def test_empty_sentence_begin_and_heartbeat_do_not_erase_pending_tail():
    stream, socket = await open_dashscope()
    try:
        await sentence(socket, sentence_id=1, sentence_end=True, begin_time=100, end_time=900, text="前缀。")
        await sentence(socket, sentence_id=2, sentence_begin=True, sentence_end=False, begin_time=0, end_time=None, text="")
        preview = await stream.preview()
        assert preview.text == "前缀。" and preview.has_unstable_tail
        await sentence(socket, sentence_id=0, heartbeat=True, text="", sentence_end=False)
        after_heartbeat = await stream.preview()
        assert after_heartbeat.model_dump() == preview.model_dump()
        await sentence(socket, sentence_id=2, sentence_end=False, begin_time=1200, text="尾部临时")
        await sentence(socket, sentence_id=2, sentence_end=False, begin_time=1210, text="尾部修正")
        await sentence(socket, sentence_id=2, sentence_end=True, begin_time=1210, end_time=1800, text="尾部修正。")
        preview = await stream.preview()
        assert preview.text == "前缀。尾部修正。" and not preview.has_unstable_tail
    finally:
        await stream.abort()


@pytest.mark.anyio
async def test_empty_sentence_final_is_idempotent_and_does_not_create_a_prefix_hole():
    stream, socket = await open_dashscope()
    try:
        for _ in range(2):
            await sentence(socket, sentence_id=1, sentence_end=True, begin_time=0, end_time=100, text="")
        await sentence(socket, sentence_id=2, sentence_end=True, begin_time=200, end_time=900, text="有效句。")
        preview = await stream.preview()
        assert preview.text == "有效句。" and not preview.has_unstable_tail
    finally:
        await stream.abort()


@pytest.mark.anyio
@pytest.mark.parametrize("use_ids", [True, False])
async def test_duplicate_final_is_idempotent_and_does_not_clear_later_tail(use_ids):
    stream, socket = await open_dashscope()
    first = dict(sentence_end=True, begin_time=100, end_time=900, text="已有句。")
    tail = dict(sentence_end=False, begin_time=1200, end_time=1700, text="新尾句")
    if use_ids:
        first["sentence_id"], tail["sentence_id"] = 1, 2
    try:
        await sentence(socket, **first)
        original = await stream.preview()
        await sentence(socket, **first)
        assert (await stream.preview()).model_dump() == original.model_dump()
        await sentence(socket, **tail)
        with_tail = await stream.preview()
        await sentence(socket, **first)
        assert (await stream.preview()).model_dump() == with_tail.model_dump()
        assert with_tail.has_unstable_tail
        final = next(item for item in await stream.finish() if item.type == "transcript.final")
        assert final.text == "已有句。新尾句" and len(final.segments) == 2
    finally:
        await stream.abort()


@pytest.mark.anyio
async def test_delayed_earlier_final_preserves_newer_empty_sentence_start():
    stream, socket = await open_dashscope()
    try:
        await sentence(socket, sentence_id=1, sentence_end=False, begin_time=100, text="较早句")
        await sentence(socket, sentence_id=2, sentence_begin=True, sentence_end=False, begin_time=0, text="")
        await sentence(socket, sentence_id=1, sentence_end=True, begin_time=100, end_time=900, text="较早句。")
        assert (await stream.preview()).has_unstable_tail
        await sentence(socket, sentence_id=2, sentence_end=False, begin_time=1000, text="后续")
        final = next(item for item in await stream.finish() if item.type == "transcript.final")
        assert final.text == "较早句。后续"
    finally:
        await stream.abort()


@pytest.mark.anyio
@pytest.mark.parametrize("earlier_final_arrives", [True, False])
async def test_multiple_pending_sentence_hypotheses_survive_newer_final(earlier_final_arrives):
    stream, socket = await open_dashscope()
    try:
        await sentence(socket, sentence_id=1, sentence_end=True, begin_time=100, end_time=500, text="第一句。")
        exposed = await stream.preview()
        await sentence(socket, sentence_id=2, sentence_end=False, begin_time=700, end_time=1000, text="待确认第二句")
        await sentence(socket, sentence_id=3, sentence_end=False, begin_time=1200, end_time=1500, text="第三句初稿")
        await sentence(socket, sentence_id=3, sentence_end=True, begin_time=1200, end_time=1600, text="第三句。")
        preview = await stream.preview()
        assert preview.text == exposed.text and preview.has_unstable_tail
        if earlier_final_arrives:
            await sentence(socket, sentence_id=2, sentence_end=True, begin_time=700, end_time=1000, text="第二句。")
        # Filling an old hole must not reorder/replace a previously exposed
        # stable prefix. Finalization may use all sentence facts in ID order.
        preview = await stream.preview()
        assert preview.text == exposed.text and preview.has_unstable_tail
        final = next(item for item in await stream.finish() if item.type == "transcript.final")
        expected_middle = "第二句。" if earlier_final_arrives else "待确认第二句"
        assert final.text == "第一句。" + expected_middle + "第三句。"
        assert final.text == "".join(segment.text for segment in final.segments)
        assert [segment.start_ms for segment in final.segments] == [100, 700, 1200]
    finally:
        await stream.abort()


@pytest.mark.anyio
async def test_first_final_with_missing_earlier_sentence_is_not_a_reliable_prefix():
    stream, socket = await open_dashscope()
    try:
        await sentence(socket, sentence_id=1, sentence_end=False, begin_time=100, end_time=500, text="早句")
        await sentence(socket, sentence_id=2, sentence_end=True, begin_time=700, end_time=1000, text="后句。")
        assert await stream.preview() is None
        final = next(item for item in await stream.finish() if item.type == "transcript.final")
        assert final.text == "早句后句。"
    finally:
        await stream.abort()


@pytest.mark.anyio
async def test_no_id_ambiguous_revised_start_does_not_manufacture_stable_prefix():
    stream, socket = await open_dashscope()
    try:
        await sentence(socket, sentence_end=False, begin_time=0, text="初稿")
        await sentence(socket, sentence_end=False, begin_time=100, text="修正句")
        await sentence(socket, sentence_end=True, begin_time=100, end_time=900, text="修正句。")
        assert await stream.preview() is None
        final = next(item for item in await stream.finish() if item.type == "transcript.final")
        assert final.text == "修正句。"
    finally:
        await stream.abort()


@pytest.mark.anyio
@pytest.mark.parametrize("budget", ["count", "text"])
async def test_unresolved_hypotheses_are_bounded_and_never_silently_evicted(budget):
    stream, socket = await open_dashscope()
    try:
        count = 65 if budget == "count" else 2
        text = "小段" if budget == "count" else "长" * 50_001
        for index in range(1, count + 1):
            await sentence(socket, sentence_id=index, sentence_end=False, begin_time=index * 1000, text=text)
        with pytest.raises(ProviderError) as caught:
            await stream.preview()
        assert caught.value.code == "provider_schema_invalid"
        assert len(stream._pending_sentences) == (64 if budget == "count" else 1)
    finally:
        await stream.abort()


@pytest.mark.anyio
@pytest.mark.parametrize("change", [{"text": "篡改句。"}, {"end_time": 950}, {"begin_time": 90}])
async def test_conflicting_final_cannot_rewrite_already_previewed_sentence(change):
    stream, socket = await open_dashscope()
    original = dict(sentence_id=1, sentence_end=True, begin_time=100, end_time=900, text="稳定句。")
    try:
        await sentence(socket, **original)
        assert (await stream.preview()).text == "稳定句。"
        await sentence(socket, **{**original, **change})
        with pytest.raises(ProviderError) as caught:
            await stream.preview()
        assert caught.value.code == "provider_schema_invalid"
        with pytest.raises(ProviderError):
            await stream.finish()
        assert stream.committed[0].text == "稳定句。"
    finally:
        await stream.abort()


@pytest.mark.anyio
@pytest.mark.parametrize("timing", [
    {}, {"begin_time": 100}, {"begin_time": -1, "end_time": 900},
    {"begin_time": 900, "end_time": 100}, {"begin_time": "100", "end_time": 900},
    {"begin_time": 100, "end_time": None, "words": [{"begin_time": 200, "end_time": 100}]},
])
async def test_unreliable_sentence_timing_is_not_a_stable_preview(timing):
    stream, socket = await open_dashscope()
    try:
        await sentence(socket, sentence_id=1, sentence_end=True, text="仍保留厂商文本。", **timing)
        assert await stream.preview() is None
        assert socket.actions == ["run-task"]
        final = next(item for item in await stream.finish() if item.type == "transcript.final")
        assert final.text == "仍保留厂商文本。"
    finally:
        await stream.abort()


@pytest.mark.anyio
async def test_missing_sentence_end_time_can_use_monotonic_provider_word_range():
    stream, socket = await open_dashscope()
    try:
        await sentence(socket, sentence_end=True, begin_time=100, end_time=None, text="确认。",
                       words=[{"begin_time": 100, "end_time": 400}, {"begin_time": 400, "end_time": 900}])
        preview = await stream.preview()
        assert preview.segments[0].start_ms == 100 and preview.segments[0].end_ms == 900
        final = next(item for item in await stream.finish() if item.type == "transcript.final")
        assert final.segments == preview.segments
    finally:
        await stream.abort()


@pytest.mark.anyio
async def test_unreliable_later_sentence_keeps_already_exposed_prefix_with_pending_tail():
    stream, socket = await open_dashscope()
    try:
        await sentence(socket, sentence_id=1, sentence_end=True, begin_time=100, end_time=900, text="可靠前缀。")
        original = await stream.preview()
        await sentence(socket, sentence_id=2, sentence_end=True, begin_time=1200, text="没有有效末尾时标。")
        preview = await stream.preview()
        assert preview.text == original.text and preview.segments == original.segments
        assert preview.has_unstable_tail and preview.revision > original.revision
        final = next(item for item in await stream.finish() if item.type == "transcript.final")
        assert final.text == "可靠前缀。没有有效末尾时标。"
    finally:
        await stream.abort()


@pytest.mark.anyio
async def test_preview_is_deep_copy_and_unfinished_tail_is_only_in_real_final():
    stream, socket = await open_dashscope()
    try:
        await sentence(socket, sentence_id=1, sentence_end=True, begin_time=100, end_time=900, text="稳定。")
        preview = await stream.preview()
        preview.segments[0].text = "不能改内部状态"
        preview.provider.model = "不能改内部来源"
        await sentence(socket, sentence_id=2, sentence_end=False, begin_time=1000, end_time=1600, text="尚未确认的尾部")
        stable = await stream.preview()
        assert stable.text == "稳定。" and stable.has_unstable_tail
        assert stable.segments[0].text == "稳定。"
        assert stable.provider.model == "qwen-audio-3.0-asr-flash-streaming"
        final = next(item for item in await stream.finish() if item.type == "transcript.final")
        assert final.text == "稳定。尚未确认的尾部"
        with pytest.raises(ProviderError):
            await stream.preview()
        assert await stream.finish() == []
    finally:
        await stream.abort()


@pytest.mark.anyio
async def test_preview_does_not_wait_for_blocked_audio_sender():
    stream, socket = await open_dashscope()
    blocked = asyncio.Event()
    release = asyncio.Event()
    original_send = socket.send

    async def send(data):
        if isinstance(data, bytes):
            blocked.set()
            await release.wait()
        await original_send(data)

    try:
        await sentence(socket, sentence_id=1, sentence_end=True, begin_time=100, end_time=900, text="已稳定。")
        socket.send = send
        await stream.send_audio(b"\x00\x01" * 1600)
        await asyncio.wait_for(blocked.wait(), timeout=1)
        preview = await asyncio.wait_for(stream.preview(), timeout=0.05)
        assert preview.text == "已稳定。" and not release.is_set()
        assert socket.actions == ["run-task"]
    finally:
        release.set()
        await stream.abort()


@pytest.mark.anyio
@pytest.mark.parametrize("sentence_id", [0, -1, True, "2", 2.0])
async def test_invalid_nonheartbeat_sentence_identity_is_not_promoted(sentence_id):
    stream, socket = await open_dashscope()
    try:
        await sentence(socket, sentence_id=sentence_id, sentence_end=True, begin_time=100, end_time=900, text="句子。")
        with pytest.raises(ProviderError) as caught:
            await stream.preview()
        assert caught.value.code == "provider_schema_invalid"
    finally:
        await stream.abort()


@pytest.mark.anyio
async def test_new_sentence_cannot_overlap_stable_time_range():
    stream, socket = await open_dashscope()
    try:
        await sentence(socket, sentence_id=1, sentence_end=True, begin_time=100, end_time=900, text="第一句。")
        await sentence(socket, sentence_id=2, sentence_end=True, begin_time=800, end_time=1200, text="第二句。")
        with pytest.raises(ProviderError) as caught:
            await stream.preview()
        assert caught.value.code == "provider_schema_invalid"
    finally:
        await stream.abort()


@pytest.mark.anyio
@pytest.mark.parametrize("enable_partial", [True, False])
async def test_mock_preview_uses_explicit_fixture_and_matches_unique_final(enable_partial):
    stream = await MockProvider().open_stream(request(enable_partial=enable_partial, metadata={
        "development_transcript": "仅合成fixture。", "duration_ms": 1800, "confidence": 0.83,
    }), provider_context("mock"))
    assert await stream.preview() is None
    await stream.send_audio(b"\x00\x01" * 1600)
    preview = await stream.preview()
    assert preview.text == "仅合成fixture。" and preview.confidence == 0.83
    for _ in range(5):
        assert (await stream.preview()).model_dump() == preview.model_dump()
    final = next(item for item in await stream.finish() if item.type == "transcript.final")
    assert final.text == preview.text and final.segments == preview.segments
    assert final.confidence == preview.confidence and final.provider == preview.provider
    assert await stream.finish() == []


@pytest.mark.anyio
@pytest.mark.parametrize("metadata", [{}, {"development_transcript": "  "}, {"development_transcript": 123}])
async def test_mock_preview_never_manufactures_fixture(metadata):
    stream = await MockProvider().open_stream(request(metadata=metadata), provider_context("mock"))
    try:
        await stream.send_audio(b"\x00\x01" * 1600)
        assert await stream.preview() is None
    finally:
        await stream.abort()
