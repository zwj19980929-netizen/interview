from copy import deepcopy
import csv
from io import StringIO
import json
from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.domain.scoring_quality import DISPUTE_FLAGS, evaluation_answers, project_evaluation, project_report
from app.core.time import utc_now
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore


class ReportService:
    """Builds reports from frozen plan data and current evaluation pointers."""

    def __init__(self, store: InMemoryStore, *, persistence: Optional[Persistence] = None) -> None:
        self.persistence = persistence or persistence_for(store)

    def _job_fit_level(self, score: int, *, requires_manual_review: bool, has_evidence: bool) -> str:
        if requires_manual_review:
            return "manual_review"
        if not has_evidence:
            return "insufficient_evidence"
        if score >= 85:
            return "strong_match"
        if score >= 70:
            return "match"
        if score >= 55:
            return "partial_match"
        return "insufficient_evidence"

    def build_report(self, interview: Dict[str, Any], *, trigger_reason: str) -> Dict[str, Any]:
        evaluations_by_id = {item["id"]: item for item in interview.get("evaluation_revisions", [])}
        turns_by_id = {item["id"]: item for item in interview.get("turns", [])}
        score_bearing_answers = [
            answer
            for answer in interview.get("answers", [])
            if not turns_by_id.get(answer.get("turn_id"), {}).get("is_followup", False)
        ]
        current_evaluations = {
            answer["id"]: evaluations_by_id[answer["current_evaluation_id"]]
            for answer in score_bearing_answers
            if answer.get("current_evaluation_id") in evaluations_by_id
        }
        item_by_snapshot = {
            item["question_snapshot_id"]: item
            for item in interview["plan_snapshot"]["question_snapshots"]
        }
        question_evaluations: List[Dict[str, Any]] = []
        weighted_score = 0.0
        completed_weight = 0.0
        dimension_totals: Dict[str, Dict[str, float]] = {}

        for answer in score_bearing_answers:
            original = current_evaluations.get(answer["id"])
            evaluation = project_evaluation(original, evaluation_answers(interview, original)) if original else None
            if evaluation is None:
                continue
            if evaluation["score"] is None:
                raise ApiError("REPORT_EVALUATION_UNAVAILABLE", "本题尚无有效评分，请重试后台评分。", status_code=409)
            plan_item = item_by_snapshot.get(
                evaluation["question_snapshot_id"],
                {"weight": 0.0, "dimension": "general"},
            )
            weight = float(plan_item.get("weight", 0.0))
            weighted_score += float(evaluation["score"] or 0) * weight
            completed_weight += weight
            dimension = plan_item.get("dimension", "general")
            totals = dimension_totals.setdefault(dimension, {"weighted": 0.0, "weight": 0.0})
            totals["weighted"] += float(evaluation["score"] or 0) * weight
            totals["weight"] += weight
            question_evaluations.append(
                {
                    "answer_id": answer["id"],
                    "evaluation_id": evaluation["id"],
                    "evaluation_revision": evaluation["revision"],
                    "question_id": evaluation["question_id"],
                    "question_snapshot_id": evaluation["question_snapshot_id"],
                    "score": evaluation["score"],
                    "confidence": evaluation["confidence"],
                    "covered_key_points": deepcopy(evaluation["covered_key_points"]),
                    "missing_key_points": deepcopy(evaluation["missing_key_points"]),
                    "evidence": [item["evidence"] for item in evaluation["covered_key_points"]],
                    "evidence_answer_ids": deepcopy(
                        evaluation.get("evidence_answer_ids") or [answer["id"]]
                    ),
                    "evidence_utterance_ids": deepcopy(
                        evaluation.get("evidence_utterance_ids") or []
                    ),
                    "summary": evaluation["feedback"],
                }
            )

        overall_score = int(round(weighted_score / completed_weight)) if completed_weight else 0
        requires_manual_review = any(
            set(item.get("review_flags") or []) - DISPUTE_FLAGS
            or (item.get("confidence", 1.0) < 0.6 and not set(item.get("review_flags") or []) & DISPUTE_FLAGS)
            for item in current_evaluations.values()
        )
        job_fit_level = self._job_fit_level(
            overall_score,
            requires_manual_review=requires_manual_review,
            has_evidence=bool(question_evaluations),
        )
        dimension_scores = [
            {
                "dimension": dimension,
                "score": int(round(value["weighted"] / value["weight"])) if value["weight"] else 0,
            }
            for dimension, value in sorted(dimension_totals.items())
        ]
        return project_report({
            "id": new_id("report"),
            "organization_id": interview["organization_id"],
            "interview_id": interview["id"],
            "overall_score": overall_score,
            "job_fit_level": job_fit_level,
            "recommendation": job_fit_level,
            "dimension_scores": dimension_scores,
            "strengths": ["关键点覆盖较好"] if overall_score >= 75 else [],
            "risks": ["存在未覆盖关键点，建议人工复核"] if overall_score < 75 else [],
            "followup_suggestions": ["针对缺失关键点安排人工追问"] if overall_score < 75 else [],
            "question_evaluations": question_evaluations,
            "skipped_questions": [
                {"turn_id": turn["id"], "question_id": turn["question_id"],
                 "reason": turn["skip_reason"], "counts_toward_score": False}
                for turn in interview.get("turns", [])
                if turn.get("status") == "skipped" and turn.get("skip_reason") == "resume_speech_not_ready"
            ],
            "evaluation_ids": [item["evaluation_id"] for item in question_evaluations],
            "trigger_reason": trigger_reason,
            "human_decision_required": True,
            "decision_notice": "本报告只提供岗位匹配证据，不自动作出录用或淘汰决定。",
            "generated_by": "system",
            "generated_at": utc_now(),
        }, interview)

    def get_report(self, interview_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        interview = self._get_interview(interview_id, organization_id)
        report_id = interview.get("current_report_id")
        report = next(
            (item for item in interview.get("report_revisions", []) if item["id"] == report_id),
            None,
        )
        if report is None:
            raise ApiError("REPORT_NOT_FOUND", "Interview report is not ready.", status_code=404)
        return project_report(report, interview)

    def list_report_revisions(
        self,
        interview_id: str,
        organization_id: str = "org_default",
    ) -> List[Dict[str, Any]]:
        interview = self._get_interview(interview_id, organization_id)
        return [project_report(item, interview) for item in interview.get("report_revisions", [])]

    def export_report(
        self,
        interview_id: str,
        *,
        export_format: str,
        actor_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        report = self.get_report(interview_id, organization_id)
        normalized = export_format.strip().lower()
        if normalized == "json":
            content = json.dumps(report, ensure_ascii=False, indent=2)
            content_type = "application/json; charset=utf-8"
            filename = "%s-report.json" % interview_id
        elif normalized == "csv":
            output = StringIO()
            fields = [
                "interview_id",
                "overall_score",
                "score_status",
                "recognition_notice",
                "recognition_warning_answer_ids",
                "job_fit_level",
                "human_decision_required",
                "dimension_scores",
                "strengths",
                "risks",
                "followup_suggestions",
                "generated_at",
            ]
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerow(
                {
                    "interview_id": interview_id,
                    "overall_score": report["overall_score"],
                    "score_status": report["score_status"],
                    "recognition_notice": report.get("recognition_notice"),
                    "recognition_warning_answer_ids": json.dumps(report.get("recognition_warning_answer_ids", [])),
                    "job_fit_level": report["job_fit_level"],
                    "human_decision_required": report["human_decision_required"],
                    "dimension_scores": json.dumps(report.get("dimension_scores", []), ensure_ascii=False),
                    "strengths": json.dumps(report.get("strengths", []), ensure_ascii=False),
                    "risks": json.dumps(report.get("risks", []), ensure_ascii=False),
                    "followup_suggestions": json.dumps(report.get("followup_suggestions", []), ensure_ascii=False),
                    "generated_at": report["generated_at"],
                }
            )
            content = output.getvalue()
            content_type = "text/csv; charset=utf-8"
            filename = "%s-report.csv" % interview_id
        else:
            raise ApiError("REPORT_EXPORT_FORMAT_INVALID", "Report export format must be json or csv.")
        with self.persistence.transaction(organization_id) as transaction:
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "interview.report.exported",
                    "resource_type": "interview_report",
                    "resource_id": report["id"],
                    "metadata": {"interview_id": interview_id, "format": normalized},
                    "created_at": utc_now(),
                }
            )
        return {"content": content, "content_type": content_type, "filename": filename}

    def _get_interview(self, interview_id: str, organization_id: str) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            interview = transaction.interview_sessions.get(interview_id)
        if interview is None:
            raise ApiError("INTERVIEW_NOT_FOUND", "Interview does not exist.", status_code=404)
        return interview
