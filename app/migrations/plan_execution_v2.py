import argparse
from copy import deepcopy
from typing import Any, Dict, Iterable, Mapping, Optional

from app.core.ids import new_id
from app.core.time import utc_now
from app.domain.question_selection import QuestionSelection
from app.persistence.provider import persistence_for
from app.repositories.provider import get_store


def migrate_plan_document(
    plan: Dict[str, Any],
    questions: Mapping[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Convert the retired writable items representation into canonical bank slots."""
    migrated = deepcopy(plan)
    if int(migrated.get("execution_schema_version", 0)) == 2 and "items" not in migrated:
        return migrated

    slots = deepcopy(migrated.get("bank_slots") or [])
    if not slots:
        for order, item in enumerate(migrated.get("items") or [], start=1):
            question = questions.get(item.get("question_id"))
            if question is None:
                raise ValueError("Plan %s refers to missing question %s." % (plan.get("id"), item.get("question_id")))
            if migrated.get("job_position_id") and question.get("job_position_id") != migrated.get("job_position_id"):
                raise ValueError("Plan %s contains a question from another position." % plan.get("id"))
            if migrated.get("knowledge_base_ids") and question.get("knowledge_base_id") not in migrated["knowledge_base_ids"]:
                raise ValueError("Plan %s contains a question from another knowledge base." % plan.get("id"))
            candidate = {
                "question_id": question["id"],
                "question_version": int(question.get("version", 1)),
                "question_hash": question.get("content_hash"),
            }
            slots.append(
                {
                    "id": item.get("slot_id") or item.get("id") or new_id("slot"),
                    "order": order,
                    "dimension": item.get("dimension") or (question.get("skills") or ["general"])[0],
                    "difficulty": question.get("difficulty", "mid"),
                    "weight": float(item.get("weight", 1.0)),
                    "expected_minutes": int(item.get("expected_minutes", 1)),
                    "candidate_pool": [candidate],
                    "candidate_pool_count": 1,
                    "candidate_pool_hash": QuestionSelection().pool_hash([candidate]),
                    "display_question_id": question["id"],
                    "allow_followup": bool(item.get("allow_followup", True)),
                    "selection_reason": item.get("selection_reason", "由执行模型 v2 迁移固定题目。"),
                }
            )
    if not slots and not migrated.get("experience_question_snapshots"):
        raise ValueError("Plan %s has no executable question." % plan.get("id"))

    migrated["bank_slots"] = slots
    migrated.pop("items", None)
    migrated.pop("canonical_execution_version", None)
    migrated["execution_schema_version"] = 2
    migrated.setdefault("migration_history", []).append(
        {
            "migration": "plan_execution_v2",
            "migrated_at": utc_now(),
            "source": "writable_items" if not plan.get("bank_slots") else "canonical_slots_v1",
        }
    )
    migrated["updated_at"] = utc_now()
    return migrated


def migrate_session_document(session: Dict[str, Any]) -> Dict[str, Any]:
    """Rename the retired plan snapshot items field without changing frozen questions."""
    migrated = deepcopy(session)
    snapshot = migrated.get("plan_snapshot") or {}
    has_snapshot_items = "items" in snapshot
    has_legacy_turn_fields = any(
        "plan_item_id" in turn or "plan_item_snapshot_id" in turn
        for turn in migrated.get("turns", [])
    )
    if not has_snapshot_items and not has_legacy_turn_fields:
        return migrated
    if has_snapshot_items and snapshot.get("question_snapshots") and snapshot["question_snapshots"] != snapshot["items"]:
        raise ValueError("Session %s contains conflicting question snapshot representations." % session.get("id"))
    if has_snapshot_items:
        snapshot["question_snapshots"] = deepcopy(snapshot.get("items") or [])
        snapshot.pop("items", None)
    migrated["plan_snapshot"] = snapshot
    for turn in migrated.get("turns", []):
        blueprint_id = turn.get("turn_blueprint_id") or turn.get("plan_item_snapshot_id") or turn.get("plan_item_id")
        if blueprint_id:
            turn["turn_blueprint_id"] = blueprint_id
        turn.pop("plan_item_id", None)
        turn.pop("plan_item_snapshot_id", None)
    migrated.setdefault("migration_history", []).append(
        {
            "migration": "plan_execution_v2_session_snapshot",
            "migrated_at": utc_now(),
            "source": "plan_snapshot_items",
        }
    )
    migrated["updated_at"] = utc_now()
    return migrated


def migrate_persisted_plans(
    organization_id: str = "org_default",
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    persistence = persistence_for(get_store())
    with persistence.transaction(organization_id) as transaction:
        plans = transaction.interview_plans.list()
        questions = {item["id"]: item for item in transaction.questions.list()}
        changed = []
        changed_sessions = []
        for plan in plans:
            migrated = migrate_plan_document(plan, questions)
            if migrated == plan:
                continue
            changed.append(plan["id"])
            if not dry_run:
                transaction.interview_plans.update(migrated, expected_version=plan["version"])
        for session in transaction.interview_sessions.list():
            migrated_session = migrate_session_document(session)
            if migrated_session == session:
                continue
            changed_sessions.append(session["id"])
            if not dry_run:
                transaction.interview_sessions.update(
                    migrated_session,
                    expected_version=session["version"],
                )
    return {
        "organization_id": organization_id,
        "dry_run": dry_run,
        "migrated_plan_ids": changed,
        "migrated_session_ids": changed_sessions,
    }


def main(argv: Optional[Iterable[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Migrate InterviewPlan documents to execution schema v2.")
    parser.add_argument("--organization-id", default="org_default")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    print(migrate_persisted_plans(args.organization_id, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
