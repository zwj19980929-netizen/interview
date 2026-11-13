"""Locate acknowledged requests inside a complete candidate transcript.

This is conversational context, never edited answer evidence or permission to
finish. An empty current_speech means the latest request has received a selected
response; that request can still describe the candidate's current intention.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Dict
from app.services.conversation_delivery import selected_events
from app.domain.speech_quality import TranscriptConfidenceSpan


_KINDS = frozenset({
    "wait", "continue", "presence", "repeat", "question_clarification",
    "transcript_correction", "audio_problem", "pause", "unclear", "interview_dialogue",
})
_MAX_EXCHANGES = 6


def capture_source_fingerprint(source):
    """Identify one durable audio capture without leaking a storage location."""
    if isinstance(source, str):
        match = re.fullmatch(r"evidence-checkpoint://([A-Za-z0-9_.:-]{1,128})/([1-9][0-9]*)/([1-9][0-9]*)", source)
        if match is None:
            return None
        identity = [match[1], int(match[2])]
    elif isinstance(source, dict):
        stream, revision = source.get("stream_id"), source.get("capture_revision")
        if (not isinstance(stream, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", stream)
                or type(revision) is not int or revision < 1):
            return None
        identity = [stream, revision]
    else:
        return None
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def _source_segments(transcript, segments):
    """Require complete, verbatim and temporally ordered server segments."""
    if not isinstance(segments, (list, tuple)) or not segments or len(segments) > 12_000:
        return []
    result, cursor, audio_start, audio_end = [], 0, 0, 0
    try:
        for raw in segments:
            row = raw.model_dump() if isinstance(raw, TranscriptConfidenceSpan) else raw
            span = TranscriptConfidenceSpan.model_validate(row)
            if (span.start != cursor or transcript[span.start:span.end] != span.text
                    or span.start_ms < audio_start or span.end_ms < audio_end
                    or (span.start_ms == audio_start and span.end_ms == audio_end)
                    or span.end_ms <= span.start_ms):
                return []
            result.append(span)
            cursor, audio_start, audio_end = span.end, span.start_ms, span.end_ms
    except (ValueError, TypeError):
        return []
    return result if cursor == len(transcript) else []


def _segment_identity(segment):
    return {"start_ms": segment.start_ms, "end_ms": segment.end_ms,
            "text_sha256": hashlib.sha256(segment.text.encode()).hexdigest()}


def reception_source_boundary(transcript, request, *, segments, source_capture):
    """Bind the newly received suffix to server text and its audio occurrence."""
    source = capture_source_fingerprint(source_capture)
    if (source is None or not isinstance(request, str) or not request.strip()
            or not transcript.endswith(request)):
        return None
    spans = _source_segments(transcript, segments)
    start = len(transcript) - len(request)
    selected = [span for span in spans if span.end > start]
    if not selected or len(selected) > 64:
        return None
    return {"version": "conversation_source_boundary.v1", "capture_fingerprint": source,
            "request_start": start, "request_end": len(transcript),
            "prefix_sha256": hashlib.sha256(transcript.encode()).hexdigest(),
            "segments": [_segment_identity(span) for span in selected]}


def _locate_source_boundary(proof, transcript, request, *, segments, source_capture, cursor):
    if (not isinstance(proof, dict) or set(proof) != {"version", "capture_fingerprint", "request_start",
            "request_end", "prefix_sha256", "segments"}
            or proof.get("version") != "conversation_source_boundary.v1"):
        return None
    source = capture_source_fingerprint(source_capture)
    if source is None or proof.get("capture_fingerprint") != source:
        return None
    start, end, prefix = proof.get("request_start"), proof.get("request_end"), proof.get("prefix_sha256")
    if (type(start) is not int or type(end) is not int or not cursor <= start < end <= len(transcript)
            or transcript[start:end] != request or not isinstance(prefix, str)
            or not re.fullmatch(r"[0-9a-f]{64}", prefix)
            or not hmac.compare_digest(prefix, hashlib.sha256(transcript[:end].encode()).hexdigest())):
        return None
    spans = _source_segments(transcript, segments)
    selected = [span for span in spans if span.start < end and span.end > start]
    recorded = proof.get("segments")
    if (not selected or not isinstance(recorded, list) or not 1 <= len(recorded) <= 64
            or recorded != [_segment_identity(span) for span in selected]):
        return None
    return start, end


def conversation_input_context(
    session: Dict[str, Any], turn: Dict[str, Any], transcript: str,
    *, segments=(), source_capture=None,
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
    for event in selected_events(session, turn):
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
        if "source_boundary" in receipt:
            located = _locate_source_boundary(receipt["source_boundary"], transcript, request,
                segments=segments, source_capture=source_capture, cursor=cursor)
            if located is None:
                # A supplied but stale source cannot fall back to a text guess.
                continue
            start, end = located
        else:
            start = transcript.find(request, cursor)
            if start < 0 or transcript.find(request, start + 1) >= 0:
                # Legacy receipts cannot distinguish two identical requests.
                continue
            end = start + len(request)
        exchange = {
            "exchange_id": receipt["exchange_id"], "act_id": act["act_id"],
            "selected_event_id": event_id, "session_sequence": sequence,
            "request_text": request, "reply_text": reply[:1000],
            "request_start": start, "request_end": end,
            "delivery_status": (event.get("delivery") or {}).get("status", "unknown"),
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
