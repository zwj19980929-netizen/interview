"""Synthetic contract tests; no network, microphone or candidate data."""

import copy

import pytest
from pydantic import ValidationError

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    ProviderMeta,
    StableTranscriptPreview,
    StreamingSTTEvent,
    StreamingSTTRequest,
)
from app.model_gateway.streaming import ValidatedSTTStream


@pytest.fixture
def anyio_backend():
    return "asyncio"


def preview_data(**overrides):
    data = {
        "stream_id": "synthetic-stream",
        "revision": 1,
        "text": "合成句子。",
        "language": "zh-CN",
        "confidence": 0.9,
        "segments": [{"text": "合成句子。", "start_ms": 0, "end_ms": 500, "confidence": 0.9}],
        "provider": {"provider_id": "fake", "model": "synthetic-stt", "request_id": "request-1", "latency_ms": 0},
        "has_unstable_tail": False,
    }
    data.update(overrides)
    return data


class LegacyStream:
    stream_id = "synthetic-stream"

    def __init__(self, *, ready_provider=True, request_id="request-1"):
        self.provider = ProviderMeta(**preview_data()["provider"]).model_copy(update={"request_id": request_id})
        self.ready_events = [StreamingSTTEvent(
            stream_id=self.stream_id, sequence=1, type="stream.ready",
            provider=self.provider if ready_provider else None,
        )]
        self.sequence = 1
        self.sent = []
        self.finishes = 0
        self.aborts = 0

    async def send_audio(self, chunk):
        self.sent.append(chunk)
        self.sequence += 1
        return [StreamingSTTEvent(
            stream_id=self.stream_id, sequence=self.sequence, type="transcript.partial",
            text="合成", provider=self.provider,
        )]

    async def finish(self):
        self.finishes += 1
        self.sequence += 1
        data = preview_data()
        return [StreamingSTTEvent(
            stream_id=self.stream_id, sequence=self.sequence, type="transcript.final",
            text=data["text"], segments=data["segments"], confidence=data["confidence"],
            provider=self.provider, is_final=True,
        )]

    async def abort(self):
        self.aborts += 1


class PreviewStream(LegacyStream):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.value = preview_data()
        self.reads = 0

    async def preview(self):
        self.reads += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def wrap(raw):
    return ValidatedSTTStream(raw, StreamingSTTRequest(interview_id="synthetic", turn_id="turn-1"))


@pytest.mark.anyio
async def test_preview_is_optional_and_none_is_not_a_final():
    legacy = LegacyStream()
    stream = wrap(legacy)
    assert not stream.supports_stable_preview
    assert await stream.preview() is None
    source = PreviewStream()
    source.value = None
    supported = wrap(source)
    assert supported.supports_stable_preview
    assert await supported.preview() is None
    assert not supported._closed and not supported._final_received
    assert source.finishes == source.aborts == 0


@pytest.mark.anyio
async def test_many_previews_do_not_consume_events_or_finish_and_final_still_occurs_once():
    source = PreviewStream()
    stream = wrap(source)
    for _ in range(25):
        snapshot = await stream.preview()
        assert snapshot.text == "合成句子。"
    assert source.reads == 25 and source.finishes == source.aborts == 0
    assert stream._last_sequence == 1 and not stream._final_received
    event = (await stream.send_audio(b"\x01\x00"))[0]
    assert event.sequence == 2 and event.type == "transcript.partial"
    final = (await stream.finish())[0]
    assert final.sequence == 3 and final.type == "transcript.final" and final.is_final
    assert await stream.finish() == [] and source.finishes == 1
    with pytest.raises(ProviderError) as error:
        await stream.preview()
    assert error.value.code == "provider_stream_closed"


@pytest.mark.anyio
async def test_returned_and_provider_owned_snapshots_do_not_mutate_stored_revision():
    source = PreviewStream()
    source.value = StableTranscriptPreview.model_validate(source.value)
    stream = wrap(source)
    snapshot = await stream.preview()
    snapshot.segments[0].text = "调用方变更"
    snapshot.provider.model = "changed-by-caller"
    assert (await stream.preview()).segments[0].text == "合成句子。"
    assert source.value.provider.model == "synthetic-stt"
    source.value.segments[0].text = "供应商变更"
    source.value.text = "供应商变更"
    with pytest.raises(ProviderError) as error:
        await stream.preview()
    assert error.value.code == "provider_schema_invalid"


@pytest.mark.anyio
async def test_stable_prefix_append_and_tail_revision_changes_are_allowed():
    source = PreviewStream()
    stream = wrap(source)
    first = await stream.preview()
    source.value["has_unstable_tail"] = True
    assert (await stream.preview()).revision == first.revision
    source.value["revision"] = 2
    source.value["has_unstable_tail"] = False
    source.value["text"] += "补充句子。"
    source.value["segments"].append({"text": "补充句子。", "start_ms": 600, "end_ms": 1000, "confidence": 0.8})
    source.value["confidence"] = 0.8
    assert len((await stream.preview()).segments) == 2
    source.value["revision"] = 3
    source.value["has_unstable_tail"] = True
    assert (await stream.preview()).revision == 3


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", [
    "revision_backwards", "same_revision_append", "same_revision_confidence", "new_revision_confidence",
    "prefix_text", "prefix_timing", "prefix_confidence", "truncated", "disappeared", "language",
])
async def test_revision_and_stable_prefix_cannot_be_rewritten(mutation):
    source = PreviewStream()
    source.value["revision"] = 2
    stream = wrap(source)
    await stream.preview()
    if mutation == "revision_backwards":
        source.value["revision"] = 1
    elif mutation == "same_revision_append":
        source.value["text"] += "追加。"
        source.value["segments"].append({"text": "追加。", "start_ms": 501, "end_ms": 900, "confidence": 0.9})
    elif mutation in {"same_revision_confidence", "new_revision_confidence"}:
        source.value["confidence"] = 0.8
        if mutation == "new_revision_confidence":
            source.value["revision"] += 1
    elif mutation.startswith("prefix_"):
        source.value["revision"] = 3
        if mutation == "prefix_text":
            source.value["segments"][0]["text"] = source.value["text"] = "被重写。"
        elif mutation == "prefix_timing":
            source.value["segments"][0]["end_ms"] = 600
        else:
            source.value["segments"][0]["confidence"] = 0.8
    elif mutation == "truncated":
        source.value["segments"] = []
    elif mutation == "disappeared":
        source.value = None
    else:
        source.value["revision"] = 3
        source.value["language"] = "en-US"
    with pytest.raises(ProviderError) as error:
        await stream.preview()
    assert error.value.code == "provider_schema_invalid"


INVALID_PREVIEWS = [
    {"stream_id": "different"}, {"stream_id": " "}, {"revision": 0}, {"revision": True},
    {"revision": "1"}, {"confidence": float("nan")}, {"confidence": float("inf")},
    {"confidence": -0.1}, {"confidence": 1.1}, {"confidence": "0.9"},
    {"text": ""}, {"text": " "}, {"text": "other-text"}, {"text": "x" * 100_001},
    {"language": ""}, {"language": " "}, {"has_unstable_tail": 1},
    {"segments": []}, {"is_final": True}, {"type": "transcript.final"}, {"extra": "forbidden"},
]


@pytest.mark.anyio
@pytest.mark.parametrize("overrides", INVALID_PREVIEWS)
async def test_strict_schema_rejects_invalid_preview_without_sensitive_error(overrides):
    source = PreviewStream()
    source.value.update(overrides)
    stream = wrap(source)
    with pytest.raises(ProviderError) as error:
        await stream.preview()
    assert error.value.code == "provider_schema_invalid" and error.value.retryable
    assert "合成句子" not in str(error.value) and error.value.details == {}
    assert error.value.__cause__ is None
    assert not stream._final_received and not stream._closed


@pytest.mark.anyio
@pytest.mark.parametrize("segment_override", [
    {"start_ms": -1}, {"end_ms": -1}, {"start_ms": 600}, {"start_ms": "0"},
    {"start_ms": True}, {"text": ""}, {"confidence": float("nan")},
    {"confidence": float("inf")}, {"confidence": 2.0}, {"is_final": True},
])
async def test_nested_segments_are_strict_and_finite(segment_override):
    source = PreviewStream()
    source.value["segments"][0].update(segment_override)
    with pytest.raises(ProviderError) as error:
        await wrap(source).preview()
    assert error.value.code == "provider_schema_invalid"


@pytest.mark.anyio
async def test_segment_order_and_total_text_are_bounded():
    source = PreviewStream()
    source.value["text"] += "重叠。"
    source.value["segments"].append({"text": "重叠。", "start_ms": 499, "end_ms": 1000, "confidence": 0.9})
    with pytest.raises(ProviderError):
        await wrap(source).preview()
    source.value = preview_data(segments=[{"text": "x" * 100_001}])
    with pytest.raises(ProviderError):
        await wrap(source).preview()


@pytest.mark.anyio
@pytest.mark.parametrize("provider_override", [
    {"provider_id": "other"}, {"model": "other"}, {"request_id": "other"},
    {"provider_id": " "}, {"latency_ms": -1}, {"latency_ms": "1"},
    {"token": "must-not-leak"},
])
async def test_provenance_is_bound_to_existing_stream_and_rejects_nested_extra_fields(provider_override):
    source = PreviewStream()
    source.value["provider"].update(provider_override)
    with pytest.raises(ProviderError) as error:
        await wrap(source).preview()
    assert error.value.code == "provider_schema_invalid"
    assert "must-not-leak" not in str(error.value)


@pytest.mark.anyio
async def test_empty_ready_request_id_binds_first_nonempty_provider_request():
    source = PreviewStream(request_id="")
    stream = wrap(source)
    assert (await stream.preview()).provider.request_id == "request-1"
    source.value["provider"]["request_id"] = "request-2"
    with pytest.raises(ProviderError):
        await stream.preview()


@pytest.mark.anyio
async def test_missing_legacy_ready_meta_does_not_invent_required_route_fields():
    source = PreviewStream(ready_provider=False)
    stream = wrap(source)
    assert (await stream.preview()).provider.model == "synthetic-stt"
    source.value["provider"]["model"] = "other"
    with pytest.raises(ProviderError):
        await stream.preview()


@pytest.mark.anyio
async def test_adapter_schema_failure_is_sanitized_but_transport_error_keeps_classification():
    source = PreviewStream()
    try:
        StableTranscriptPreview.model_validate(preview_data(confidence="sensitive-invalid-text"))
    except ValidationError as error:
        source.value = error
    with pytest.raises(ProviderError) as error:
        await wrap(source).preview()
    assert error.value.code == "provider_schema_invalid"
    assert "sensitive-invalid-text" not in str(error.value) and error.value.__cause__ is None
    transient = ProviderError("provider_timeout", "Safe timeout.", retryable=True)
    source.value = transient
    with pytest.raises(ProviderError) as error:
        await wrap(source).preview()
    assert error.value is transient


def test_stable_preview_cannot_be_validated_as_a_final_event():
    snapshot = StableTranscriptPreview.model_validate(preview_data())
    assert not hasattr(snapshot, "is_final") and not hasattr(snapshot, "type")
    with pytest.raises(ValidationError):
        StreamingSTTEvent.model_validate(snapshot.model_dump())


@pytest.mark.anyio
async def test_mutated_model_instances_are_revalidated_including_nested_values():
    source = PreviewStream()
    for mutate in (
        lambda item: setattr(item, "revision", "1"),
        lambda item: setattr(item.segments[0], "start_ms", "0"),
        lambda item: setattr(item.segments[0], "confidence", float("nan")),
        lambda item: setattr(item.provider, "latency_ms", "0"),
    ):
        source.value = StableTranscriptPreview.model_validate(copy.deepcopy(preview_data()))
        mutate(source.value)
        with pytest.raises(ProviderError) as error:
            await wrap(source).preview()
        assert error.value.code == "provider_schema_invalid"
