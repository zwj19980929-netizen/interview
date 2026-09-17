"""Bounded, source-linked conversational memory; never a replacement for Evidence."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Dict

from app.domain.adaptive_interview import closed_source_question_ids


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
                   "question_id": t.get("question_id")} for t in session.get("turns", [])],
        "input_completed": session.get("candidate_input_completed_at"),
        "scheduled_end_at": session.get("scheduled_end_at"),
    }
    raw = json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def conversation_memory(session: Dict[str, Any]) -> Dict[str, Any]:
    dialogue = []
    preferences = []
    for turn in session.get("turns", [])[-8:]:
        utterances = turn.get("utterances") or []
        if utterances:
            dialogue.append({"speaker": "interviewer", "turn_id": turn["id"],
                             "text": str(turn.get("question_spoken_text") or "")[:1000]})
        for utterance in utterances[-2:]:
            dialogue.append({"speaker": "candidate", "turn_id": turn["id"],
                             "utterance_id": utterance.get("utterance_id"), "text": str(utterance.get("text") or "")[:1600]})
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
                  "answered": item.get("delivery_status") == "delivered"} for turn in session.get("turns", [])[-8:]
                 for item in turn.get("company_question_exchanges", [])[-8:]]
    return {"recent_dialogue": dialogue[-12:], "topic_preferences": preferences[-8:],
            "company_question_exchanges": exchanges[-12:],
            "competency_evidence": ledger, "agenda": agenda[-50:]}
