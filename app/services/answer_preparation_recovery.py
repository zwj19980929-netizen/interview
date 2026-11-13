"""Allow-listed recovery diagnostics; never include provider output or evidence."""
from typing import Any

_STAGES = {"answer_understanding", "supplement_prompt"}
_REASONS = {"timeout", "unavailable", "invalid_result", "retry_exhausted", "non_retryable",
            "call_budget_exhausted", "provider_timeout", "provider_auth_failed", "provider_billing_required",
            "provider_rate_limited", "provider_circuit_open", "provider_unavailable"}


def public_preparation_recovery(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    if (value.get("stage") not in _STAGES or value.get("reason_code") not in _REASONS
            or type(value.get("attempt")) is not int or not 0 <= value["attempt"] <= 2
            or type(value.get("max_attempts")) is not int or value["max_attempts"] != 2
            or type(value.get("retryable")) is not bool):
        return {}
    return {key: value[key] for key in ("stage", "reason_code", "attempt", "max_attempts", "retryable")}
