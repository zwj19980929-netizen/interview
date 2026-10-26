"""HTTP reuse is explicitly scoped; all traffic uses synthetic transports."""

import asyncio
import json
from contextlib import asynccontextmanager
from time import monotonic

import httpx
import pytest

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import TTSSynthesizeRequest
from app.providers.dashscope.provider import DashScopeProvider
from app.providers.dashscope import tts_http_pool as pools
from test_tts_streaming import PacketStream, context, payload, sse


ENDPOINT = "https://synthetic.invalid/tts"


class FakeClient:
    def __init__(self):
        self.closed = False
        self.closes = 0

    async def aclose(self):
        self.closes += 1
        self.closed = True


def direct_pool(**options):
    clients = []

    def create(**kwargs):
        client = FakeClient()
        clients.append(client)
        return client

    return pools.TTSHTTPClientPool(client_factory=create, **options), clients


@asynccontextmanager
async def http_pool(monkeypatch, handler):
    clients, configurations = [], []
    original = pools._new_client

    def create(**kwargs):
        configurations.append(kwargs)
        client = original(**kwargs)
        # Keep the actual shared client behavior but replace network I/O.
        client._transport = httpx.MockTransport(handler)
        clients.append(client)
        return client

    monkeypatch.setattr(pools, "_new_client", create)
    pools.start_tts_http_pools()
    try:
        yield clients, configurations
    finally:
        await pools.close_tts_http_pools()


@pytest.mark.anyio
async def test_same_configuration_reuses_client_auth_and_timeout_stay_per_request(monkeypatch):
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream", "set-cookie": "old_auth=private; Path=/"},
                              stream=PacketStream(sse(payload(), payload(final=True))))

    async with http_pool(monkeypatch, handler) as (clients, configurations):
        provider = DashScopeProvider()
        for key, timeout in [("synthetic-first", .8), ("synthetic-rotated", .3)]:
            stream = await provider.open_tts_stream(TTSSynthesizeRequest(text="合成连接测试。"),
                context(credentials={"api_key": key}, timeout_s=timeout))
            assert [event.type async for event in stream.events()] == ["audio.chunk", "audio.final"]
        assert len(clients) == 1 and not clients[0].is_closed
        assert "authorization" not in clients[0].headers and not list(clients[0].cookies)
        assert [item.headers["authorization"] for item in seen] == ["Bearer synthetic-first", "Bearer synthetic-rotated"]
        assert "cookie" not in seen[1].headers
        assert [item.extensions["timeout"]["read"] for item in seen] == [.8, .3]
        assert json.loads(seen[0].content) == json.loads(seen[1].content)
        assert configurations == [{"trust_env": False, "max_connections": 8, "keepalive_expiry": 30.0}]
    assert clients[0].is_closed


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_read", [False, True])
async def test_aborting_one_response_keeps_concurrent_stream_and_shared_client_alive(monkeypatch, cancel_read):
    entered = asyncio.Event()
    bodies = []

    class HeldBody(PacketStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

    async def handler(request):
        body = HeldBody(b"") if not bodies else PacketStream(sse(payload(), payload(final=True)))
        bodies.append(body)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    async with http_pool(monkeypatch, handler) as (clients, _):
        provider = DashScopeProvider()
        first, second = await asyncio.gather(*[provider.open_tts_stream(TTSSynthesizeRequest(text="合成测试。"), context()) for _ in range(2)])
        if cancel_read:
            task = asyncio.create_task(first.events().__anext__())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await first.abort()
        assert bodies[0].closed and not bodies[1].closed
        assert len(clients) == 1 and not clients[0].is_closed
        assert [event.type async for event in second.events()] == ["audio.chunk", "audio.final"]
        assert bodies[1].closed and not clients[0].is_closed


@pytest.mark.anyio
@pytest.mark.parametrize("enabled", [False, True])
async def test_unscoped_default_and_explicit_factory_keep_per_stream_lifetime(monkeypatch, enabled):
    clients = []

    async def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=PacketStream(sse(payload(), payload(final=True))))

    def create(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    if enabled:
        pools.start_tts_http_pools()
    else:
        # Treat the synthetic factory as the default to exercise no-lifespan
        # behavior without network access or a global httpx monkeypatch.
        monkeypatch.setattr(pools, "_DEFAULT_CLIENT_FACTORY", create)
    try:
        provider = DashScopeProvider(client_factory=create)
        for _ in range(2):
            stream = await provider.open_tts_stream(TTSSynthesizeRequest(text="合成测试。"), context())
            _ = [event async for event in stream.events()]
        assert len(clients) == 2 and all(client.is_closed for client in clients)
        if enabled:
            assert not pools._pools[asyncio.get_running_loop()]._entries
    finally:
        if enabled:
            await pools.close_tts_http_pools()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["organization", "connection", "model", "endpoint", "proxy", "settings", "provider_model", "defaults"])
async def test_configuration_scopes_never_share_clients(change):
    pool, clients = direct_pool()
    original = context()
    values, endpoint = {}, ENDPOINT
    if change == "organization": values["organization_id"] = "other_org"
    elif change == "connection": values["provider_connection_id"] = "other_connection"
    elif change == "model": values["model_configuration_id"] = "other_model"
    elif change == "endpoint": endpoint += "/other"
    elif change == "proxy": values["model_settings"] = {"use_environment_proxy": True}
    elif change == "provider_model": values["model"] = "qwen3-tts-other"
    elif change == "defaults": values["default_parameters"] = {"voice_profile_id": "other_voice"}
    else: values["model_settings"] = {"default_voice": "other_voice"}
    try:
        async with pool.lease(original, ENDPOINT) as first:
            async with pool.lease(context(**values), endpoint) as second:
                assert first is not second
        assert len(clients) == 2 and not any(client.closed for client in clients)
    finally:
        await pool.aclose()
    assert all(client.closed for client in clients)


@pytest.mark.anyio
async def test_stream_capacity_wait_is_bounded_and_cancel_does_not_steal_permit():
    pool, clients = direct_pool(max_streams=1)
    async def borrower(ctx):
        async with pool.lease(ctx, ENDPOINT):
            raise AssertionError("The occupied slot must not be acquired")
    try:
        async with pool.lease(context(), ENDPOINT):
            with pytest.raises(httpx.PoolTimeout):
                await borrower(context(timeout_s=.01))
            task = asyncio.create_task(borrower(context()))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            entry = next(iter(pool._entries.values()))
            assert entry.users == 1 and entry.slots.locked()
        async with pool.lease(context(), ENDPOINT):
            assert len(clients) == 1
    finally:
        await pool.aclose()


@pytest.mark.anyio
async def test_cancellation_on_same_turn_as_slot_acquisition_propagates_and_returns_slot(monkeypatch):
    pool, clients = direct_pool(max_streams=1)
    try:
        async with pool.lease(context(), ENDPOINT):
            pass
        entry = next(iter(pool._entries.values()))
        acquire = entry.slots.acquire
        async def simultaneous():
            result = await acquire()
            asyncio.get_running_loop().call_soon(task.cancel)
            return result
        async def borrower():
            async with pool.lease(context(), ENDPOINT):
                raise AssertionError("A cancelled borrower cannot use the client")
        with monkeypatch.context() as scoped:
            scoped.setattr(entry.slots, "acquire", simultaneous)
            task = asyncio.create_task(borrower())
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.sleep(0)
        assert entry.users == 0 and not entry.slots.locked()
        async with pool.lease(context(), ENDPOINT):
            assert len(clients) == 1
    finally:
        await pool.aclose()


@pytest.mark.anyio
async def test_idle_timer_never_closes_active_stream_and_expiry_releases_client():
    pool, clients = direct_pool(idle_seconds=.015)
    try:
        async with pool.lease(context(), ENDPOINT):
            await asyncio.sleep(.025)
            assert not clients[0].closed
        await asyncio.sleep(.025)
        assert clients[0].closed and not pool._entries
        async with pool.lease(context(), ENDPOINT):
            assert len(clients) == 2
    finally:
        await pool.aclose()


@pytest.mark.anyio
async def test_scope_capacity_never_evicts_active_client_and_retires_idle_client():
    pool, clients = direct_pool(max_scopes=1)
    other = context(organization_id="other_org")
    try:
        async with pool.lease(context(), ENDPOINT):
            with pytest.raises(httpx.PoolTimeout):
                async with pool.lease(other, ENDPOINT):
                    pass
            assert len(clients) == 1 and not clients[0].closed
        with pytest.raises(httpx.PoolTimeout):
            async with pool.lease(other, ENDPOINT):
                pass
        await asyncio.gather(*(entry.closing for entry in pool._entries.values()))
        assert clients[0].closed
        async with pool.lease(other, ENDPOINT):
            assert len(clients) == 2
    finally:
        await pool.aclose()


@pytest.mark.anyio
async def test_shutdown_closes_active_clients_rejects_late_leases_and_can_start_new_lifespan(monkeypatch):
    clients = []
    def create(**kwargs):
        client = FakeClient()
        clients.append(client)
        return client
    monkeypatch.setattr(pools, "_new_client", create)
    pools.start_tts_http_pools()
    pool = pools._pools[asyncio.get_running_loop()]
    async with pool.lease(context(), ENDPOINT):
        await pools.close_tts_http_pools()
        assert clients[0].closed
        with pytest.raises(ProviderError, match="shut down"):
            async with pools.tts_http_client(pools._DEFAULT_CLIENT_FACTORY, context(), ENDPOINT):
                pass
    pools.start_tts_http_pools()
    assert pools._pools[asyncio.get_running_loop()] is not pool
    await pools.close_tts_http_pools()


@pytest.mark.anyio
async def test_uncooperative_client_close_cannot_block_shutdown():
    released, cancelled, ended = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Slow(FakeClient):
        async def aclose(self):
            try:
                await released.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await released.wait()
            finally:
                ended.set()
    pool = pools.TTSHTTPClientPool(client_factory=lambda **kwargs: Slow(), close_seconds=.01)
    async with pool.lease(context(), ENDPOINT):
        pass
    started = monotonic()
    try:
        await pool.aclose()
        assert monotonic() - started < .2
        await asyncio.wait_for(cancelled.wait(), .2)
        assert not pool._entries
    finally:
        released.set()
        await asyncio.wait_for(ended.wait(), .2)


def test_pool_is_not_reused_across_event_loops_or_processes(monkeypatch):
    previous = []
    async def scenario():
        pools.start_tts_http_pools()
        current = pools._pools[asyncio.get_running_loop()]
        assert current not in previous
        for old in previous:
            with pytest.raises(ProviderError, match="execution scope"):
                async with old.lease(context(), ENDPOINT):
                    pass
        pid = pools.os.getpid()
        with monkeypatch.context() as scoped:
            scoped.setattr(pools.os, "getpid", lambda: pid + 1)
            with pytest.raises(ProviderError, match="execution scope"):
                async with current.lease(context(), ENDPOINT):
                    pass
        previous.append(current)
        await pools.close_tts_http_pools()
    asyncio.run(scenario())
    asyncio.run(scenario())
