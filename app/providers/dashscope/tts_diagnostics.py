"""Bounded vendor classification and transport timings, never response text."""

import asyncio
import json
import re
from time import monotonic
from typing import Any, Dict

from app.model_gateway.errors import ProviderError


MAX_ERROR_BYTES = 16_384
ERROR_BODY_TIMEOUT = .5
VENDOR_CODES = frozenset({
    "Arrearage", "AllocationQuota.FreeTierOnly", "InvalidApiKey",
    "Throttling", "Throttling.RateQuota", "Throttling.AllocationQuota", "Throttling.AccessDenied",
})
_REQUEST_ID = re.compile(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z")


def vendor_fields(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = {}
    code = payload.get("code")
    if isinstance(code, str) and code in VENDOR_CODES:
        result["vendor_code"] = code
    request_id = payload.get("request_id")
    if isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id):
        result["provider_request_id"] = request_id
    return result


async def error_payload(response: Any) -> Dict[str, Any]:
    """Read at most one small JSON error; do not decompress untrusted errors."""
    encoding = response.headers.get("content-encoding", "identity").lower().strip()
    if encoding not in {"", "identity"}:
        return {}
    content = bytearray()
    async for packet in response.aiter_bytes(chunk_size=1024):
        if len(content) + len(packet) > MAX_ERROR_BYTES:
            return {}
        content.extend(packet)
    try:
        payload = json.loads(content)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return {}
    return vendor_fields(payload)


async def bounded_error_payload(response: Any) -> Dict[str, Any]:
    # httpx closes its body iterator on EOF. Bound that close as well as reads;
    # neither an error body nor a slow transport cleanup may consume the call.
    task = asyncio.create_task(error_payload(response))
    try:
        done, _ = await asyncio.wait({task}, timeout=ERROR_BODY_TIMEOUT)
        if done:
            return task.result()
        raise asyncio.TimeoutError()
    finally:
        if not task.done():
            task.cancel()
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())


def annotate_cancellation(error: BaseException, diagnostics: Dict[str, Any], *, provider_error=None) -> None:
    error.provider_diagnostics = diagnostics
    if isinstance(provider_error, ProviderError):
        error.provider_error = provider_error
    # Python 3.9 Task reconstructs CancelledError and drops custom attributes.
    # The gateway owns this task and reads only this small, fixed diagnostic.
    task = asyncio.current_task()
    if task is not None:
        task.provider_diagnostics = diagnostics
        if isinstance(provider_error, ProviderError):
            task.provider_error = provider_error


def response_error(status: int, fields: Dict[str, Any], *, stream: bool = False) -> ProviderError:
    """Known business codes outrank coarse HTTP status without echoing a body."""
    vendor = fields.get("vendor_code")
    if vendor == "Arrearage":
        code, message, retryable = "provider_billing_required", "DashScope TTS account billing requires attention.", False
    elif vendor == "AllocationQuota.FreeTierOnly":
        code, message, retryable = "provider_billing_required", "DashScope TTS free-tier quota is exhausted and paid usage is disabled.", False
    elif vendor == "InvalidApiKey" or status in {401, 403}:
        code, message, retryable = "provider_auth_failed", "DashScope TTS credentials or permissions were rejected.", False
    elif status == 402:
        code, message, retryable = "provider_billing_required", "DashScope TTS account billing or quota requires attention.", False
    elif (vendor or "").startswith("Throttling") or status == 429:
        # AllocationQuota is a TPS/TPM limit, not evidence of an empty balance.
        code, message, retryable = "provider_rate_limited", "DashScope TTS rate limit was reached.", True
    elif status >= 500 or (stream and status == 200):
        code, message, retryable = "provider_server_error", "DashScope TTS is unavailable.", True
    else:
        code, message, retryable = "provider_bad_request", "DashScope rejected the TTS stream request.", False
    return ProviderError(code, message, retryable=retryable, details={
        **fields, "http_status": status, "fallback_allowed": retryable,
    })


class TransportTrace:
    """Read fixed httpcore event names only; never retain its kwargs or result."""

    _PHASES = {
        "connect_tcp": "connect", "connect_unix_socket": "connect", "start_tls": "tls",
        "send_request_headers": "request_write", "send_request_body": "request_write",
        "receive_response_headers": "response_headers", "receive_response_body": "response_body",
    }

    def __init__(self) -> None:
        self.started = self.phase_started = monotonic()
        self.phase = "request_start"
        self.completed_ms = None

    def enter(self, phase: str) -> None:
        self.phase, self.phase_started, self.completed_ms = phase, monotonic(), None

    async def __call__(self, name: str, _info: Any) -> None:
        parts = name.split(".")
        phase = self._PHASES.get(parts[-2]) if len(parts) >= 2 else None
        if phase is None:
            return
        if parts[-1] == "started":
            self.enter(phase)
        elif parts[-1] in {"complete", "failed"} and phase == self.phase:
            self.completed_ms = max(0, round((monotonic() - self.phase_started) * 1000))

    def snapshot(self, outcome: str) -> Dict[str, Any]:
        now = monotonic()
        return {
            "transport_phase": self.phase,
            "transport_phase_ms": self.completed_ms if self.completed_ms is not None else max(0, round((now - self.phase_started) * 1000)),
            "transport_elapsed_ms": max(0, round((now - self.started) * 1000)),
            "transport_outcome": outcome,
        }
