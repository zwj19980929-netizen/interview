import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from copy import deepcopy
from dataclasses import dataclass
from time import monotonic, perf_counter
from typing import Any, Callable, Dict, Optional

from pydantic import BaseModel

from app.core.ids import new_id
from app.core.model_diagnostics import (
    current_model_call_scope, model_call_scope, model_timing_fields, record_model_diagnostic,
    safe_model_call_context, safe_model_diagnostic_fields,
)
from app.core.prompt.validation import StructuredResponseValidationError, validate_structured_response
from app.core.time import utc_now
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.dialogue import ValidatedSpeechDialogueStream
from app.model_gateway.registry import ProviderRegistry
from app.model_gateway.schemas import (
    AvatarSpeakRequest,
    AvatarSpeakResponse,
    BatchSTTRequest,
    BatchSTTResponse,
    ChatJSONRequest,
    ChatJSONResponse,
    ChatTextRequest,
    ChatTextResponse,
    InvocationRequest,
    InvocationResponse,
    ProviderContext,
    TTSSynthesizeRequest,
    TTSSynthesizeResponse,
    TextEmbeddingRequest,
    TextEmbeddingResponse,
    StreamingSTTRequest,
    RealtimeSpeechDialogueRequest,
)
from app.model_gateway.streaming import ValidatedSTTStream
from app.model_gateway.tts_streaming import TTSStreamEvent, ValidatedTTSStream
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


async def _abort_unclaimed_stream(provider_stream: Any, timeout_s: float, *,
                                  completion: Optional[asyncio.Future] = None) -> None:
    """Release an opened stream when validation/audit prevents handoff.

    Shield a separately bounded cleanup so a second caller cancellation cannot
    orphan the provider socket. Cleanup must never mask the original failure.
    """
    def finished(done=None):
        released = done is not None and not done.cancelled() and done.exception() is None
        if completion is not None and not completion.done():
            completion.set_result(released)

    async def cleanup() -> None:
        abort_task = None
        try:
            abort_task = asyncio.create_task(provider_stream.abort())
            # A bounded wrapper can return while a provider ignores cancel.
            # Ownership ends only when the actual abort has finished.
            abort_task.add_done_callback(finished)
            done, _ = await asyncio.wait({abort_task}, timeout=max(0.01, min(2.0, timeout_s)))
            if done:
                abort_task.result()
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            if abort_task is not None:
                if not abort_task.done():
                    abort_task.cancel()
            else:
                finished()

    task = asyncio.create_task(cleanup())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # The task continues only for its own bounded cleanup deadline.
        pass


@dataclass
class _CircuitState:
    failures: int = 0
    opened_at: Optional[float] = None


class CircuitBreaker:
    """Process-local circuit state; the invocation module owns its policy semantics."""

    def __init__(self, time_source: Callable[[], float] = monotonic) -> None:
        self._time_source = time_source
        self._states: Dict[str, _CircuitState] = {}

    def is_open(self, key: str, *, threshold: int, recovery_seconds: float) -> bool:
        if threshold <= 0:
            return False
        state = self._states.get(key)
        if state is None or state.failures < threshold or state.opened_at is None:
            return False
        if self._time_source() - state.opened_at >= max(0.0, recovery_seconds):
            self._states.pop(key, None)
            return False
        return True

    def record_failure(self, key: str, *, threshold: int) -> None:
        if threshold <= 0:
            return
        state = self._states.setdefault(key, _CircuitState())
        state.failures += 1
        if state.failures >= threshold and state.opened_at is None:
            state.opened_at = self._time_source()

    def record_success(self, key: str) -> None:
        self._states.pop(key, None)


_PROCESS_CIRCUITS = CircuitBreaker()


class PersistentCircuitBreaker:
    """Database-backed breaker state shared by every application instance."""

    def __init__(self, persistence: Persistence, organization_id: str = "org_default") -> None:
        self.persistence = persistence
        self.organization_id = organization_id

    def is_open(self, key: str, *, threshold: int, recovery_seconds: float) -> bool:
        if threshold <= 0:
            return False
        item_id = self._id(key)
        organization_id = self._organization_for(key)
        with self.persistence.transaction(organization_id) as transaction:
            state = transaction.model_circuit_states.get(item_id)
            if state is None or int(state.get("failures", 0)) < threshold or not state.get("opened_at"):
                return False
            opened = datetime.fromisoformat(str(state["opened_at"]).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - opened >= timedelta(seconds=max(0.0, recovery_seconds)):
                state["failures"] = 0
                state["opened_at"] = None
                state["updated_at"] = utc_now()
                transaction.model_circuit_states.update(state, expected_version=state["version"])
                return False
            return True

    def record_failure(self, key: str, *, threshold: int) -> None:
        if threshold <= 0:
            return
        item_id = self._id(key)
        organization_id = self._organization_for(key)
        with self.persistence.transaction(organization_id) as transaction:
            state = transaction.model_circuit_states.get(item_id)
            if state is None:
                transaction.model_circuit_states.add(
                    {
                        "id": item_id,
                        "organization_id": organization_id,
                        "circuit_key_hash": item_id.removeprefix("circuit_"),
                        "failures": 1,
                        "opened_at": utc_now() if threshold <= 1 else None,
                        "updated_at": utc_now(),
                    }
                )
                return
            state["failures"] = int(state.get("failures", 0)) + 1
            if state["failures"] >= threshold and not state.get("opened_at"):
                state["opened_at"] = utc_now()
            state["updated_at"] = utc_now()
            transaction.model_circuit_states.update(state, expected_version=state["version"])

    def record_success(self, key: str) -> None:
        item_id = self._id(key)
        organization_id = self._organization_for(key)
        with self.persistence.transaction(organization_id) as transaction:
            state = transaction.model_circuit_states.get(item_id)
            if state is None:
                return
            state["failures"] = 0
            state["opened_at"] = None
            state["updated_at"] = utc_now()
            transaction.model_circuit_states.update(state, expected_version=state["version"])

    def _id(self, key: str) -> str:
        return "circuit_%s" % hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _organization_for(self, key: str) -> str:
        return key.split(":", 1)[0] or self.organization_id


class ModelGateway:
    """Deep Model Invocation module behind one capability/request interface."""

    def __init__(
        self,
        store: InMemoryStore,
        provider_clients: Optional[Dict[str, Any]] = None,
        *,
        persistence: Optional[Persistence] = None,
        provider_registry: Optional[ProviderRegistry] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.providers = provider_registry or ProviderRegistry(provider_clients)
        self.circuits = circuit_breaker or PersistentCircuitBreaker(self.persistence)

    def tts_routing_snapshot(self, request: TTSSynthesizeRequest) -> Dict[str, Any]:
        """Resolve a ready primary and an opaque identity for fixed speech.

        A cache cannot authorize a disabled configuration or silently reuse
        another voice. Credentials are read only to invalidate the fingerprint
        on rotation; neither credentials nor connection settings leave here.
        """
        self._validate_request(cap.TTS_SYNTHESIZE, request)
        route = self._resolve_route(request.organization_id, cap.TTS_SYNTHESIZE, request.purpose)
        if (not route.get("enabled", True)
                or route.get("organization_id", request.organization_id) != request.organization_id
                or route.get("capability") != cap.TTS_SYNTHESIZE
                or route.get("purpose") != request.purpose):
            raise ProviderError("provider_route_invalid", "TTS route is unavailable.", retryable=False)
        model, connection, credentials = self._model_connection(
            request.organization_id,
            str((route.get("primary") or {}).get("model_configuration_id") or ""),
            cap.TTS_SYNTHESIZE,
        )
        if (model.get("organization_id") != request.organization_id
                or connection.get("organization_id") != request.organization_id
                or model.get("provider_id") != connection.get("provider_id")
                or model.get("model_type") != cap.CAPABILITY_MODEL_TYPES[cap.TTS_SYNTHESIZE]):
            raise ProviderError("provider_route_invalid", "TTS configuration identity is inconsistent.", retryable=False)
        if connection.get("credential_status") == "invalid":
            raise ProviderError("provider_auth_failed", "TTS connection credentials have failed validation.", retryable=False)
        # Resolve the plugin too: a ready row alone does not prove support.
        adapter = self.providers.adapter(model["provider_id"], cap.TTS_SYNTHESIZE)
        if model["provider_id"] == "mock" or not callable(getattr(adapter, "open_tts_stream", None)):
            raise ProviderError("provider_streaming_not_supported", "Fixed speech requires a real PCM provider.", retryable=False)
        # Model defaults may tune speech, never change the tenant, purpose or
        # approved text. Freeze validated effective values before cache keys
        # and the later provider call apply defaults a second time.
        defaults = {key: value for key, value in (model.get("default_parameters") or {}).items()
                    if key in {"language", "voice_profile_id", "format", "speaking_rate"}}
        effective = self._apply_model_defaults(request, defaults)
        try:
            effective = TTSSynthesizeRequest.model_validate(effective.model_dump(warnings=False))
        except ValueError:
            # Invalid defaults can contain private configuration values; do
            # not emit serializer warnings or expose validation input values.
            raise ProviderError("provider_bad_request", "TTS defaults do not satisfy the speech request contract.", retryable=False) from None
        fingerprint = hashlib.sha256(json.dumps({
            "route": route, "model": model, "connection": connection,
            "credential_fingerprint": hashlib.sha256(json.dumps(credentials, sort_keys=True).encode()).hexdigest(),
        }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        primary_route = deepcopy(route)
        primary_route["fallbacks"] = []
        primary_route["policy"] = {**(primary_route.get("policy") or {}), "fallback_on": []}
        return {
            "route": primary_route,
            "identity": {"organization_id": request.organization_id,
                         "provider_id": model["provider_id"], "model": model["provider_model_id"],
                         "fingerprint": fingerprint},
            "request": effective,
        }

    async def invoke(
        self, capability: str, request: InvocationRequest, *, route: Optional[Dict[str, Any]] = None,
    ) -> InvocationResponse:
        metadata = safe_model_call_context(getattr(request, "metadata", None))
        with model_call_scope(interview_id=metadata.get("interview_id"), turn_id=metadata.get("turn_id"),
                              capture_id=metadata.get("capture_id"), stage=metadata.get("stage") or "provider"):
            return await self._invoke(capability, request, route=route)

    async def _invoke(
        self,
        capability: str,
        request: InvocationRequest,
        *,
        route: Optional[Dict[str, Any]] = None,
    ) -> InvocationResponse:
        self._validate_request(capability, request)
        purpose = request.purpose
        resolved_route = deepcopy(route) if route is not None else self._resolve_route(
            request.organization_id,
            capability,
            purpose,
        )
        if resolved_route.get("capability") != capability:
            raise ProviderError(
                "provider_route_invalid",
                "Model route capability does not match the invocation capability.",
                retryable=False,
            )
        if resolved_route.get("organization_id", request.organization_id) != request.organization_id:
            raise ProviderError(
                "provider_route_invalid",
                "Model route belongs to a different organization.",
                retryable=False,
            )
        invocation_id = new_id("model_invocation")
        request_hash = self._request_hash(request)
        targets = [resolved_route.get("primary") or {}] + list(resolved_route.get("fallbacks") or [])
        policy = resolved_route.get("policy") or {}
        retry_count = min(3, max(0, int(policy.get("retry_count", 0))))
        execution_budget = getattr(request, "execution_budget", None)
        if execution_budget is not None:
            retry_count = min(retry_count, execution_budget.max_provider_retries)
        attempt_limit = execution_budget.max_provider_attempts if execution_budget is not None else None
        backoff_ms = min(1000, max(0, int(policy.get("retry_backoff_ms", 0))))
        circuit_threshold = max(0, int(policy.get("circuit_failure_threshold", 3)))
        circuit_recovery_seconds = max(0.0, float(policy.get("circuit_recovery_seconds", 30)))
        total_attempts = 0
        last_error: Optional[ProviderError] = None

        for fallback_index, target in enumerate(targets):
            model_configuration_id = str(target.get("model_configuration_id") or "")
            model = "unknown"
            timeout_s = execution_budget.timeout_s if execution_budget is not None else max(0.01, float(target.get("timeout_s", 20)))
            provider_id = "unknown"
            provider_connection_id = "unknown"
            circuit_key = "%s:%s:%s" % (request.organization_id, model_configuration_id, capability)

            for attempt in range(1, retry_count + 2):
                total_attempts += 1
                started_at = perf_counter()
                model_configuration: Dict[str, Any] = {}
                diagnostic_scope = current_model_call_scope()
                diagnostic_fields = {**safe_model_call_context(request.metadata, diagnostic_scope),
                                     **model_timing_fields(timeout_s), "provider_invoked": False}
                attempt_started = False
                audit_started = False

                def observe_attempt(event, **details):
                    record_model_diagnostic(event, **{
                        **diagnostic_fields, "invocation_id": invocation_id,
                        "capability": capability, "purpose": purpose,
                        "organization_id": request.organization_id, "route_id": resolved_route.get("id"),
                        "provider_connection_id": provider_connection_id,
                        "model_configuration_id": model_configuration_id, "provider_id": provider_id,
                        "model": model, "attempt": attempt, "fallback_index": fallback_index,
                        "elapsed_ms": int((perf_counter() - started_at) * 1000), **details,
                    })

                try:
                    model_configuration, provider_connection, credentials = self._model_connection(
                        request.organization_id,
                        model_configuration_id,
                        capability,
                        allow_unready=resolved_route.get("id") == "model_configuration_test",
                    )
                    provider_id = model_configuration["provider_id"]
                    provider_connection_id = model_configuration["provider_connection_id"]
                    model = model_configuration["provider_model_id"]
                    if capability == cap.STT_BATCH and provider_id == "mock":
                        fixture = request.metadata.get("development_transcript")
                        if (
                            os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production"
                            or not isinstance(fixture, str)
                            or not fixture.strip()
                        ):
                            raise ProviderError(
                                "provider_real_stt_required",
                                "Real audio repair requires a non-mock STT route; mock STT only accepts explicit development fixtures.",
                                retryable=False,
                            )
                    if self.circuits.is_open(
                        circuit_key,
                        threshold=circuit_threshold,
                        recovery_seconds=circuit_recovery_seconds,
                    ):
                        raise ProviderError(
                            "provider_circuit_open",
                            "Provider circuit is open for this capability.",
                            retryable=True,
                        )
                    adapter = self.providers.adapter(provider_id, capability)
                    context = ProviderContext(
                        organization_id=request.organization_id,
                        invocation_id=invocation_id,
                        route_id=str(resolved_route.get("id") or "route_inline"),
                        provider_connection_id=provider_connection_id,
                        model_configuration_id=model_configuration_id,
                        model_type=model_configuration["model_type"],
                        capability=capability,
                        purpose=purpose,
                        model=model,
                        timeout_s=timeout_s,
                        attempt=attempt,
                        fallback_index=fallback_index,
                        connection_config=provider_connection.get("connection_config") or {},
                        model_settings=model_configuration.get("settings") or {},
                        default_parameters=model_configuration.get("default_parameters") or {},
                        credentials=credentials,
                        metadata=request.metadata,
                    )
                    effective_request = self._apply_model_defaults(request, context.default_parameters)
                    diagnostic_fields["provider_invoked"] = True
                    observe_attempt("attempt_started", outcome="started")
                    attempt_started = True
                    try:
                        response = await asyncio.wait_for(
                            adapter.invoke(capability, effective_request, context),
                            timeout=timeout_s,
                        )
                    except asyncio.TimeoutError as exc:
                        raise ProviderError(
                            "provider_timeout",
                            "Provider request exceeded the invocation timeout.",
                            retryable=True,
                        ) from exc
                    self._validate_response(capability, request, response)
                    latency_ms = int((perf_counter() - started_at) * 1000)
                    response.provider.provider_id = provider_id
                    response.provider.model = model
                    response.provider.latency_ms = latency_ms
                    estimated_cost = self._estimated_cost(response, model_configuration, target)
                    max_cost = policy.get("max_cost_usd_per_call")
                    if max_cost is not None and estimated_cost > float(max_cost):
                        raise ProviderError(
                            "provider_cost_limit_exceeded",
                            "Provider response exceeded the configured per-call cost limit.",
                            retryable=False,
                        )
                    observe_attempt("attempt_finished", outcome="fallback_success" if fallback_index else "success",
                                    provider_request_id=response.provider.request_id)
                    audit_started = True
                    self._log_invocation(
                        invocation_id=invocation_id,
                        organization_id=request.organization_id,
                        capability=capability,
                        purpose=purpose,
                        route=resolved_route,
                        provider_connection_id=provider_connection_id,
                        model_configuration_id=model_configuration_id,
                        provider_id=provider_id,
                        model=model,
                        status="fallback_success" if fallback_index else "success",
                        latency_ms=latency_ms,
                        attempt=attempt,
                        fallback_index=fallback_index,
                        request_hash=request_hash,
                        response=response,
                        estimated_cost_usd=estimated_cost,
                        prompt_version=request.metadata.get("prompt_version"),
                        diagnostic_context={**diagnostic_fields, "outcome": "fallback_success" if fallback_index else "success"},
                    )
                    self.circuits.record_success(circuit_key)
                    return response
                except asyncio.CancelledError:
                    # Caller cancellation is not a provider outage or permission
                    # to retry. Its owning deadline is correlated by scope ID.
                    if not attempt_started:
                        observe_attempt("attempt_started", outcome="started")
                    reason = diagnostic_scope.cancel_reason if diagnostic_scope else "caller_cancelled"
                    cancelled_fields = {**diagnostic_fields, "cancel_reason": reason, "outcome": "cancelled"}
                    observe_attempt("attempt_finished", outcome="cancelled", cancel_reason=reason,
                                    error_code="provider_cancelled")
                    try:
                        self._log_invocation(
                            invocation_id=invocation_id, organization_id=request.organization_id,
                            capability=capability, purpose=purpose, route=resolved_route,
                            provider_connection_id=provider_connection_id, model_configuration_id=model_configuration_id,
                            provider_id=provider_id, model=model, status="cancelled",
                            latency_ms=int((perf_counter() - started_at) * 1000), attempt=attempt,
                            fallback_index=fallback_index, request_hash=request_hash, error_code="provider_cancelled",
                            error_details={"transport_outcome": "cancelled"},
                            prompt_version=request.metadata.get("prompt_version"), diagnostic_context=cancelled_fields,
                        )
                    except Exception:
                        observe_attempt("audit_failed", outcome="cancelled", cancel_reason=reason)
                    raise
                except ProviderError as exc:
                    if audit_started:
                        observe_attempt("audit_failed", outcome="success")
                        raise
                    last_error = exc
                    if not attempt_started:
                        observe_attempt("attempt_started", outcome="started")
                    observe_attempt("attempt_finished", outcome="failed", error_code=exc.code,
                                    retryable=exc.retryable, provider_request_id=exc.details.get("provider_request_id"))
                    try:
                        self._log_invocation(
                            invocation_id=invocation_id,
                            organization_id=request.organization_id,
                            capability=capability,
                            purpose=purpose,
                            route=resolved_route,
                            provider_connection_id=provider_connection_id,
                            model_configuration_id=model_configuration_id,
                            provider_id=provider_id,
                            model=model,
                            status="failed",
                            latency_ms=int((perf_counter() - started_at) * 1000),
                            attempt=attempt,
                            fallback_index=fallback_index,
                            request_hash=request_hash,
                            error_code=exc.code,
                            error_details=exc.details,
                            prompt_version=request.metadata.get("prompt_version"),
                            diagnostic_context={**diagnostic_fields, "outcome": "failed"},
                        )
                    except Exception:
                        # Optional audit failures cannot replace the provider's
                        # original code or change its retry/fallback decision.
                        observe_attempt("audit_failed", outcome="failed", error_code=exc.code)
                    can_retry = (exc.retryable and exc.code != "provider_circuit_open" and attempt <= retry_count
                                 and (attempt_limit is None or total_attempts < attempt_limit))
                    if can_retry:
                        if backoff_ms:
                            await asyncio.sleep((backoff_ms * attempt) / 1000.0)
                        continue
                    if exc.retryable and exc.code != "provider_circuit_open":
                        self.circuits.record_failure(circuit_key, threshold=circuit_threshold)
                    break
                except Exception:
                    if not attempt_started:
                        observe_attempt("attempt_started", outcome="started")
                    observe_attempt("audit_failed" if audit_started else "attempt_finished",
                                    outcome="failed", error_code="provider_unexpected_error")
                    if not audit_started:
                        try:
                            self._log_invocation(
                                invocation_id=invocation_id, organization_id=request.organization_id,
                                capability=capability, purpose=purpose, route=resolved_route,
                                provider_connection_id=provider_connection_id, model_configuration_id=model_configuration_id,
                                provider_id=provider_id, model=model, status="failed",
                                latency_ms=int((perf_counter() - started_at) * 1000), attempt=attempt,
                                fallback_index=fallback_index, request_hash=request_hash,
                                error_code="provider_unexpected_error", prompt_version=request.metadata.get("prompt_version"),
                                diagnostic_context={**diagnostic_fields, "outcome": "failed"},
                            )
                        except Exception:
                            observe_attempt("audit_failed", outcome="failed", error_code="provider_unexpected_error")
                    # Keep unexpected adapter/audit errors unchanged. In
                    # particular they do not purchase retries or open circuits.
                    raise

            if last_error is None:
                break
            has_fallback = fallback_index + 1 < len(targets)
            if (not has_fallback or not self._allows_fallback(last_error, policy)
                    or (attempt_limit is not None and total_attempts >= attempt_limit)):
                self._annotate_error(last_error, invocation_id, resolved_route, total_attempts)
                raise last_error

        error = last_error or ProviderError(
            "provider_route_invalid",
            "Model route does not contain an invokable target.",
            retryable=False,
        )
        self._annotate_error(error, invocation_id, resolved_route, total_attempts)
        raise error

    async def open_tts_stream(
        self,
        request: TTSSynthesizeRequest,
        *,
        route: Optional[Dict[str, Any]] = None,
        first_audio_deadline: Optional[float] = None,
    ) -> ValidatedTTSStream:
        """Stream approved TTS text through the existing synthesis route policy.

        Retries/fallbacks may occur while opening, before any PCM is exposed.
        After returning, the managed stream never replays an invocation.
        """
        caller_task = asyncio.current_task()
        if hasattr(caller_task, "tts_cleanup_completion"):
            del caller_task.tts_cleanup_completion
        cleanup_completions = []

        def register_cleanup():
            completion = asyncio.get_running_loop().create_future()
            cleanup_completions.append(completion)
            joined = asyncio.get_running_loop().create_future()

            def finish(_=None):
                if not joined.done() and all(item.done() for item in cleanup_completions):
                    errors = [getattr(item, "provider_error", None) for item in cleanup_completions]
                    errors = [error for error in errors if isinstance(error, ProviderError)]
                    if errors:
                        joined.provider_error = next((error for error in errors if not error.retryable), errors[0])
                    joined.set_result(all(not item.cancelled() and item.result() is True
                                          for item in cleanup_completions))

            for item in cleanup_completions:
                item.add_done_callback(finish)
            caller_task.tts_cleanup_completion = joined
            return completion

        previous_error = getattr(caller_task, "_tts_gateway_provider_error", None)
        if previous_error is not None:
            if getattr(caller_task, "provider_error", None) is previous_error:
                del caller_task.provider_error
            del caller_task._tts_gateway_provider_error
        capability = cap.TTS_SYNTHESIZE
        self._validate_request(capability, request)
        if not request.text.strip():
            raise ProviderError("provider_bad_request", "TTS text cannot be blank.", retryable=False)
        resolved_route = deepcopy(route) if route is not None else self._resolve_route(
            request.organization_id, capability, request.purpose
        )
        if (
            resolved_route.get("capability") != capability
            or resolved_route.get("organization_id", request.organization_id) != request.organization_id
            or not resolved_route.get("enabled", True)
        ):
            raise ProviderError("provider_route_invalid", "TTS stream route is disabled or outside the invocation scope.", retryable=False)
        invocation_id = new_id("model_invocation")
        request_hash = self._request_hash(request)
        targets = [resolved_route.get("primary") or {}] + list(resolved_route.get("fallbacks") or [])
        policy = resolved_route.get("policy") or {}
        retry_count = min(3, max(0, int(policy.get("retry_count", 0))))
        backoff_ms = min(1000, max(0, int(policy.get("retry_backoff_ms", 0))))
        threshold = max(0, int(policy.get("circuit_failure_threshold", 3)))
        recovery = max(0.0, float(policy.get("circuit_recovery_seconds", 30)))
        total_attempts = 0
        last_error = None

        def remaining_attempts(fallback_index: int, attempt: int) -> int:
            # Reserve time only for fallbacks that can actually open. All route
            # eligibility checks still run again immediately before their call.
            count = retry_count + 2 - attempt
            # A shorter slice can itself cause a timeout. Do not sacrifice the
            # primary's time for a fallback that this timeout cannot reach.
            if not self._allows_fallback(ProviderError("provider_timeout", "TTS open timed out.", retryable=True), policy):
                return count
            for candidate in targets[fallback_index + 1:]:
                candidate_id = str(candidate.get("model_configuration_id") or "")
                try:
                    configuration, _, _ = self._model_connection(
                        request.organization_id, candidate_id, capability,
                        allow_unready=resolved_route.get("id") == "model_configuration_test",
                    )
                    adapter = self.providers.adapter(configuration["provider_id"], capability)
                    key = "%s:%s:%s" % (request.organization_id, candidate_id, capability)
                    if (callable(getattr(adapter, "open_tts_stream", None))
                            and not self.circuits.is_open(key, threshold=threshold, recovery_seconds=recovery)):
                        count += retry_count + 1
                except ProviderError:
                    continue
            return max(1, count)

        for fallback_index, target in enumerate(targets):
            model_configuration_id = str(target.get("model_configuration_id") or "")
            timeout_s = max(0.01, float(target.get("timeout_s", 20)))
            provider_id = provider_connection_id = model = "unknown"
            circuit_key = "%s:%s:%s" % (request.organization_id, model_configuration_id, capability)
            for attempt in range(1, retry_count + 2):
                if first_audio_deadline is not None and monotonic() >= first_audio_deadline:
                    error = ProviderError("provider_timeout", "TTS first audio budget exhausted.", retryable=False)
                    self._annotate_error(error, invocation_id, resolved_route, total_attempts)
                    raise error
                total_attempts += 1
                started_at = perf_counter()
                provider_stream = None
                provider_task = None
                cleanup_started = False
                attempted = False
                attempt_diagnostics = {}
                attempt_state = {}
                log_fields = dict(
                    invocation_id=invocation_id, organization_id=request.organization_id,
                    capability=capability, purpose=request.purpose, route=resolved_route,
                    model_configuration_id=model_configuration_id, attempt=attempt,
                    fallback_index=fallback_index, request_hash=request_hash,
                )

                async def cancel_open():
                    nonlocal cleanup_started
                    if provider_task is None or provider_stream is not None or cleanup_started:
                        return
                    cleanup_started = True
                    completion = register_cleanup()

                    def discard_result(done, cleanup_timeout=timeout_s, completion=completion):
                        # A transport may finish opening while cancellation is
                        # being delivered. It was never handed to a consumer.
                        try:
                            source = done.result()
                        except (Exception, asyncio.CancelledError) as exc:
                            reported = exc if isinstance(exc, ProviderError) else getattr(done, "provider_error", None)
                            if isinstance(reported, ProviderError):
                                # Late cleanup never writes into a caller Task
                                # that may already be running another request.
                                completion.provider_error = reported
                            if not completion.done():
                                completion.set_result(True)
                            return
                        asyncio.create_task(_abort_unclaimed_stream(source, cleanup_timeout, completion=completion))

                    provider_task.add_done_callback(discard_result)
                    provider_task.cancel()
                    cleanup_budget = 1.0
                    if first_audio_deadline is not None:
                        cleanup_budget = min(cleanup_budget, max(0, first_audio_deadline - monotonic()))
                    try:
                        await asyncio.wait({provider_task}, timeout=cleanup_budget)
                    finally:
                        diagnostics = getattr(provider_task, "provider_diagnostics", {})
                        if isinstance(diagnostics, dict):
                            attempt_diagnostics.update(diagnostics)
                        reported_error = getattr(provider_task, "provider_error", None)
                        if provider_task.done() and not provider_task.cancelled():
                            completed_error = provider_task.exception()
                            if isinstance(completed_error, ProviderError):
                                reported_error = completed_error
                        if isinstance(reported_error, ProviderError):
                            attempt_state["provider_error"] = reported_error

                async def release_unclaimed():
                    if provider_stream is None:
                        return
                    completion = register_cleanup()
                    cleanup = asyncio.create_task(_abort_unclaimed_stream(provider_stream, timeout_s, completion=completion))
                    if first_audio_deadline is None:
                        await asyncio.shield(cleanup)
                    else:
                        await asyncio.wait({cleanup}, timeout=max(0, first_audio_deadline - monotonic()))

                try:
                    configuration, connection, credentials = self._model_connection(
                        request.organization_id, model_configuration_id, capability,
                        allow_unready=resolved_route.get("id") == "model_configuration_test",
                    )
                    provider_id = configuration["provider_id"]
                    provider_connection_id = configuration["provider_connection_id"]
                    model = configuration["provider_model_id"]
                    if self.circuits.is_open(circuit_key, threshold=threshold, recovery_seconds=recovery):
                        raise ProviderError("provider_circuit_open", "Provider circuit is open for this capability.", retryable=True)
                    adapter = self.providers.adapter(provider_id, capability)
                    open_stream = getattr(adapter, "open_tts_stream", None)
                    if not callable(open_stream):
                        raise ProviderError("provider_streaming_not_supported", "Provider adapter does not implement TTS PCM streaming.", retryable=False)
                    attempt_timeout = timeout_s
                    if first_audio_deadline is not None:
                        slots = remaining_attempts(fallback_index, attempt)
                        remaining = first_audio_deadline - monotonic()
                        if remaining <= 0:
                            raise ProviderError("provider_timeout", "TTS first audio budget exhausted.", retryable=False)
                        attempt_timeout = min(timeout_s, remaining / slots)
                    context = ProviderContext(
                        organization_id=request.organization_id, invocation_id=invocation_id,
                        route_id=str(resolved_route.get("id") or "route_inline"),
                        provider_connection_id=provider_connection_id,
                        model_configuration_id=model_configuration_id,
                        model_type=configuration["model_type"], capability=capability,
                        purpose=request.purpose, model=model,
                        timeout_s=attempt_timeout,
                        attempt=attempt, fallback_index=fallback_index,
                        connection_config=connection.get("connection_config") or {},
                        model_settings=configuration.get("settings") or {},
                        default_parameters=configuration.get("default_parameters") or {},
                        credentials=credentials, metadata=request.metadata,
                    )
                    async def open_attempt(open_stream=open_stream, context=context, diagnostics=attempt_diagnostics, state=attempt_state):
                        try:
                            return await open_stream(self._apply_model_defaults(request, context.default_parameters), context)
                        except asyncio.CancelledError as exc:
                            cancellation_details = getattr(exc, "provider_diagnostics", None)
                            if not isinstance(cancellation_details, dict):
                                cancellation_details = getattr(asyncio.current_task(), "provider_diagnostics", {})
                            if isinstance(cancellation_details, dict):
                                diagnostics.update(cancellation_details)
                            reported_error = getattr(exc, "provider_error", None)
                            if not isinstance(reported_error, ProviderError):
                                reported_error = getattr(asyncio.current_task(), "provider_error", None)
                            if isinstance(reported_error, ProviderError):
                                state["provider_error"] = reported_error
                            raise

                    try:
                        attempted = True
                        provider_task = asyncio.create_task(open_attempt())
                        done, _ = await asyncio.wait({provider_task}, timeout=context.timeout_s)
                        if not done:
                            await cancel_open()
                            raise asyncio.TimeoutError()
                        provider_stream = provider_task.result()
                    except asyncio.TimeoutError as exc:
                        reported_error = attempt_state.get("provider_error")
                        raise ProviderError(
                            reported_error.code if reported_error else "provider_timeout",
                            "TTS stream could not open within its time budget.",
                            retryable=reported_error.retryable if reported_error else True,
                            details={**(reported_error.details if reported_error else {}),
                                     **attempt_diagnostics, "transport_outcome": "timeout"},
                        ) from exc
                    log_fields.update(provider_connection_id=provider_connection_id, provider_id=provider_id, model=model)

                    def terminal(status: str, event: Optional[TTSStreamEvent], error: Optional[ProviderError]):
                        cost = self._estimated_cost(event, configuration, target) if event else 0.0
                        maximum = policy.get("max_cost_usd_per_call")
                        if event is not None and maximum is not None and cost > float(maximum):
                            raise ProviderError("provider_cost_limit_exceeded", "TTS response exceeded the configured per-call cost limit.", retryable=False)
                        if error:
                            self._annotate_error(error, invocation_id, resolved_route, total_attempts)
                        self._log_invocation(
                            **log_fields, status="stream_%s" % status,
                            latency_ms=int((perf_counter() - started_at) * 1000),
                            response=event, estimated_cost_usd=cost,
                            error_code=error.code if error else "",
                            error_details=error.details if error else None,
                        )
                        if status == "success":
                            self.circuits.record_success(circuit_key)
                        elif error and error.retryable and error.code != "provider_circuit_open":
                            self.circuits.record_failure(circuit_key, threshold=threshold)

                    stream = ValidatedTTSStream(
                        provider_stream, provider_id=provider_id, model=model,
                        read_timeout_s=min(8.0, timeout_s), on_terminal=terminal,
                        first_audio_deadline=first_audio_deadline,
                    )
                    self._log_invocation(
                        **log_fields,
                        status="stream_fallback_opened" if fallback_index else "stream_opened",
                        latency_ms=int((perf_counter() - started_at) * 1000),
                    )
                    return stream
                except asyncio.CancelledError as cancelled:
                    # An outer first-audio watchdog may win the race against
                    # wait_for. Keep that deadline observable without changing
                    # cancellation into a normal ProviderError return.
                    expired = first_audio_deadline is not None and monotonic() >= first_audio_deadline
                    try:
                        await cancel_open()
                    except asyncio.CancelledError:
                        # A repeated cancellation is still propagated after
                        # recording this attempt; cleanup already owns the task.
                        pass
                    reported_error = attempt_state.get("provider_error")
                    if isinstance(reported_error, ProviderError):
                        cancelled.provider_error = reported_error
                        caller_task.provider_error = reported_error
                        caller_task._tts_gateway_provider_error = reported_error
                    log_fields.update(provider_connection_id=provider_connection_id, provider_id=provider_id, model=model)
                    try:
                        self._log_invocation(
                            **log_fields, status="failed" if expired else "stream_cancelled",
                            latency_ms=int((perf_counter() - started_at) * 1000),
                            error_code=reported_error.code if reported_error else "provider_timeout" if expired else "provider_cancelled",
                            error_details={**(reported_error.details if reported_error else {}), **attempt_diagnostics,
                                           "transport_outcome": "timeout" if expired else "cancelled"},
                        )
                        if expired and attempted and (reported_error is None or reported_error.retryable):
                            self.circuits.record_failure(circuit_key, threshold=threshold)
                    except Exception:
                        # A failing audit sink must not replace cancellation.
                        pass
                    finally:
                        await release_unclaimed()
                    raise
                except ProviderError as exc:
                    exc.details = {"fallback_allowed": True, **exc.details, "audio_started": False}
                    last_error = exc
                    log_fields.update(provider_connection_id=provider_connection_id, provider_id=provider_id, model=model)
                    self._log_invocation(
                        **log_fields, status="failed",
                        latency_ms=int((perf_counter() - started_at) * 1000),
                        error_code=exc.code, error_details=exc.details,
                    )
                    if attempted and exc.retryable and exc.code != "provider_circuit_open":
                        self.circuits.record_failure(circuit_key, threshold=threshold)
                    await release_unclaimed()
                    if exc.retryable and exc.code != "provider_circuit_open" and attempt <= retry_count:
                        if self.circuits.is_open(circuit_key, threshold=threshold, recovery_seconds=recovery):
                            # This call already has a real failure. Stop trying
                            # this target without replacing its cause, which
                            # still governs whether a fallback is permitted.
                            break
                        if backoff_ms:
                            delay = backoff_ms * attempt / 1000.0
                            if first_audio_deadline is not None:
                                delay = min(delay, max(0, first_audio_deadline - monotonic())
                                            / (remaining_attempts(fallback_index, attempt + 1) + 1))
                            await asyncio.sleep(delay)
                        continue
                    break
            if fallback_index + 1 >= len(targets) or not self._allows_fallback(last_error, policy):
                self._annotate_error(last_error, invocation_id, resolved_route, total_attempts)
                raise last_error
        raise ProviderError("provider_route_invalid", "TTS route contains no invokable target.", retryable=False)

    async def open_stream(
        self,
        request: StreamingSTTRequest,
        *,
        route: Optional[Dict[str, Any]] = None,
    ) -> ValidatedSTTStream:
        """Open a streaming STT session; fallback is allowed only before audio is accepted."""
        capability = cap.STT_STREAMING
        resolved_route = deepcopy(route) if route is not None else self._resolve_route(
            request.organization_id, capability, request.purpose
        )
        if resolved_route.get("capability") != capability:
            raise ProviderError("provider_route_invalid", "STT stream route capability is invalid.", retryable=False)
        targets = [resolved_route.get("primary") or {}] + list(resolved_route.get("fallbacks") or [])
        invocation_id = new_id("model_invocation")
        request_hash = self._request_hash(request)
        last_error: Optional[ProviderError] = None
        for fallback_index, target in enumerate(targets):
            model_configuration_id = str(target.get("model_configuration_id") or "")
            model = "unknown"
            timeout_s = max(0.01, float(target.get("timeout_s", 10)))
            provider_id = "unknown"
            provider_connection_id = "unknown"
            started_at = perf_counter()
            try:
                model_configuration, provider_connection, credentials = self._model_connection(
                    request.organization_id,
                    model_configuration_id,
                    capability,
                    allow_unready=resolved_route.get("id") == "model_configuration_test",
                )
                provider_id = model_configuration["provider_id"]
                provider_connection_id = model_configuration["provider_connection_id"]
                model = model_configuration["provider_model_id"]
                adapter = self.providers.adapter(provider_id, capability)
                open_stream = getattr(adapter, "open_stream", None)
                if not callable(open_stream):
                    raise ProviderError(
                        "provider_streaming_not_supported",
                        "Provider adapter does not implement open_stream.",
                        retryable=False,
                    )
                context = ProviderContext(
                    organization_id=request.organization_id,
                    invocation_id=invocation_id,
                    route_id=str(resolved_route.get("id") or "route_inline"),
                    provider_connection_id=provider_connection_id,
                    model_configuration_id=model_configuration_id,
                    model_type=model_configuration["model_type"],
                    capability=capability,
                    purpose=request.purpose,
                    model=model,
                    timeout_s=timeout_s,
                    attempt=1,
                    fallback_index=fallback_index,
                    connection_config=provider_connection.get("connection_config") or {},
                    model_settings=model_configuration.get("settings") or {},
                    default_parameters=model_configuration.get("default_parameters") or {},
                    credentials=credentials,
                    metadata=request.metadata,
                )
                try:
                    provider_stream = await asyncio.wait_for(open_stream(request, context), timeout=timeout_s)
                except asyncio.TimeoutError as exc:
                    raise ProviderError("provider_timeout", "Provider stream open timed out.", retryable=True) from exc
                try:
                    stream = ValidatedSTTStream(provider_stream, request)
                    self._log_invocation(
                        invocation_id=invocation_id,
                        organization_id=request.organization_id,
                        capability=capability,
                        purpose=request.purpose,
                        route=resolved_route,
                        provider_connection_id=provider_connection_id,
                        model_configuration_id=model_configuration_id,
                        provider_id=provider_id,
                        model=model,
                        status="stream_opened" if not fallback_index else "stream_fallback_opened",
                        latency_ms=int((perf_counter() - started_at) * 1000),
                        attempt=1,
                        fallback_index=fallback_index,
                        request_hash=request_hash,
                    )
                except BaseException:
                    await _abort_unclaimed_stream(provider_stream, timeout_s)
                    raise
                return stream
            except ProviderError as exc:
                last_error = exc
                self._log_invocation(
                    invocation_id=invocation_id,
                    organization_id=request.organization_id,
                    capability=capability,
                    purpose=request.purpose,
                    route=resolved_route,
                    provider_connection_id=provider_connection_id,
                    model_configuration_id=model_configuration_id,
                    provider_id=provider_id,
                    model=model,
                    status="failed",
                    latency_ms=int((perf_counter() - started_at) * 1000),
                    attempt=1,
                    fallback_index=fallback_index,
                    request_hash=request_hash,
                    error_code=exc.code,
                    error_details=exc.details,
                )
                if not exc.retryable or fallback_index + 1 >= len(targets):
                    self._annotate_error(exc, invocation_id, resolved_route, fallback_index + 1)
                    raise
        error = last_error or ProviderError(
            "provider_route_invalid", "STT stream route has no target.", retryable=False
        )
        self._annotate_error(error, invocation_id, resolved_route, len(targets))
        raise error

    async def open_speech_dialogue(
        self,
        request: RealtimeSpeechDialogueRequest,
        *,
        route: Optional[Dict[str, Any]] = None,
    ) -> ValidatedSpeechDialogueStream:
        """Open a provider-neutral S2S stream; fallback ends after audio starts."""
        capability = cap.SPEECH_DIALOGUE_REALTIME
        resolved_route = deepcopy(route) if route is not None else self._resolve_route(
            request.organization_id, capability, request.purpose
        )
        if resolved_route.get("capability") != capability:
            raise ProviderError(
                "provider_route_invalid",
                "Realtime speech dialogue route capability is invalid.",
                retryable=False,
            )
        targets = [resolved_route.get("primary") or {}] + list(
            resolved_route.get("fallbacks") or []
        )
        invocation_id = new_id("model_invocation")
        request_hash = self._request_hash(request)
        last_error: Optional[ProviderError] = None
        for fallback_index, target in enumerate(targets):
            model_configuration_id = str(target.get("model_configuration_id") or "")
            model = "unknown"
            timeout_s = max(0.01, float(target.get("timeout_s", 10)))
            provider_id = "unknown"
            provider_connection_id = "unknown"
            started_at = perf_counter()
            try:
                model_configuration, provider_connection, credentials = self._model_connection(
                    request.organization_id,
                    model_configuration_id,
                    capability,
                    allow_unready=resolved_route.get("id") == "model_configuration_test",
                )
                provider_id = model_configuration["provider_id"]
                provider_connection_id = model_configuration["provider_connection_id"]
                model = model_configuration["provider_model_id"]
                adapter = self.providers.adapter(provider_id, capability)
                open_dialogue = getattr(adapter, "open_dialogue", None)
                if not callable(open_dialogue):
                    raise ProviderError(
                        "provider_streaming_not_supported",
                        "Provider adapter does not implement open_dialogue.",
                        retryable=False,
                    )
                context = ProviderContext(
                    organization_id=request.organization_id,
                    invocation_id=invocation_id,
                    route_id=str(resolved_route.get("id") or "route_inline"),
                    provider_connection_id=provider_connection_id,
                    model_configuration_id=model_configuration_id,
                    model_type=model_configuration["model_type"],
                    capability=capability,
                    purpose=request.purpose,
                    model=model,
                    timeout_s=timeout_s,
                    attempt=1,
                    fallback_index=fallback_index,
                    connection_config=provider_connection.get("connection_config") or {},
                    model_settings=model_configuration.get("settings") or {},
                    default_parameters=model_configuration.get("default_parameters") or {},
                    credentials=credentials,
                    metadata=request.metadata,
                )
                try:
                    provider_stream = await asyncio.wait_for(
                        open_dialogue(request, context), timeout=timeout_s
                    )
                except asyncio.TimeoutError as exc:
                    raise ProviderError(
                        "provider_timeout",
                        "Realtime speech dialogue open timed out.",
                        retryable=True,
                    ) from exc
                try:
                    stream = ValidatedSpeechDialogueStream(provider_stream, request)
                    self._log_invocation(
                        invocation_id=invocation_id,
                        organization_id=request.organization_id,
                        capability=capability,
                        purpose=request.purpose,
                        route=resolved_route,
                        provider_connection_id=provider_connection_id,
                        model_configuration_id=model_configuration_id,
                        provider_id=provider_id,
                        model=model,
                        status="stream_opened" if not fallback_index else "stream_fallback_opened",
                        latency_ms=int((perf_counter() - started_at) * 1000),
                        attempt=1,
                        fallback_index=fallback_index,
                        request_hash=request_hash,
                    )
                except BaseException:
                    await _abort_unclaimed_stream(provider_stream, timeout_s)
                    raise
                return stream
            except ProviderError as exc:
                last_error = exc
                self._log_invocation(
                    invocation_id=invocation_id,
                    organization_id=request.organization_id,
                    capability=capability,
                    purpose=request.purpose,
                    route=resolved_route,
                    provider_connection_id=provider_connection_id,
                    model_configuration_id=model_configuration_id,
                    provider_id=provider_id,
                    model=model,
                    status="failed",
                    latency_ms=int((perf_counter() - started_at) * 1000),
                    attempt=1,
                    fallback_index=fallback_index,
                    request_hash=request_hash,
                    error_code=exc.code,
                    error_details=exc.details,
                )
                if not exc.retryable or fallback_index + 1 >= len(targets):
                    self._annotate_error(exc, invocation_id, resolved_route, fallback_index + 1)
                    raise
        error = last_error or ProviderError(
            "provider_route_invalid",
            "Realtime speech dialogue route has no target.",
            retryable=False,
        )
        self._annotate_error(error, invocation_id, resolved_route, len(targets))
        raise error

    def _resolve_route(self, organization_id: str, capability: str, purpose: str) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            routes = transaction.model_routes.list()
        enabled = [
            route
            for route in routes
            if route.get("capability") == capability and route.get("enabled", True)
        ]
        exact = next((route for route in enabled if route.get("purpose") == purpose), None)
        default = next((route for route in enabled if route.get("purpose") == "default"), None)
        runtime_env = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower()
        if exact:
            return deepcopy(exact)
        if default and runtime_env != "production":
            return deepcopy(default)
        if runtime_env == "production":
            raise ProviderError(
                "provider_route_missing",
                "Production model invocation requires an explicit capability and purpose route.",
                retryable=False,
            )
        return {
            "id": "route_mock",
            "organization_id": organization_id,
            "capability": capability,
            "purpose": purpose,
            "primary": {
                "model_configuration_id": self._default_mock_model_configuration_id(capability),
                "timeout_s": 2,
            },
            "fallbacks": [],
            "policy": {"retry_count": 0},
            "enabled": True,
        }

    def _model_connection(
        self,
        organization_id: str,
        model_configuration_id: str,
        capability: str,
        *,
        allow_unready: bool = False,
    ) -> tuple:
        with self.persistence.transaction(organization_id) as transaction:
            model_configuration = transaction.model_configurations.get(model_configuration_id)
            provider_connection = (
                transaction.provider_connections.get(model_configuration["provider_connection_id"])
                if model_configuration else None
            )
            credentials = transaction.provider_secrets.get(provider_connection["id"]) if provider_connection else {}
        if model_configuration is None and model_configuration_id == self._default_mock_model_configuration_id(capability):
            model_configuration = {
                "id": model_configuration_id,
                "organization_id": organization_id,
                "provider_id": "mock",
                "provider_connection_id": "provider_conn_mock",
                "provider_model_id": self._default_mock_model(capability),
                "model_type": cap.CAPABILITY_MODEL_TYPES[capability],
                "supported_capabilities": [capability],
                "enabled": True,
                "status": "ready",
                "settings": {},
                "default_parameters": {},
            }
            provider_connection = {
                "id": "provider_conn_mock",
                "provider_id": "mock",
                "enabled": True,
                "connection_config": {},
            }
            return model_configuration, provider_connection, {}
        if model_configuration is None:
            raise ProviderError(
                "model_configuration_missing",
                "Model configuration does not exist.",
                retryable=False,
            )
        if not model_configuration.get("enabled", True) or (
            model_configuration.get("status") != "ready" and not allow_unready
        ):
            raise ProviderError(
                "model_configuration_not_ready",
                "Model configuration is disabled or has not passed validation.",
                retryable=True,
            )
        if capability not in model_configuration.get("supported_capabilities", []):
            raise ProviderError(
                "provider_capability_missing",
                "Model configuration does not support the requested capability.",
                retryable=False,
            )
        if provider_connection is None or not provider_connection.get("enabled", True):
            raise ProviderError(
                "provider_connection_disabled",
                "Provider connection is missing or disabled.",
                retryable=True,
            )
        return model_configuration, provider_connection, credentials

    def _validate_request(self, capability: str, request: InvocationRequest) -> None:
        expected = {
            cap.LLM_CHAT_JSON: ChatJSONRequest,
            cap.LLM_CHAT_TEXT: ChatTextRequest,
            cap.EMBEDDING_TEXT: TextEmbeddingRequest,
            cap.STT_BATCH: BatchSTTRequest,
            cap.TTS_SYNTHESIZE: TTSSynthesizeRequest,
            cap.AVATAR_SPEAK: AvatarSpeakRequest,
        }.get(capability)
        if expected is None:
            raise ProviderError(
                "provider_capability_missing",
                "No unified invocation schema is implemented for %s." % capability,
                retryable=False,
            )
        if not isinstance(request, expected):
            raise ProviderError(
                "provider_bad_request",
                "Invocation request does not match capability %s." % capability,
                retryable=False,
            )

    def _validate_response(self, capability: str, request: InvocationRequest, response: Any) -> None:
        expected = {
            cap.LLM_CHAT_JSON: ChatJSONResponse,
            cap.LLM_CHAT_TEXT: ChatTextResponse,
            cap.EMBEDDING_TEXT: TextEmbeddingResponse,
            cap.STT_BATCH: BatchSTTResponse,
            cap.TTS_SYNTHESIZE: TTSSynthesizeResponse,
            cap.AVATAR_SPEAK: AvatarSpeakResponse,
        }[capability]
        if not isinstance(response, expected):
            self._schema_error("Provider response does not match the unified response type.")
        if isinstance(request, ChatJSONRequest) and isinstance(response, ChatJSONResponse):
            if request.json_schema:
                try:
                    validate_structured_response(response.data, request.json_schema)
                except StructuredResponseValidationError as exc:
                    raise ProviderError("provider_schema_invalid", "Structured response did not match the request contract.",
                                        retryable=True, details={"schema_reason": exc.reason_code,
                                                                 "schema_path": exc.schema_path}) from exc
        elif isinstance(request, ChatTextRequest) and isinstance(response, ChatTextResponse):
            if not response.text.strip():
                self._schema_error("Text response cannot be empty.")
        elif isinstance(request, TextEmbeddingRequest) and isinstance(response, TextEmbeddingResponse):
            if len(response.vectors) != len(request.texts):
                self._schema_error("Embedding response count does not match request count.")
            if response.dimensions <= 0:
                self._schema_error("Embedding response dimensions must be positive.")
            if any(len(vector) != response.dimensions for vector in response.vectors):
                self._schema_error("Embedding response vectors have inconsistent dimensions.")
        elif isinstance(request, AvatarSpeakRequest) and isinstance(response, AvatarSpeakResponse):
            if not response.text or response.mode not in {"browser_speech", "audio", "video", "webrtc"}:
                self._schema_error("Avatar response is missing a supported delivery mode or spoken text.")
        elif isinstance(request, BatchSTTRequest) and isinstance(response, BatchSTTResponse):
            if not response.text.strip() or response.source not in {"server_streaming", "server_batch", "server_batch_repair"}:
                self._schema_error("STT response is missing authoritative transcript text or source.")
        elif isinstance(request, TTSSynthesizeRequest) and isinstance(response, TTSSynthesizeResponse):
            if not response.audio_uri or not response.content_type.startswith("audio/"):
                self._schema_error("TTS response is missing a supported audio asset.")

    def _schema_error(self, message: str) -> None:
        raise ProviderError("provider_schema_invalid", message, retryable=True)

    def _allows_fallback(self, error: ProviderError, policy: Dict[str, Any]) -> bool:
        if not error.retryable:
            return False
        configured = policy.get("fallback_on")
        if configured is None:
            return True
        aliases = {error.code, error.code.removeprefix("provider_")}
        return bool(aliases.intersection(set(configured)))

    def _request_hash(self, request: BaseModel) -> str:
        serialized = json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _estimated_cost(
        self,
        response: InvocationResponse,
        model_configuration: Dict[str, Any],
        target: Dict[str, Any],
    ) -> float:
        pricing = target.get("pricing") or model_configuration.get("settings", {}).get("pricing") or {}
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0.0
        input_cost = float(pricing.get("input_per_million_tokens", 0)) * usage.input_tokens / 1_000_000
        output_cost = float(pricing.get("output_per_million_tokens", 0)) * usage.output_tokens / 1_000_000
        return round(input_cost + output_cost, 8)

    def _log_invocation(
        self,
        *,
        invocation_id: str,
        organization_id: str,
        capability: str,
        purpose: str,
        route: Dict[str, Any],
        provider_connection_id: str,
        model_configuration_id: str,
        provider_id: str,
        model: str,
        status: str,
        latency_ms: int,
        attempt: int,
        fallback_index: int,
        request_hash: str,
        response: Optional[InvocationResponse] = None,
        estimated_cost_usd: float = 0.0,
        error_code: str = "",
        error_details: Optional[Dict[str, Any]] = None,
        prompt_version: Optional[str] = None,
        diagnostic_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        usage = getattr(response, "usage", None)
        diagnostics = error_details or {}
        input_tokens = usage.input_tokens if usage else _nonnegative_int(
            diagnostics.get("input_tokens")
        )
        output_tokens = usage.output_tokens if usage else _nonnegative_int(
            diagnostics.get("output_tokens")
        )
        total_tokens = usage.total_tokens if usage else _nonnegative_int(
            diagnostics.get("total_tokens")
        )
        item = {
            "id": new_id("mil"),
            "invocation_id": invocation_id,
            "organization_id": organization_id,
            "capability": capability,
            "purpose": purpose,
            "route_id": route.get("id"),
            "provider_connection_id": provider_connection_id,
            "model_configuration_id": model_configuration_id,
            "provider_id": provider_id,
            "model": model,
            "status": status,
            "attempt": attempt,
            "fallback_index": fallback_index,
            "latency_ms": latency_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "audio_seconds": None,
            "estimated_cost_usd": estimated_cost_usd,
            "error_code": error_code,
            "prompt_version": prompt_version if prompt_version in {
                "company_question_reply.v1", "answer_evaluation.v7", "answer_evaluation.v8", "controlled_followup.v2", "controlled_followup.v3", "controlled_followup.v4", "controlled_followup.v5", "controlled_followup.v6", "controlled_followup_repair.v1", "supplement_reply.v4", "supplement_reply.v5", "supplement_reply.v6", "supplement_reply.v7", "supplement_reply.v8", "supplement_reply.v9", "supplement_reply.v10", "supplement_reply.v11", "supplement_reply.v12", "conversation_reception.v1", "conversation_reception.v2", "conversation_reception.v3", "conversation_reception.v4", "conversation_reception.v5", "conversation_reception.v6", "conversation_reception.v7", "conversation_reception.v8", "conversation_reception.v9", "conversation_reception.v10", "conversation_reception_speech.v1", "conversation_reception_speech.v2", "conversation_reception_speech.v3", "conversation_reception_speech.v4", "interviewer_supervisor.v1", "interviewer_supervisor.v2", "interviewer_supervisor.v3", "interviewer_supervisor.v4", "interviewer_evidence_expert.v1", "interviewer_evidence_expert.v2", "interviewer_transition.v1",
                "interview_turn_understanding.v10", "interview_turn_decision.v9",
                "interview_turn_understanding.v11", "interview_turn_decision.v10",
                "interview_turn_understanding.v12", "interview_turn_understanding.v13",
                "interview_turn_decision.v11", "interview_turn_decision.v12",
                "interview_turn_understanding.v14", "interview_turn_understanding.v15", "interview_turn_decision.v13", "interview_turn_decision.v14", "interview_turn_understanding.v16", "interview_turn_understanding.v17", "interview_turn_understanding.v18", "interview_turn_understanding.v19", "interview_turn_decision.v15", "interview_turn_decision.v16", "interview_turn_decision.v17", "interview_turn_decision.v18", "interview_turn_understanding.v20", "interview_turn_understanding.v21", "interview_turn_understanding.v22", "interview_turn_understanding.v23", "interview_turn_decision.v19", "interview_turn_decision.v20", "interview_turn_decision.v21", "interview_turn_decision.v22",
                "interview_turn_understanding.v1", "interview_turn_understanding.v2", "interview_turn_understanding.v3",
                "interview_turn_understanding.v4", "interview_turn_understanding.v5",
                "interview_turn_understanding.v6", "interview_turn_understanding.v7",
                "interview_turn_understanding.v8", "interview_turn_understanding.v58", "interview_turn_understanding.v70", "interview_turn_understanding.v82", "interview_turn_understanding.v94", "interview_turn_understanding.v106", "interview_turn_understanding.v44", "interview_turn_understanding.v60", "interview_turn_understanding.v72", "interview_turn_understanding.v84", "interview_turn_understanding.v96", "interview_turn_understanding.v108", "interview_turn_understanding.v9", "interview_turn_understanding.v59", "interview_turn_understanding.v71", "interview_turn_understanding.v83", "interview_turn_understanding.v95", "interview_turn_understanding.v107", "interview_turn_understanding.v45", "interview_turn_understanding.v61", "interview_turn_understanding.v73", "interview_turn_understanding.v85", "interview_turn_understanding.v97", "interview_turn_understanding.v109",
                "interview_inquiry_units.v1", "interview_inquiry_units.v2", "answer_evaluation.v6",
                "interview_turn_decision.v3", "interview_turn_decision.v4", "answer_evaluation.v2",
                "answer_evaluation.v3", "answer_evaluation.v4", "answer_evaluation.v5",
                "interview_turn_decision.v5", "interview_turn_decision.v6",
                "interview_turn_decision.v7", "interview_turn_decision.v8", "interview_turn_decision.v23", "interview_turn_decision.v24",
                "interview_turn_understanding.v24", "interview_turn_understanding.v25", "interview_turn_understanding.v26", "interview_turn_understanding.v27", "interview_turn_decision.v25", "interview_turn_decision.v26", "interview_turn_decision.v27", "interview_turn_decision.v28", "interview_turn_decision.v29", "interview_turn_decision.v30", "interview_turn_understanding.v28", "interview_turn_understanding.v29", "interview_turn_understanding.v30", "interview_turn_understanding.v31", "interview_turn_decision.v31", "interview_turn_decision.v32", "interview_turn_decision.v33", "interview_turn_decision.v34", "interview_turn_decision.v35", "interview_turn_decision.v36", "interview_turn_decision.v37", "interview_turn_decision.v38", "interview_turn_decision.v39", "interview_turn_decision.v40", "interview_turn_understanding.v32", "interview_turn_understanding.v36", "interview_turn_understanding.v40", "interview_turn_understanding.v50", "interview_turn_understanding.v62", "interview_turn_understanding.v74", "interview_turn_understanding.v86", "interview_turn_understanding.v98", "interview_turn_understanding.v110", "interview_turn_understanding.v118", "interview_turn_understanding.v46", "interview_turn_understanding.v54", "interview_turn_understanding.v66", "interview_turn_understanding.v78", "interview_turn_understanding.v90", "interview_turn_understanding.v102", "interview_turn_understanding.v114", "interview_turn_understanding.v122", "interview_turn_understanding.v33", "interview_turn_understanding.v37", "interview_turn_understanding.v41", "interview_turn_understanding.v51", "interview_turn_understanding.v63", "interview_turn_understanding.v75", "interview_turn_understanding.v87", "interview_turn_understanding.v99", "interview_turn_understanding.v111", "interview_turn_understanding.v119", "interview_turn_understanding.v47", "interview_turn_understanding.v55", "interview_turn_understanding.v67", "interview_turn_understanding.v79", "interview_turn_understanding.v91", "interview_turn_understanding.v103", "interview_turn_understanding.v115", "interview_turn_understanding.v123", "interview_turn_understanding.v34", "interview_turn_understanding.v38", "interview_turn_understanding.v42", "interview_turn_understanding.v52", "interview_turn_understanding.v64", "interview_turn_understanding.v76", "interview_turn_understanding.v88", "interview_turn_understanding.v100", "interview_turn_understanding.v112", "interview_turn_understanding.v120", "interview_turn_understanding.v126", "interview_turn_understanding.v130", "interview_turn_understanding.v48", "interview_turn_understanding.v56", "interview_turn_understanding.v68", "interview_turn_understanding.v80", "interview_turn_understanding.v92", "interview_turn_understanding.v104", "interview_turn_understanding.v116", "interview_turn_understanding.v124", "interview_turn_understanding.v128", "interview_turn_understanding.v132", "interview_turn_understanding.v35", "interview_turn_understanding.v39", "interview_turn_understanding.v43", "interview_turn_understanding.v53", "interview_turn_understanding.v65", "interview_turn_understanding.v77", "interview_turn_understanding.v89", "interview_turn_understanding.v101", "interview_turn_understanding.v113", "interview_turn_understanding.v121", "interview_turn_understanding.v127", "interview_turn_understanding.v131", "interview_turn_understanding.v49", "interview_turn_understanding.v57", "interview_turn_understanding.v69", "interview_turn_understanding.v81", "interview_turn_understanding.v93", "interview_turn_understanding.v105", "interview_turn_understanding.v117", "interview_turn_understanding.v125", "interview_turn_understanding.v129", "interview_turn_understanding.v133", "interview_turn_decision.v41", "interview_turn_decision.v42", "interview_turn_decision.v43", "interview_turn_decision.v44", "interview_turn_decision.v45", "interview_turn_decision.v46", "interview_turn_decision.v47", "interview_turn_decision.v48", "interview_turn_decision.v49", "interview_turn_decision.v50", "interview_turn_decision.v51", "interview_turn_decision.v52", "interview_turn_decision.v53", "interview_turn_decision.v59", "interview_turn_decision.v65", "interview_turn_decision.v71", "interview_turn_decision.v81", "interview_turn_decision.v93", "interview_turn_decision.v105", "interview_turn_decision.v117", "interview_turn_decision.v129", "interview_turn_decision.v141", "interview_turn_decision.v149", "interview_turn_decision.v77", "interview_turn_decision.v85", "interview_turn_decision.v97", "interview_turn_decision.v109", "interview_turn_decision.v121", "interview_turn_decision.v133", "interview_turn_decision.v145", "interview_turn_decision.v153", "interview_turn_decision.v54", "interview_turn_decision.v60", "interview_turn_decision.v66", "interview_turn_decision.v72", "interview_turn_decision.v82", "interview_turn_decision.v94", "interview_turn_decision.v106", "interview_turn_decision.v118", "interview_turn_decision.v130", "interview_turn_decision.v142", "interview_turn_decision.v150", "interview_turn_decision.v78", "interview_turn_decision.v86", "interview_turn_decision.v98", "interview_turn_decision.v110", "interview_turn_decision.v122", "interview_turn_decision.v134", "interview_turn_decision.v146", "interview_turn_decision.v154", "interview_turn_decision.v55", "interview_turn_decision.v61", "interview_turn_decision.v67", "interview_turn_decision.v73", "interview_turn_decision.v83", "interview_turn_decision.v95", "interview_turn_decision.v107", "interview_turn_decision.v119", "interview_turn_decision.v131", "interview_turn_decision.v143", "interview_turn_decision.v151", "interview_turn_decision.v157", "interview_turn_decision.v161", "interview_turn_decision.v79", "interview_turn_decision.v87", "interview_turn_decision.v99", "interview_turn_decision.v111", "interview_turn_decision.v123", "interview_turn_decision.v135", "interview_turn_decision.v147", "interview_turn_decision.v155", "interview_turn_decision.v159", "interview_turn_decision.v163", "interview_turn_decision.v56", "interview_turn_decision.v62", "interview_turn_decision.v68", "interview_turn_decision.v74", "interview_turn_decision.v84", "interview_turn_decision.v96", "interview_turn_decision.v108", "interview_turn_decision.v120", "interview_turn_decision.v132", "interview_turn_decision.v144", "interview_turn_decision.v152", "interview_turn_decision.v158", "interview_turn_decision.v162", "interview_turn_decision.v80", "interview_turn_decision.v88", "interview_turn_decision.v100", "interview_turn_decision.v112", "interview_turn_decision.v124", "interview_turn_decision.v136", "interview_turn_decision.v148", "interview_turn_decision.v156", "interview_turn_decision.v160", "interview_turn_decision.v164", "interview_turn_decision.v57", "interview_turn_decision.v63", "interview_turn_decision.v69", "interview_turn_decision.v89", "interview_turn_decision.v101", "interview_turn_decision.v113", "interview_turn_decision.v125", "interview_turn_decision.v137", "interview_turn_decision.v75", "interview_turn_decision.v91", "interview_turn_decision.v103", "interview_turn_decision.v115", "interview_turn_decision.v127", "interview_turn_decision.v139", "interview_turn_decision.v58", "interview_turn_decision.v64", "interview_turn_decision.v70", "interview_turn_decision.v90", "interview_turn_decision.v102", "interview_turn_decision.v114", "interview_turn_decision.v126", "interview_turn_decision.v138", "interview_turn_decision.v76", "interview_turn_decision.v92", "interview_turn_decision.v104", "interview_turn_decision.v116", "interview_turn_decision.v128", "interview_turn_decision.v140",
                "interview_turn_decision.v1", "interview_turn_decision.v2", "supplement_reply.v1", "supplement_reply.v2", "supplement_reply.v3", "candidate_exit_reply.v1",
            } else None,
            "http_status": diagnostics.get("http_status") if type(diagnostics.get("http_status")) is int
                and diagnostics.get("http_status") in {200, 400, 401, 402, 403, 404, 408, 409, 413, 422, 429, 500, 502, 503, 504} else None,
            "vendor_code": diagnostics.get("vendor_code") if isinstance(diagnostics.get("vendor_code"), str)
                and diagnostics.get("vendor_code") in {
                "Arrearage", "AllocationQuota.FreeTierOnly", "InvalidApiKey", "Throttling",
                "Throttling.RateQuota", "Throttling.AllocationQuota", "Throttling.AccessDenied",
            } else None,
            "provider_request_id": diagnostics.get("provider_request_id") if isinstance(diagnostics.get("provider_request_id"), str) and re.fullmatch(
                r"(?:[A-Fa-f0-9]{32}|[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12})",
                str(diagnostics.get("provider_request_id", ""))) else None,
            "transport_phase": diagnostics.get("transport_phase") if isinstance(diagnostics.get("transport_phase"), str)
                and diagnostics.get("transport_phase") in {
                "request_start", "connect", "tls", "request_write", "response_headers", "response_body",
            } else None,
            "transport_phase_ms": diagnostics.get("transport_phase_ms") if type(diagnostics.get("transport_phase_ms")) is int
                and 0 <= diagnostics.get("transport_phase_ms") <= 86_400_000 else None,
            "transport_elapsed_ms": diagnostics.get("transport_elapsed_ms") if type(diagnostics.get("transport_elapsed_ms")) is int
                and 0 <= diagnostics.get("transport_elapsed_ms") <= 86_400_000 else None,
            "transport_outcome": diagnostics.get("transport_outcome") if isinstance(diagnostics.get("transport_outcome"), str)
                and diagnostics.get("transport_outcome") in {
                "timeout", "cancelled", "network_error", "http_error", "stream_error", "success",
            } else None,
            "schema_reason": diagnostics.get("schema_reason") if diagnostics.get("schema_reason") in {
                "enum", "type", "required", "min_properties", "additional_properties", "min_items", "max_items",
                "unique_items", "minimum", "maximum", "min_length", "max_length",
            } else None,
            "schema_path": diagnostics.get("schema_path") if re.fullmatch(r"\$[A-Za-z0-9_.\[\]]{0,160}", str(diagnostics.get("schema_path", ""))) else None,
            "json_failure_kind": diagnostics.get("json_failure_kind")
                if isinstance(diagnostics.get("json_failure_kind"), str)
                and diagnostics.get("json_failure_kind") in {"empty_content", "invalid_json_object"} else None,
            "json_decode_reason": diagnostics.get("json_decode_reason")
                if isinstance(diagnostics.get("json_decode_reason"), str)
                and diagnostics.get("json_decode_reason") in {"non_object", "extra_data", "syntax_error"} else None,
            "json_error_offset": diagnostics.get("json_error_offset")
                if type(diagnostics.get("json_error_offset")) is int
                and 0 <= diagnostics.get("json_error_offset") <= 10_000_000 else None,
            "requested_response_format": diagnostics.get("requested_response_format")
                if isinstance(diagnostics.get("requested_response_format"), str)
                and diagnostics.get("requested_response_format") in {"json_schema", "json_object", "prompt"} else None,
            "content_length": diagnostics.get("content_length")
                if type(diagnostics.get("content_length")) is int
                and 0 <= diagnostics.get("content_length") <= 10_000_000 else None,
            "rejection_category": diagnostics.get("rejection_category") if diagnostics.get("rejection_category") in {
                "request_rejected", "structured_schema_rejected", "output_limit_rejected",
            } else None,
            "finish_reason": diagnostics.get("finish_reason"),
            "requested_max_output_tokens": diagnostics.get(
                "requested_max_output_tokens"
            ),
            "reasoning_tokens": diagnostics.get("reasoning_tokens"),
            "redacted_request_hash": request_hash,
            "created_at": utc_now(),
        }
        if diagnostic_context:
            item.update({key: value for key, value in safe_model_diagnostic_fields(diagnostic_context).items()
                         if key not in item})
        if status == "cancelled" and usage is None:
            # Cancellation establishes no usage/billing fact; zero would imply
            # an unverified free call even when a provider accepted the request.
            item.update(input_tokens=None, output_tokens=None, total_tokens=None, estimated_cost_usd=None)
        with self.persistence.transaction(organization_id) as transaction:
            transaction.model_invocations.append(item)

    def _annotate_error(
        self,
        error: ProviderError,
        invocation_id: str,
        route: Dict[str, Any],
        attempts: int,
    ) -> None:
        error.details.update(
            {
                "invocation_id": invocation_id,
                "route_id": route.get("id"),
                "attempts": attempts,
            }
        )

    def _default_mock_model(self, capability: str) -> str:
        return {
            cap.LLM_CHAT_JSON: "mock-json",
            cap.LLM_CHAT_TEXT: "mock-text",
            cap.EMBEDDING_TEXT: "mock-embedding",
            cap.STT_STREAMING: "mock-stt",
            cap.STT_BATCH: "mock-stt",
            cap.TTS_SYNTHESIZE: "mock-tts",
            cap.AVATAR_SPEAK: "mock-avatar",
            cap.SPEECH_DIALOGUE_REALTIME: "mock-dialogue",
        }.get(capability, "mock")

    def _default_mock_model_configuration_id(self, capability: str) -> str:
        return "model_cfg_mock_%s" % capability.replace(".", "_")

    def _apply_model_defaults(self, request: InvocationRequest, defaults: Dict[str, Any]) -> InvocationRequest:
        updates = {
            key: value
            for key, value in defaults.items()
            if hasattr(request, key) and key not in request.model_fields_set
        }
        return request.model_copy(update=updates) if updates else request
