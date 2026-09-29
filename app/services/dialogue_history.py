"""Candidate-visible dialogue in selection order, without approval-only speech.

Selected events establish an interviewer exchange, not proof of audible playback
or answer evidence. Reused acts can appear several times, with each event's time.
"""
from typing import Any, Dict, List


def recent_turn_dialogue(
    session: Dict[str, Any], turn: Dict[str, Any], *, max_acts: int = 4,
    max_utterances: int = 2, candidate_limit: int = 1600, interviewer_limit: int = 1000,
) -> List[Dict[str, Any]]:
    """Project a bounded exchange history, preserving exact source identities."""
    turn_id = turn["id"]
    acts = {item["act_id"]: item for item in (turn.get("conversation_acts") or [])
            if item.get("act_id") and item.get("turn_id") in {None, turn_id}}
    selected = []
    live_requests = {}
    for event in session.get("agent_events") or []:
        if event.get("type") != "conversation.act.selected" or event.get("turn_id") != turn_id:
            continue
        payload = event.get("payload") or {}
        act = acts.get(payload.get("act_id"))
        sequence = event.get("session_sequence")
        occurred_at = event.get("occurred_at")
        if (not act or payload.get("approved") is not True
                or type(sequence) is not int or sequence < 1
                or not isinstance(occurred_at, str) or not occurred_at
                or not isinstance(act.get("text"), str) or not act["text"].strip()
                or payload.get("text") != act["text"]):
            continue
        exchange = next((item for item in reversed(turn.get("reception_exchanges") or [])
                         if item.get("selected_event_id") == event.get("event_id")
                         and item.get("act_id") == act["act_id"]
                         and item.get("session_sequence") == sequence
                         and item.get("exchange_id")
                         and isinstance(item.get("request_text"), str) and item["request_text"].strip()), None)
        if exchange is not None:
            live_requests[sequence] = {"speaker": "candidate", "turn_id": turn_id,
                "exchange_id": exchange["exchange_id"], "text": exchange["request_text"][-candidate_limit:],
                "created_at": occurred_at}
        selected.append((sequence, {"speaker": "interviewer", "turn_id": turn_id,
            "act_id": act["act_id"], "text": act["text"][-interviewer_limit:],
            "created_at": occurred_at}))
    selected.sort(key=lambda item: item[0])
    interviewer = selected[-max_acts:] if max_acts > 0 else []
    utterances = (turn.get("utterances") or [])[-max_utterances:] if max_utterances > 0 else []
    candidate = [{"speaker": "candidate", "turn_id": turn_id,
                  "utterance_id": item.get("utterance_id"), "text": item["text"][-candidate_limit:],
                  "created_at": item.get("created_at") if isinstance(item.get("created_at"), str) else ""}
                 for item in utterances if isinstance(item.get("text"), str) and item["text"].strip()]
    candidate.sort(key=lambda item: item["created_at"])
    result = []
    if not selected and candidate:
        # Legacy/truncated event history can still supply the frozen question
        # as context. It cannot certify that an arbitrary approved act was said.
        question = str(turn.get("question_spoken_text") or "")[:interviewer_limit]
        if question:
            result.append({"speaker": "interviewer", "turn_id": turn_id,
                           "text": question, "created_at": str(turn.get("started_at") or "")})
    index = 0
    for sequence, item in interviewer:
        # Session sequence is authoritative between interviewer events even
        # if the wall clock moves. Place candidate replies by their source time
        # without reordering those repeated selections back to approval time.
        while index < len(candidate) and candidate[index]["created_at"] <= item["created_at"]:
            result.append(candidate[index])
            index += 1
        if sequence in live_requests:
            result.append(live_requests[sequence])
        result.append(item)
    result.extend(candidate[index:])
    return result
