"""Separate the active assessment question from conversational explanations.

An explanation is useful memory, but cannot replace the frozen parent/child
question or create assessment evidence. Explicitly repeating that explanation
uses a separate target; the repeat button always reads the current question.
"""
from typing import Any, Dict


def question_for_repeat(turn: Dict[str, Any]) -> str:
    """Read the current turn's question, including a formal child question."""
    frozen = str(turn.get("question_spoken_text") or "").strip()
    if frozen:
        return frozen
    for act in reversed(turn.get("conversation_acts") or []):
        if (act.get("act_type") in {"question", "followup"}
                and act.get("turn_id") in {None, turn.get("id")}):
            return str(act.get("text") or "")
    return str((turn.get("question_snapshot") or {}).get("question_text") or "")


def clarification_for_repeat(turn: Dict[str, Any]) -> str:
    """Return only the clarification selected for this turn, never another turn."""
    focus_id = (turn.get("dialogue_focus") or {}).get("act_id")
    for act in reversed(turn.get("conversation_acts") or []):
        if (focus_id and act.get("act_id") == focus_id
                and act.get("turn_id") == turn.get("id")
                and act.get("act_type") in {"clarification", "conversation_acknowledgement"}):
            return str(act.get("text") or "")
    return ""


def text_for_repeat(turn: Dict[str, Any], *, target: str = "current_question") -> str:
    if target == "last_clarification":
        return clarification_for_repeat(turn) or question_for_repeat(turn)
    return question_for_repeat(turn)
