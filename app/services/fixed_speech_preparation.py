"""Optional fixed-speech preparation owned by the API event-loop lifetime."""

import asyncio
import contextvars
import hashlib
import json
import logging
import os
from time import monotonic
from weakref import WeakKeyDictionary

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import TTSSynthesizeRequest

_LOG = logging.getLogger(__name__)
_SCOPES = WeakKeyDictionary()
_RETRY_BACKOFF_SECONDS = 2.0


class FixedSpeechPreparation:
    def __init__(self, gateway):
        from app.services.fixed_prompt_audio import FixedPromptAudioCache
        self.gateway = gateway
        self.cache = FixedPromptAudioCache(gateway)
        self.pid = os.getpid()
        self.closed = False
        self.jobs = {}
        self.attempted = {}
        self.retry_jobs = {}

    def _cancel_stale_retries(self):
        for key, (task, request, identity) in list(self.retry_jobs.items()):
            try:
                current = self.gateway.tts_routing_snapshot(request)
                stale = current["identity"] != identity
            except ProviderError:
                stale = True
            if stale:
                task.cancel()
                if self.jobs.get(key) is task:
                    self.jobs.pop(key, None)
                self.retry_jobs.pop(key, None)

    def schedule(self, request, snapshot=None):
        if self.closed or self.pid != os.getpid():
            return
        self._cancel_stale_retries()
        try:
            snapshot = snapshot or self.gateway.tts_routing_snapshot(request)
        except ProviderError:
            return
        effective = snapshot["request"]
        key = hashlib.sha256(json.dumps({
            "identity": snapshot["identity"],
            "request": effective.model_dump(exclude={"text", "metadata"}),
        }, sort_keys=True).encode()).hexdigest()
        if key in self.jobs or len(self.jobs) >= 2 or monotonic() - self.attempted.get(key, -1e9) < 60:
            return
        self.attempted[key] = monotonic()
        if len(self.attempted) > 64:
            self.attempted.pop(next(iter(self.attempted)))

        async def prepare():
            try:
                await self.cache.prewarm_catalog(effective, route=snapshot["route"],
                                                 routing_identity=snapshot["identity"])
                if (not self.closed and self.pid == os.getpid()
                        and self.cache.has_retryable_missing(effective, routing_identity=snapshot["identity"])):
                    # Transfer this profile's job slot to one independent
                    # delayed refill; no loop and no interaction budget.
                    task = contextvars.Context().run(asyncio.create_task, refill())
                    self.jobs[key] = task
                    self.retry_jobs[key] = (task, effective, snapshot["identity"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOG.warning("Fixed speech preparation unavailable: error_type=%s", type(exc).__name__)
            finally:
                if self.jobs.get(key) is asyncio.current_task():
                    self.jobs.pop(key, None)

        async def refill():
            try:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
                if self.closed or self.pid != os.getpid():
                    return
                current = self.gateway.tts_routing_snapshot(effective)
                if current["identity"] != snapshot["identity"] or current["route"] != snapshot["route"]:
                    return
                await self.cache.prewarm_catalog(effective, route=snapshot["route"],
                                                 routing_identity=snapshot["identity"], retry_only=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOG.info("Fixed speech refill unavailable: error_type=%s", type(exc).__name__)
            finally:
                if self.jobs.get(key) is asyncio.current_task():
                    self.jobs.pop(key, None)
                self.retry_jobs.pop(key, None)

        # Prewarming has its own deadline; it cannot inherit an expired turn.
        self.jobs[key] = contextvars.Context().run(asyncio.create_task, prepare())

    async def aclose(self):
        self.closed = True
        tasks = list(self.jobs.values())
        for task in tasks:
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=1.0)
        await self.cache.aclose()
        self.jobs.clear()
        self.retry_jobs.clear()


def start_fixed_speech_preparation(gateway, *, organization_id="org_default"):
    loop = asyncio.get_running_loop()
    scopes = _SCOPES.setdefault(loop, {})
    key = id(gateway.persistence)
    scope = scopes.get(key)
    if scope is not None and not scope.closed and scope.pid == os.getpid():
        return scope
    scope = FixedSpeechPreparation(gateway)
    scopes[key] = scope
    scope.schedule(_template(organization_id, {}))
    return scope


async def close_fixed_speech_preparation():
    scopes = _SCOPES.pop(asyncio.get_running_loop(), {})
    if scopes:
        await asyncio.gather(*(scope.aclose() for scope in scopes.values()))


def _scope(gateway):
    scope = _SCOPES.get(asyncio.get_running_loop(), {}).get(id(gateway.persistence))
    return scope if (scope is not None and not scope.closed and scope.pid == os.getpid()
                     and scope.gateway.persistence is gateway.persistence) else None


def _template(organization_id, settings):
    from app.core.prompt.fixed_speech import WARMUP_CONFIRMATION_SPEECH
    return TTSSynthesizeRequest(
        organization_id=organization_id, purpose="interview_agent_expression",
        text=WARMUP_CONFIRMATION_SPEECH, language=settings.get("language", "zh-CN"),
        voice_profile_id=settings.get("voice_profile_id") or "voice_default_cn", format="audio/wav",
    )


def prepare_fixed_speech_for_session(gateway, organization_id, settings):
    scope = _scope(gateway)
    if scope is not None:
        scope.schedule(_template(organization_id, settings))


def cached_fixed_speech(gateway, request, *, first_audio_deadline):
    scope = _scope(gateway)
    if scope is None:
        return None
    from app.core.prompt.fixed_speech import fixed_speech_for_text
    if fixed_speech_for_text(request.text) is None:
        return None
    # Re-resolve on every lookup: disablement, voice changes and key rotation
    # immediately invalidate old PCM, even while its TTL has not elapsed.
    try:
        snapshot = gateway.tts_routing_snapshot(request)
    except ProviderError:
        return None
    stream = scope.cache.get_stream(snapshot["request"], routing_identity=snapshot["identity"],
                                    first_audio_deadline=first_audio_deadline)
    if stream is None:
        scope.schedule(snapshot["request"], snapshot)
    return stream
