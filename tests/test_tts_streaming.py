import asyncio
import base64
import json

import httpx
import pytest

from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import CircuitBreaker, ModelGateway
from app.model_gateway.schemas import ProviderContext, ProviderMeta, TTSSynthesizeRequest, Usage
from app.model_gateway.tts_streaming import TTSStreamEvent, ValidatedTTSStream
from app.providers.dashscope.provider import DashScopeProvider
from app.providers.mock.provider import MockProvider
from app.repositories.memory import InMemoryStore


def event(kind, sequence, **kwargs):
    return TTSStreamEvent(
        stream_id="stream_test", sequence=sequence, type=kind, sample_rate_hz=24000,
        provider=ProviderMeta(provider_id="dashscope", model="qwen3-tts-flash", request_id="request_test", latency_ms=0),
        **kwargs,
    )


class FixtureStream:
    stream_id = "stream_test"

    def __init__(self, events=None, ready=None):
        self.ready_event = ready or event("stream.ready", 1)
        self.items = events if events is not None else [event("audio.chunk", 2, pcm_s16le=b"\x01\x00" * 10), event("audio.final", 3, total_audio_bytes=20)]
        self.aborted = 0

    async def events(self):
        for item in self.items:
            if isinstance(item, Exception):
                raise item
            yield item

    async def abort(self):
        self.aborted += 1


def managed(stream, **kwargs):
    return ValidatedTTSStream(stream, provider_id="dashscope", model="qwen3-tts-flash", read_timeout_s=0.1, **kwargs)


@pytest.mark.anyio
async def test_pcm_stream_returns_validated_bytes_but_never_serializes_audio():
    source = FixtureStream()
    statuses = []
    stream = managed(source, on_terminal=lambda status, event, error: statuses.append(status))
    assert not stream.audio_started
    assert stream.ready_event.provider.model == "qwen3-tts-flash"
    output = [item async for item in stream.events()]
    assert [item.type for item in output] == ["audio.chunk", "audio.final"]
    assert output[0].pcm_s16le == b"\x01\x00" * 10
    assert "pcm_s16le" not in output[0].model_dump()
    assert '"pcm_s16le":' not in output[0].model_dump_json()
    assert "pcm_s16le=" not in repr(output[0])
    assert stream.audio_started and stream.byte_count == 20
    assert statuses == ["success"] and source.aborted == 1
    await stream.abort()
    with pytest.raises(ProviderError, match="one consumer"):
        _ = [item async for item in stream.events()]


@pytest.mark.anyio
@pytest.mark.parametrize("items,started", [
    ([], False),
    ([event("audio.final", 2, total_audio_bytes=0)], False),
    ([event("audio.chunk", 2, pcm_s16le=b"x")], False),
    ([event("audio.chunk", 3, pcm_s16le=b"xx")], False),
    ([event("audio.chunk", 2, pcm_s16le=b"xx"), event("audio.final", 3, total_audio_bytes=4)], True),
    ([event("audio.chunk", 2, pcm_s16le=b"xx")], True),
    ([event("audio.chunk", 2, pcm_s16le=b"xx"), event("audio.final", 3, total_audio_bytes=2), event("audio.final", 4, total_audio_bytes=2)], True),
    ([event("stream.ready", 2)], False),
    ([event("audio.chunk", 2, pcm_s16le=b"xx").model_copy(update={"sample_rate_hz": 16000})], False),
    ([event("audio.chunk", 2, pcm_s16le=b"xx").model_copy(update={"stream_id": "other"})], False),
    ([event("audio.chunk", 2, pcm_s16le=b"xx").model_copy(update={"channels": 2})], False),
    ([event("audio.chunk", 2, pcm_s16le=b"xx").model_copy(update={"sample_format": "float32"})], False),
])
async def test_pcm_stream_rejects_invalid_protocol_without_replaying(items, started):
    source = FixtureStream(items)
    stream = managed(source)
    with pytest.raises(ProviderError) as error:
        _ = [item async for item in stream.events()]
    assert error.value.details["audio_started"] is started
    assert error.value.details["fallback_allowed"] is (not started)
    assert source.aborted == 1


@pytest.mark.anyio
@pytest.mark.parametrize("bounds", [{"max_chunk_bytes": 10}, {"max_total_bytes": 10}, {"max_audio_seconds": 0.0001}, {"max_wall_seconds": 0}])
async def test_pcm_stream_bounds_are_enforced_before_output(bounds):
    stream = managed(FixtureStream(), **bounds)
    with pytest.raises(ProviderError) as error:
        _ = [item async for item in stream.events()]
    assert not error.value.details["audio_started"]


@pytest.mark.anyio
async def test_pcm_read_timeout_and_cancellation_close_transport():
    class Slow(FixtureStream):
        async def events(self):
            await asyncio.sleep(60)
            yield event("audio.chunk", 2, pcm_s16le=b"xx")

    source = Slow()
    stream = managed(source)
    with pytest.raises(ProviderError) as error:
        _ = [item async for item in stream.events()]
    assert error.value.code == "provider_timeout" and source.aborted == 1
    source = Slow()
    statuses = []
    stream = managed(source, on_terminal=lambda status, event, error: statuses.append(status))
    task = asyncio.create_task(stream.events().__anext__())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source.aborted == 1 and statuses == ["cancelled"]


@pytest.mark.anyio
async def test_downstream_backpressure_is_not_per_read_timeout():
    stream = managed(FixtureStream())
    iterator = stream.events()
    await iterator.__anext__()
    await asyncio.sleep(0.12)
    assert (await iterator.__anext__()).type == "audio.final"
    await iterator.aclose()


def context(**changes):
    values = dict(
        organization_id="org_default", invocation_id="invocation_test", route_id="route_test",
        provider_connection_id="connection_test", model_configuration_id="config_test", model_type="tts",
        capability=cap.TTS_SYNTHESIZE, purpose="agent_expression", model="qwen3-tts-flash", timeout_s=1,
        attempt=1, fallback_index=0, connection_config={"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
        credentials={"api_key": "synthetic-secret"},
    )
    values.update(changes)
    return ProviderContext(**values)


def payload(pcm=b"\x01\x00" * 10, *, final=False, **changes):
    result = {
        "status_code": 200, "request_id": "request_test", "code": "", "message": "",
        "output": {"text": None, "finish_reason": "stop" if final else None, "audio": {
            "data": "" if final else base64.b64encode(pcm).decode(),
            "url": "https://synthetic.invalid/private-result.wav" if final else "", "id": "audio_test",
        }},
        "usage": {"input_tokens": 0, "output_tokens": 0, "characters": 12},
    }
    result.update(changes)
    return result


def sse(*items):
    return b"".join(("data: %s\r\n\r\n" % (item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))).encode() for item in items)


class PacketStream(httpx.AsyncByteStream):
    def __init__(self, content, *, packet_size=7):
        self.content = content
        self.packet_size = packet_size
        self.closed = False

    async def __aiter__(self):
        for offset in range(0, len(self.content), self.packet_size):
            yield self.content[offset:offset + self.packet_size]

    async def aclose(self):
        self.closed = True


def provider_for(content, *, status=200, content_type="text/event-stream", seen=None, packet_size=7):
    body = PacketStream(content, packet_size=packet_size)

    async def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, headers={"content-type": content_type}, stream=body)

    transport = httpx.MockTransport(handler)
    return DashScopeProvider(client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs)), body


@pytest.mark.anyio
async def test_qwen_http_sse_uses_approved_text_and_pcm_not_vendor_asset_url():
    seen = []
    provider, body = provider_for(b": heartbeat\n\n" + sse(payload(), payload(), payload(final=True), "[DONE]"), seen=seen)
    ctx = context(model_settings={"voice_map": {"voice_frozen": "Cherry"}, "sample_rate_hz": 24000})
    raw = await provider.open_tts_stream(TTSSynthesizeRequest(text="这是已批准的合成测试。", voice_profile_id="voice_frozen"), ctx)
    assert raw.ready_event.provider.provider_id == "dashscope"
    stream = managed(raw)
    items = [item async for item in stream.events()]
    assert b"".join(item.pcm_s16le for item in items) == b"\x01\x00" * 20
    assert items[-1].total_audio_bytes == 40
    assert items[-1].provider.model == ctx.model
    assert items[-1].provider.request_id == "request_test"
    assert "private-result.wav" not in repr(items)
    assert body.closed and len(seen) == 1
    wire = json.loads(seen[0].content)
    assert wire == {"model": ctx.model, "input": {"text": "这是已批准的合成测试。", "voice": "Cherry", "language_type": "Chinese"}}
    assert seen[0].headers["x-dashscope-sse"] == "enable"
    assert seen[0].url.path == "/api/v1/services/aigc/multimodal-generation/generation"


@pytest.mark.anyio
@pytest.mark.parametrize("content", [
    sse(payload()),
    sse(payload(final=True)),
    sse(payload(), payload(final=True), payload(final=True)),
    sse(payload(), payload(final=True), payload()),
    sse(payload(), "[DONE]"),
    sse("[DONE]"),
    sse("not json"),
    sse([1, 2]),
    sse(payload(b"x")),
    sse(payload(b"")),
    sse(payload(), payload(final=True, request_id="changed")),
    sse(payload(), payload(final=True, usage={"input_tokens": -1})),
    sse(payload())[:-1],
    b"data: " + b"x" * 524289,
])
async def test_qwen_sse_invalid_or_truncated_streams_fail_closed(content):
    provider, body = provider_for(content, packet_size=4096)
    raw = await provider.open_tts_stream(TTSSynthesizeRequest(text="合成测试"), context())
    with pytest.raises(ProviderError):
        _ = [item async for item in managed(raw).events()]
    assert body.closed


@pytest.mark.anyio
@pytest.mark.parametrize("status,code", [(401, "provider_auth_failed"), (403, "provider_auth_failed"), (429, "provider_rate_limited"), (500, "provider_server_error"), (400, "provider_bad_request"), (302, "provider_bad_request")])
async def test_qwen_stream_http_errors_do_not_echo_body_or_credentials(status, code):
    provider, body = provider_for(b"synthetic-secret: input content", status=status)
    with pytest.raises(ProviderError) as error:
        await provider.open_tts_stream(TTSSynthesizeRequest(text="合成测试"), context())
    assert error.value.code == code
    assert "synthetic-secret" not in str(error.value) and body.closed


@pytest.mark.anyio
@pytest.mark.parametrize("tts_request,ctx,code", [
    (TTSSynthesizeRequest(text="x" * 601), context(), "provider_bad_request"),
    (TTSSynthesizeRequest(text="测试", speaking_rate=1.2), context(), "provider_bad_request"),
    (TTSSynthesizeRequest(text="测试", format="audio/mp3"), context(), "provider_audio_format_unsupported"),
    (TTSSynthesizeRequest(text="测试"), context(model_settings={"sample_rate_hz": 16000}), "provider_audio_format_unsupported"),
    (TTSSynthesizeRequest(text="测试"), context(model="cosyvoice-v3-plus"), "provider_streaming_not_supported"),
    (TTSSynthesizeRequest(text="测试"), context(model="qwen3-tts-flash", credentials={}), "provider_auth_failed"),
])
async def test_qwen_unsupported_inputs_fail_before_network(tts_request, ctx, code):
    seen = []
    provider, _ = provider_for(b"", seen=seen)
    with pytest.raises(ProviderError) as error:
        await provider.open_tts_stream(tts_request, ctx)
    assert error.value.code == code and seen == []


@pytest.mark.anyio
async def test_non_sse_response_is_explicitly_unsupported_before_pcm():
    provider, body = provider_for(b'{"output":{"audio":{"url":"https://synthetic.invalid/a.wav"}}}', content_type="application/json")
    with pytest.raises(ProviderError) as error:
        await provider.open_tts_stream(TTSSynthesizeRequest(text="合成测试"), context())
    assert error.value.code == "provider_streaming_not_supported" and body.closed


@pytest.mark.anyio
async def test_mock_pcm_requires_explicit_development_fixture(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "development")
    provider = MockProvider()
    with pytest.raises(ProviderError):
        await provider.open_tts_stream(TTSSynthesizeRequest(text="测试"), context(model="mock-tts"))
    request = TTSSynthesizeRequest(text="测试", metadata={"development_tts_fixture": True})
    raw = await provider.open_tts_stream(request, context(model="mock-tts"))
    stream = ValidatedTTSStream(raw, provider_id="mock", model="mock-tts", read_timeout_s=1)
    items = [item async for item in stream.events()]
    assert items[-1].total_audio_bytes == 960 and items[-1].provider.provider_id == "mock"
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    with pytest.raises(ProviderError):
        await provider.open_tts_stream(request, context(model="mock-tts"))


def store_and_route():
    store = InMemoryStore()
    store.provider_connections["connection_test"] = {
        "id": "connection_test", "organization_id": "org_default", "provider_id": "dashscope", "enabled": True,
        "connection_config": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
        "credential_ref": "secret://provider-connections/connection_test", "credential_status": "valid",
    }
    store.model_configurations["config_test"] = {
        "id": "config_test", "organization_id": "org_default", "provider_connection_id": "connection_test",
        "provider_id": "dashscope", "model_type": "tts", "provider_model_id": "qwen3-tts-flash",
        "supported_capabilities": [cap.TTS_SYNTHESIZE], "enabled": True, "status": "ready", "settings": {}, "default_parameters": {},
    }
    store.save_provider_secret("connection_test", {"api_key": "synthetic-secret"})
    route = {"id": "route_test", "organization_id": "org_default", "enabled": True, "capability": cap.TTS_SYNTHESIZE,
             "primary": {"model_configuration_id": "config_test", "timeout_s": 1}, "fallbacks": [], "policy": {}}
    return store, route


@pytest.mark.anyio
async def test_gateway_stream_uses_existing_tts_route_and_redacted_terminal_audit():
    store, route = store_and_route()
    provider, _ = provider_for(sse(payload(), payload(final=True)))
    gateway = ModelGateway(store, {"dashscope": provider}, circuit_breaker=CircuitBreaker())
    stream = await gateway.open_tts_stream(TTSSynthesizeRequest(text="synthetic approved test"), route=route)
    _ = [item async for item in stream.events()]
    assert [record["status"] for record in store.model_invocations] == ["stream_opened", "stream_success"]
    assert all(record["capability"] == cap.TTS_SYNTHESIZE for record in store.model_invocations)
    audit = json.dumps(store.model_invocations)
    assert "synthetic approved test" not in audit and "synthetic-secret" not in audit and "private-result" not in audit


@pytest.mark.anyio
@pytest.mark.parametrize("change,code", [("org", "provider_route_invalid"), ("capability", "provider_route_invalid"), ("disabled", "provider_route_invalid"), ("model_unhealthy", "model_configuration_not_ready"), ("connection_disabled", "provider_connection_disabled"), ("circuit", "provider_circuit_open")])
async def test_gateway_stream_preserves_route_health_scope_and_circuit(change, code):
    store, route = store_and_route()
    seen = []
    provider, _ = provider_for(sse(payload(), payload(final=True)), seen=seen)
    circuits = CircuitBreaker()
    if change == "org":
        route["organization_id"] = "other_org"
    elif change == "capability":
        route["capability"] = cap.STT_STREAMING
    elif change == "disabled":
        route["enabled"] = False
    elif change == "model_unhealthy":
        store.model_configurations["config_test"]["status"] = "failed"
    elif change == "connection_disabled":
        store.provider_connections["connection_test"]["enabled"] = False
    else:
        for _ in range(3):
            circuits.record_failure("org_default:config_test:%s" % cap.TTS_SYNTHESIZE, threshold=3)
    gateway = ModelGateway(store, {"dashscope": provider}, circuit_breaker=circuits)
    with pytest.raises(ProviderError) as error:
        await gateway.open_tts_stream(TTSSynthesizeRequest(text="测试"), route=route)
    assert error.value.code == code and seen == []


@pytest.mark.anyio
async def test_gateway_retries_only_open_and_never_replays_a_partial_stream():
    store, route = store_and_route()
    route["policy"] = {"retry_count": 2}

    class Adapter(DashScopeProvider):
        calls = 0

        async def open_tts_stream(self, request, ctx):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError("provider_rate_limited", "retry", retryable=True)
            return FixtureStream([event("audio.chunk", 2, pcm_s16le=b"xx"), ProviderError("provider_network_error", "dropped", retryable=True)])

    adapter = Adapter()
    stream = await ModelGateway(store, {"dashscope": adapter}, circuit_breaker=CircuitBreaker()).open_tts_stream(TTSSynthesizeRequest(text="测试"), route=route)
    with pytest.raises(ProviderError) as error:
        _ = [item async for item in stream.events()]
    assert adapter.calls == 2 and error.value.details["audio_started"] is True
    assert not error.value.details["fallback_allowed"]
    assert [record["status"] for record in store.model_invocations] == ["failed", "stream_opened", "stream_failed"]


@pytest.mark.anyio
async def test_gateway_applies_frozen_voice_defaults_and_cost_limit():
    store, route = store_and_route()
    store.model_configurations["config_test"]["default_parameters"] = {"voice_profile_id": "default_voice", "speaking_rate": 1.0}
    store.model_configurations["config_test"]["settings"] = {"pricing": {"input_per_million_tokens": 1.0}}
    route["policy"] = {"max_cost_usd_per_call": 0.1}
    requests = []

    class Adapter(DashScopeProvider):
        async def open_tts_stream(self, request, ctx):
            requests.append(request)
            return FixtureStream([event("audio.chunk", 2, pcm_s16le=b"xx"), event("audio.final", 3, total_audio_bytes=2, usage=Usage(input_tokens=1000000))])

    stream = await ModelGateway(store, {"dashscope": Adapter()}, circuit_breaker=CircuitBreaker()).open_tts_stream(TTSSynthesizeRequest(text="测试", voice_profile_id="frozen_voice"), route=route)
    with pytest.raises(ProviderError) as error:
        _ = [item async for item in stream.events()]
    assert requests[0].voice_profile_id == "frozen_voice"
    assert error.value.code == "provider_cost_limit_exceeded"
    assert not error.value.details["fallback_allowed"]
    assert store.model_invocations[-1]["status"] == "stream_failed"


@pytest.mark.anyio
async def test_explicit_abort_cancels_pending_read_even_if_telemetry_fails():
    reading = asyncio.Event()

    class Slow(FixtureStream):
        async def events(self):
            reading.set()
            await asyncio.sleep(60)
            yield event("audio.chunk", 2, pcm_s16le=b"xx")

    def broken_audit(*args):
        raise RuntimeError("synthetic audit failure")

    source = Slow()
    stream = managed(source, on_terminal=broken_audit)
    task = asyncio.create_task(stream.events().__anext__())
    await reading.wait()
    await stream.abort()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.1)
    assert source.aborted == 1 and not stream.audio_started


@pytest.mark.anyio
@pytest.mark.parametrize("changes", [
    {"channels": True},
    {"sequence": "2"},
    {"sample_rate_hz": "24000"},
    {"pcm_s16le": "xx"},
    {"unexpected": True},
    {"provider": {"provider_id": "dashscope", "model": "qwen3-tts-flash", "request_id": "request_test", "latency_ms": 0, "secret": "forbidden"}},
    {"provider": {"provider_id": "dashscope", "model": "changed", "request_id": "request_test", "latency_ms": 0}},
])
async def test_stream_rejects_coercion_extra_fields_and_provider_drift(changes):
    raw = {**event("audio.chunk", 2, pcm_s16le=b"xx").model_dump(), "pcm_s16le": b"xx", **changes}
    stream = managed(FixtureStream([raw]))
    with pytest.raises(ProviderError) as error:
        _ = [item async for item in stream.events()]
    assert error.value.code == "provider_schema_invalid" and not stream.audio_started


@pytest.mark.anyio
async def test_gateway_missing_stream_adapter_is_explicit_and_not_retried():
    store, route = store_and_route()
    route["policy"] = {"retry_count": 3}

    class BatchOnly:
        provider_id = "dashscope"

        async def invoke(self, *args):
            raise AssertionError("must not invoke batch automatically")

    with pytest.raises(ProviderError) as error:
        await ModelGateway(store, {"dashscope": BatchOnly()}, circuit_breaker=CircuitBreaker()).open_tts_stream(TTSSynthesizeRequest(text="测试"), route=route)
    assert error.value.code == "provider_streaming_not_supported"
    assert error.value.details["audio_started"] is False
    assert error.value.details["attempts"] == 1 and len(store.model_invocations) == 1


@pytest.mark.anyio
async def test_sse_abort_after_first_pcm_closes_http_response_without_final():
    provider, body = provider_for(sse(payload(), payload(), payload(final=True)))
    raw = await provider.open_tts_stream(TTSSynthesizeRequest(text="合成测试"), context())
    stream = managed(raw)
    iterator = stream.events()
    assert (await iterator.__anext__()).type == "audio.chunk"
    await stream.abort()
    assert body.closed
    with pytest.raises(ProviderError):
        await iterator.__anext__()
    await iterator.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("allow", [True, False])
async def test_gateway_open_respects_existing_fallback_allowlist(allow):
    store, route = store_and_route()
    store.model_configurations["config_fallback"] = {**store.model_configurations["config_test"], "id": "config_fallback"}
    route["fallbacks"] = [{"model_configuration_id": "config_fallback", "timeout_s": 1}]
    route["policy"] = {"fallback_on": ["rate_limited"] if allow else ["timeout"]}
    calls = []

    class Adapter(DashScopeProvider):
        async def open_tts_stream(self, request, ctx):
            calls.append(ctx.model_configuration_id)
            if ctx.model_configuration_id == "config_test":
                raise ProviderError("provider_rate_limited", "synthetic 429", retryable=True)
            return FixtureStream()

    gateway = ModelGateway(store, {"dashscope": Adapter()}, circuit_breaker=CircuitBreaker())
    if allow:
        stream = await gateway.open_tts_stream(TTSSynthesizeRequest(text="测试"), route=route)
        _ = [item async for item in stream.events()]
        assert calls == ["config_test", "config_fallback"]
        assert store.model_invocations[-2]["status"] == "stream_fallback_opened"
    else:
        with pytest.raises(ProviderError) as error:
            await gateway.open_tts_stream(TTSSynthesizeRequest(text="测试"), route=route)
        assert error.value.code == "provider_rate_limited" and calls == ["config_test"]


@pytest.mark.anyio
async def test_qwen_sse_rejects_invalid_base64_and_structured_vendor_error():
    bad = payload()
    bad["output"]["audio"]["data"] = "%%%not-base64%%%"
    provider, body = provider_for(sse(bad))
    raw = await provider.open_tts_stream(TTSSynthesizeRequest(text="合成测试"), context())
    with pytest.raises(ProviderError) as error:
        _ = [item async for item in managed(raw).events()]
    assert error.value.code == "provider_schema_invalid" and body.closed
    provider, body = provider_for(sse(payload(status_code=429, message="synthetic input must not be echoed")))
    raw = await provider.open_tts_stream(TTSSynthesizeRequest(text="合成测试"), context())
    with pytest.raises(ProviderError) as error:
        _ = [item async for item in managed(raw).events()]
    assert error.value.code == "provider_rate_limited" and "synthetic input" not in str(error.value)
