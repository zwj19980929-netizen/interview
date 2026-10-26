"""Read-only media guard projection; never a writable interview aggregate."""
from copy import deepcopy
from typing import Any, Dict, List, Optional


# Shared field order for SQL projection and the in-memory adapter. SQL extracts
# these paths before Python sees the row, avoiding full history decoding/copying.
REALTIME_PATHS = (
    ("id",), ("organization_id",), ("version",), ("status",), ("current_turn_id",),
    ("candidate_input_completed_at",),
    ("agent_runtime", "floor"), ("agent_runtime", "active_performance_id"),
    ("agent_runtime", "completion_emitted_at"), ("agent_runtime", "takeover", "status"),
    ("agent_runtime", "calibration_status"), ("agent_runtime", "calibration_updated_at"),
    ("plan_snapshot", "enterprise_skill_snapshot"),
)


def realtime_state_from_values(
    values: List[Any], selected: Optional[List[Any]], *, plan_type: Optional[str], runtime_type: Optional[str],
    takeover_type: Optional[str],
) -> Optional[Dict[str, Any]]:
    # Never turn a malformed non-object plan into an apparently absent Skill.
    if any(kind not in (None, "null", "object") for kind in (plan_type, runtime_type, takeover_type)):
        return None
    identity, org, version, status, turn, completed, floor, performance, emitted, takeover, calibration, calibration_at, skill = values
    return {
        "id": identity, "organization_id": org, "version": version if version is not None else 1,
        "status": status, "current_turn_id": turn, "candidate_input_completed_at": completed,
        "agent_runtime": {"floor": floor, "active_performance_id": performance,
                          "completion_emitted_at": emitted, "takeover": {"status": takeover},
                          "calibration_status": calibration, "calibration_updated_at": calibration_at},
        "plan_snapshot": {"enterprise_skill_snapshot": deepcopy(skill)},
        "agent_events": ([{"type": "conversation.act.selected", "event_id": selected[0],
                           "payload": {"act_id": selected[1], "approved": selected[2]}}] if selected else []),
    }


def project_realtime_state(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    def path_value(path):
        value = item
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        return value

    def container_type(path):
        value = path_value(path)
        return "object" if isinstance(value, dict) else "null" if value is None else "invalid"

    events = item.get("agent_events")
    selected = next((event for event in reversed(events) if isinstance(event, dict)
                     and event.get("type") == "conversation.act.selected"), None) if isinstance(events, list) else None
    payload = selected.get("payload") if selected else None
    selected_values = ([selected.get("event_id"),
                        payload.get("act_id") if isinstance(payload, dict) else None,
                        payload.get("approved") if isinstance(payload, dict) else None] if selected else None)
    return realtime_state_from_values(
        [path_value(path) for path in REALTIME_PATHS], selected_values,
        plan_type=container_type(("plan_snapshot",)), runtime_type=container_type(("agent_runtime",)),
        takeover_type=container_type(("agent_runtime", "takeover")),
    )
