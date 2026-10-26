"""HTTP adapter + route policy + PCM terminal audit; synthetic content only."""

import asyncio
import json
from time import monotonic

import httpx
import pytest

from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import CircuitBreaker, ModelGateway
from app.model_gateway.schemas import TTSSynthesizeRequest
from app.providers.dashscope.provider import DashScopeProvider
from test_tts_streaming import PacketStream, payload, sse, store_and_route


def pipeline(handler):
    store, route = store_and_route()
    route["policy"] = {"retry_count": 1, "retry_backoff_ms": 0, "circuit_failure_threshold": 3}
    transport = httpx.MockTransport(handler)
    adapter = DashScopeProvider(client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs))
    return store, route, ModelGateway(store, {"dashscope": adapter}, circuit_breaker=CircuitBreaker())


@pytest.mark.anyio
async def test_header_stall_retries_within_shared_budget_and_delivers_one_complete_audio():
    requests = []
    closed = asyncio.Event()

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=PacketStream(sse(payload(), payload(final=True)), packet_size=8192))

    store, route, gateway = pipeline(handler)
    started = monotonic()
    stream = await gateway.open_tts_stream(
        TTSSynthesizeRequest(text="synthetic retry probe"), route=route, first_audio_deadline=started + .6,
    )
    events = [event async for event in stream.events()]
    assert closed.is_set() and len(requests) == 2
    assert events[-1].type == "audio.final" and events[-1].total_audio_bytes == 20
    assert sum(len(event.pcm_s16le) for event in events) == 20
    assert monotonic() - started < .6
    assert [row["status"] for row in store.model_invocations] == ["failed", "stream_opened", "stream_success"]
    assert store.model_invocations[0]["error_code"] == "provider_timeout"
    assert "synthetic retry probe" not in json.dumps(store.model_invocations)


@pytest.mark.anyio
@pytest.mark.parametrize("streaming", [False, True])
async def test_billing_rejection_is_not_retried_and_raw_error_is_never_logged(streaming):
    calls = []
    private_marker = "SYNTHETIC_PRIVATE_RESPONSE_MARKER"

    async def handler(request):
        calls.append(request)
        error = {"code": "Arrearage", "request_id": "07400000-0000-0000-0000-000000000001", "message": private_marker}
        if not streaming:
            return httpx.Response(400, json=error)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=PacketStream(sse(payload(), error), packet_size=8192))

    store, route, gateway = pipeline(handler)
    events = []
    with pytest.raises(ProviderError) as raised:
        stream = await gateway.open_tts_stream(TTSSynthesizeRequest(text="synthetic billing probe"), route=route)
        async for event in stream.events():
            events.append(event)
    assert raised.value.code == "provider_billing_required"
    assert not raised.value.retryable and len(calls) == 1
    assert bool(events) is streaming
    if streaming:
        assert raised.value.details["audio_started"] is True
        assert raised.value.details["fallback_allowed"] is False
    audit = json.dumps(store.model_invocations)
    assert private_marker not in audit and "synthetic-secret" not in audit
    assert "synthetic billing probe" not in audit
    assert store.model_invocations[-1]["error_code"] == "provider_billing_required"
