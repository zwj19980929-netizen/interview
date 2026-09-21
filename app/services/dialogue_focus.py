"""Resolve the last question actually addressed to the candidate, within one turn.

This is presentation context, never answer evidence or permission to add a probe.
Formal probes remain separate InterviewTurns with their own capture and answer.
"""
from typing import Any, Dict


def question_for_repeat(turn: Dict[str, Any]) -> str:
    acts = turn.get("conversation_acts") or []
    focus = turn.get("dialogue_focus") or {}
    for act in reversed(acts):
        if (act.get("act_id") == focus.get("act_id")
                and act.get("turn_id") == turn.get("id")):
            return str(act.get("text") or "")
    for act in reversed(acts):
        if act.get("act_type") in {"question", "followup"}:
            return str(act.get("text") or "")
        # v2 could accidentally ask substantive questions on the reception
        # channel. Repeating its actual words is safe; inventing historical
        # child turns/answers from that mixed recording would not be.
        if (act.get("act_type") == "conversation_acknowledgement"
                and act.get("prompt_version") == "conversation_reception.v2"
                and any(mark in str(act.get("text") or "") for mark in ("?", "？"))):
            return str(act["text"])
    return str(turn.get("question_spoken_text") or turn.get("question_snapshot", {}).get("question_text") or "")
