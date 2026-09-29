"""Locate acknowledged requests inside a complete candidate transcript.

This is conversational context, never edited answer evidence or permission to
finish. An empty current_speech means the latest request has received a selected
response; that request can still describe the candidate's current intention.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Dict


_KINDS = frozenset({
    "wait", "continue", "presence", "repeat", "question_clarification",
    "transcript_correction", "audio_problem", "pause", "unclear", "interview_dialogue",
})
_MAX_EXCHANGES = 6


def conversation_input_context(
    session: Dict[str, Any], turn: Dict[str, Any], transcript: str,
) -> Dict[str, Any]:
    """Project source-linked responses and the latest reliably located speech.

    A receipt must name one actual approved selection in this turn. Its complete
    request hash must match, and its request must occur exactly once after the
    preceding located receipt. Truncated requests, rewritten ASR and repeated
    phrases cannot manufacture a boundary. No text is removed from the source
    transcript, and gaps between received requests remain answer evidence.
    """
    if not isinstance(transcript, str):
        raise TypeError("Conversation input requires a text transcript")
    result: Dict[str, Any] = {
        "version": "conversation_input_context.v1",
        "current_speech": transcript,
        "current_speech_start": 0,
        "responded_exchanges": [],
    }
    turn_id = turn.get("id")
    if not turn_id or session.get("current_turn_id") != turn_id:
        return result

    acts = {}
    for act in turn.get("conversation_acts") or []:
        if (isinstance(act, dict) and act.get("turn_id") == turn_id
                and isinstance(act.get("act_id"), str) and act["act_id"]):
            acts.setdefault(act["act_id"], []).append(act)
    events = {}
    for event in session.get("agent_events") or []:
        if (isinstance(event, dict) and event.get("type") == "conversation.act.selected"
                and event.get("turn_id") == turn_id
                and isinstance(event.get("event_id"), str) and event["event_id"]):
            events.setdefault(event["event_id"], []).append(event)

    receipts = [item for item in (turn.get("reception_exchanges") or [])[-_MAX_EXCHANGES:]
                if isinstance(item, dict) and type(item.get("session_sequence")) is int
                and item["session_sequence"] > 0]
    receipts.sort(key=lambda item: item["session_sequence"])
    cursor, used_events, used_sequences = 0, set(), set()
    for receipt in receipts:
        request, digest = receipt.get("request_text"), receipt.get("request_sha256")
        event_id, sequence = receipt.get("selected_event_id"), receipt["session_sequence"]
        if (not isinstance(request, str) or not request.strip() or len(request) > 1600
                or not isinstance(digest, str) or len(digest) != 64 or not digest.isascii()
                or not isinstance(event_id, str) or not isinstance(receipt.get("act_id"), str)
                or not isinstance(receipt.get("exchange_id"), str) or not receipt["exchange_id"]
                or not isinstance(receipt.get("source_stream_id"), str) or not receipt["source_stream_id"]
                or event_id in used_events or sequence in used_sequences
                or not hmac.compare_digest(hashlib.sha256(request.encode("utf-8")).hexdigest(), digest)):
            continue
        matches = events.get(event_id, [])
        matching_acts = acts.get(receipt.get("act_id"), [])
        if len(matches) != 1 or len(matching_acts) != 1:
            continue
        event, act = matches[0], matching_acts[0]
        payload = event.get("payload") or {}
        reply = act.get("text")
        if (event.get("session_sequence") != sequence or type(event.get("session_sequence")) is not int
                or not isinstance(payload, dict)
                or payload.get("approved") is not True or payload.get("act_id") != act["act_id"]
                or not isinstance(reply, str) or not reply.strip() or payload.get("text") != reply):
            continue
        start = transcript.find(request, cursor)
        if start < 0 or transcript.find(request, start + 1) >= 0:
            # A repeated request may be new speech. Do not silently consume it
            # as an earlier identical question, even with a valid old receipt.
            continue
        end = start + len(request)
        exchange = {
            "exchange_id": receipt["exchange_id"], "act_id": act["act_id"],
            "selected_event_id": event_id, "session_sequence": sequence,
            "request_text": request, "reply_text": reply[:1000],
            "request_start": start, "request_end": end,
        }
        if isinstance(receipt.get("kind"), str) and receipt["kind"] in _KINDS:
            exchange["kind"] = receipt["kind"]
        result["responded_exchanges"].append(exchange)
        used_events.add(event_id)
        used_sequences.add(sequence)
        cursor = end

    # Keep offsets exact in the original final while avoiding whitespace-only
    # new requests. The complete original remains available to claim extraction.
    if cursor:
        while cursor < len(transcript) and transcript[cursor].isspace():
            cursor += 1
        result["current_speech_start"] = cursor
        result["current_speech"] = transcript[cursor:]
    return result
