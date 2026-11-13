"""Bounded process-local complete PCM for approved fixed speech only.

This cache has no participant URL and does not select acts, publish tracks or
acknowledge playback. A hit is a fresh server stream for the existing approved
output path. The caller supplies a freshly authorized gateway routing identity.
"""

import asyncio
from collections import OrderedDict
from contextvars import Context
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import logging
from time import monotonic
from typing import Any, Dict, Optional

from app.core.ids import new_id
from app.core.prompt.fixed_speech import FIXED_SPEECH_CATALOG, fixed_speech_for_text
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ProviderMeta, TTSSynthesizeRequest, Usage
from app.model_gateway.tts_streaming import TTSStreamEvent, ValidatedTTSStream


_LOG = logging.getLogger(__name__)
_PURPOSE = "interview_agent_expression"
_RETRYABLE_PREWARM_ERRORS = frozenset({
    "provider_timeout", "provider_network_error", "provider_server_error", "provider_rate_limited",
    "provider_circuit_open", "provider_stream_failed", "provider_audio_final_missing", "provider_schema_invalid",
})


@dataclass(frozen=True)
class _Entry:
    pcm: bytes
    checksum: str
    sample_rate_hz: int
    provider_id: str
    model: str
    created_at: float


class _CachedPCMStream:
    def __init__(self, entry: _Entry) -> None:
        self.stream_id = new_id("fixed_speech_stream")
        self._entry = entry
        self._closed = False
        self._consumed = False
        self._provider = ProviderMeta(provider_id=entry.provider_id, model=entry.model,
                                      request_id=new_id("fixed_speech_cache"), latency_ms=0)
        self.ready_event = self._event(1, "stream.ready")

    def _event(self, sequence, kind, **fields):
        return TTSStreamEvent(stream_id=self.stream_id, sequence=sequence, type=kind,
                              sample_rate_hz=self._entry.sample_rate_hz, provider=self._provider, **fields)

    async def events(self):
        if self._closed or self._consumed:
            raise ProviderError("provider_stream_closed", "Fixed speech stream is closed.", retryable=False)
        self._consumed = True
        sequence = 1
        for offset in range(0, len(self._entry.pcm), 9600):
            if self._closed:
                raise ProviderError("provider_stream_closed", "Fixed speech stream was cancelled.", retryable=False)
            sequence += 1
            yield self._event(sequence, "audio.chunk", pcm_s16le=self._entry.pcm[offset:offset + 9600])
        if self._closed:
            raise ProviderError("provider_stream_closed", "Fixed speech stream was cancelled.", retryable=False)
        yield self._event(sequence + 1, "audio.final", total_audio_bytes=len(self._entry.pcm), usage=Usage())

    async def abort(self):
        self._closed = True


class FixedPromptAudioCache:
    def __init__(self, gateway: Any, *, max_entries: int = 64, max_bytes: int = 16 * 1024 * 1024,
                 max_item_bytes: int = 1024 * 1024, ttl_s: float = 1800,
                 max_inflight: int = 2, prewarm_timeout_s: float = 8.0,
                 clock=monotonic) -> None:
        self.gateway = gateway
        self.max_entries = max(1, min(64, int(max_entries)))
        self.max_bytes = max(2, min(16 * 1024 * 1024, int(max_bytes)))
        self.max_item_bytes = max(2, min(1024 * 1024, self.max_bytes, int(max_item_bytes)))
        self.ttl_s = max(.01, min(1800, float(ttl_s)))
        self.max_inflight = max(1, min(2, int(max_inflight)))
        self.prewarm_timeout_s = max(.01, min(8.0, float(prewarm_timeout_s)))
        self._clock = clock
        self._entries = OrderedDict()
        self._entry_sizes = {}
        self._bytes = 0
        self._inflight = {}
        self._collectors = set()
        self._collecting_keys = {}
        self._slots = set()
        self._slot_collectors = {}
        self._closed = False
        self._epoch = 0
        self._retryable_failures = OrderedDict()
        self._counters = {key: 0 for key in ("hit", "miss", "stored", "failed", "timeout", "busy", "corrupt", "evicted")}

    def _key(self, request: TTSSynthesizeRequest, routing_identity: Dict[str, Any]):
        prompt = fixed_speech_for_text(request.text)
        if (prompt is None or request.purpose != _PURPOSE or not isinstance(routing_identity, dict)
                or set(routing_identity) != {"organization_id", "provider_id", "model", "fingerprint"}
                or any(not isinstance(value, str) or not value or len(value) > 256
                       for value in routing_identity.values())
                or routing_identity["organization_id"] != request.organization_id
                or routing_identity["provider_id"] == "mock"):
            return None
        payload = {"request": request.model_dump(exclude={"metadata"}), "routing": routing_identity,
                   "prompt_id": prompt.id, "prompt_version": prompt.version}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                         allow_nan=False, separators=(",", ":")).encode()).hexdigest()

    def _remove(self, key):
        self._entries.pop(key, None)
        self._bytes -= self._entry_sizes.pop(key, 0)

    def _entry(self, key):
        entry = self._entries.get(key)
        if entry is None:
            return None
        if not isinstance(entry, _Entry):
            self._remove(key)
            self._counters["corrupt"] += 1
            return None
        if self._clock() - entry.created_at >= self.ttl_s:
            self._remove(key)
            return None
        if (not isinstance(entry.pcm, bytes) or not entry.pcm or len(entry.pcm) % 2
                or len(entry.pcm) > self.max_item_bytes
                or type(entry.sample_rate_hz) is not int or not 8000 <= entry.sample_rate_hz <= 48000
                or hashlib.sha256(entry.pcm).hexdigest() != entry.checksum):
            self._remove(key)
            self._counters["corrupt"] += 1
            return None
        self._entries.move_to_end(key)
        return entry

    def get_stream(self, request: TTSSynthesizeRequest, *, routing_identity: Dict[str, Any],
                   first_audio_deadline: Optional[float] = None) -> Optional[ValidatedTTSStream]:
        key = None if self._closed else self._key(request, routing_identity)
        entry = self._entry(key) if key else None
        if entry is not None and (entry.provider_id != routing_identity["provider_id"]
                                  or entry.model != routing_identity["model"]):
            self._remove(key)
            self._counters["corrupt"] += 1
            entry = None
        if entry is None:
            self._counters["miss"] += 1
            return None
        self._counters["hit"] += 1
        return ValidatedTTSStream(_CachedPCMStream(entry), provider_id=entry.provider_id, model=entry.model,
                                  read_timeout_s=1.0, max_total_bytes=self.max_item_bytes,
                                  max_audio_seconds=60, first_audio_deadline=first_audio_deadline)

    def start_prewarm(self, request: TTSSynthesizeRequest, *, route: Dict[str, Any],
                      routing_identity: Dict[str, Any]) -> Optional[asyncio.Future]:
        """Start bounded background work; cancelling one waiter never cancels it.

        A miss or exhausted capacity returns None immediately. Each caller gets
        a shielded view of the single flight; aclose owns actual cancellation.
        """
        key = None if self._closed else self._key(request, routing_identity)
        if key is None or self._entry(key) is not None:
            return None
        existing = self._inflight.get(key)
        if existing is not None:
            return asyncio.shield(existing)
        # A timed-out wrapper may finish before a cancellation-resistant read
        # actually closes. Never open another source for that same key.
        if key in self._collecting_keys:
            return None
        if len(self._slots) >= self.max_inflight:
            self._counters["busy"] += 1
            return None
        if (not isinstance(route, dict) or route.get("organization_id", request.organization_id) != request.organization_id
                or route.get("fallbacks") or not route.get("enabled", True)):
            return None
        prompt = fixed_speech_for_text(request.text)
        safe_request = request.model_copy(update={"metadata": {
            "approved": True, "fixed_prompt_id": prompt.id, "fixed_prompt_version": prompt.version,
        }}, deep=True)
        token = object()
        self._retryable_failures.pop(key, None)
        self._slots.add(token)
        # Background warmup must not inherit an expired interaction deadline.
        coroutine = self._warm(key, safe_request, deepcopy(route), dict(routing_identity), token, self._epoch)
        task = Context().run(asyncio.create_task, coroutine)
        self._inflight[key] = task

        def complete(done):
            if self._inflight.get(key) is done:
                self._inflight.pop(key, None)
            if token not in self._slot_collectors:
                self._slots.discard(token)
            try:
                done.result()
            except (Exception, asyncio.CancelledError):
                pass
        task.add_done_callback(complete)
        return asyncio.shield(task)

    def retryable_missing(self, request: TTSSynthesizeRequest, *, routing_identity: Dict[str, Any]) -> bool:
        """Eligibility only; busy, absent, invalid and still-closing work is not retried."""
        key = None if self._closed else self._key(request, routing_identity)
        failed_at = self._retryable_failures.get(key)
        return bool(key and failed_at is not None and self._clock() - failed_at < self.ttl_s
                    and self._entry(key) is None and key not in self._inflight
                    and key not in self._collecting_keys and len(self._slots) < self.max_inflight)

    def has_retryable_missing(self, request_template: TTSSynthesizeRequest, *, routing_identity: Dict[str, Any]) -> bool:
        return any(self.retryable_missing(request_template.model_copy(update={"text": prompt.text}),
                                         routing_identity=routing_identity) for prompt in FIXED_SPEECH_CATALOG)

    async def prewarm_catalog(self, request_template: TTSSynthesizeRequest, *, route: Dict[str, Any],
                              routing_identity: Dict[str, Any], retry_only: bool = False) -> None:
        """At most five sequential attempts, each independently bounded to 8s.

        Caller starts/supervises this coroutine before the first prompt is
        needed. It never blocks a control command or changes interview state.
        """
        for prompt in FIXED_SPEECH_CATALOG:
            if self._closed:
                return
            request = request_template.model_copy(update={"text": prompt.text}, deep=True)
            if retry_only and not self.retryable_missing(request, routing_identity=routing_identity):
                continue
            work = self.start_prewarm(request, route=route, routing_identity=routing_identity)
            if work is not None:
                await work

    async def _collect(self, request, route, identity, attempt_state):
        source = None
        stream = None
        try:
            if not self._identity_current(request, identity, route=route):
                raise ProviderError("provider_route_changed", "Fixed speech routing changed before warmup.", retryable=False)
            source = await self.gateway.open_tts_stream(request, route=route,
                first_audio_deadline=monotonic() + self.prewarm_timeout_s)
            attempt_state["source"] = source
            if (source.ready_event.provider.provider_id != identity["provider_id"]
                    or source.ready_event.provider.model != identity["model"]):
                raise ProviderError("provider_route_changed", "Fixed speech source differs from the requested route.", retryable=False)
            stream = ValidatedTTSStream(source, provider_id=identity["provider_id"], model=identity["model"],
                read_timeout_s=self.prewarm_timeout_s, max_total_bytes=self.max_item_bytes,
                max_audio_seconds=60, max_wall_seconds=self.prewarm_timeout_s)
            attempt_state["stream"] = stream
            pcm = bytearray()
            final = False
            async for event in stream.events():
                if event.type == "audio.chunk":
                    pcm.extend(event.pcm_s16le)
                elif event.type == "audio.final":
                    final = event.total_audio_bytes == len(pcm) and bool(pcm)
            if not final:
                raise ProviderError("provider_audio_final_missing", "Fixed speech has no complete PCM final.", retryable=True)
            audio = bytes(pcm)
            return _Entry(audio, hashlib.sha256(audio).hexdigest(), stream.sample_rate_hz,
                          identity["provider_id"], identity["model"], self._clock())
        except ProviderError as exc:
            attempt_state["error"] = exc
            raise
        finally:
            if stream is not None:
                await stream.abort()
            elif source is not None:
                await source.abort()

    def _identity_current(self, request, identity, *, route=None):
        snapshot = self.gateway.tts_routing_snapshot(request)
        return (isinstance(snapshot, dict) and snapshot.get("identity") == identity
                and (route is None or snapshot.get("route") == route))

    async def _warm(self, key, request, route, identity, token, epoch):
        attempt_state = {}
        collector = asyncio.create_task(self._collect(request, route, identity, attempt_state))
        self._collectors.add(collector)
        self._collecting_keys[key] = collector
        self._slot_collectors[token] = collector

        def release_slot(done):
            self._collectors.discard(done)
            if self._collecting_keys.get(key) is done:
                self._collecting_keys.pop(key, None)
            self._slot_collectors.pop(token, None)
            self._slots.discard(token)

        def collected(done):
            reported = getattr(done, "provider_error", None)
            if isinstance(reported, ProviderError):
                attempt_state["error"] = reported
            known = self._known_failure(attempt_state)
            if known is not None and not self._retryable_error(known):
                # A rejection can precede a slow cleanup and become visible
                # only after the wrapper's timer. Never turn that into retry.
                self._retryable_failures.pop(key, None)
            try:
                done.result()
            except (Exception, asyncio.CancelledError):
                pass
            cleanup = getattr(done, "tts_cleanup_completion", None)
            if not isinstance(cleanup, asyncio.Future):
                release_slot(done)
                return

            def cleaned(completion):
                reported = getattr(completion, "provider_error", None)
                if isinstance(reported, ProviderError) and not self._retryable_error(reported):
                    # A provider can report its rejection only after it has
                    # acknowledged cancellation and finished opening cleanup.
                    self._retryable_failures.pop(key, None)
                if not completion.cancelled() and completion.exception() is None and completion.result() is True:
                    release_slot(done)
                else:
                    # A failed abort has not established release. Quarantine
                    # only this occupied slot/key for the remaining scope.
                    self._retryable_failures.pop(key, None)

            # The gateway can return cancellation before its provider
            # finishes opening/aborting. Keep the original slot and key.
            if cleanup.done():
                cleaned(cleanup)
            else:
                cleanup.add_done_callback(cleaned)
        collector.add_done_callback(collected)
        try:
            done, _ = await asyncio.wait({collector}, timeout=self.prewarm_timeout_s)
            if not done:
                self._counters["timeout"] += 1
                known = self._known_failure(attempt_state)
                self._set_retryable(key, known is None or self._retryable_error(known))
                collector.cancel()
                _LOG.info("Fixed speech prewarm unavailable: reason=timeout")
                return False
            entry = collector.result()
            if self._closed or self._epoch != epoch or not self._identity_current(request, identity):
                return False
            # Complete, validated PCM alone can become a cache entry.
            for expired_key in list(self._entries):
                self._entry(expired_key)
            while self._entries and (len(self._entries) >= self.max_entries
                                     or self._bytes + len(entry.pcm) > self.max_bytes):
                self._remove(next(iter(self._entries)))
                self._counters["evicted"] += 1
            self._remove(key)
            self._entries[key] = entry
            self._entry_sizes[key] = len(entry.pcm)
            self._bytes += len(entry.pcm)
            self._counters["stored"] += 1
            self._retryable_failures.pop(key, None)
            return True
        except asyncio.CancelledError:
            collector.cancel()
            raise
        except Exception as exc:
            self._counters["failed"] += 1
            known = self._known_failure(attempt_state)
            self._set_retryable(key, self._retryable_error(known if known is not None else exc))
            # Never log provider bodies, text, route details or credentials.
            _LOG.info("Fixed speech prewarm unavailable: error_type=%s", type(exc).__name__)
            return False

    @staticmethod
    def _retryable_error(error):
        return isinstance(error, asyncio.TimeoutError) or (
            isinstance(error, ProviderError) and error.retryable and error.code in _RETRYABLE_PREWARM_ERRORS)

    @staticmethod
    def _known_failure(state):
        errors = [state.get("error"), *(getattr(state.get(key), "terminal_error", None) for key in ("stream", "source"))]
        errors = [error for error in errors if isinstance(error, ProviderError) and error.code != "provider_cancelled"]
        return next((error for error in errors if not error.retryable), errors[0] if errors else None)

    def _set_retryable(self, key, retryable):
        self._retryable_failures.pop(key, None)
        if retryable and not self._closed:
            self._retryable_failures[key] = self._clock()
            while len(self._retryable_failures) > self.max_entries:
                self._retryable_failures.popitem(last=False)

    def stats(self):
        return {**self._counters, "entries": len(self._entries), "bytes": self._bytes,
                "inflight": len(self._slots), "closed": self._closed}

    async def aclose(self) -> None:
        self._closed = True
        self._epoch += 1
        self._entries.clear()
        self._entry_sizes.clear()
        self._retryable_failures.clear()
        self._bytes = 0
        tasks = set(self._inflight.values()) | set(self._collectors)
        for task in tasks:
            task.cancel()
        if tasks:
            # Uncooperative providers cannot prolong application shutdown or
            # publish late PCM. Their slots remain held until they really stop.
            await asyncio.wait(tasks, timeout=1.0)
