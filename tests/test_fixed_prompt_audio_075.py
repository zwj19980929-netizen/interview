"""Complete fixed PCM is an optimization, never playback/turn authority."""

import asyncio
from dataclasses import replace
from time import monotonic

import pytest

from app.core.interaction_budget import interaction_deadline, remaining_interaction_seconds
from app.core.prompt.candidate_exit import EXIT_CONFIRMATION_SPEECH, EXIT_CONFIRMATION_SPEECH_VERSION
from app.core.prompt.contracts import SUPPLEMENT_SPEECH, SUPPLEMENT_SPEECH_VERSION
from app.core.prompt.fixed_speech import (
    FIXED_SPEECH_CATALOG, WARMUP_CONFIRMATION_SPEECH, WARMUP_CONFIRMATION_SPEECH_VERSION,
    fixed_speech_for_text,
)
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ProviderMeta, TTSSynthesizeRequest
from app.model_gateway.tts_streaming import TTSStreamEvent
from app.model_gateway.tts_streaming import ValidatedTTSStream
from app.services.fixed_prompt_audio import FixedPromptAudioCache


def identity(**overrides):
    return {"organization_id": "org_default", "provider_id": "synthetic_tts", "model": "synthetic_voice",
            "fingerprint": "routing-revision-1", **overrides}


def request(**overrides):
    return TTSSynthesizeRequest(purpose="interview_agent_expression", text=WARMUP_CONFIRMATION_SPEECH, **overrides)


ROUTE = {"organization_id": "org_default", "capability": "tts.synthesize", "enabled": True,
         "primary": {"model_configuration_id": "synthetic_config"}, "fallbacks": []}


class Source:
    def __init__(self, *, pcm=b"\0\0" * 10, mode="valid", gate=None, ready_provider=None):
        self.pcm = pcm
        self.mode = mode
        self.gate = gate
        self.closed = False
        self.stream_id = "synthetic_stream"
        self.provider = ready_provider or ProviderMeta(provider_id="synthetic_tts", model="synthetic_voice",
                                                       request_id="synthetic", latency_ms=0)
        self.ready_event = self.event(1, "stream.ready")

    def event(self, sequence, kind, **fields):
        return TTSStreamEvent(stream_id=self.stream_id, sequence=sequence, type=kind,
                              sample_rate_hz=24000, provider=self.provider, **fields)

    async def events(self):
        if self.gate:
            await self.gate.wait()
        yield self.event(2, "audio.chunk", pcm_s16le=self.pcm)
        if self.mode == "truncated":
            return
        yield self.event(3, "audio.final", total_audio_bytes=len(self.pcm) + (2 if self.mode == "bad_final" else 0))
        if self.mode == "trailing":
            yield self.event(4, "audio.chunk", pcm_s16le=b"\0\0")

    async def abort(self):
        self.closed = True


class Gateway:
    def __init__(self, source=None):
        self.source_factory = source or Source
        self.current_identity = identity()
        self.calls = []
        self.sources = []
        self.deadlines = []

    def tts_routing_snapshot(self, request):
        return {"identity": dict(self.current_identity), "route": ROUTE}

    async def open_tts_stream(self, request, **kwargs):
        self.calls.append(request)
        self.deadlines.append(remaining_interaction_seconds())
        source = self.source_factory()
        self.sources.append(source)
        return source


def test_catalog_is_versioned_exact_and_contains_only_existing_fixed_contracts():
    assert len(FIXED_SPEECH_CATALOG) == 5
    assert len({item.id for item in FIXED_SPEECH_CATALOG}) == 5
    assert len({item.text for item in FIXED_SPEECH_CATALOG}) == 5
    assert fixed_speech_for_text(WARMUP_CONFIRMATION_SPEECH).version == WARMUP_CONFIRMATION_SPEECH_VERSION
    assert fixed_speech_for_text(EXIT_CONFIRMATION_SPEECH).version == EXIT_CONFIRMATION_SPEECH_VERSION
    for text in SUPPLEMENT_SPEECH.values():
        assert fixed_speech_for_text(text).version == SUPPLEMENT_SPEECH_VERSION
    assert fixed_speech_for_text(" " + WARMUP_CONFIRMATION_SPEECH) is None
    assert fixed_speech_for_text("候选人姓名和动态追问") is None


@pytest.mark.anyio
async def test_complete_pcm_reuses_synthesis_but_creates_independent_streams_and_no_playback_authority():
    gateway = Gateway()
    cache = FixedPromptAudioCache(gateway)
    req = request(metadata={"interview_id": "private_interview", "candidate": "private_candidate"})
    assert await cache.start_prewarm(req, route=ROUTE, routing_identity=identity()) is True
    first = cache.get_stream(req, routing_identity=identity())
    second = cache.get_stream(req, routing_identity=identity())
    assert first.stream_id != second.stream_id
    one = [event async for event in first.events()]
    two = [event async for event in second.events()]
    assert b"".join(e.pcm_s16le for e in one) == b"".join(e.pcm_s16le for e in two) == b"\0\0" * 10
    assert one[-1].type == "audio.final" and one[-1].usage.total_tokens == 0
    assert len(gateway.calls) == 1 and gateway.sources[0].closed
    assert gateway.calls[0].metadata == {"approved": True, "fixed_prompt_id": "warmup_confirmation",
                                          "fixed_prompt_version": WARMUP_CONFIRMATION_SPEECH_VERSION}
    assert "private_" not in repr(cache._entries)
    assert not hasattr(cache, "performance_id")
    await cache.aclose()


@pytest.mark.anyio
async def test_singleflight_and_cancelling_one_waiter_does_not_cancel_shared_warmup():
    gate = asyncio.Event()
    gateway = Gateway(lambda: Source(gate=gate))
    cache = FixedPromptAudioCache(gateway)
    first = cache.start_prewarm(request(), route=ROUTE, routing_identity=identity())
    second = cache.start_prewarm(request(), route=ROUTE, routing_identity=identity())
    first.cancel()
    gate.set()
    assert await second is True
    assert len(gateway.calls) == 1
    await cache.aclose()


@pytest.mark.anyio
async def test_nonwhitelisted_text_and_wrong_purpose_never_start_provider_work():
    gateway = Gateway()
    cache = FixedPromptAudioCache(gateway)
    for req in (request().model_copy(update={"text": "根据张某简历提出的追问"}),
                request().model_copy(update={"purpose": "candidate_answer"})):
        assert cache.start_prewarm(req, route=ROUTE, routing_identity=identity()) is None
        assert cache.get_stream(req, routing_identity=identity()) is None
    assert not gateway.calls
    await cache.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("change", [
    {"voice_profile_id": "another_voice"}, {"language": "en-US"}, {"speaking_rate": 1.2}, {"format": "audio/pcm"},
])
async def test_audio_parameters_do_not_share_cache_entries(change):
    cache = FixedPromptAudioCache(Gateway())
    assert await cache.start_prewarm(request(), route=ROUTE, routing_identity=identity())
    assert cache.get_stream(request(**change), routing_identity=identity()) is None
    await cache.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("change", [{"fingerprint": "revision-2"}, {"provider_id": "other"}, {"model": "other"},
                                        {"organization_id": "org_other"}])
async def test_route_model_provider_and_organization_do_not_share_cache_entries(change):
    cache = FixedPromptAudioCache(Gateway())
    assert await cache.start_prewarm(request(), route=ROUTE, routing_identity=identity())
    req = request(organization_id=change.get("organization_id", "org_default"))
    assert cache.get_stream(req, routing_identity=identity(**change)) is None
    await cache.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["truncated", "bad_final", "trailing"])
async def test_incomplete_or_invalid_pcm_is_not_cached(mode):
    gateway = Gateway(lambda: Source(mode=mode))
    cache = FixedPromptAudioCache(gateway)
    assert await cache.start_prewarm(request(), route=ROUTE, routing_identity=identity()) is False
    assert cache.get_stream(request(), routing_identity=identity()) is None
    assert gateway.sources[0].closed and cache.stats()["failed"] == 1
    await cache.aclose()


@pytest.mark.anyio
async def test_oversized_pcm_is_rejected_and_failed_warmup_can_retry():
    gateway = Gateway(lambda: Source(pcm=b"\0\0" * 11))
    cache = FixedPromptAudioCache(gateway, max_item_bytes=20)
    assert await cache.start_prewarm(request(), route=ROUTE, routing_identity=identity()) is False
    gateway.source_factory = Source
    assert await cache.start_prewarm(request(), route=ROUTE, routing_identity=identity()) is True
    assert cache.stats()["bytes"] == 20
    await cache.aclose()


@pytest.mark.anyio
async def test_capacity_ttl_and_corruption_return_miss_then_allow_rebuild():
    now = [0.]
    gateway = Gateway()
    cache = FixedPromptAudioCache(gateway, max_entries=1, max_bytes=20, ttl_s=1, clock=lambda: now[0])
    req = request()
    assert await cache.start_prewarm(req, route=ROUTE, routing_identity=identity())
    second = req.model_copy(update={"text": SUPPLEMENT_SPEECH["check"]})
    assert await cache.start_prewarm(second, route=ROUTE, routing_identity=identity())
    assert cache.get_stream(req, routing_identity=identity()) is None
    assert cache.stats()["entries"] == 1 and cache.stats()["bytes"] == 20
    key = next(iter(cache._entries))
    cache._entries[key] = replace(cache._entries[key], pcm=b"\x01\x01" * 10)
    assert cache.get_stream(second, routing_identity=identity()) is None
    assert cache.stats()["corrupt"] == 1 and cache.stats()["bytes"] == 0
    assert await cache.start_prewarm(req, route=ROUTE, routing_identity=identity())
    now[0] = 1.
    assert cache.get_stream(req, routing_identity=identity()) is None
    await cache.aclose()


@pytest.mark.anyio
async def test_timeout_is_bounded_and_a_late_result_cannot_enter_cache():
    gate = asyncio.Event()
    gateway = Gateway(lambda: Source(gate=gate))
    cache = FixedPromptAudioCache(gateway, prewarm_timeout_s=.02)
    started = monotonic()
    assert await cache.start_prewarm(request(), route=ROUTE, routing_identity=identity()) is False
    assert monotonic() - started < .2
    gate.set()
    await asyncio.sleep(.01)
    assert cache.get_stream(request(), routing_identity=identity()) is None
    assert cache.stats()["timeout"] == 1
    await cache.aclose()


@pytest.mark.anyio
async def test_inflight_limit_is_reserved_before_background_task_starts():
    gate = asyncio.Event()
    gateway = Gateway(lambda: Source(gate=gate))
    cache = FixedPromptAudioCache(gateway, max_inflight=1)
    first = cache.start_prewarm(request(), route=ROUTE, routing_identity=identity())
    second_req = request().model_copy(update={"text": SUPPLEMENT_SPEECH["check"]})
    assert cache.start_prewarm(second_req, route=ROUTE, routing_identity=identity()) is None
    gate.set()
    await first
    assert cache.stats()["busy"] == 1
    await cache.aclose()


@pytest.mark.anyio
async def test_background_prewarm_does_not_inherit_expired_interaction_deadline():
    gateway = Gateway()
    cache = FixedPromptAudioCache(gateway)
    with interaction_deadline(monotonic() - 1):
        assert await cache.start_prewarm(request(), route=ROUTE, routing_identity=identity())
    assert gateway.deadlines == [None]
    await cache.aclose()


@pytest.mark.anyio
async def test_shutdown_before_prewarm_task_runs_releases_reservation_and_never_stores():
    cache = FixedPromptAudioCache(Gateway())
    waiter = cache.start_prewarm(request(), route=ROUTE, routing_identity=identity())
    await cache.aclose()
    assert waiter.cancelled()
    assert cache.stats()["inflight"] == 0 and cache.stats()["entries"] == 0
    assert cache.start_prewarm(request(), route=ROUTE, routing_identity=identity()) is None


@pytest.mark.anyio
async def test_route_change_during_synthesis_is_discarded():
    gate = asyncio.Event()
    gateway = Gateway(lambda: Source(gate=gate))
    cache = FixedPromptAudioCache(gateway)
    waiter = cache.start_prewarm(request(), route=ROUTE, routing_identity=identity())
    for _ in range(10):
        if gateway.calls:
            break
        await asyncio.sleep(0)
    gateway.current_identity = identity(fingerprint="newer-voice-configuration")
    gate.set()
    assert await waiter is False
    assert cache.get_stream(request(), routing_identity=identity()) is None
    await cache.aclose()


@pytest.mark.anyio
async def test_fallback_route_and_wrong_actual_provider_never_warm_cache():
    gateway = Gateway(lambda: Source(ready_provider=ProviderMeta(provider_id="fallback", model="synthetic_voice",
                                                                request_id="synthetic", latency_ms=0)))
    cache = FixedPromptAudioCache(gateway)
    assert cache.start_prewarm(request(), route={**ROUTE, "fallbacks": [{"model_configuration_id": "other"}]},
                               routing_identity=identity()) is None
    assert await cache.start_prewarm(request(), route=ROUTE, routing_identity=identity()) is False
    assert cache.get_stream(request(), routing_identity=identity()) is None
    await cache.aclose()


@pytest.mark.anyio
async def test_catalog_prewarm_is_serial_and_contains_only_five_fixed_texts():
    gateway = Gateway()
    cache = FixedPromptAudioCache(gateway)
    await cache.prewarm_catalog(request(metadata={"candidate": "private"}), route=ROUTE, routing_identity=identity())
    assert [call.text for call in gateway.calls] == [item.text for item in FIXED_SPEECH_CATALOG]
    assert all(set(call.metadata) == {"approved", "fixed_prompt_id", "fixed_prompt_version"} for call in gateway.calls)
    assert cache.stats()["entries"] == 5 and cache.stats()["inflight"] == 0
    await cache.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("code", ["provider_billing_required", "provider_rate_limited", "provider_authentication_failed"])
async def test_provider_failure_keeps_normal_cache_miss_without_exposing_details(code, caplog):
    class Rejected(Gateway):
        async def open_tts_stream(self, request, **kwargs):
            raise ProviderError(code, "PRIVATE_PROVIDER_BODY_AND_SECRET", retryable=False)
    cache = FixedPromptAudioCache(Rejected())
    with caplog.at_level("INFO"):
        assert await cache.start_prewarm(request(), route=ROUTE, routing_identity=identity()) is False
    assert cache.get_stream(request(), routing_identity=identity()) is None
    assert "PRIVATE_PROVIDER" not in caplog.text
    assert cache.stats()["failed"] == 1
    await cache.aclose()


@pytest.mark.anyio
async def test_disabled_or_changed_route_before_provider_call_is_not_invoked():
    gateway = Gateway()
    gateway.current_identity = identity(fingerprint="updated-before-run")
    cache = FixedPromptAudioCache(gateway)
    assert await cache.start_prewarm(request(), route=ROUTE, routing_identity=identity()) is False
    assert not gateway.calls
    await cache.aclose()


@pytest.mark.anyio
async def test_mismatched_route_cannot_store_another_voice_under_current_identity():
    gateway = Gateway()
    cache = FixedPromptAudioCache(gateway)
    other_route = {**ROUTE, "primary": {"model_configuration_id": "same_provider_different_voice"}}
    assert await cache.start_prewarm(request(), route=other_route, routing_identity=identity()) is False
    assert not gateway.calls
    await cache.aclose()


@pytest.mark.anyio
async def test_uncooperative_read_keeps_its_slot_until_cleanup_and_cannot_store_late_audio():
    gate = asyncio.Event()
    cancelled = asyncio.Event()
    class SlowSource(Source):
        async def events(self):
            try:
                await gate.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await gate.wait()
            yield self.event(2, "audio.chunk", pcm_s16le=self.pcm)
            yield self.event(3, "audio.final", total_audio_bytes=len(self.pcm))
    gateway = Gateway(SlowSource)
    cache = FixedPromptAudioCache(gateway, max_inflight=1, prewarm_timeout_s=.02)
    assert await cache.start_prewarm(request(), route=ROUTE, routing_identity=identity()) is False
    await asyncio.wait_for(cancelled.wait(), timeout=.2)
    assert cache.stats()["inflight"] == 1
    another = request().model_copy(update={"text": SUPPLEMENT_SPEECH["check"]})
    assert cache.start_prewarm(another, route=ROUTE, routing_identity=identity()) is None
    gate.set()
    for _ in range(30):
        if cache.stats()["inflight"] == 0:
            break
        await asyncio.sleep(0)
    assert cache.stats()["inflight"] == 0 and cache.stats()["entries"] == 0
    assert gateway.sources[0].closed
    await cache.aclose()


@pytest.mark.anyio
async def test_timeout_does_not_duplicate_same_key_while_another_slot_is_available():
    release = asyncio.Event()
    cancelled = asyncio.Event()
    class SlowOpen(Gateway):
        async def open_tts_stream(self, request, **kwargs):
            self.calls.append(request)
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return Source()
    gateway = SlowOpen()
    cache = FixedPromptAudioCache(gateway, max_inflight=2, prewarm_timeout_s=.02)
    assert await cache.start_prewarm(request(), route=ROUTE, routing_identity=identity()) is False
    await cancelled.wait()
    assert cache.stats()["inflight"] == 1
    assert not cache.retryable_missing(request(), routing_identity=identity())
    assert cache.start_prewarm(request(), route=ROUTE, routing_identity=identity()) is None
    assert len(gateway.calls) == 1
    release.set()
    for _ in range(30):
        if cache.stats()["inflight"] == 0:
            break
        await asyncio.sleep(0)
    assert cache.retryable_missing(request(), routing_identity=identity())
    await cache.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("max_inflight", [1, 2])
async def test_real_gateway_detached_open_retains_cache_ownership_until_late_abort_finishes(max_inflight):
    from test_fixed_speech_preparation_075 import configured_gateway

    release_open = asyncio.Event()
    opening_cancelled = asyncio.Event()
    release_abort = asyncio.Event()
    abort_started = asyncio.Event()
    abort_cancelled = asyncio.Event()

    class LateSource(Source):
        async def abort(self):
            abort_started.set()
            while not release_abort.is_set():
                try:
                    await release_abort.wait()
                except asyncio.CancelledError:
                    abort_cancelled.set()
            self.closed = True

    store, provider, gateway = configured_gateway()
    # Exercise the helper's bounded cleanup without slowing the test by 2s.
    route = store.model_routes["route_test"]
    route["primary"]["timeout_s"] = .03
    late_source = LateSource()
    calls = []

    async def slow_open(req, context):
        calls.append(req)
        while not release_open.is_set():
            try:
                await release_open.wait()
            except asyncio.CancelledError:
                opening_cancelled.set()
        return late_source

    provider.open_tts_stream = slow_open
    snapshot = gateway.tts_routing_snapshot(request())
    cache = FixedPromptAudioCache(gateway, max_inflight=max_inflight, prewarm_timeout_s=.03)
    req, identity_, route_ = snapshot["request"], snapshot["identity"], snapshot["route"]
    assert not await cache.start_prewarm(req, route=route_, routing_identity=identity_)
    await asyncio.wait_for(opening_cancelled.wait(), timeout=.2)
    await asyncio.sleep(.01)
    assert cache.stats()["inflight"] == 1
    assert not cache.retryable_missing(req, routing_identity=identity_)
    assert cache.start_prewarm(req, route=route_, routing_identity=identity_) is None
    assert len(calls) == 1

    release_open.set()
    await asyncio.wait_for(abort_started.wait(), timeout=.2)
    await asyncio.wait_for(abort_cancelled.wait(), timeout=.2)
    assert cache.stats()["inflight"] == 1
    assert not cache.retryable_missing(req, routing_identity=identity_)
    assert cache.start_prewarm(req, route=route_, routing_identity=identity_) is None
    assert not late_source.closed and len(calls) == 1
    release_abort.set()
    for _ in range(50):
        if not cache.stats()["inflight"]:
            break
        await asyncio.sleep(0)
    assert late_source.closed and cache.stats()["inflight"] == 0
    assert cache.stats()["entries"] == 0
    assert cache.retryable_missing(req, routing_identity=identity_)
    await cache.aclose()


@pytest.mark.anyio
async def test_real_gateway_failed_late_abort_quarantines_only_its_cache_slot():
    from test_fixed_speech_preparation_075 import configured_gateway

    release = asyncio.Event()
    cancelled = asyncio.Event()
    aborted = asyncio.Event()

    class BrokenAbort(Source):
        async def abort(self):
            aborted.set()
            raise RuntimeError("synthetic cleanup failure")

    _, provider, gateway = configured_gateway()
    calls = []
    async def slow_open(req, context):
        calls.append(req)
        if len(calls) == 1:
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return BrokenAbort()
        return Source(ready_provider=ProviderMeta(provider_id="dashscope", model="qwen3-tts-flash",
                                                  request_id="synthetic", latency_ms=0))
    provider.open_tts_stream = slow_open
    snapshot = gateway.tts_routing_snapshot(request())
    cache = FixedPromptAudioCache(gateway, max_inflight=2, prewarm_timeout_s=.03)
    req, identity_, route_ = snapshot["request"], snapshot["identity"], snapshot["route"]
    assert not await cache.start_prewarm(req, route=route_, routing_identity=identity_)
    await cancelled.wait()
    release.set()
    await asyncio.wait_for(aborted.wait(), timeout=.2)
    await asyncio.sleep(.01)
    assert cache.stats()["inflight"] == 1
    assert not cache.retryable_missing(req, routing_identity=identity_)
    assert cache.start_prewarm(req, route=route_, routing_identity=identity_) is None
    other = req.model_copy(update={"text": SUPPLEMENT_SPEECH["check"]})
    assert await cache.start_prewarm(other, route=route_, routing_identity=identity_)
    assert len(calls) == 2 and cache.stats()["inflight"] == 1
    started = monotonic()
    await cache.aclose()
    assert monotonic() - started < .1


@pytest.mark.anyio
@pytest.mark.parametrize("code", ["provider_billing_required", "provider_auth_failed"])
async def test_real_gateway_late_terminal_open_error_revokes_refill_before_releasing_slot(code):
    from test_fixed_speech_preparation_075 import configured_gateway

    release = asyncio.Event()
    cancelled = asyncio.Event()
    _, provider, gateway = configured_gateway()
    calls = []

    async def slow_rejection(req, context):
        calls.append(req)
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        raise ProviderError(code, "synthetic late rejection", retryable=False)

    provider.open_tts_stream = slow_rejection
    snapshot = gateway.tts_routing_snapshot(request())
    cache = FixedPromptAudioCache(gateway, max_inflight=1, prewarm_timeout_s=.03)
    req, identity_, route_ = snapshot["request"], snapshot["identity"], snapshot["route"]
    assert not await cache.start_prewarm(req, route=route_, routing_identity=identity_)
    await asyncio.wait_for(cancelled.wait(), timeout=.2)
    assert cache.stats()["inflight"] == 1
    assert not cache.retryable_missing(req, routing_identity=identity_)
    release.set()
    for _ in range(50):
        if not cache.stats()["inflight"]:
            break
        await asyncio.sleep(0)
    assert cache.stats()["inflight"] == 0
    assert not cache.retryable_missing(req, routing_identity=identity_)
    await cache.prewarm_catalog(req, route=route_, routing_identity=identity_, retry_only=True)
    assert len(calls) == 1 and cache.stats()["entries"] == 0
    await cache.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("code,retryable,eligible", [
    ("provider_timeout", True, True), ("provider_network_error", True, True),
    ("provider_server_error", True, True), ("provider_rate_limited", True, True),
    ("provider_timeout", False, False), ("provider_billing_required", True, False),
    ("provider_auth_failed", True, False), ("model_configuration_not_ready", True, False),
    ("provider_bad_request", True, False), ("provider_route_changed", True, False),
])
async def test_only_known_transient_failures_are_eligible_for_background_refill(code, retryable, eligible):
    class FailedGateway(Gateway):
        async def open_tts_stream(self, request, **kwargs):
            raise ProviderError(code, "synthetic", retryable=retryable)
    cache = FixedPromptAudioCache(FailedGateway())
    assert not await cache.start_prewarm(request(), route=ROUTE, routing_identity=identity())
    assert cache.retryable_missing(request(), routing_identity=identity()) is eligible
    assert cache.has_retryable_missing(request(), routing_identity=identity()) is eligible
    await cache.aclose()


@pytest.mark.anyio
async def test_busy_and_never_attempted_entries_are_not_refill_candidates():
    gate = asyncio.Event()
    cache = FixedPromptAudioCache(Gateway(lambda: Source(gate=gate)), max_inflight=1)
    work = cache.start_prewarm(request(), route=ROUTE, routing_identity=identity())
    other = request().model_copy(update={"text": SUPPLEMENT_SPEECH["check"]})
    assert cache.start_prewarm(other, route=ROUTE, routing_identity=identity()) is None
    assert not cache.retryable_missing(other, routing_identity=identity())
    gate.set()
    await work
    assert not cache.has_retryable_missing(other, routing_identity=identity())
    await cache.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("nested", [False, True])
async def test_known_terminal_rejection_during_blocked_cleanup_is_not_refill_eligible(nested):
    release = asyncio.Event()
    cleaning = asyncio.Event()
    class RejectedSource(Source):
        async def events(self):
            raise ProviderError("provider_billing_required", "synthetic rejection", retryable=False)
            yield
        async def abort(self):
            cleaning.set()
            await release.wait()
            self.closed = True
    source = RejectedSource()
    def make_source():
        return ValidatedTTSStream(source, provider_id="synthetic_tts", model="synthetic_voice", read_timeout_s=1) if nested else source
    cache = FixedPromptAudioCache(Gateway(make_source), prewarm_timeout_s=.03)
    assert not await cache.start_prewarm(request(), route=ROUTE, routing_identity=identity())
    assert cleaning.is_set() and cache.stats()["timeout"] == 1
    release.set()
    await asyncio.sleep(.01)
    assert not cache.retryable_missing(request(), routing_identity=identity())
    await cache.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("streaming", [False, True])
async def test_actual_dashscope_gateway_rejection_before_blocked_cleanup_does_not_become_retry(monkeypatch, streaming):
    import httpx
    from app.model_gateway.gateway import ModelGateway
    from app.providers.dashscope.provider import DashScopeProvider
    from app.providers.dashscope.tts_streaming import DashScopeTTSStream
    from test_tts_streaming import PacketStream, sse, store_and_route

    release = asyncio.Event()
    cleaning = asyncio.Event()
    original_abort = DashScopeTTSStream.abort
    async def delayed_cleanup(source):
        if source.terminal_error is not None:
            cleaning.set()
            try:
                await release.wait()
            finally:
                await original_abort(source)
        else:
            await original_abort(source)
    monkeypatch.setattr(DashScopeTTSStream, "abort", delayed_cleanup)
    async def handler(req):
        if not streaming:
            return httpx.Response(400, json={"code": "Arrearage", "message": "synthetic rejection"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            stream=PacketStream(sse({"code": "Arrearage", "message": "synthetic rejection", "request_id": "synthetic"})))
    transport = httpx.MockTransport(handler)
    adapter = DashScopeProvider(client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs))
    store, route = store_and_route()
    store.model_routes[route["id"]] = {**route, "purpose": "interview_agent_expression"}
    gateway = ModelGateway(store, {"dashscope": adapter})
    snapshot = gateway.tts_routing_snapshot(request())
    cache = FixedPromptAudioCache(gateway, prewarm_timeout_s=.03)
    assert not await cache.start_prewarm(snapshot["request"], route=snapshot["route"], routing_identity=snapshot["identity"])
    assert cleaning.is_set()
    release.set()
    await asyncio.sleep(.01)
    assert not cache.retryable_missing(snapshot["request"], routing_identity=snapshot["identity"])
    assert len(store.model_invocations) == (2 if streaming else 1)
    await cache.aclose()


@pytest.mark.anyio
async def test_known_open_rejection_annotation_does_not_leak_into_next_cancelled_call_on_same_task(monkeypatch):
    import httpx
    from app.model_gateway.gateway import ModelGateway
    from app.providers.dashscope.provider import DashScopeProvider
    from app.providers.dashscope.tts_streaming import DashScopeTTSStream
    from test_tts_streaming import store_and_route

    calls = []
    original_abort = DashScopeTTSStream.abort
    async def delayed_cleanup(source):
        if source.terminal_error is not None:
            try:
                await asyncio.Event().wait()
            finally:
                await original_abort(source)
        else:
            await original_abort(source)
    monkeypatch.setattr(DashScopeTTSStream, "abort", delayed_cleanup)
    async def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(400, json={"code": "Arrearage", "message": "synthetic rejection"})
        await asyncio.Event().wait()
    transport = httpx.MockTransport(handler)
    adapter = DashScopeProvider(client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs))
    store, route = store_and_route()
    gateway = ModelGateway(store, {"dashscope": adapter})
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for expected in ("provider_billing_required", None):
        timer = loop.call_later(.03, task.cancel)
        try:
            with pytest.raises(asyncio.CancelledError):
                await gateway.open_tts_stream(request(), route=route)
        finally:
            timer.cancel()
        error = getattr(task, "provider_error", None)
        assert (error.code if error else None) == expected
    assert len(calls) == 2


@pytest.mark.anyio
async def test_cached_output_abort_does_not_invalidate_complete_shared_pcm():
    cache = FixedPromptAudioCache(Gateway())
    assert await cache.start_prewarm(request(), route=ROUTE, routing_identity=identity())
    first = cache.get_stream(request(), routing_identity=identity())
    await first.abort()
    with pytest.raises(ProviderError, match="one consumer"):
        _ = [event async for event in first.events()]
    second = cache.get_stream(request(), routing_identity=identity())
    assert [event async for event in second.events()][-1].type == "audio.final"
    await cache.aclose()
