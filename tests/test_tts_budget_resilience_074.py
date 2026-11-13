"""Opening budgets and cancellation audit use synthetic transports only."""

import asyncio
import json
from time import monotonic

import pytest

from app.model_gateway import capabilities as cap
from app.model_gateway import gateway as gateway_module
from app.model_gateway import tts_streaming as stream_module
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import CircuitBreaker, ModelGateway
from app.model_gateway.schemas import TTSSynthesizeRequest
from app.providers.dashscope.provider import DashScopeProvider
from test_tts_streaming import FixtureStream, event, store_and_route


KEY = "org_default:config_test:%s" % cap.TTS_SYNTHESIZE
REQUEST = TTSSynthesizeRequest(text="Synthetic approved speech.")
DIAGNOSTICS = {
    "http_status": 429,
    "vendor_code": "Throttling.AllocationQuota",
    "provider_request_id": "00112233-4455-6677-8899-aabbccddeeff",
    "transport_phase": "response_headers",
    "transport_phase_ms": 23,
    "transport_elapsed_ms": 42,
    "transport_outcome": "cancelled",
    "raw_body": "must-never-be-stored",
    "credentials": "must-never-be-stored",
}


def setup(open_impl, *, retries=1, threshold=10, fallback=False, policy=None):
    store, route = store_and_route()
    route["primary"]["timeout_s"] = 12
    route["policy"] = {"retry_count": retries, "circuit_failure_threshold": threshold, **(policy or {})}
    if fallback:
        store.model_configurations["config_fallback"] = {
            **store.model_configurations["config_test"], "id": "config_fallback"}
        route["fallbacks"] = [{"model_configuration_id": "config_fallback", "timeout_s": 12}]
    calls = []

    class Adapter(DashScopeProvider):
        async def open_tts_stream(self, request, context):
            calls.append(context)
            return await open_impl(context)

    circuits = CircuitBreaker()
    gateway = ModelGateway(store, {"dashscope": Adapter()}, circuit_breaker=circuits)
    return gateway, store, route, circuits, calls


async def stalled():
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError as exc:
        exc.provider_diagnostics = dict(DIAGNOSTICS)
        raise


@pytest.mark.anyio
async def test_first_open_timeout_leaves_a_real_retry_inside_shared_budget():
    async def opening(context):
        if context.attempt == 1:
            await stalled()
        return FixtureStream()

    gateway, store, route, circuits, calls = setup(opening)
    # Keep the real asyncio timeout/cancellation path, with enough scheduling
    # headroom for a loaded full suite. The former 160 ms total left only
    # ~80 ms after the first timeout for cleanup, auditing and the retry.
    budget_s = 1.0
    started = monotonic()
    stream = await gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=started + budget_s)
    assert len(calls) == 2 and budget_s / 4 < calls[0].timeout_s <= budget_s / 2
    assert circuits._states[KEY].failures == 1
    _ = [item async for item in stream]
    assert monotonic() - started < budget_s
    assert KEY not in circuits._states
    assert [record["status"] for record in store.model_invocations] == ["failed", "stream_opened", "stream_success"]
    failed = store.model_invocations[0]
    assert failed["error_code"] == "provider_timeout"
    assert failed["transport_phase"] == "response_headers" and failed["transport_outcome"] == "timeout"
    assert "must-never-be-stored" not in json.dumps(store.model_invocations)


@pytest.mark.anyio
async def test_ready_fallback_receives_budget_after_primary_timeouts():
    async def opening(context):
        if context.fallback_index == 0:
            await stalled()
        return FixtureStream()

    gateway, store, route, circuits, calls = setup(opening, fallback=True)
    started = monotonic()
    stream = await gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=started + .20)
    _ = [item async for item in stream]
    assert [(c.fallback_index, c.attempt) for c in calls] == [(0, 1), (0, 2), (1, 1)]
    assert calls[0].timeout_s <= .05 and monotonic() - started < .20
    assert circuits._states[KEY].failures == 2
    assert store.model_invocations[-2]["status"] == "stream_fallback_opened"


@pytest.mark.anyio
@pytest.mark.parametrize("unavailable", ["unhealthy", "circuit", "forbidden", "auth_only", "rate_only"])
async def test_unavailable_fallback_does_not_reduce_primary_budget(unavailable):
    async def opening(context):
        return FixtureStream()

    gateway, store, route, circuits, calls = setup(opening, retries=0, fallback=True)
    if unavailable == "unhealthy":
        store.model_configurations["config_fallback"]["status"] = "failed"
    elif unavailable == "circuit":
        key = KEY.replace("config_test", "config_fallback")
        for _ in range(10):
            circuits.record_failure(key, threshold=10)
    elif unavailable == "forbidden":
        route["policy"]["fallback_on"] = []
    else:
        route["policy"]["fallback_on"] = ["provider_auth_failed" if unavailable == "auth_only" else "rate_limited"]
    stream = await gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=monotonic() + .20)
    assert .15 < calls[0].timeout_s <= .20
    await stream.abort()


@pytest.mark.anyio
async def test_all_open_timeouts_obey_total_deadline_and_record_each_failure():
    async def opening(context):
        await stalled()

    gateway, store, route, circuits, calls = setup(opening, retries=2)
    started = monotonic()
    with pytest.raises(ProviderError) as caught:
        await gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=started + .12)
    assert caught.value.code == "provider_timeout" and len(calls) == 3
    assert .10 <= monotonic() - started < .18
    assert circuits._states[KEY].failures == 3
    assert len(store.model_invocations) == 3
    assert all(row["error_code"] == "provider_timeout" for row in store.model_invocations)


@pytest.mark.anyio
@pytest.mark.parametrize("returns_late_stream", [False, True])
async def test_cancel_cleanup_cannot_extend_deadline_and_late_stream_is_aborted(returns_late_stream):
    release = asyncio.Event()
    finished = asyncio.Event()
    source = FixtureStream()

    async def opening(context):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            asyncio.current_task().provider_diagnostics = dict(DIAGNOSTICS)
            await release.wait()
            if returns_late_stream:
                return source
            raise
        finally:
            finished.set()

    gateway, store, route, circuits, calls = setup(opening, retries=0)
    started = monotonic()
    task = asyncio.create_task(gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=started + .04))
    try:
        done, _ = await asyncio.wait({task}, timeout=.12)
        assert done and not finished.is_set()
        with pytest.raises(ProviderError) as caught:
            await task
        assert caught.value.code == "provider_timeout" and len(calls) == 1
        assert circuits._states[KEY].failures == 1
        assert store.model_invocations[0]["transport_phase"] == "response_headers"
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), .2)
        if not task.done():
            try:
                await task
            except ProviderError:
                pass
        for _ in range(5):
            await asyncio.sleep(0)
    assert source.aborted == int(returns_late_stream)


@pytest.mark.anyio
async def test_invalid_ready_cleanup_does_not_outlive_invocation_budget_or_lose_failure():
    release, closed = asyncio.Event(), asyncio.Event()

    class Invalid(FixtureStream):
        async def abort(self):
            self.aborted += 1
            await release.wait()
            closed.set()

    source = Invalid(ready=event("stream.ready", 2))

    async def opening(context):
        return source

    gateway, store, route, circuits, calls = setup(opening)
    task = asyncio.create_task(gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=monotonic() + .04))
    try:
        done, _ = await asyncio.wait({task}, timeout=.12)
        assert done and not closed.is_set()
        with pytest.raises(ProviderError):
            await task
        assert len(calls) == 1 and circuits._states[KEY].failures == 1
        assert store.model_invocations[0]["error_code"] == "provider_schema_invalid"
    finally:
        release.set()
        await asyncio.wait_for(closed.wait(), .2)
    assert source.aborted == 1


@pytest.mark.anyio
async def test_retry_backoff_cannot_consume_the_remaining_open_budget():
    async def opening(context):
        if context.attempt == 1:
            raise ProviderError("provider_rate_limited", "Synthetic rate limit.", retryable=True)
        return FixtureStream()

    gateway, _, route, _, calls = setup(opening, policy={"retry_backoff_ms": 1000})
    started = monotonic()
    stream = await gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=started + .12)
    assert len(calls) == 2 and monotonic() - started < .12
    await stream.abort()


@pytest.mark.anyio
async def test_each_real_failure_opens_circuit_without_extra_provider_calls():
    async def opening(context):
        raise ProviderError("provider_network_error", "Synthetic connection failure.", retryable=True)

    gateway, store, route, circuits, calls = setup(opening, retries=3, threshold=2)
    for expected in ("provider_network_error", "provider_circuit_open"):
        with pytest.raises(ProviderError) as caught:
            await gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=monotonic() + .20)
        assert caught.value.code == expected
    assert len(calls) == 2 and circuits._states[KEY].failures == 2
    assert sum(row["error_code"] == "provider_network_error" for row in store.model_invocations) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("fallback", [False, True])
async def test_current_call_keeps_timeout_cause_when_it_opens_the_circuit(fallback):
    async def opening(context):
        if context.fallback_index == 0:
            await stalled()
        return FixtureStream()

    gateway, store, route, circuits, calls = setup(
        opening, retries=3, threshold=2, fallback=fallback, policy={"fallback_on": ["timeout"]})
    started = monotonic()
    if fallback:
        stream = await gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=started + .24)
        _ = [item async for item in stream]
        assert [(c.fallback_index, c.attempt) for c in calls] == [(0, 1), (0, 2), (1, 1)]
        assert store.model_invocations[-2]["status"] == "stream_fallback_opened"
    else:
        with pytest.raises(ProviderError) as caught:
            await gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=started + .24)
        assert caught.value.code == "provider_timeout" and caught.value.details["attempts"] == 2
        assert len(calls) == 2
    assert monotonic() - started < .24 and circuits._states[KEY].failures == 2
    assert [row["error_code"] for row in store.model_invocations if row["status"] == "failed"] == [
        "provider_timeout", "provider_timeout"]

    # A later independent request is fenced before any new provider attempt.
    count = len(calls)
    with pytest.raises(ProviderError) as caught:
        await gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=monotonic() + .24)
    assert caught.value.code == "provider_circuit_open" and len(calls) == count
    assert store.model_invocations[-1]["error_code"] == "provider_circuit_open"
    assert circuits._states[KEY].failures == 2


@pytest.mark.anyio
@pytest.mark.parametrize("code", ["provider_billing_error", "provider_authentication_failed", "provider_cost_limit_exceeded"])
async def test_nonretryable_rejection_never_retries_or_counts_as_transient_failure(code):
    async def opening(context):
        raise ProviderError(code, "Synthetic rejection.", retryable=False, details={"fallback_allowed": False})

    gateway, store, route, circuits, calls = setup(opening, retries=3, fallback=True)
    with pytest.raises(ProviderError) as caught:
        await gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=monotonic() + .20)
    assert caught.value.code == code and not caught.value.details["fallback_allowed"]
    assert len(calls) == len(store.model_invocations) == 1 and KEY not in circuits._states


@pytest.mark.anyio
@pytest.mark.parametrize("code,retryable,status", [
    ("provider_auth_failed", False, 403), ("provider_bad_request", False, 400),
    ("provider_billing_required", False, 402), ("provider_rate_limited", True, 429),
    ("provider_server_error", True, 503),
])
async def test_cancelled_error_body_keeps_adapter_normalized_rejection(code, retryable, status):
    async def opening(context):
        if context.attempt > 1:
            return FixtureStream()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            task = asyncio.current_task()
            task.provider_error = ProviderError(code, "must-never-be-stored", retryable=retryable,
                                                details={"http_status": status, "fallback_allowed": retryable})
            task.provider_diagnostics = {"http_status": status, "transport_phase": "response_body"}
            raise

    gateway, store, route, circuits, calls = setup(opening)
    if retryable:
        stream = await gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=monotonic() + .08)
        assert len(calls) == 2 and circuits._states[KEY].failures == 1
        await stream.abort()
    else:
        with pytest.raises(ProviderError) as caught:
            await gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=monotonic() + .08)
        assert caught.value.code == code and not caught.value.retryable
        assert "must-never-be-stored" not in str(caught.value)
        assert len(calls) == 1 and KEY not in circuits._states
    assert store.model_invocations[0]["error_code"] == code
    assert store.model_invocations[0]["http_status"] == status
    assert store.model_invocations[0]["transport_outcome"] == "timeout"
    assert "must-never-be-stored" not in json.dumps(store.model_invocations)


@pytest.mark.anyio
@pytest.mark.parametrize("code,retryable", [("provider_auth_failed", False), ("provider_billing_required", False), ("provider_network_error", True)])
async def test_completed_provider_error_wins_same_deadline_race(monkeypatch, code, retryable):
    async def opening(context):
        raise ProviderError(code, "Synthetic normalized failure.", retryable=retryable)

    original_wait = asyncio.wait
    forced = False

    async def deadline_wins(tasks, timeout=None, **kwargs):
        nonlocal forced
        if not forced:
            forced = True
            await original_wait(tasks, timeout=timeout, **kwargs)
            return set(), set(tasks)
        return await original_wait(tasks, timeout=timeout, **kwargs)

    monkeypatch.setattr(asyncio, "wait", deadline_wins)
    gateway, store, route, circuits, calls = setup(opening, retries=0)
    with pytest.raises(ProviderError) as caught:
        await gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=monotonic() + .20)
    assert caught.value.code == code and caught.value.retryable is retryable
    assert len(calls) == 1 and store.model_invocations[0]["error_code"] == code
    assert (KEY in circuits._states) is retryable


@pytest.mark.anyio
async def test_unclaimed_cleanup_stays_bounded_even_when_abort_defers_cancellation():
    release, closed = asyncio.Event(), asyncio.Event()

    class Source:
        async def abort(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            finally:
                closed.set()

    task = asyncio.create_task(gateway_module._abort_unclaimed_stream(Source(), .01))
    try:
        done, _ = await asyncio.wait({task}, timeout=.08)
        assert done and not closed.is_set()
        await task
    finally:
        release.set()
        await asyncio.wait_for(closed.wait(), .2)


@pytest.mark.anyio
@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("task_diagnostics", [False, True])
async def test_outer_cancellation_is_audited_and_propagated_with_known_deadline(monkeypatch, expired, task_diagnostics):
    entered = asyncio.Event()
    clock = [0.0]
    monkeypatch.setattr(gateway_module, "monotonic", lambda: clock[0])

    async def opening(context):
        entered.set()
        if not task_diagnostics:
            await stalled()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            asyncio.current_task().provider_diagnostics = dict(DIAGNOSTICS)
            raise

    gateway, store, route, circuits, calls = setup(opening, retries=0)
    task = asyncio.create_task(gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=12))
    await entered.wait()
    clock[0] = 12 if expired else 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == len(store.model_invocations) == 1
    record = store.model_invocations[0]
    assert record["status"] == ("failed" if expired else "stream_cancelled")
    assert record["error_code"] == ("provider_timeout" if expired else "provider_cancelled")
    assert record["transport_phase"] == "response_headers"
    assert record["transport_outcome"] == ("timeout" if expired else "cancelled")
    assert (KEY in circuits._states) is expired


@pytest.mark.anyio
async def test_broken_cancellation_audit_never_replaces_parent_cancellation(monkeypatch):
    entered = asyncio.Event()

    async def opening(context):
        entered.set()
        await stalled()

    gateway, _, route, circuits, _ = setup(opening)

    def broken(**kwargs):
        raise RuntimeError("Synthetic storage failure.")

    monkeypatch.setattr(gateway, "_log_invocation", broken)
    task = asyncio.create_task(gateway.open_tts_stream(REQUEST, route=route))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert KEY not in circuits._states


@pytest.mark.anyio
@pytest.mark.parametrize("first_chunk", [False, True])
async def test_returned_stream_never_reopens_even_before_first_pcm(first_chunk):
    async def opening(context):
        items = [event("audio.chunk", 2, pcm_s16le=b"xx")] if first_chunk else []
        items.append(ProviderError("provider_network_error", "Synthetic dropped stream.", retryable=True))
        return FixtureStream(items)

    gateway, store, route, circuits, calls = setup(opening, fallback=True)
    stream = await gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=monotonic() + 1)
    with pytest.raises(ProviderError) as caught:
        _ = [item async for item in stream]
    assert caught.value.details["audio_started"] is first_chunk
    assert len(calls) == 1 and circuits._states[KEY].failures == 1
    assert [row["status"] for row in store.model_invocations] == ["stream_opened", "stream_failed"]


@pytest.mark.anyio
@pytest.mark.parametrize("expired", [False, True])
async def test_cancelled_first_pcm_read_preserves_diagnostics_and_deadline(monkeypatch, expired):
    entered = asyncio.Event()
    clock = [0.0]
    monkeypatch.setattr(gateway_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(stream_module, "monotonic", lambda: clock[0])

    class Source(FixtureStream):
        async def events(self):
            entered.set()
            await stalled()
            yield event("audio.chunk", 2, pcm_s16le=b"xx")

    source = Source()

    async def opening(context):
        return source

    gateway, store, route, circuits, calls = setup(opening)
    stream = await gateway.open_tts_stream(REQUEST, route=route, first_audio_deadline=12)
    task = asyncio.create_task(stream.events().__anext__())
    await entered.wait()
    clock[0] = 12 if expired else 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1 and source.aborted == 1
    row = store.model_invocations[-1]
    assert row["status"] == ("stream_failed" if expired else "stream_cancelled")
    assert row["error_code"] == ("provider_timeout" if expired else "provider_cancelled")
    assert row["transport_phase"] == "response_headers"
    assert (KEY in circuits._states) is expired


@pytest.mark.anyio
async def test_vendor_rejection_and_diagnostic_allowlists_are_preserved_after_stream_return():
    details = {**DIAGNOSTICS, "fallback_allowed": False, "http_status": 200,
               "vendor_code": "Arrearage", "transport_outcome": "stream_error"}

    async def opening(context):
        return FixtureStream([ProviderError("provider_billing_error", "Synthetic account rejection.", details=details)])

    gateway, store, route, circuits, _ = setup(opening)
    stream = await gateway.open_tts_stream(REQUEST, route=route)
    with pytest.raises(ProviderError) as caught:
        _ = [item async for item in stream]
    assert not caught.value.details["fallback_allowed"] and KEY not in circuits._states
    record = store.model_invocations[-1]
    assert record["http_status"] == 200 and record["vendor_code"] == "Arrearage"
    assert record["provider_request_id"] == DIAGNOSTICS["provider_request_id"]
    assert "must-never-be-stored" not in json.dumps(store.model_invocations)


@pytest.mark.anyio
@pytest.mark.parametrize("value", ["must-never-be-stored", {"message": "must-never-be-stored"}, ["must-never-be-stored"], True])
async def test_unknown_diagnostic_values_cannot_smuggle_sensitive_text_into_audit(value):
    details = dict.fromkeys(DIAGNOSTICS, value)

    async def opening(context):
        raise ProviderError("provider_bad_request", "Synthetic rejection.", details=details)

    gateway, store, route, _, _ = setup(opening)
    with pytest.raises(ProviderError):
        await gateway.open_tts_stream(REQUEST, route=route)
    assert "must-never-be-stored" not in json.dumps(store.model_invocations)
    assert all(store.model_invocations[0][field] is None for field in DIAGNOSTICS
               if field not in {"raw_body", "credentials"})
