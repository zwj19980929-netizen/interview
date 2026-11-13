"""Durable approved selections and performance-bound delivery facts.

These facts survive the client replay window. They never authorize completion;
the separate content/withdrawal handshakes still validate current evidence.
"""
from copy import deepcopy
import hashlib


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def selected_events(session, turn):
    """Return verified selections, deduplicating durable and replay receipts."""
    acts = {}
    for act in turn.get("conversation_acts") or []:
        if (isinstance(act, dict) and act.get("turn_id") in {None, turn.get("id")}
                and isinstance(act.get("act_id"), str) and act["act_id"]):
            acts.setdefault(act["act_id"], []).append(act)
    events = {}
    for act_id, matches in acts.items():
        if len(matches) != 1:
            continue
        act = matches[0]
        text = act.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        for receipt in act.get("selection_receipts") or []:
            if (receipt.get("version") != "conversation_selection.v1"
                    or receipt.get("turn_id") != turn["id"] or receipt.get("act_id") != act_id
                    or receipt.get("text_sha256") != _digest(text)
                    or type(receipt.get("session_sequence")) is not int or receipt["session_sequence"] < 1
                    or not isinstance(receipt.get("event_id"), str) or not receipt["event_id"]
                    or not isinstance(receipt.get("occurred_at"), str) or not receipt["occurred_at"]):
                continue
            events[receipt["event_id"]] = {
                **{key: receipt[key] for key in ("event_id", "turn_id", "session_sequence", "occurred_at")},
                "type": "conversation.act.selected", "payload": {"act_id": act_id, "text": text, "approved": True},
                "delivery": deepcopy(receipt.get("delivery") or {"status": "selected"}),
            }
    legacy = []
    for event in session.get("agent_events") or []:
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") or {}
        if not isinstance(payload, dict) or not isinstance(payload.get("act_id"), str):
            continue
        matches = acts.get(payload.get("act_id"), [])
        if (event.get("type") == "conversation.act.selected" and event.get("turn_id") == turn.get("id")
                and len(matches) == 1 and payload.get("approved") is True
                and payload.get("text") == matches[0].get("text")
                and event.get("event_id") not in events):
            legacy.append({**event, "delivery": {"status": "unknown"}})
    return list(events.values()) + legacy


def record_selection(session, event):
    if event.get("type") != "conversation.act.selected":
        return
    payload = event.get("payload") or {}
    turn = next((t for t in session.get("turns", []) if t.get("id") == event.get("turn_id")), None)
    target = ((session.get("agent_runtime") or {}).get("session_conversation_acts", [])
              if event.get("turn_id") is None else (turn or {}).get("conversation_acts", []))
    acts = [a for a in target
            if a.get("act_id") == payload.get("act_id") and a.get("turn_id") == event.get("turn_id")]
    if (len(acts) != 1 or payload.get("approved") is not True
            or not acts[0].get("text") or payload.get("text") != acts[0]["text"]):
        return
    receipts = acts[0].setdefault("selection_receipts", [])
    if any(r.get("event_id") == event["event_id"] for r in receipts):
        return
    receipts.append({"version": "conversation_selection.v1", "act_id": acts[0]["act_id"],
                     **{key: event[key] for key in ("event_id", "turn_id", "session_sequence", "occurred_at")},
                     "text_sha256": _digest(acts[0]["text"]), "delivery": {"status": "selected"}})
    del receipts[:-8]


def update_delivery(session, *, event_id, performance_id, status, updated_at):
    """Bind delivery to one selection; only an active failed player may retry.

    The browser retains an approved performance after a recoverable playback
    failure. Its retry can finish that same delivery while it remains active;
    cleared or replaced performances cannot revive a failed receipt.
    """
    if not event_id or not performance_id or status not in {"selected", "playing", "delivered", "interrupted", "failed"}:
        return False
    for acts in _delivery_groups(session):
        for act in acts:
            for receipt in act.get("selection_receipts", []):
                if receipt.get("event_id") != event_id:
                    continue
                delivery = receipt.get("delivery") or {}
                if (delivery.get("performance_id") not in {None, performance_id}
                        or delivery.get("status") in {"delivered", "interrupted"}
                        or (delivery.get("status") == "playing" and status == "selected")):
                    return False
                if delivery.get("status") == "failed":
                    active = session.get("agent_runtime") or {}
                    if (status not in {"playing", "delivered"}
                            or delivery.get("performance_id") != performance_id
                            or active.get("active_performance_id") != performance_id
                            or active.get("active_expression_act_event_id") != event_id):
                        return False
                receipt["delivery"] = {"performance_id": performance_id, "status": status, "updated_at": updated_at}
                return True
    return False


def mark_unplayed_selection(session, *, event_id, status, updated_at):
    """Record failed/cancelled preparation only before a performance exists."""
    if not event_id or status not in {"failed", "interrupted"}:
        return False
    for acts in _delivery_groups(session):
        for act in acts:
            for receipt in act.get("selection_receipts", []):
                delivery = receipt.get("delivery") or {}
                if (receipt.get("event_id") == event_id and not delivery.get("performance_id")
                        and delivery.get("status") == "selected"):
                    receipt["delivery"] = {"status": status, "updated_at": updated_at}
                    return True
    return False


def _delivery_groups(session):
    # Session speech has delivery facts but is never returned by
    # selected_events(), which only supplies evidence for a real turn.
    for turn in session.get("turns", []):
        yield [act for act in turn.get("conversation_acts", []) if act.get("turn_id") == turn.get("id")]
    yield [act for act in (session.get("agent_runtime") or {}).get("session_conversation_acts", [])
           if act.get("turn_id") is None]
