"""One combined primary-question limit, independent of source and follow-ups.

Frozen assessment/scoring contracts remain historical facts. This execution
policy only limits which new primary questions may be activated.
"""
from copy import deepcopy


MAX_PRIMARY_QUESTIONS = 10
NORMAL_MIN_PRIMARY_QUESTIONS = 8
PRIMARY_QUESTION_POLICY_VERSION = "combined_primary_questions.v1"
_TERMINAL = {"completed", "report_generating", "report_ready", "cancelled"}


def primary_question_budget(session):
    plan = session.get("plan_snapshot") or {}
    adaptive = session.get("execution_schema_version") == 3 or plan.get("execution_schema_version") == 3
    contract = (plan.get("assessment_contract") or session.get("assessment_contract") or {}) if adaptive else {}
    frozen = contract.get("budget", {}).get("max_root_questions")
    if frozen is None:
        frozen = len([turn for turn in session.get("turns", []) if not turn.get("is_followup")])
    recorded = session.get("execution_question_budget")
    if recorded:
        return deepcopy(recorded)
    # Do not retroactively reinterpret completed interviews on read/report.
    historical = session.get("candidate_input_completed_at") or session.get("status") in _TERMINAL
    return {"policy_version": PRIMARY_QUESTION_POLICY_VERSION,
            "max_root_questions": int(frozen) if historical else min(MAX_PRIMARY_QUESTIONS, int(frozen)),
            "frozen_max_root_questions": int(frozen)}


def apply_primary_question_budget(session, now):
    """Record policy once in the lifecycle's isolated transactional copy."""
    if (session.get("execution_question_budget") or session.get("candidate_input_completed_at")
            or session.get("status") in _TERMINAL):
        return None
    value = {**primary_question_budget(session), "applied_at": now}
    if value["max_root_questions"] == value["frozen_max_root_questions"]:
        return None
    session["execution_question_budget"] = value
    return deepcopy(value)


def started_primary_question_count(session):
    # A pending blueprint is not presented, even if an imported/copied legacy
    # document retains a start timestamp. Its state owns activation semantics.
    return sum(not turn.get("is_followup") and turn.get("status") != "pending"
               and bool(turn.get("started_at") or turn.get("status") in
               {"asking", "answering", "transcribing", "evaluating", "answered", "completed"})
               for turn in session.get("turns", []))


def has_budget_skipped_questions(session):
    return any(turn.get("status") == "skipped"
               and turn.get("skip_reason") == "primary_question_budget_exhausted"
               for turn in session.get("turns", []))
