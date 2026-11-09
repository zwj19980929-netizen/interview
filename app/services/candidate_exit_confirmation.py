"""Validate the server's separate, source-bound withdrawal exchange."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from typing import Any

from app.core.errors import ApiError
from app.core.prompt.candidate_exit import EXIT_CONFIRMATION_SPEECH, EXIT_CONFIRMATION_SPEECH_VERSION
from app.services.conversation_delivery import selected_events


@dataclass(frozen=True)
class PreparedCandidateExit:
    """Private control proof, deliberately without an answer understanding."""

    interview_id: str
    turn_id: str
    text: str
    stt_fingerprint: str
    context_fingerprint: str
    exit_confirmation: dict[str, Any]
    capture_binding: Any = None


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validated_exit_context(session, turn, transcript, context):
    if context is None:
        return None
    required = {"version", "interview_id", "turn_id", "capture_id", "question_act_id", "question",
                "request_text", "reply_text", "decision", "transcript_sha256"}
    valid = (isinstance(context, dict) and set(context) == required
             and all(isinstance(context.get(key), str) and context[key] for key in required))
    if valid:
        request, reply = context["request_text"], context["reply_text"]
        fresh = transcript[len(request):].lstrip() if transcript.startswith(request) else ""
        reply_end = len(transcript) - len(fresh) + len(reply)
        acts = [act for act in turn.get("conversation_acts", [])
                if act.get("act_id") == context["question_act_id"] and act.get("turn_id") == turn["id"]]
        selected = [event for event in selected_events(session, turn)
                    if event.get("type") == "conversation.act.selected" and event.get("turn_id") == turn["id"]
                    and (event.get("payload") or {}).get("act_id") == context["question_act_id"]
                    and (event.get("payload") or {}).get("approved") is True
                    and (event.get("payload") or {}).get("text") == EXIT_CONFIRMATION_SPEECH]
        valid = (context["version"] == EXIT_CONFIRMATION_SPEECH_VERSION
                 and context["interview_id"] == session["id"] and context["turn_id"] == turn["id"]
                 and context["question"] == EXIT_CONFIRMATION_SPEECH
                 and context["decision"] in {"confirm", "continue"}
                 and len(acts) == 1 and acts[0].get("prompt_version") == EXIT_CONFIRMATION_SPEECH_VERSION
                 and acts[0].get("text") == EXIT_CONFIRMATION_SPEECH and bool(selected)
                 and bool(fresh) and fresh.startswith(reply)
                 and context["transcript_sha256"] == text_hash(transcript[:reply_end])
                 and (context["decision"] != "confirm" or fresh == reply))
    if not valid:
        raise ApiError("EXIT_CONFIRMATION_REQUIRED", "A separate current withdrawal confirmation is required.", status_code=409)
    return deepcopy(context)


def confirmed_exit_receipt(context, transcript, *, confirmed_at):
    if not context or context.get("decision") != "confirm" or context.get("transcript_sha256") != text_hash(transcript):
        raise ApiError("EXIT_CONFIRMATION_REQUIRED", "A separate current withdrawal confirmation is required.", status_code=409)
    return {"version": context["version"], "state": "confirmed", "confirmed_at": confirmed_at,
            "question_act_id": context["question_act_id"], "capture_id": context["capture_id"],
            "transcript_sha256": context["transcript_sha256"],
            "request_sha256": text_hash(context["request_text"]), "reply_sha256": text_hash(context["reply_text"])}


def validated_delivered_exit_context(session, turn, transcript, context):
    """Require an actual delivered question for the independent exit effect.

    Legacy answer preparations retain their original runtime delivery gate.
    This new control path additionally checks the durable delivery receipt;
    an old delivered selection cannot cover a later unplayed selection.
    """
    context = validated_exit_context(session, turn, transcript, context)
    if context is None or context["decision"] != "confirm":
        raise ApiError("EXIT_CONFIRMATION_REQUIRED", "A fresh confirmed withdrawal is required.", status_code=409)
    selections = [event for event in selected_events(session, turn)
                  if (event.get("payload") or {}).get("act_id") == context["question_act_id"]]
    latest = max(selections, key=lambda item: item.get("session_sequence", 0), default={})
    delivery = latest.get("delivery") or {}
    if delivery.get("status") != "delivered" or not delivery.get("performance_id"):
        raise ApiError("EXIT_CONFIRMATION_REQUIRED", "The withdrawal question was not delivered.", status_code=409)
    return context
