"""Synthetic HTTP/SSE failures retain safe billing and transport diagnostics."""

import asyncio
import json
from time import monotonic

import httpx
import pytest

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import TTSSynthesizeRequest
from app.providers.dashscope.provider import DashScopeProvider
from app.providers.dashscope import tts_streaming
from app.providers.dashscope import tts_diagnostics
from app.providers.dashscope.tts_diagnostics import MAX_ERROR_BYTES
from test_tts_streaming import PacketStream, context, payload, sse


PRIVATE = "synthetic-private-input-and-credential"
REQUEST_ID = "12345678-abcd-4321-abcd-123456789abc"


def provider_for(body, *, status=200, headers=None, on_request=None):
    async def handler(request):
        if on_request:
            await on_request(request)
        return httpx.Response(status, headers={"content-type": "text/event-stream", **(headers or {})}, stream=body)

    transport = httpx.MockTransport(handler)
    return DashScopeProvider(client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs))


async def open_stream(provider):
    return await provider.open_tts_stream(TTSSynthesizeRequest(text="合成诊断测试。"), context())


def assert_private(error):
    assert PRIVATE not in str(error) + json.dumps(error.details)
    assert not {"message", "input", "headers", "url", "credentials"}.intersection(error.details)


@pytest.mark.anyio
@pytest.mark.parametrize("wire", ["http", "sse"])
@pytest.mark.parametrize("status,vendor,code,retryable", [
    (400, "Arrearage", "provider_billing_required", False),
    (403, "AllocationQuota.FreeTierOnly", "provider_billing_required", False),
    (401, "InvalidApiKey", "provider_auth_failed", False),
    (429, "Throttling.RateQuota", "provider_rate_limited", True),
    (429, "Throttling.AllocationQuota", "provider_rate_limited", True),
    (429, "Throttling.AccessDenied", "provider_rate_limited", True),
    (402, "", "provider_billing_required", False),
    (403, "", "provider_auth_failed", False),
    (500, "", "provider_server_error", True),
    (400, "", "provider_bad_request", False),
])
async def test_http_and_sse_business_errors_are_distinct_and_private(wire, status, vendor, code, retryable):
    error = {"status_code": status, "code": vendor, "request_id": REQUEST_ID, "message": PRIVATE, "input": PRIVATE}
    content = json.dumps(error).encode() if wire == "http" else sse(error)
    body = PacketStream(content)
    provider = provider_for(body, status=status if wire == "http" else 200)
    with pytest.raises(ProviderError) as raised:
        stream = await open_stream(provider)
        _ = [event async for event in stream.events()]
    failure = raised.value
    assert (failure.code, failure.retryable) == (code, retryable)
    assert failure.details["fallback_allowed"] is retryable
    assert failure.details["http_status"] == status
    assert failure.details.get("vendor_code", "") == vendor
    assert failure.details["provider_request_id"] == REQUEST_ID
    assert failure.details["transport_phase"] == "response_body"
    assert_private(failure)
    assert body.closed


@pytest.mark.anyio
@pytest.mark.parametrize("vendor,code", [
    ("Arrearage", "provider_billing_required"),
    ("AllocationQuota.FreeTierOnly", "provider_billing_required"),
    ("Throttling.AllocationQuota", "provider_rate_limited"),
    (PRIVATE, "provider_server_error"),
])
async def test_sse_business_code_without_http_failure_is_not_misreported(vendor, code):
    body = PacketStream(sse({"code": vendor, "message": PRIVATE, "request_id": PRIVATE}))
    with pytest.raises(ProviderError) as raised:
        stream = await open_stream(provider_for(body))
        _ = [event async for event in stream.events()]
    assert raised.value.code == code
    assert "provider_request_id" not in raised.value.details
    assert_private(raised.value)


@pytest.mark.anyio
@pytest.mark.parametrize("content", [b"not json", b'[]', b'{"code": "unknown-billing-word"}', b'{"code": {"value": "Arrearage"}}'])
async def test_unknown_or_malformed_http_400_stays_nonretryable_bad_request(content):
    body = PacketStream(content)
    with pytest.raises(ProviderError) as raised:
        await open_stream(provider_for(body, status=400))
    assert raised.value.code == "provider_bad_request"
    assert not raised.value.retryable and raised.value.details["fallback_allowed"] is False
    assert "vendor_code" not in raised.value.details
    assert body.closed


@pytest.mark.anyio
async def test_error_body_size_is_bounded_and_oversized_prefix_is_not_trusted():
    class Oversized(PacketStream):
        read_bytes = 0

        async def __aiter__(self):
            async for packet in super().__aiter__():
                self.read_bytes += len(packet)
                yield packet

    content = json.dumps({"code": "Arrearage", "message": PRIVATE * MAX_ERROR_BYTES}).encode()
    body = Oversized(content, packet_size=1024)
    with pytest.raises(ProviderError) as raised:
        await open_stream(provider_for(body, status=400))
    assert raised.value.code == "provider_bad_request" and "vendor_code" not in raised.value.details
    assert body.read_bytes <= MAX_ERROR_BYTES + 1024
    assert body.closed
    assert_private(raised.value)


@pytest.mark.anyio
async def test_compressed_error_body_is_not_decompressed():
    class NeverRead(PacketStream):
        async def __aiter__(self):
            raise AssertionError("Unexpected encoded error bodies must not be consumed")
            yield b""

    body = NeverRead(b"")
    with pytest.raises(ProviderError) as raised:
        await open_stream(provider_for(body, status=402, headers={"content-encoding": "gzip"}))
    assert raised.value.code == "provider_billing_required" and body.closed


@pytest.mark.anyio
@pytest.mark.parametrize("event,phase,error_type", [
    ("connection.connect_tcp.started", "connect", httpx.ConnectTimeout),
    ("connection.start_tls.started", "tls", httpx.ConnectTimeout),
    ("http11.receive_response_headers.started", "response_headers", httpx.ReadTimeout),
    ("http11.send_request_body.started", "request_write", httpx.WriteError),
])
async def test_network_failure_reports_only_fixed_stage_and_elapsed_time(event, phase, error_type):
    async def fail(request):
        await request.extensions["trace"](event, {"host": PRIVATE, "headers": PRIVATE})
        await asyncio.sleep(.003)
        raise error_type(PRIVATE)

    with pytest.raises(ProviderError) as raised:
        await open_stream(provider_for(PacketStream(b""), on_request=fail))
    failure = raised.value
    assert failure.details["transport_phase"] == phase
    assert failure.details["transport_phase_ms"] >= 1
    assert failure.details["transport_elapsed_ms"] >= failure.details["transport_phase_ms"]
    assert failure.code == ("provider_timeout" if issubclass(error_type, httpx.TimeoutException) else "provider_network_error")
    assert_private(failure)


@pytest.mark.anyio
async def test_read_timeout_reports_response_body_phase_without_supplier_text():
    class StalledBody(PacketStream):
        async def __aiter__(self):
            await asyncio.sleep(.003)
            raise httpx.ReadTimeout(PRIVATE)
            yield b""

    body = StalledBody(b"")
    stream = await open_stream(provider_for(body))
    with pytest.raises(ProviderError) as raised:
        _ = [event async for event in stream.events()]
    assert raised.value.code == "provider_timeout"
    assert raised.value.details["transport_phase"] == "response_body"
    assert raised.value.details["transport_outcome"] == "timeout"
    assert_private(raised.value)
    assert body.closed


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["connect", "tls", "response_headers", "response_body"])
async def test_cancel_propagates_with_safe_transport_diagnostics_and_cleanup(phase):
    entered = asyncio.Event()
    trace_name = {"connect": "connection.connect_tcp.started", "tls": "connection.start_tls.started",
                  "response_headers": "http11.receive_response_headers.started"}

    async def cancel_open(request):
        if phase != "response_body":
            await request.extensions["trace"](trace_name[phase], {"secret": PRIVATE})
            entered.set()
            await asyncio.Event().wait()

    class BlockedBody(PacketStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

    body = BlockedBody(b"")
    provider = provider_for(body, on_request=cancel_open)

    async def run():
        stream = await open_stream(provider)
        _ = [event async for event in stream.events()]

    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    diagnostics = task.provider_diagnostics
    assert diagnostics["transport_phase"] == phase and diagnostics["transport_outcome"] == "cancelled"
    assert not hasattr(task, "provider_error")
    assert PRIVATE not in json.dumps(diagnostics)
    if phase == "response_body":
        assert body.closed


@pytest.mark.anyio
async def test_outer_deadline_cause_keeps_transport_stage():
    async def stall(request):
        await request.extensions["trace"]("connection.start_tls.started", {"secret": PRIVATE})
        await asyncio.Event().wait()

    task = asyncio.create_task(open_stream(provider_for(PacketStream(b""), on_request=stall)))
    with pytest.raises(asyncio.TimeoutError) as raised:
        await asyncio.wait_for(task, .02)
    cause = raised.value.__cause__
    assert isinstance(cause, asyncio.CancelledError)
    assert task.provider_diagnostics["transport_phase"] == "tls"
    assert PRIVATE not in json.dumps(task.provider_diagnostics)


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("status,code,retryable", [
    (400, "provider_bad_request", False),
    (401, "provider_auth_failed", False),
    (402, "provider_billing_required", False),
    (403, "provider_auth_failed", False),
    (429, "provider_rate_limited", True),
    (503, "provider_server_error", True),
])
async def test_stalled_error_body_never_erases_known_http_rejection(monkeypatch, cancel, status, code, retryable):
    monkeypatch.setattr(tts_diagnostics, "ERROR_BODY_TIMEOUT", .02 if not cancel else 1)
    entered = asyncio.Event()

    class StalledError(PacketStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

    body = StalledError(b"")
    task = asyncio.create_task(open_stream(provider_for(body, status=status)))
    await asyncio.wait_for(entered.wait(), .2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        details = task.provider_diagnostics
        assert details["transport_outcome"] == "cancelled"
        failure = task.provider_error
        assert (failure.code, failure.retryable) == (code, retryable)
        assert failure.details["fallback_allowed"] is retryable
        assert failure.details["http_status"] == status
        assert_private(failure)
    else:
        with pytest.raises(ProviderError) as raised:
            await asyncio.wait_for(task, .2)
        assert (raised.value.code, raised.value.retryable) == (code, retryable)
        details = raised.value.details
        assert details["transport_outcome"] == "timeout"
    assert details["http_status"] == status and details["transport_phase"] == "response_body"
    assert "vendor_code" not in details and PRIVATE not in json.dumps(details)
    assert body.closed


@pytest.mark.anyio
async def test_cleanup_is_bounded_and_cannot_mask_classified_rejection(monkeypatch):
    monkeypatch.setattr(tts_streaming, "_CLOSE_TIMEOUT", .01)
    released, cancelled = asyncio.Event(), asyncio.Event()

    class SlowClose(PacketStream):
        async def aclose(self):
            try:
                await released.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await released.wait()
            self.closed = True

    body = SlowClose(b"")
    started = monotonic()
    try:
        with pytest.raises(ProviderError) as raised:
            await open_stream(provider_for(body, headers={"content-type": "application/json"}))
        assert raised.value.code == "provider_streaming_not_supported"
        assert monotonic() - started < .2
        await asyncio.wait_for(cancelled.wait(), .2)
    finally:
        released.set()
        await asyncio.sleep(0)
    assert body.closed
