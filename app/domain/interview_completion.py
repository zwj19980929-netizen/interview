"""Explain an end from frozen limits and recorded facts, without changing history."""
from copy import deepcopy
from datetime import datetime

from app.domain.adaptive_interview import assessment_contract, budget_summary, is_adaptive
from app.domain.primary_question_budget import has_budget_skipped_questions, primary_question_budget


def presented_question_counts(session):
    answers = {item.get("turn_id") for item in session.get("answers", [])}
    counts = {"primary": 0, "followup": 0}
    for turn in session.get("turns", []):
        if turn.get("status") == "pending":
            continue
        if (turn.get("id") in answers or turn.get("started_at") or turn.get("status") in
                {"asking", "answering", "transcribing", "evaluating", "answered", "completed"}):
            counts["followup" if turn.get("is_followup") else "primary"] += 1
    return counts


def _instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def budget_completion_detail(session, now, *, allow_pool=False):
    if not is_adaptive(session):
        if has_budget_skipped_questions(session):
            return {"trigger": "primary_question_limit",
                    "primary_question_limit": primary_question_budget(session)["max_root_questions"]}
        return None
    contract = assessment_contract(session)
    limits = contract["budget"]
    elapsed = None
    if session.get("started_at"):
        elapsed = max(0, (_instant(now) - _instant(session["started_at"])).total_seconds())
    remaining = limits["max_duration_seconds"] - limits["closing_reserve_seconds"]
    limit_seconds = limits["max_duration_seconds"]
    time_exhausted = elapsed is not None and elapsed >= remaining
    if session.get("scheduled_end_at"):
        time_exhausted = time_exhausted or (
            _instant(session["scheduled_end_at"]) - _instant(now)).total_seconds() <= limits["closing_reserve_seconds"]
        if session.get("started_at"):
            limit_seconds = min(limit_seconds, max(0, (
                _instant(session["scheduled_end_at"]) - _instant(session["started_at"])).total_seconds()))
    if time_exhausted:
        return {"trigger": "time_limit", "limit_minutes": round(limit_seconds / 60, 2),
                **({"elapsed_seconds": int(elapsed)} if elapsed is not None else {})}
    limit = primary_question_budget(session)["max_root_questions"]
    if presented_question_counts(session)["primary"] >= limit:
        return {"trigger": "primary_question_limit", "primary_question_limit": limit}
    # Only the ending transaction may prove pool exhaustion: afterwards skipped
    # turns can close sources, so historical reads must not infer it from them.
    if allow_pool and budget_summary(session, now)["remaining_eligible_units"] == 0:
        return {"trigger": "candidate_pool_exhausted"}
    return None


def completion_projection(session):
    recorded = session.get("candidate_input_completion_detail")
    if recorded and recorded.get("trigger") in {"time_limit", "primary_question_limit", "candidate_pool_exhausted"}:
        return {key: deepcopy(value) for key, value in recorded.items()
                if key in {"trigger", "limit_minutes", "elapsed_seconds", "primary_question_limit"}}
    if (session.get("candidate_input_completion_reason") != "budget_exhausted"
            and not has_budget_skipped_questions(session)):
        return None
    now = session.get("candidate_input_completed_at") or session.get("completed_at")
    if not now:
        return None
    try:
        return budget_completion_detail(session, now)
    except (KeyError, TypeError, ValueError):
        return None
