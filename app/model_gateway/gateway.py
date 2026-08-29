import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from copy import deepcopy
from dataclasses import dataclass
from time import monotonic, perf_counter
from typing import Any, Callable, Dict, Optional

from pydantic import BaseModel

from app.core.ids import new_id
from app.core.prompt.validation import StructuredResponseValidationError, validate_structured_response
from app.core.time import utc_now
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
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
)
from app.model_gateway.streaming import ValidatedSTTStream
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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

    async def invoke(
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
        backoff_ms = min(1000, max(0, int(policy.get("retry_backoff_ms", 0))))
        circuit_threshold = max(0, int(policy.get("circuit_failure_threshold", 3)))
        circuit_recovery_seconds = max(0.0, float(policy.get("circuit_recovery_seconds", 30)))
        total_attempts = 0
        last_error: Optional[ProviderError] = None

        for fallback_index, target in enumerate(targets):
            model_configuration_id = str(target.get("model_configuration_id") or "")
            model = "unknown"
            timeout_s = max(0.01, float(target.get("timeout_s", 20)))
            provider_id = "unknown"
            provider_connection_id = "unknown"
            circuit_key = "%s:%s:%s" % (request.organization_id, model_configuration_id, capability)

            for attempt in range(1, retry_count + 2):
                total_attempts += 1
                started_at = perf_counter()
                model_configuration: Dict[str, Any] = {}
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
                    try:
                        response = await asyncio.wait_for(
                            adapter.invoke(capability, self._apply_model_defaults(request, context.default_parameters), context),
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
                    )
                    self.circuits.record_success(circuit_key)
                    return response
                except ProviderError as exc:
                    last_error = exc
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
                    )
                    can_retry = exc.retryable and exc.code != "provider_circuit_open" and attempt <= retry_count
                    if can_retry:
                        if backoff_ms:
                            await asyncio.sleep((backoff_ms * attempt) / 1000.0)
                        continue
                    if exc.retryable and exc.code != "provider_circuit_open":
                        self.circuits.record_failure(circuit_key, threshold=circuit_threshold)
                    break

            if last_error is None:
                break
            has_fallback = fallback_index + 1 < len(targets)
            if not has_fallback or not self._allows_fallback(last_error, policy):
                self._annotate_error(last_error, invocation_id, resolved_route, total_attempts)
                raise last_error

        error = last_error or ProviderError(
            "provider_route_invalid",
            "Model route does not contain an invokable target.",
            retryable=False,
        )
        self._annotate_error(error, invocation_id, resolved_route, total_attempts)
        raise error

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
                    self._schema_error(str(exc))
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
            "finish_reason": diagnostics.get("finish_reason"),
            "requested_max_output_tokens": diagnostics.get(
                "requested_max_output_tokens"
            ),
            "reasoning_tokens": diagnostics.get("reasoning_tokens"),
            "redacted_request_hash": request_hash,
            "created_at": utc_now(),
        }
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
