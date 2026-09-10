from copy import deepcopy
from typing import Any, Dict, List, Optional

from app.core.time import utc_now


def turn_speech_asset_id(turn: Dict[str, Any]) -> Optional[str]:
    """Runtime binding for deferred resume speech; immutable snapshot otherwise."""
    if turn.get("deferred_speech"):
        prepared = turn.get("speech_preparation") or {}
        return prepared.get("asset_id") if prepared.get("status") == "ready" else None
    return (turn.get("question_snapshot") or {}).get("speech_asset_id")


def summarize_speech_preparation(
    items: List[Dict[str, Any]],
    *,
    profile_fingerprint: Optional[str],
    requested_at: Optional[str],
) -> Dict[str, Any]:
    normalized = deepcopy(items)
    total = len(normalized)
    ready = sum(item.get("status") == "ready" for item in normalized)
    failed = sum(item.get("status") == "failed" for item in normalized)
    if total == ready:
        status = "ready"
    elif failed:
        status = "failed"
    elif any(item.get("status") == "building" for item in normalized):
        status = "building"
    elif any(item.get("status") == "queued" for item in normalized):
        status = "queued"
    elif any(item.get("status") == "cancelled" for item in normalized):
        status = "cancelled"
    else:
        status = "not_requested"
    return {
        "status": status,
        "profile_fingerprint": profile_fingerprint,
        "total": total,
        "ready": ready,
        "failed": failed,
        "items": normalized,
        "requested_at": requested_at,
        "updated_at": utc_now(),
    }


def update_speech_preparation_item(
    preparation: Dict[str, Any],
    *,
    question_id: str,
    source_version: int,
    status: str,
    asset_id: Optional[str] = None,
    error_code: Optional[str] = None,
) -> Dict[str, Any]:
    items = deepcopy(preparation.get("items") or [])
    item = next(
        (
            value
            for value in items
            if value.get("question_id") == question_id
            and int(value.get("source_version", 0)) == int(source_version)
        ),
        None,
    )
    if item is None:
        return deepcopy(preparation)
    item["status"] = status
    item["asset_id"] = asset_id
    item["last_error_code"] = error_code
    return summarize_speech_preparation(
        items,
        profile_fingerprint=preparation.get("profile_fingerprint"),
        requested_at=preparation.get("requested_at"),
    )
