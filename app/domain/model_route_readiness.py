"""Pure, provider-neutral interpretation of model-route health evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


SAFE_PROBE_CODES = frozenset({
    "provider_auth_failed", "provider_bad_request", "provider_capability_missing",
    "provider_circuit_open", "provider_connection_disabled", "provider_cost_limit_exceeded",
    "provider_health_failed", "provider_model_unavailable", "provider_network_error",
    "provider_not_implemented", "provider_not_installed", "provider_rate_limited",
    "provider_route_invalid", "provider_route_missing", "provider_schema_invalid",
    "provider_server_error", "provider_stream_closed", "provider_stream_failed",
    "provider_stream_interrupted", "provider_stream_open_failed", "provider_streaming_not_supported",
    "provider_timeout", "provider_transport_unavailable", "provider_probe_failed",
    "provider_probe_cancelled", "provider_probe_cleanup_failed", "provider_output_truncated", "provider_output_audio_too_large",
})


def safe_probe_code(value: Any) -> str:
    return value if isinstance(value, str) and value in SAFE_PROBE_CODES else "provider_probe_failed"


def utc_timestamp(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def configuration_fingerprint(route: dict, targets: list[tuple[Optional[dict], Optional[dict]]]) -> str:
    """Bind semantic configuration, never generic versions or health writes."""
    def selected(item: Optional[dict], fields: tuple[str, ...]) -> Any:
        return {key: item.get(key) for key in fields} if item is not None else None

    snapshot = {
        "route": selected(route, ("id", "organization_id", "capability", "purpose", "enabled", "primary", "fallbacks", "policy")),
        "targets": [
            {
                "model": selected(model, ("id", "provider_connection_id", "provider_id", "model_type", "provider_model_id",
                                          "supported_capabilities", "configuration_revision", "enabled", "settings", "default_parameters")),
                "connection": selected(connection, ("id", "provider_id", "configuration_revision", "enabled", "connection_config", "credential_ref")),
            }
            for model, connection in targets
        ],
    }
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def project_route_readiness(
    route: Optional[dict], *, model: Optional[dict], connection: Optional[dict],
    manifest: Optional[dict], fingerprint: Optional[str], now: datetime,
) -> dict:
    """Expired is not failed; legacy evidence stays valid only within its TTL."""
    state = {"route_id": route.get("id") if route else None, "status": "configuration_invalid",
             "ready": False, "reason_code": "ROUTE_MISSING", "checked_at": None,
             "expires_at": None, "retry_at": None, "can_refresh": False}
    invalid = (
        "ROUTE_MISSING" if not route else
        "ROUTE_DISABLED" if not route.get("enabled", True) else
        "MODEL_CONFIGURATION_MISSING" if not model else
        "MODEL_CONFIGURATION_DISABLED" if not model.get("enabled", True) else
        "MODEL_NOT_READY" if model.get("status") != "ready" else
        "MODEL_CAPABILITY_MISMATCH" if route.get("capability") not in model.get("supported_capabilities", []) else
        "PROVIDER_CONNECTION_MISSING" if not connection else
        "PROVIDER_CONNECTION_DISABLED" if not connection.get("enabled", True) else
        "NON_PRODUCTION_PROVIDER" if model.get("provider_id") == "mock" else
        "PROVIDER_NOT_IMPLEMENTED" if not manifest or not manifest.get("implemented", False) else
        "PROVIDER_CAPABILITY_MISMATCH" if route.get("capability") not in manifest.get("capabilities", []) else None
    )
    if invalid:
        state["reason_code"] = invalid
        return state
    health = route.get("last_health") or {}
    checked = utc_timestamp(health.get("checked_at"))
    try:
        ttl = max(1, min(86400, int((route.get("policy") or {}).get("readiness_ttl_seconds", 60))))
    except (ValueError, TypeError, OverflowError):
        state["reason_code"] = "ROUTE_POLICY_INVALID"
        return state
    expires = checked + timedelta(seconds=ttl) if checked else None
    state.update(checked_at=timestamp(checked) if checked else None, expires_at=timestamp(expires) if expires else None)
    lease = route.get("health_probe") or {}
    lease_expires = utc_timestamp(lease.get("expires_at"))
    if lease.get("token") and lease.get("configuration_fingerprint") == fingerprint and lease_expires and lease_expires > now:
        state.update(status="checking", reason_code="ROUTE_PROBE_IN_PROGRESS", retry_at=timestamp(lease_expires))
        return state
    if health.get("configuration_fingerprint") and health["configuration_fingerprint"] != fingerprint:
        state.update(status="expired", reason_code="ROUTE_CONFIGURATION_CHANGED", can_refresh=True)
    elif health.get("status") == "healthy" and expires is not None and checked <= now <= expires:
        state.update(status="healthy", ready=True, reason_code="ROUTE_HEALTH_FRESH")
    elif health.get("status") == "healthy" and checked is not None:
        state.update(status="expired", reason_code="ROUTE_HEALTH_EXPIRED", can_refresh=True)
    elif health.get("status") == "failed" and checked is not None:
        retry = utc_timestamp(health.get("retry_at")) or checked + timedelta(seconds=30)
        state.update(status="failed", reason_code=safe_probe_code(health.get("reason_code")),
                     retry_at=timestamp(retry), can_refresh=health.get("retryable", True) is not False and retry <= now)
    else:
        state.update(status="untested", reason_code="ROUTE_HEALTH_UNTESTED", can_refresh=True)
    return state
