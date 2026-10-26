"""Loop-local HTTP connection reuse; response leases never own the client.

Only the default HTTP transport is shared. Explicit client factories retain
their existing per-stream lifetime, including test transports and custom TLS.
Authentication and timeouts remain request-scoped, never client defaults.
"""

import asyncio
import hashlib
import json
import logging
import os
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from time import monotonic

import httpx

from app.model_gateway.errors import ProviderError


_DEFAULT_CLIENT_FACTORY = httpx.AsyncClient
_LOG = logging.getLogger(__name__)
_MAX_SCOPES = 32
_MAX_STREAMS = 8
_IDLE_SECONDS = 30.0
_CLOSE_SECONDS = 1.0
_process_id = os.getpid()
_pools = weakref.WeakKeyDictionary()


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False


def _new_client(*, trust_env, max_connections, keepalive_expiry):
    return _DEFAULT_CLIENT_FACTORY(
        trust_env=trust_env, follow_redirects=False, timeout=None,
        cookies=CookieJar(policy=_NoCookies()),
        limits=httpx.Limits(max_connections=max_connections,
                            max_keepalive_connections=max_connections,
                            keepalive_expiry=keepalive_expiry),
    )


def _scope(context, endpoint):
    # Fingerprints isolate changed transport/model settings without retaining
    # their contents. Credentials are deliberately absent: auth is per request.
    encoded = json.dumps({"endpoint": endpoint, "model": context.model, "config": context.config,
                          "default_parameters": context.default_parameters},
                         sort_keys=True, separators=(",", ":")).encode()
    return (context.organization_id, context.provider_connection_id,
            context.model_configuration_id, hashlib.sha256(encoded).hexdigest())


def _consume(task):
    if not task.cancelled():
        task.exception()


async def _acquire_slot(slots, timeout):
    # Keep ownership explicit when cancellation and a free slot arrive on the
    # same loop turn. wait_for on older Python can swallow that cancellation.
    waiter = asyncio.create_task(slots.acquire())
    acquired = False
    try:
        done, _ = await asyncio.wait({waiter}, timeout=timeout)
        if not done:
            raise httpx.PoolTimeout("TTS HTTP stream capacity wait timed out.")
        waiter.result()
        acquired = True
    finally:
        if not acquired:
            def release_late_slot(task):
                if not task.cancelled() and task.exception() is None and task.result():
                    slots.release()
            waiter.cancel()
            waiter.add_done_callback(release_late_slot)


@dataclass
class _Entry:
    client: object
    slots: asyncio.Semaphore
    users: int = 0
    last_used: float = 0.0
    idle_timer: object = None
    closing: object = None


class TTSHTTPClientPool:
    """Bounded configuration scopes owned by exactly one process/event loop."""

    def __init__(self, *, client_factory=None, max_scopes=_MAX_SCOPES,
                 max_streams=_MAX_STREAMS, idle_seconds=_IDLE_SECONDS,
                 close_seconds=_CLOSE_SECONDS):
        self._loop = weakref.ref(asyncio.get_running_loop())
        self._pid = os.getpid()
        self._factory = client_factory or _new_client
        self._max_scopes = max_scopes
        self._max_streams = max_streams
        self._idle_seconds = idle_seconds
        self._close_seconds = close_seconds
        self._entries = {}
        self._closed = False

    def _check_owner(self):
        if self._pid != os.getpid() or self._loop() is not asyncio.get_running_loop():
            raise ProviderError("provider_stream_closed", "TTS HTTP pool belongs to another execution scope.", retryable=False)
        if self._closed:
            raise ProviderError("provider_stream_closed", "TTS HTTP pool has shut down.", retryable=False)

    def _retire(self, key, entry):
        if entry.closing is not None:
            return entry.closing
        if entry.idle_timer is not None:
            entry.idle_timer.cancel()
            entry.idle_timer = None

        async def close():
            task = asyncio.create_task(entry.client.aclose())
            try:
                done, _ = await asyncio.wait({task}, timeout=self._close_seconds)
                if done:
                    task.result()
                else:
                    _LOG.warning("TTS HTTP idle client cleanup exceeded its time limit.")
            except Exception as exc:
                _LOG.warning("TTS HTTP client cleanup failed: error_type=%s", type(exc).__name__)
            finally:
                if not task.done():
                    task.cancel()
                    task.add_done_callback(_consume)
                if self._entries.get(key) is entry:
                    self._entries.pop(key)

        entry.closing = asyncio.create_task(close())
        entry.closing.add_done_callback(_consume)
        return entry.closing

    def _idle(self, key, entry):
        if not entry.users and self._entries.get(key) is entry:
            self._retire(key, entry)

    @asynccontextmanager
    async def lease(self, context, endpoint):
        self._check_owner()
        key = _scope(context, endpoint)
        entry = self._entries.get(key)
        if entry is not None and entry.closing is not None:
            raise httpx.PoolTimeout("TTS HTTP configuration is being released.")
        if entry is None:
            if len(self._entries) >= self._max_scopes:
                idle = [(k, e) for k, e in self._entries.items() if not e.users and e.closing is None]
                if idle:
                    old_key, old_entry = min(idle, key=lambda pair: pair[1].last_used)
                    # Retire outside this request's critical path. Capacity is
                    # retained until close completes; a retry may then acquire.
                    self._retire(old_key, old_entry)
                raise httpx.PoolTimeout("TTS HTTP configuration capacity is occupied.")
            entry = _Entry(self._factory(
                trust_env=bool(context.config.get("use_environment_proxy", False)),
                max_connections=self._max_streams, keepalive_expiry=self._idle_seconds,
            ), asyncio.Semaphore(self._max_streams))
            self._entries[key] = entry
        if entry.idle_timer is not None:
            entry.idle_timer.cancel()
            entry.idle_timer = None
        entry.users += 1
        acquired = False
        try:
            await _acquire_slot(entry.slots, context.timeout_s)
            acquired = True
            self._check_owner()
            yield entry.client
        finally:
            if acquired:
                entry.slots.release()
            entry.users -= 1
            entry.last_used = monotonic()
            if not entry.users and not self._closed and entry.closing is None:
                entry.idle_timer = asyncio.get_running_loop().call_later(
                    self._idle_seconds, self._idle, key, entry)

    async def aclose(self):
        # Shutdown never borrows another loop's sockets.
        if self._pid != os.getpid() or self._loop() is not asyncio.get_running_loop():
            return
        self._closed = True
        tasks = {self._retire(key, entry) for key, entry in list(self._entries.items())}
        if tasks:
            # Each owned retire task bounds its transport close independently.
            await asyncio.gather(*tasks)


def _loop_pool(*, restart=False):
    global _pools, _process_id
    if _process_id != os.getpid():
        # Never operate a parent's pooled socket/asyncio primitive after fork.
        _pools, _process_id = weakref.WeakKeyDictionary(), os.getpid()
    loop = asyncio.get_running_loop()
    pool = _pools.get(loop)
    if pool is None or (restart and pool._closed):
        pool = TTSHTTPClientPool()
        _pools[loop] = pool
    return pool


def start_tts_http_pools():
    """API startup hook; permits a new lifespan after a completed shutdown."""
    _loop_pool(restart=True)


async def close_tts_http_pools():
    """API shutdown hook; active responses close and new leases are rejected."""
    pool = _pools.get(asyncio.get_running_loop())
    if pool is not None:
        await pool.aclose()


@asynccontextmanager
async def tts_http_client(client_factory, context, endpoint):
    pool = _pools.get(asyncio.get_running_loop()) if _process_id == os.getpid() else None
    # Worker asyncio.run scopes and custom transports are intentionally
    # unpooled unless a lifespan has explicitly enabled this loop.
    if client_factory is not _DEFAULT_CLIENT_FACTORY or pool is None:
        async with client_factory(timeout=context.timeout_s,
                                  trust_env=bool(context.config.get("use_environment_proxy", False)),
                                  follow_redirects=False) as client:
            yield client
    else:
        async with pool.lease(context, endpoint) as client:
            yield client
