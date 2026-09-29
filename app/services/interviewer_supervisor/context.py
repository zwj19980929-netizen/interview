"""Bounded, source-linked conversational memory; never a replacement for Evidence."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Dict

from app.core.errors import ApiError
from app.domain.adaptive_interview import closed_source_question_ids
from app.services.dialogue_history import recent_turn_dialogue


def assessment_contract(session: Dict[str, Any]) -> Dict[str, Any]:
    return (session.get("plan_snapshot") or {}).get("assessment_contract") or {}


def is_adaptive(session: Dict[str, Any]) -> bool:
    return (session.get("plan_snapshot") or {}).get("execution_schema_version") == 3


def context_fingerprint(session: Dict[str, Any]) -> str:
    facts = {
        "organization_id": session.get("organization_id"), "interview_id": session["id"],
        "decision_revision": session.get("decision_revision", 0),
        "current_turn_id": session.get("current_turn_id"),
        "control_events": [event.get("id") for event in session.get("lifecycle_events", [])
                           if event.get("type") in {"interview.paused", "interview.resumed", "interview.recovered", "interview.timed_out"}],
        "takeover": {key: (session.get("agent_runtime", {}).get("takeover") or {}).get(key)
                     for key in ("lease_id", "version", "status", "expires_at")},
        "contract": assessment_contract(session),
        "skill_snapshot": (session.get("plan_snapshot") or {}).get("enterprise_skill_snapshot"),
        "company_context": (session.get("plan_snapshot") or {}).get("company_context"),
        "agenda": session.get("agenda"), "competency_evidence": session.get("competency_evidence"),
        "turns": [{"id": t["id"], "understanding": t.get("current_understanding"),
                   "dialogue_focus": t.get("dialogue_focus"),
                   "question_id": t.get("question_id")} for t in session.get("turns", [])],
        "answer_sources": [{"id": answer.get("id"), "turn_id": answer.get("turn_id"),
                            "transcript_sha256": hashlib.sha256(
                                str(answer.get("final_transcript") or "").encode()).hexdigest()}
                           for answer in session.get("answers", [])],
        "input_completed": session.get("candidate_input_completed_at"),
        "scheduled_end_at": session.get("scheduled_end_at"),
    }
    raw = json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def conversation_memory(session: Dict[str, Any]) -> Dict[str, Any]:
    dialogue = []
    preferences = []
    turns = session.get("turns", [])
    current_index = next((index for index, turn in enumerate(turns)
                          if turn.get("id") == session.get("current_turn_id")), len(turns) - 1)
    # Preallocated future turns are agenda, not past conversation. Keep the
    # active question in memory even when the fixed plan has many later turns.
    conversational_turns = turns[:current_index + 1]
    for turn in conversational_turns[-8:]:
        dialogue.extend(recent_turn_dialogue(session, turn))
        understanding = turn.get("current_understanding") or {}
        if understanding.get("intent") == "answer_declined" or understanding.get("followup_allowed") is False:
            preferences.append({"turn_id": turn["id"], "question_id": turn.get("question_id"),
                                "intent": understanding.get("turn_intent") or understanding.get("intent"),
                                "evidence_quotes": deepcopy(understanding.get("evidence_quotes") or [])[:2]})
    # Derive the compact evidence ledger from committed server facts. It stays
    # useful after old dialogue leaves the recent window, without a second PII store.
    answered = {answer["turn_id"]: answer for answer in session.get("answers", [])}
    closed_sources = closed_source_question_ids(session)
    ledger = {}
    agenda = []
    for turn in session.get("turns", []):
        understanding = turn.get("current_understanding") or {}
        root = next((item for item in session.get("turns", []) if item["id"] == turn.get("root_turn_id")), turn)
        if not turn.get("is_followup"):
            agenda.append({"turn_id": turn["id"], "question_id": turn.get("question_id"),
                           "inquiry_unit_id": turn.get("inquiry_unit_id"), "answered": turn["id"] in answered,
                           "topic_closed": turn.get("question_id") in closed_sources})
        if turn["id"] not in answered:
            continue
        for competency in root.get("competency_ids", []):
            row = ledger.setdefault(competency, {"evidence_turn_ids": [], "observations": []})
            row["evidence_turn_ids"].append(turn["id"])
            row["observations"].append({"turn_id": turn["id"], "answer_id": answered[turn["id"]]["id"],
                "intent": understanding.get("turn_intent") or understanding.get("intent"),
                "claims": [{"claim": str(claim.get("claim") or "")[:160],
                            "evidence_quote": str(claim.get("evidence_quote") or "")[:160]}
                           for claim in understanding.get("claims", [])[:1]],
                "assessed_rubric_point_ids": deepcopy(turn.get("assessed_rubric_point_ids") or []),
            })
            del row["observations"][:-2]
    exchanges = [{"turn_id": turn["id"], "question_span": deepcopy(item["question_span"]),
                  "answered": item.get("delivery_status") == "delivered"} for turn in conversational_turns[-8:]
                 for item in turn.get("company_question_exchanges", [])[-8:]]
    return {"recent_dialogue": dialogue[-12:], "topic_preferences": preferences[-8:],
            "company_question_exchanges": exchanges[-12:],
            "competency_evidence": ledger, "agenda": agenda[-50:]}


def committed_turn_evidence(session: Dict[str, Any], turn_id: str) -> Dict[str, Any]:
    """Resolve an old in-session turn without widening the rolling memory.

    Read the same immutable source snapshot as the supervisor. A quoted claim
    must still exist in the committed answer; this grants no new scoring or
    completion authority and cannot retrieve another interview.
    """
    def unavailable():
        return ApiError("INTERVIEW_TOOL_REFERENCE_INVALID",
                        "A unique committed turn in the current interview is required.", status_code=409)

    if not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 128:
        raise unavailable()
    turns = [turn for turn in session.get("turns", []) if turn.get("id") == turn_id]
    answers = [answer for answer in session.get("answers", []) if answer.get("turn_id") == turn_id]
    if len(turns) != 1 or len(answers) != 1:
        raise unavailable()
    turn, answer = turns[0], answers[0]
    transcript = answer.get("final_transcript")
    if not isinstance(transcript, str):
        raise unavailable()
    understanding = turn.get("current_understanding") or {}
    claims = []
    for item in (understanding.get("claims") or [])[:12]:
        if not isinstance(item, dict):
            continue
        claim, quote = item.get("claim"), item.get("evidence_quote")
        if (not isinstance(claim, str) or not 1 <= len(claim.strip()) <= 600
                or not isinstance(quote, str) or not 1 <= len(quote.strip()) <= 300):
            continue
        count = transcript.count(quote)
        if not count:
            continue
        start = transcript.index(quote) if count == 1 else None
        claims.append({"claim": claim, "evidence_quote": quote, "occurrences": count,
                       "start": start, "end": start + len(quote) if start is not None else None})
    revision = understanding.get("revision")
    return {
        "turn_id": turn_id, "root_turn_id": turn.get("root_turn_id") or turn_id,
        "parent_turn_id": turn.get("parent_turn_id"), "is_followup": bool(turn.get("is_followup")),
        "question_text": str(turn.get("question_spoken_text") or "")[:2000],
        "answer_id": answer["id"], "answer_summary": str(understanding.get("answer_summary") or "")[:800],
        "understanding_revision": revision if type(revision) is int and revision >= 1 else None,
        "source": "committed_answer", "claims": claims,
    }
