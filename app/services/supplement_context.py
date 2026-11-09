"""Private supplement phase from current-turn delivery; never completion authority."""

from typing import Any, Dict

from app.core.prompt.contracts import SUPPLEMENT_SPEECH, SUPPLEMENT_SPEECH_VERSION
from app.services.conversation_delivery import selected_events


def supplement_context(session: Dict[str, Any], turn: Dict[str, Any], *,
                       completion_confirmed: bool = False) -> Dict[str, Any]:
    """Project delivery separately from the caller's existing confirmation grant.

    Runtime UI state and candidate words cannot establish a delivered question.
    A newer selected/interrupted check also cannot inherit an older delivery.
    The result describes context only; callers retain all evidence/owner gates.
    """
    result = {"version": "supplement_context.v1", "stage": "not_delivered"}
    if (session.get("status") != "in_progress" or not turn.get("id")
            or session.get("current_turn_id") != turn["id"]
            or turn.get("status") not in {"asking", "transcribing"}):
        return result
    if completion_confirmed is True:
        result["stage"] = "confirmed"
    acts = {act.get("act_id"): act for act in turn.get("conversation_acts", [])
            if isinstance(act, dict) and act.get("turn_id") == turn["id"]
            and act.get("act_type") in {"supplement_check", "supplement_clarify"}}
    events = [event for event in selected_events(session, turn)
              if (event.get("payload") or {}).get("act_id") in acts]
    if not events:
        return result
    event = max(events, key=lambda item: item.get("session_sequence", 0))
    act = acts[event["payload"]["act_id"]]
    delivery = event.get("delivery") or {}
    expected = SUPPLEMENT_SPEECH["check" if act["act_type"] == "supplement_check" else "clarify"]
    if (act.get("prompt_version") != SUPPLEMENT_SPEECH_VERSION or act.get("text") != expected
            or delivery.get("status") != "delivered"
            or not isinstance(delivery.get("performance_id"), str) or not delivery["performance_id"]):
        return result
    result.update(stage="confirmed" if completion_confirmed is True else "awaiting_reply",
                  question_act_id=act["act_id"], selection_event_id=event["event_id"],
                  performance_id=delivery["performance_id"], question=expected)
    return result
