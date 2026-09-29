"""Source-bound proof that a specific content clarification was delivered."""
from copy import deepcopy
import hashlib

from app.core.errors import ApiError
from app.core.prompt.contracts import CLARIFICATION_SPEECH_VERSION, clarification_speech


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validated_content_clarification(session, turn, transcript, context):
    if context is None:
        return None
    required = {"version", "request_text", "focus_quote", "reply_text", "question_act_id", "delivered"}
    valid = isinstance(context, dict) and set(context) == required
    if valid:
        valid = (context["version"] == "content_clarification.v1" and context["delivered"] is True
                 and all(isinstance(context[k], str) and context[k].strip()
                         for k in required - {"delivered"}))
    if valid:
        request, focus, reply = (context[k] for k in ("request_text", "focus_quote", "reply_text"))
        receipt = turn.get("content_clarification") or {}
        acts = [a for a in turn.get("conversation_acts", []) if a.get("act_id") == context["question_act_id"]]
        valid = (receipt.get("status") == "delivered" and bool(receipt.get("capture_id"))
                 and receipt.get("question_act_id") == context["question_act_id"]
                 and receipt.get("request_sha256") == text_hash(request)
                 and receipt.get("focus_sha256") == text_hash(focus)
                 and focus in request and transcript.startswith(request)
                 and transcript[len(request):].strip() == reply
                 and len(acts) == 1 and acts[0].get("turn_id") == turn["id"]
                 and acts[0].get("prompt_version") == CLARIFICATION_SPEECH_VERSION
                 and acts[0].get("text") == clarification_speech(focus)
                 and any(e.get("type") == "conversation.act.selected" and e.get("turn_id") == turn["id"]
                         and (e.get("payload") or {}).get("act_id") == context["question_act_id"]
                         and (e.get("payload") or {}).get("approved") is True
                         and (e.get("payload") or {}).get("text") == clarification_speech(focus)
                         for e in session.get("agent_events", [])))
    if not valid:
        raise ApiError("CONTENT_CLARIFICATION_REQUIRED", "A delivered current content clarification is required.", status_code=409)
    return deepcopy(context)
