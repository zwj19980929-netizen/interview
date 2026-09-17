"""Read-only interview tools are bound to an immutable, tenant-owned contract."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Optional

from app.core.errors import ApiError
from app.core.prompt.interviewer_supervisor import TOOLS
from app.domain.adaptive_interview import closed_source_question_ids
from .context import assessment_contract
from .context import conversation_memory

COMPANY_CONTEXT_REFERENCE = "company:context"


def company_context(session: Dict[str, Any]) -> str:
    value = (session.get("plan_snapshot") or {}).get("company_context")
    return value.strip() if isinstance(value, str) else ""


class InterviewerTools:
    def __init__(self, session: Dict[str, Any], skill: Optional[Dict[str, Any]] = None) -> None:
        self.session = session
        self.skill = skill or {}
        self.contract = assessment_contract(session)
        self.references = {item["id"]: deepcopy(item) for item in self.skill.get("resources", [])}
        supplied_company = company_context(session)
        if supplied_company:
            self.references[COMPANY_CONTEXT_REFERENCE] = {
                "id": COMPANY_CONTEXT_REFERENCE, "title": "本场提供的企业资料", "content": supplied_company,
            }
        roots = [turn for turn in session.get("turns", []) if not turn.get("is_followup")]
        asked = {(turn.get("question_id"), turn.get("inquiry_unit_id")) for turn in roots}
        declined = closed_source_question_ids(session)
        self.candidates = {}
        for source in self.contract.get("candidate_questions", []):
            identity = source.get("question_id")
            if not identity or identity in declined:
                continue
            candidate = deepcopy(source)
            units = candidate.get("inquiry_units") or []
            if units:
                candidate["inquiry_units"] = [unit for unit in units if (identity, unit["id"]) not in asked]
                if not candidate["inquiry_units"]:
                    continue
                candidate["competency_ids"] = sorted({name for unit in candidate["inquiry_units"]
                    for name in unit.get("competency_ids", source.get("competency_ids", []))})
            elif any(question_id == identity for question_id, _ in asked):
                continue
            self.candidates[str(identity)] = candidate
        snapshot = (session.get("plan_snapshot") or {}).get("enterprise_skill_snapshot")
        granted = set((snapshot or {}).get("allowed_tools", TOOLS))
        self.allowed = tuple(name for name in TOOLS if name in granted
                             and (name != "company.read_reference" or self.references))

    def catalog(self):
        return [{"question_id": key, "competency_ids": q.get("competency_ids", []),
                 "source_type": q.get("source_type", "position_bank"),
                 "difficulty": (q.get("frozen_question") or {}).get("difficulty", "mid"),
                 "title": str((q.get("frozen_question") or {}).get("title") or "")[:100],
                 "inquiry_units": [{"id": unit["id"], "question_text": unit["question_text"], "competency_ids": unit.get("competency_ids", q.get("competency_ids", []))} for unit in q.get("inquiry_units", [])]}
                for key, q in self.candidates.items()]

    def require_selection(self, question_id: str, unit_id: Optional[str]) -> None:
        question = self.candidates.get(question_id)
        if question is None:
            raise ValueError("question outside frozen scope")
        units = question.get("inquiry_units") or []
        if units and unit_id not in {unit["id"] for unit in units}:
            raise ValueError("unit outside the selected question scope")
        if not units and unit_id is not None:
            raise ValueError("legacy complete question has no inquiry unit")

    def execute(self, name: str, argument: Optional[str]) -> Dict[str, Any]:
        if name not in self.allowed or name == "specialists.consult":
            raise ApiError("INTERVIEW_TOOL_FORBIDDEN", "This tool is not available in the approved interview scope.", status_code=403)
        if name == "questions.search":
            items = self.catalog()
            if argument is not None:
                items = [q for q in items if argument in q["competency_ids"]]
            return {"status": "ok", "items": items[:40], "total": len(items)}
        if name == "interview.read_context":
            if argument is not None:
                raise ApiError("INTERVIEW_TOOL_REFERENCE_INVALID", "This tool is already scoped to the current interview.", status_code=409)
            return {"status": "ok", "context": conversation_memory(self.session)}
        if name in {"questions.read", "resume.read_evidence"}:
            q = self.candidates.get(argument or "")
            if q is None:
                raise ApiError("INTERVIEW_TOOL_REFERENCE_INVALID", "Only an eligible frozen question can be read.", status_code=409)
            frozen = q.get("frozen_question") or {}
            if name == "resume.read_evidence" and q.get("source_type") != "resume_experience":
                raise ApiError("INTERVIEW_TOOL_REFERENCE_INVALID", "An approved experience reference is required.", status_code=409)
            # Explicit allow-list prevents standard answers, credentials and private locations escaping.
            fields = ("question_text", "spoken_text", "title", "skills", "difficulty")
            result = {field: deepcopy(frozen[field]) for field in fields if field in frozen}
            result["inquiry_units"] = [{"id": unit["id"], "question_text": unit["question_text"], "competency_ids": unit.get("competency_ids", q.get("competency_ids", []))} for unit in q.get("inquiry_units", [])]
            if name == "resume.read_evidence":
                result["project_evidence"] = deepcopy(frozen.get("project_evidence") or frozen.get("evidence_refs") or [])
            return {"status": "ok", "question_id": argument, "question": result}
        if name == "company.read_reference":
            value = self.references.get(argument or "")
            if value is None:
                raise ApiError("INTERVIEW_TOOL_REFERENCE_INVALID", "The optional reference does not exist in this interview.", status_code=404)
            return {"status": "ok", "reference": deepcopy(value)}
        raise ApiError("INTERVIEW_TOOL_FORBIDDEN", "Unknown interview tool.", status_code=403)
