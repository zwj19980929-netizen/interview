"""An explicit same-task retraction is neither lost words nor an answer."""
import asyncio

import pytest

from app.model_gateway.errors import ProviderError
from app.model_gateway.streaming import ValidatedSTTStream
from app.services.continuous_stt import ContinuousSTT
from test_continuous_stt import _RawStream, _validated
from test_stt_empty_completion import CompletionStream, EmptySocket, completion, open_socket
from test_stt_stable_preview_gateway import preview_data
from test_stt_stable_preview_providers import request


def retracted_sentences():
    return [
        {"sentence_id": 1, "text": "临时", "sentence_end": False, "begin_time": 0, "end_time": 240},
        {"sentence_id": 1, "text": "", "sentence_end": True, "begin_time": 0, "end_time": 650},
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("partial", [True, False])
async def test_adapter_and_gateway_accept_acknowledged_explicit_retraction_only_once(partial):
    socket = EmptySocket(retracted_sentences())
    stream = await open_socket(socket, partial=partial)
    assert not stream.empty_completion_reconciled
    await stream.send_audio(bytes(640))
    events = await stream.finish()
    assert [event.type for event in events][-2:] == ["transcript.empty", "stream.closed"]
    assert stream.has_observed_text and stream.empty_completion_reconciled
    assert not stream._final_received and socket.closed
    assert await stream.finish() == []


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["unkeyed", "unfinished_retraction", "pending_empty"])
async def test_adapter_requires_every_observed_sentence_to_be_explicitly_retracted(kind):
    sentences = retracted_sentences()
    if kind == "unkeyed":
        for sentence in sentences:
            sentence.pop("sentence_id")
            sentence.pop("begin_time")
    elif kind == "unfinished_retraction":
        sentences[-1]["sentence_end"] = False
    else:
        sentences.append({"sentence_id": 2, "text": "", "sentence_end": False,
                          "begin_time": 660, "end_time": 700})
    stream = await open_socket(EmptySocket(sentences))
    await stream.send_audio(bytes(640))
    with pytest.raises(ProviderError) as error:
        await stream.finish()
    assert error.value.code == "provider_final_transcript_missing"
    assert not stream.empty_completion_reconciled


@pytest.mark.anyio
async def test_another_sentence_empty_end_cannot_withdraw_an_earlier_hypothesis():
    sentences = retracted_sentences()
    sentences[-1]["sentence_id"] = 2
    stream = await open_socket(EmptySocket(sentences))
    await stream.send_audio(bytes(640))
    events = await stream.finish()
    final = next(event for event in events if event.type == "transcript.final")
    assert final.text == "临时" and not stream.empty_completion_reconciled


@pytest.mark.anyio
async def test_retraction_receipt_waits_for_audio_drain_and_complete_ack():
    socket = EmptySocket(retracted_sentences(), block_audio=True)
    stream = await open_socket(socket)
    await stream.send_audio(bytes(640))
    finish = asyncio.create_task(stream.finish())
    await socket.audio_started.wait()
    assert not stream.empty_completion_reconciled
    assert not stream._stream.empty_completion_reconciled
    assert "finish-task" not in socket.actions
    socket.audio_release.set()
    events = await finish
    assert any(event.type == "transcript.empty" for event in events)
    assert stream.empty_completion_reconciled


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["missing_ack", "failed_task"])
async def test_retraction_without_successful_task_completion_cannot_certify_empty(failure):
    socket = EmptySocket(retracted_sentences(), acknowledge=failure != "missing_ack",
                         failed=failure == "failed_task")
    stream = await open_socket(socket)
    await stream.send_audio(bytes(640))
    with pytest.raises(ProviderError):
        await stream.finish()
    assert not stream.empty_completion_reconciled
    assert socket.closed


@pytest.mark.anyio
async def test_partial_retraction_preserves_all_nonwithdrawn_final_words_and_timings():
    sentences = retracted_sentences() + [
        {"sentence_id": 2, "text": "完整回答。", "sentence_end": True,
         "begin_time": 1000, "end_time": 1800},
    ]
    stream = await open_socket(EmptySocket(sentences))
    await stream.send_audio(bytes(640))
    events = await stream.finish()
    final = next(event for event in events if event.type == "transcript.final")
    assert final.text == "完整回答。"
    assert [(item.text, item.start_ms, item.end_ms) for item in final.segments] == [
        ("完整回答。", 1000, 1800),
    ]
    assert not stream.empty_completion_reconciled


@pytest.mark.anyio
@pytest.mark.parametrize("fact", [None, False, 1, "true", {}, []])
async def test_gateway_optional_reconciliation_fact_requires_exact_true(fact):
    raw = CompletionStream([completion(sequence=3)])
    raw.empty_completion_reconciled = fact
    raw.partial = [completion("transcript.partial", is_final=False, text="临时")]
    stream = ValidatedSTTStream(raw, request())
    await stream.send_audio(bytes(640))
    with pytest.raises(ProviderError) as error:
        await stream.finish()
    assert error.value.code == "provider_schema_invalid"
    assert not stream.empty_completion_reconciled


@pytest.mark.anyio
async def test_gateway_retraction_fact_cannot_erase_an_already_exposed_stable_prefix():
    raw = CompletionStream([completion(sequence=3)])
    raw.empty_completion_reconciled = True
    raw.preview_value = preview_data()
    stream = ValidatedSTTStream(raw, request())
    await stream.preview()
    with pytest.raises(ProviderError):
        await stream.finish()
    assert not stream.empty_completion_reconciled


@pytest.mark.anyio
async def test_gateway_does_not_publish_receipt_when_completion_batch_is_invalid():
    raw = CompletionStream([completion(sequence=3), completion(sequence=4)])
    raw.empty_completion_reconciled = True
    raw.has_observed_text = True
    stream = ValidatedSTTStream(raw, request())
    with pytest.raises(ProviderError):
        await stream.finish()
    await stream.abort()
    assert not stream.empty_completion_reconciled


@pytest.mark.anyio
async def test_successfully_retracted_tail_advances_once_without_replaying_recorded_pcm():
    first = _RawStream("answer", text="合成完整回答。")
    retracted = await open_socket(EmptySocket(retracted_sentences()))
    next_raw = _RawStream("next", text="新增回答。")
    streams, recorded = [retracted, _validated(next_raw)], []

    async def reopen():
        return streams.pop(0)

    capture = ContinuousSTT(_validated(first), reopen=reopen, record=recorded.append,
                            bytes_per_second=32000)
    try:
        await capture.send_audio(bytes(640))
        original = await capture.snapshot()
        await capture.send_audio(bytes(55040))
        final = await capture.snapshot(resume=False)
        assert final == original
        assert not capture._unresolved and capture._segment_start_bytes == 55680
        assert not capture._segment_audio and not capture._segment_text_seen
        assert not capture._prior_stream_text_seen
        await capture.resume()
        assert next_raw.chunks == []
        assert sum(map(len, recorded)) == 55680
        await capture.send_audio(bytes(640))
        final = await capture.snapshot(resume=False)
        assert final.text == "合成完整回答。新增回答。"
        assert final.segments[-1].start_ms == 1750
        assert capture.commit_final() == final
    finally:
        await capture.abort()


@pytest.mark.anyio
async def test_retracted_first_stream_still_cannot_create_answer_or_confirmation():
    stream = await open_socket(EmptySocket(retracted_sentences()))

    async def reopen():
        raise AssertionError("Unexpected replay")

    capture = ContinuousSTT(stream, reopen=reopen, record=lambda _: None, bytes_per_second=32000)
    try:
        await capture.send_audio(bytes(640))
        assert await capture.snapshot(resume=False) is None
        assert not capture._unresolved and capture._segment_start_bytes == 640
        with pytest.raises(ProviderError):
            capture.commit_final()
    finally:
        await capture.abort()


@pytest.mark.anyio
@pytest.mark.parametrize("initial_failure", ["missing_final", "timeout"])
async def test_replacement_retractions_cannot_erase_prior_failed_stream_words(initial_failure):
    first = _RawStream("lost", text=None)
    if initial_failure == "timeout":
        first.finish_gate = asyncio.Event()
    replacements, recorded = [], []

    async def reopen():
        stream = await open_socket(EmptySocket(retracted_sentences()))
        replacements.append(stream)
        return stream

    capture = ContinuousSTT(_validated(first), reopen=reopen, record=recorded.append,
                            bytes_per_second=32000, final_timeout_seconds=.02)
    try:
        await capture.send_audio(bytes(640))
        if initial_failure == "timeout":
            with pytest.raises(ProviderError):
                await capture.snapshot(resume=False)
            await capture.recover()
        else:
            assert await capture.snapshot() is None
        assert capture._prior_stream_text_seen
        assert await capture.snapshot(resume=False) is None
        assert replacements[-1].empty_completion_reconciled
        assert capture._unresolved and capture._segment_start_bytes == 0
        assert capture._segment_audio == recorded == [bytes(640)]
        with pytest.raises(ProviderError):
            capture.commit_final()
    finally:
        await capture.abort()
