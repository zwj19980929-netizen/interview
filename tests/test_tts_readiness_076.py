"""Route health uses the runtime TTS protocol; every transport is synthetic."""

import asyncio
import json
from time import monotonic

import httpx
import pytest

from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import CircuitBreaker, ModelGateway
from app.model_gateway.schemas import ProviderMeta, TTSSynthesizeResponse
from app.providers.dashscope.provider import DashScopeProvider
from app.providers.dashscope import tts_streaming as dashscope_streaming
from app.services.model_admin import ModelAdminService
from test_tts_streaming import FixtureStream, PacketStream, event, payload, sse, store_and_route


def setup(source=None, *, purpose="interview_agent_expression", open_impl=None, batch_impl=None):
    store, route = store_and_route()
    route["purpose"] = purpose
    route["primary"]["timeout_s"] = 30
    store.model_routes[route["id"]] = route
    calls = []

    class Adapter(DashScopeProvider):
        async def open_tts_stream(self, request, context):
            calls.append(("stream", request, context))
            return await open_impl() if open_impl else source or FixtureStream()

        async def invoke(self, capability, request, context):
            calls.append(("batch", request, context))
            if batch_impl:
                return await batch_impl()
            return TTSSynthesizeResponse(
                audio_uri="https://synthetic.invalid/result.wav", content_type="audio/wav",
                content_hash="synthetic", duration_ms=100,
                provider=ProviderMeta(provider_id="dashscope", model="qwen3-tts-flash", request_id="synthetic", latency_ms=0),
            )

    service = ModelAdminService(store)
    service.gateway = ModelGateway(store, {"dashscope": Adapter()}, circuit_breaker=CircuitBreaker())
    return service, store, route, calls


@pytest.mark.anyio
async def test_expression_probe_requires_complete_validated_pcm_and_safe_audit(monkeypatch):
    monkeypatch.delenv("INTERVIEWER_STREAMING_TTS_ENABLED", raising=False)
    source = FixtureStream()
    service, store, route, calls = setup(source)
    result = await service.test_route(route["id"])
    assert result == {"probe_mode": "tts_stream", "audio_chunks": 1, "audio_bytes": 20}
    assert [item[0] for item in calls] == ["stream"]
    assert 0 < calls[0][2].timeout_s <= 12
    assert source.aborted == 1
    assert store.model_routes[route["id"]]["last_health"]["status"] == "healthy"
    assert [item["status"] for item in store.model_invocations] == ["stream_opened", "stream_success"]
    encoded = json.dumps([result, store.model_invocations], ensure_ascii=False)
    assert "连接测试" not in encoded and "synthetic-secret" not in encoded and "pcm_s16le" not in encoded


@pytest.mark.anyio
@pytest.mark.parametrize("purpose,enabled", [
    ("question_speech_generation", "true"), ("other_tts", "true"),
    ("interview_agent_expression", "false"),
])
async def test_other_assets_and_disabled_streaming_keep_batch_probe(monkeypatch, purpose, enabled):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", enabled)
    service, store, route, calls = setup(purpose=purpose)
    result = await service.test_route(route["id"])
    assert result["content_type"] == "audio/wav"
    assert [item[0] for item in calls] == ["batch"]
    assert store.model_routes[route["id"]]["last_health"]["status"] == "healthy"


@pytest.mark.anyio
async def test_probe_checks_route_voice_defaults_without_claiming_interview_voice(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")
    service, store, route, calls = setup()
    store.model_configurations["config_test"]["default_parameters"] = {
        "voice_profile_id": "configured_synthetic_voice", "language": "en-US", "speaking_rate": 1.0,
    }
    await service.test_route(route["id"])
    request = calls[0][1]
    assert request.voice_profile_id == "configured_synthetic_voice" and request.language == "en-US"
    assert request.purpose == route["purpose"]
    assert "interview_id" not in request.metadata and "candidate" not in request.metadata


@pytest.mark.anyio
@pytest.mark.parametrize("items,code", [
    ([], "provider_audio_final_missing"),
    ([event("audio.chunk", 2, pcm_s16le=b"xx")], "provider_audio_final_missing"),
    ([event("audio.chunk", 2, pcm_s16le=b"x")], "provider_schema_invalid"),
    ([event("audio.chunk", 2, pcm_s16le=b"xx"), event("audio.final", 3, total_audio_bytes=4)], "provider_schema_invalid"),
    ([event("audio.chunk", 2, pcm_s16le=b"xx"), event("audio.final", 3, total_audio_bytes=2),
      event("audio.chunk", 4, pcm_s16le=b"xx")], "provider_schema_invalid"),
    ([event("audio.chunk", 2, pcm_s16le=b"xx"),
      ProviderError("provider_billing_required", "synthetic private body", retryable=False)], "provider_billing_required"),
    ([event("audio.chunk", 2, pcm_s16le=b"xx"), event("audio.final", 3, total_audio_bytes=2),
      ProviderError("provider_billing_required", "synthetic private body", retryable=False)], "provider_billing_required"),
    ([event("audio.chunk", 2, pcm_s16le=b"xx"),
      ProviderError("provider_streaming_not_supported", "synthetic private body", retryable=False)], "provider_streaming_not_supported"),
])
async def test_ready_or_first_audio_cannot_hide_missing_final_bad_pcm_or_rejection(monkeypatch, items, code):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")
    source = FixtureStream(items)
    service, store, route, calls = setup(source)
    with pytest.raises(ProviderError) as error:
        await service.test_route(route["id"])
    assert error.value.code == code and source.aborted == 1
    health = store.model_routes[route["id"]]["last_health"]
    assert health["status"] == "failed" and health["reason_code"] == code
    assert len(calls) == 1 and calls[0][0] == "stream"
    assert "synthetic private body" not in json.dumps([health, store.model_invocations])


@pytest.mark.anyio
async def test_outer_cancel_during_open_is_audited_and_never_retried(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def opening():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service, store, route, calls = setup(open_impl=opening)
    task = asyncio.create_task(service._probe_route(route, "org_default"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and len(calls) == 1
    assert [item["status"] for item in store.model_invocations] == ["stream_cancelled"]
    assert store.model_invocations[0]["error_code"] == "provider_cancelled"


@pytest.mark.anyio
async def test_slow_tail_is_bounded_by_whole_probe_budget(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")

    class SlowTail(FixtureStream):
        async def events(self):
            yield event("audio.chunk", 2, pcm_s16le=b"xx")
            await asyncio.Event().wait()

    source = SlowTail()
    service, store, route, calls = setup(source)
    service._route_readiness_refresher.probe_timeout = 0.3
    started = monotonic()
    with pytest.raises(ProviderError) as error:
        await service.test_route(route["id"])
    assert error.value.code == "provider_timeout" and monotonic() - started < 1
    assert source.aborted == 1 and len(calls) == 1
    assert store.model_routes[route["id"]]["last_health"]["status"] == "failed"
    assert [item["status"] for item in store.model_invocations] == ["stream_opened", "stream_cancelled"]


@pytest.mark.anyio
async def test_known_rejection_survives_cancel_while_cleanup_is_blocked(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")
    aborting, release = asyncio.Event(), asyncio.Event()

    class BlockedCleanup(FixtureStream):
        async def abort(self):
            self.aborted += 1
            aborting.set()
            await release.wait()

    source = BlockedCleanup([ProviderError("provider_billing_required", "private rejection", retryable=False)])
    service, store, route, calls = setup(source)
    task = asyncio.create_task(service._probe_route(route, "org_default"))
    await aborting.wait()
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.provider_error.code == "provider_billing_required"
        assert task.provider_error.retryable is False
        assert store.model_invocations[-1]["error_code"] == "provider_billing_required"
        assert len(calls) == 1
    finally:
        release.set()


@pytest.mark.anyio
async def test_blocked_cleanup_cannot_mark_complete_pcm_healthy(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")
    release = asyncio.Event()

    class BlockedCleanup(FixtureStream):
        async def abort(self):
            self.aborted += 1
            await release.wait()

    source = BlockedCleanup()
    service, store, route, calls = setup(source)
    service._route_readiness_refresher.probe_timeout = 0.3
    try:
        with pytest.raises(ProviderError) as error:
            await service.test_route(route["id"])
        assert error.value.code == "provider_timeout"
        assert store.model_routes[route["id"]]["last_health"]["status"] == "failed"
        assert len(calls) == 1
    finally:
        release.set()


@pytest.mark.anyio
@pytest.mark.parametrize("billing", [True, False])
async def test_cleanup_exception_preserves_known_rejection_or_is_safely_classified(monkeypatch, billing):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")
    source = FixtureStream([ProviderError("provider_billing_required", "private body", retryable=False)] if billing else None)
    service, store, route, _ = setup(source)
    original = service.gateway.open_tts_stream

    async def opening(*args, **kwargs):
        stream = await original(*args, **kwargs)

        async def failed_cleanup():
            raise RuntimeError("private cleanup diagnostic")

        stream.abort = failed_cleanup
        return stream

    monkeypatch.setattr(service.gateway, "open_tts_stream", opening)
    with pytest.raises(ProviderError) as error:
        await service.test_route(route["id"])
    assert error.value.code == ("provider_billing_required" if billing else "provider_probe_cleanup_failed")
    health = store.model_routes[route["id"]]["last_health"]
    assert health["status"] == "failed" and health["reason_code"] == error.value.code
    assert "private" not in json.dumps([health, store.model_invocations])


@pytest.mark.anyio
async def test_cancelled_probe_never_consumes_late_uncooperative_open(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")
    entered, cancelled, release, aborted = (asyncio.Event() for _ in range(4))

    class LateStream(FixtureStream):
        consumed = False

        async def events(self):
            self.consumed = True
            async for item in super().events():
                yield item

        async def abort(self):
            await super().abort()
            aborted.set()

    source = LateStream()

    async def opening():
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        return source

    service, store, route, calls = setup(open_impl=opening)
    task = asyncio.create_task(service._probe_route(route, "org_default"))
    await entered.wait()
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert cancelled.is_set() and not source.consumed
        release.set()
        await asyncio.wait_for(aborted.wait(), 1)
        assert not source.consumed and len(calls) == 1
        assert all(item["status"] != "stream_success" for item in store.model_invocations)
    finally:
        release.set()


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["success", "exception", "timeout"])
async def test_actual_dashscope_gateway_requires_confirmed_transport_cleanup(monkeypatch, outcome):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")
    monkeypatch.setattr(dashscope_streaming, "_CLOSE_TIMEOUT", 0.03)
    closed, cancelled = asyncio.Event(), asyncio.Event()
    body = PacketStream(sse(payload(), payload(final=True), "[DONE]"))

    class Client(httpx.AsyncClient):
        async def __aexit__(self, *args):
            await super().__aexit__(*args)
            if outcome == "exception":
                raise RuntimeError("private close failure")
            if outcome == "timeout":
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            closed.set()

    async def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    adapter = DashScopeProvider(client_factory=lambda **kwargs: Client(transport=httpx.MockTransport(handler), **kwargs))
    service, store, route, _ = setup()
    service.gateway = ModelGateway(store, {"dashscope": adapter}, circuit_breaker=CircuitBreaker())
    if outcome == "success":
        result = await service.test_route(route["id"])
        assert result["audio_bytes"] == 20 and closed.is_set()
        assert store.model_routes[route["id"]]["last_health"]["status"] == "healthy"
    else:
        with pytest.raises(ProviderError) as error:
            await service.test_route(route["id"])
        assert error.value.code == "provider_probe_cleanup_failed"
        health = store.model_routes[route["id"]]["last_health"]
        assert health["status"] == "failed" and health["reason_code"] == "provider_probe_cleanup_failed"
        if outcome == "timeout":
            await asyncio.wait_for(cancelled.wait(), 1)
    assert body.closed
    assert "private close failure" not in json.dumps([store.model_routes, store.model_invocations])


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["exception", "timeout"])
async def test_gateway_observes_source_abort_failure_without_changing_abort_semantics(monkeypatch, outcome):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")

    class FailedClose(FixtureStream):
        async def abort(self):
            self.aborted += 1
            if outcome == "exception":
                raise RuntimeError("private cleanup failure")
            await asyncio.Event().wait()

    source = FailedClose()
    service, store, route, _ = setup(source)
    route["primary"]["timeout_s"] = 0.03
    stream = await service.gateway.open_tts_stream(service._probe_request(route["capability"], "org_default", route["purpose"]), route=route)
    items = [item async for item in stream.events()]
    assert items[-1].type == "audio.final"
    assert stream.cleanup_confirmed is False
    assert stream.cleanup_error.code == "provider_stream_cleanup_failed"
    # Runtime abort keeps its existing exception-swallowing behavior; the
    # readiness owner can now see the failure through a read-only seam.
    await stream.abort()
    assert source.aborted == 1


@pytest.mark.anyio
async def test_batch_only_adapter_keeps_same_route_and_default_voice(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")

    async def unsupported():
        raise ProviderError("provider_streaming_not_supported", "Synthetic batch-only provider.", retryable=False)

    service, store, route, calls = setup(open_impl=unsupported)
    route["policy"]["retry_count"] = 2
    store.model_configurations["config_test"]["default_parameters"] = {"voice_profile_id": "same_synthetic_voice"}
    result = await service.test_route(route["id"])
    assert result["content_type"] == "audio/wav"
    assert [item[0] for item in calls] == ["stream", "batch"]
    assert all(item[1].voice_profile_id == "same_synthetic_voice" for item in calls)
    assert calls[0][1] == calls[1][1]
    assert calls[0][2].route_id == calls[1][2].route_id == route["id"]
    assert calls[0][2].model_configuration_id == calls[1][2].model_configuration_id == "config_test"
    assert store.model_routes[route["id"]]["last_health"]["status"] == "healthy"
    assert [item["status"] for item in store.model_invocations] == ["failed", "success"]


@pytest.mark.anyio
@pytest.mark.parametrize("code", ["provider_auth_failed", "provider_billing_required", "provider_timeout", "provider_schema_invalid"])
async def test_only_explicit_unsupported_can_use_batch_compatibility(monkeypatch, code):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")

    async def rejected():
        raise ProviderError(code, "Private rejection.", retryable=False)

    service, store, route, calls = setup(open_impl=rejected)
    route["policy"]["retry_count"] = 2
    with pytest.raises(ProviderError) as error:
        await service.test_route(route["id"])
    assert error.value.code == code
    assert [item[0] for item in calls] == ["stream"]
    assert store.model_routes[route["id"]]["last_health"]["status"] == "failed"


@pytest.mark.anyio
async def test_batch_compatibility_shares_original_probe_deadline(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")
    cancelled = asyncio.Event()

    async def unsupported():
        await asyncio.sleep(0.6)
        raise ProviderError("provider_streaming_not_supported", "Synthetic batch-only provider.", retryable=False)

    async def batch():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service, store, route, calls = setup(open_impl=unsupported, batch_impl=batch)
    service._route_readiness_refresher.probe_timeout = 1
    started = monotonic()
    with pytest.raises(ProviderError) as error:
        await service.test_route(route["id"])
    assert error.value.code == "provider_timeout"
    assert monotonic() - started < 1.3  # A renewed 1s budget after open would exceed 1.6s.
    assert cancelled.is_set() and [item[0] for item in calls] == ["stream", "batch"]
    assert store.model_routes[route["id"]]["last_health"]["reason_code"] == "provider_timeout"


@pytest.mark.anyio
@pytest.mark.parametrize("cleanup_result", [False, None, True])
async def test_batch_compatibility_requires_open_cleanup_confirmation(monkeypatch, cleanup_result):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")
    service, store, route, calls = setup()
    cleanup = asyncio.get_running_loop().create_future()
    if cleanup_result is not None:
        cleanup.set_result(cleanup_result)

    async def opening(request, **kwargs):
        asyncio.current_task().tts_cleanup_completion = cleanup
        raise ProviderError("provider_streaming_not_supported", "Synthetic unsupported.", retryable=False)

    monkeypatch.setattr(service.gateway, "open_tts_stream", opening)
    service._route_readiness_refresher.probe_timeout = 0.3
    if cleanup_result is True:
        assert (await service.test_route(route["id"]))["content_type"] == "audio/wav"
        assert [item[0] for item in calls] == ["batch"]
    else:
        with pytest.raises(ProviderError) as error:
            await service.test_route(route["id"])
        assert error.value.code in {"provider_probe_cleanup_failed", "provider_timeout"}
        assert calls == []
    if not cleanup.done():
        cleanup.set_result(True)


@pytest.mark.anyio
@pytest.mark.parametrize("code", ["provider_billing_required", "provider_auth_failed"])
async def test_late_terminal_error_on_confirmed_open_cleanup_prevents_batch(monkeypatch, code):
    monkeypatch.setenv("INTERVIEWER_STREAMING_TTS_ENABLED", "true")
    service, store, route, calls = setup()

    async def opening(request, **kwargs):
        cleanup = asyncio.get_running_loop().create_future()
        cleanup.provider_error = ProviderError(code, "Private terminal failure.", retryable=False)
        cleanup.set_result(True)
        asyncio.current_task().tts_cleanup_completion = cleanup
        raise ProviderError("provider_streaming_not_supported", "Synthetic unsupported.", retryable=False)

    monkeypatch.setattr(service.gateway, "open_tts_stream", opening)
    with pytest.raises(ProviderError) as error:
        await service.test_route(route["id"])
    assert error.value.code == code and calls == []
    assert store.model_routes[route["id"]]["last_health"]["reason_code"] == code
