"""API-lifetime preparation with the real gateway and synthetic PCM only."""

import asyncio
from time import monotonic

import pytest

from app.core.interaction_budget import interaction_deadline, remaining_interaction_seconds
from app.core.prompt.fixed_speech import FIXED_SPEECH_CATALOG, WARMUP_CONFIRMATION_SPEECH
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import TTSSynthesizeRequest
from app.providers.dashscope.provider import DashScopeProvider
from app.services.fixed_speech_preparation import (
    cached_fixed_speech, close_fixed_speech_preparation, prepare_fixed_speech_for_session,
    start_fixed_speech_preparation,
)
from test_tts_streaming import FixtureStream, event, store_and_route


pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def close_scopes_after_test():
    yield
    await close_fixed_speech_preparation()


class SyntheticProvider(DashScopeProvider):
    def __init__(self, *, gate=None, pcm=b"\x01\0" * 10):
        super().__init__()
        self.gate = gate
        self.pcm = pcm
        self.calls = []
        self.context_deadlines = []
        self.started = asyncio.Event()
        self.failures = {}

    async def open_tts_stream(self, request, context):
        self.calls.append((request, context))
        self.context_deadlines.append(remaining_interaction_seconds())
        self.started.set()
        failures = self.failures.get(request.text)
        if failures:
            raise failures.pop(0)
        if self.gate is not None:
            await self.gate.wait()
        return FixtureStream([event("audio.chunk", 2, pcm_s16le=self.pcm),
                              event("audio.final", 3, total_audio_bytes=len(self.pcm))])


def configured_gateway(*, gate=None, pcm=b"\x01\0" * 10, defaults=None):
    store, route = store_and_route()
    store.model_routes[route["id"]] = {**route, "purpose": "interview_agent_expression"}
    store.model_configurations["config_test"]["default_parameters"] = defaults or {}
    provider = SyntheticProvider(gate=gate, pcm=pcm)
    return store, provider, ModelGateway(store, {"dashscope": provider})


def live_request(*, voice="voice_default_cn", language="zh-CN", **changes):
    # Match the approved expression request, including explicit profile defaults
    # and session metadata that must not change the reusable PCM identity.
    return TTSSynthesizeRequest(
        organization_id="org_default", purpose="interview_agent_expression",
        text=WARMUP_CONFIRMATION_SPEECH, language=language, voice_profile_id=voice,
        format="audio/wav", metadata={"interview_id": "synthetic_interview", "turn_id": "synthetic_turn", "approved": True},
        **changes,
    )


async def finished(scope):
    if scope.jobs:
        await asyncio.wait_for(asyncio.gather(*list(scope.jobs.values())), timeout=2)


async def pcm_of(stream):
    assert stream is not None
    return b"".join([item.pcm_s16le async for item in stream.events()])


async def test_start_is_nonblocking_idempotent_and_shutdown_closes_only_owned_resources():
    gate = asyncio.Event()
    _, provider, gateway = configured_gateway(gate=gate)
    started = monotonic()
    scope = start_fixed_speech_preparation(gateway)
    assert monotonic() - started < .05 and len(scope.jobs) == 1
    assert start_fixed_speech_preparation(gateway) is scope
    await asyncio.wait_for(provider.started.wait(), timeout=.2)
    assert len(provider.calls) == 1 and not scope.cache.stats()["entries"]
    await close_fixed_speech_preparation()
    assert scope.closed and scope.cache.stats()["closed"] and not scope.jobs
    assert cached_fixed_speech(gateway, live_request(), first_audio_deadline=monotonic() + 1) is None


async def test_startup_cache_hits_actual_runtime_request_despite_metadata_and_model_defaults():
    store, provider, gateway = configured_gateway(defaults={"speaking_rate": 1.15, "voice_profile_id": "model_default_voice"})
    scope = start_fixed_speech_preparation(gateway)
    await finished(scope)
    assert len(provider.calls) == 5 and all(req.speaking_rate == 1.15 for req, _ in provider.calls)
    assert all(req.voice_profile_id == "voice_default_cn" for req, _ in provider.calls)
    # A runtime constructs another gateway over the same application store.
    runtime_gateway = ModelGateway(store, {"dashscope": provider})
    stream = cached_fixed_speech(runtime_gateway, live_request(), first_audio_deadline=monotonic() + 1)
    assert await pcm_of(stream) == b"\x01\0" * 10
    assert len(provider.calls) == 5 and scope.cache.stats()["hit"] == 1
    assert all("interview_id" not in req.metadata for req, _ in provider.calls)


async def test_explicit_voice_and_language_profile_prewarm_matches_only_that_profile():
    _, provider, gateway = configured_gateway()
    scope = start_fixed_speech_preparation(gateway)
    await finished(scope)
    prepare_fixed_speech_for_session(gateway, "org_default", {"voice_profile_id": "profile_voice", "language": "en-US"})
    await finished(scope)
    assert len(provider.calls) == 10
    assert await pcm_of(cached_fixed_speech(gateway, live_request(voice="profile_voice", language="en-US"),
                                            first_audio_deadline=monotonic() + 1)) == b"\x01\0" * 10
    assert cached_fixed_speech(gateway, live_request(voice="profile_voice", language="zh-CN"),
                                first_audio_deadline=monotonic() + 1) is None
    assert scope.cache.stats()["hit"] == 1


async def test_distinct_persistence_scopes_do_not_reuse_other_application_pcm():
    _, first_provider, first_gateway = configured_gateway(pcm=b"\x01\0" * 10)
    _, second_provider, second_gateway = configured_gateway(pcm=b"\x02\0" * 10)
    first = start_fixed_speech_preparation(first_gateway)
    second = start_fixed_speech_preparation(second_gateway)
    assert first is not second
    await finished(first)
    await finished(second)
    assert await pcm_of(cached_fixed_speech(first_gateway, live_request(), first_audio_deadline=monotonic() + 1)) == b"\x01\0" * 10
    assert await pcm_of(cached_fixed_speech(second_gateway, live_request(), first_audio_deadline=monotonic() + 1)) == b"\x02\0" * 10
    assert len(first_provider.calls) == len(second_provider.calls) == 5


@pytest.mark.parametrize("change", ["disabled_model", "unready_model", "disabled_connection", "rotated_secret", "changed_voice"])
async def test_invalidated_configuration_cannot_return_old_pcm(change):
    store, provider, gateway = configured_gateway()
    scope = start_fixed_speech_preparation(gateway)
    await finished(scope)
    if change == "disabled_model":
        store.model_configurations["config_test"]["enabled"] = False
    elif change == "unready_model":
        store.model_configurations["config_test"]["status"] = "failed"
    elif change == "disabled_connection":
        store.provider_connections["connection_test"]["enabled"] = False
    elif change == "rotated_secret":
        store.save_provider_secret("connection_test", {"api_key": "different-synthetic-secret"})
    else:
        store.model_configurations["config_test"]["settings"] = {"voice": "new_voice"}
    assert cached_fixed_speech(gateway, live_request(), first_audio_deadline=monotonic() + 1) is None
    assert scope.cache.stats()["hit"] == 0


async def test_prewarm_has_independent_context_and_repeated_session_joins_are_coalesced():
    _, provider, gateway = configured_gateway()
    with interaction_deadline(monotonic() - 1):
        scope = start_fixed_speech_preparation(gateway)
        for _ in range(5):
            prepare_fixed_speech_for_session(gateway, "org_default", {})
    await finished(scope)
    prepare_fixed_speech_for_session(gateway, "org_default", {})
    assert not scope.jobs and len(provider.calls) == 5
    assert provider.context_deadlines == [None] * 5


async def test_two_profile_jobs_are_bounded_without_cooling_down_rejected_third_profile():
    gate = asyncio.Event()
    _, _, gateway = configured_gateway(gate=gate)
    scope = start_fixed_speech_preparation(gateway)
    prepare_fixed_speech_for_session(gateway, "org_default", {"voice_profile_id": "voice_two"})
    prepare_fixed_speech_for_session(gateway, "org_default", {"voice_profile_id": "voice_three"})
    assert len(scope.jobs) == len(scope.attempted) == 2
    gate.set()
    await finished(scope)
    prepare_fixed_speech_for_session(gateway, "org_default", {"voice_profile_id": "voice_three"})
    assert len(scope.jobs) == 1 and len(scope.attempted) == 3
    await finished(scope)


async def test_dynamic_speech_lookup_never_schedules_catalog_or_hits_cache():
    _, provider, gateway = configured_gateway()
    scope = start_fixed_speech_preparation(gateway)
    await finished(scope)
    dynamic = live_request().model_copy(update={"text": "请根据你的项目解释某项实现"})
    assert cached_fixed_speech(gateway, dynamic, first_audio_deadline=monotonic() + 1) is None
    assert not scope.jobs and len(provider.calls) == len(FIXED_SPEECH_CATALOG)


async def test_lifecycle_close_is_bounded_even_when_a_catalog_task_ignores_cancellation():
    _, _, gateway = configured_gateway()
    scope = start_fixed_speech_preparation(gateway)
    await finished(scope)
    release = asyncio.Event()
    entered = asyncio.Event()
    async def uncooperative():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
    task = asyncio.create_task(uncooperative())
    scope.jobs["synthetic_noncooperative"] = task
    await entered.wait()
    started = monotonic()
    await close_fixed_speech_preparation()
    assert monotonic() - started < 1.3
    assert scope.closed and scope.cache.stats()["closed"]
    release.set()
    await task


async def finished_rounds(scope):
    async def drain():
        while scope.jobs:
            await asyncio.gather(*list(scope.jobs.values()))
    await asyncio.wait_for(drain(), timeout=2)


async def test_transient_first_round_failure_gets_one_independent_refill_of_only_the_missing_prompt(monkeypatch):
    from app.services import fixed_speech_preparation as preparation
    monkeypatch.setattr(preparation, "_RETRY_BACKOFF_SECONDS", .01)
    _, provider, gateway = configured_gateway()
    provider.failures[WARMUP_CONFIRMATION_SPEECH] = [ProviderError("provider_timeout", "synthetic timeout", retryable=True)]
    with interaction_deadline(monotonic() - 1):
        scope = start_fixed_speech_preparation(gateway)
    first_round = list(scope.jobs.values())
    await asyncio.gather(*first_round)
    assert len(provider.calls) == 5 and len(scope.retry_jobs) == 1 and scope.cache.stats()["entries"] == 4
    prepare_fixed_speech_for_session(gateway, "org_default", {})
    assert len(scope.jobs) == 1
    await finished_rounds(scope)
    assert len(provider.calls) == 6
    assert [req.text for req, _ in provider.calls].count(WARMUP_CONFIRMATION_SPEECH) == 2
    assert scope.cache.stats()["entries"] == 5
    assert provider.context_deadlines == [None] * 6
    assert await pcm_of(cached_fixed_speech(gateway, live_request(), first_audio_deadline=monotonic() + 1))


async def test_actual_prewarm_timer_expiry_is_refilled_once_after_the_remaining_prompts_succeed(monkeypatch):
    from app.services import fixed_speech_preparation as preparation
    monkeypatch.setattr(preparation, "_RETRY_BACKOFF_SECONDS", .01)
    _, provider, gateway = configured_gateway()
    original = provider.open_tts_stream
    stalled = False
    async def slow_once(req, context):
        nonlocal stalled
        if req.text == WARMUP_CONFIRMATION_SPEECH and not stalled:
            stalled = True
            provider.calls.append((req, context))
            await asyncio.Event().wait()
        return await original(req, context)
    provider.open_tts_stream = slow_once
    scope = start_fixed_speech_preparation(gateway)
    scope.cache.prewarm_timeout_s = .02
    await finished_rounds(scope)
    assert stalled and len(provider.calls) == 6
    assert scope.cache.stats()["entries"] == 5 and not scope.retry_jobs


async def test_refill_failure_does_not_start_a_third_round_or_periodic_loop(monkeypatch):
    from app.services import fixed_speech_preparation as preparation
    monkeypatch.setattr(preparation, "_RETRY_BACKOFF_SECONDS", .01)
    _, provider, gateway = configured_gateway()
    provider.failures[WARMUP_CONFIRMATION_SPEECH] = [
        ProviderError("provider_network_error", "synthetic", retryable=True) for _ in range(3)
    ]
    scope = start_fixed_speech_preparation(gateway)
    await finished_rounds(scope)
    await asyncio.sleep(.03)
    assert len(provider.calls) == 6 and len(provider.failures[WARMUP_CONFIRMATION_SPEECH]) == 1
    assert scope.cache.stats()["entries"] == 4 and not scope.jobs and not scope.retry_jobs
    prepare_fixed_speech_for_session(gateway, "org_default", {})
    assert not scope.jobs  # The existing external-trigger cooldown remains.


@pytest.mark.parametrize("code", ["provider_billing_required", "provider_auth_failed", "model_configuration_not_ready", "provider_bad_request"])
async def test_nontransient_failure_does_not_schedule_a_refill_even_if_marked_retryable(code, monkeypatch):
    from app.services import fixed_speech_preparation as preparation
    monkeypatch.setattr(preparation, "_RETRY_BACKOFF_SECONDS", .01)
    _, provider, gateway = configured_gateway()
    provider.failures[WARMUP_CONFIRMATION_SPEECH] = [ProviderError(code, "synthetic", retryable=True)]
    scope = start_fixed_speech_preparation(gateway)
    await finished_rounds(scope)
    assert len(provider.calls) == 5 and scope.cache.stats()["entries"] == 4
    assert not scope.retry_jobs


async def test_shutdown_cancels_the_delayed_refill_before_new_provider_work(monkeypatch):
    from app.services import fixed_speech_preparation as preparation
    monkeypatch.setattr(preparation, "_RETRY_BACKOFF_SECONDS", .1)
    _, provider, gateway = configured_gateway()
    provider.failures[WARMUP_CONFIRMATION_SPEECH] = [ProviderError("provider_timeout", "synthetic", retryable=True)]
    scope = start_fixed_speech_preparation(gateway)
    await asyncio.gather(*list(scope.jobs.values()))
    assert len(scope.retry_jobs) == 1
    await close_fixed_speech_preparation()
    await asyncio.sleep(.12)
    assert len(provider.calls) == 5 and not scope.jobs and not scope.retry_jobs


async def test_changed_configuration_cancels_old_refill_and_allows_new_profile_job(monkeypatch):
    from app.services import fixed_speech_preparation as preparation
    monkeypatch.setattr(preparation, "_RETRY_BACKOFF_SECONDS", .05)
    store, provider, gateway = configured_gateway()
    provider.failures[WARMUP_CONFIRMATION_SPEECH] = [ProviderError("provider_timeout", "synthetic", retryable=True)]
    scope = start_fixed_speech_preparation(gateway)
    await asyncio.gather(*list(scope.jobs.values()))
    old_retry = next(iter(scope.retry_jobs.values()))[0]
    store.model_configurations["config_test"]["settings"] = {"voice": "changed_voice"}
    prepare_fixed_speech_for_session(gateway, "org_default", {})
    await finished_rounds(scope)
    assert old_retry.cancelled()
    assert len(provider.calls) == 10  # Five new-identity prompts, no old-identity refill.
    await asyncio.sleep(.06)
    assert len(provider.calls) == 10


async def test_route_change_without_new_schedule_is_checked_again_after_backoff(monkeypatch):
    from app.services import fixed_speech_preparation as preparation
    monkeypatch.setattr(preparation, "_RETRY_BACKOFF_SECONDS", .01)
    store, provider, gateway = configured_gateway()
    provider.failures[WARMUP_CONFIRMATION_SPEECH] = [ProviderError("provider_timeout", "synthetic", retryable=True)]
    scope = start_fixed_speech_preparation(gateway)
    await asyncio.gather(*list(scope.jobs.values()))
    store.model_configurations["config_test"]["enabled"] = False
    await finished_rounds(scope)
    assert len(provider.calls) == 5 and not scope.jobs
