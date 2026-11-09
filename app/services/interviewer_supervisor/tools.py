"""Read-only interview tools are bound to an immutable, tenant-owned contract."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Dict, Optional

from app.core.errors import ApiError
from app.core.prompt.interviewer_supervisor import TOOLS
from app.domain.adaptive_interview import closed_source_question_ids
from .context import assessment_contract
from .context import conversation_memory, committed_turn_evidence

COMPANY_CONTEXT_REFERENCE = "company:context"
CATALOG_PAGE_ITEMS = 12
TOOL_RESULT_MAX_CHARS = 6_000


def serialized_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False))


def company_context(session: Dict[str, Any]) -> str:
    value = (session.get("plan_snapshot") or {}).get("company_context")
    return value.strip() if isinstance(value, str) else ""


class InterviewerTools:
    def __init__(self, session: Dict[str, Any], skill: Optional[Dict[str, Any]] = None,
                 *, remaining_seconds: Optional[int] = None) -> None:
        self.session = session
        self.skill = skill or {}
        self.contract = assessment_contract(session)
        self.remaining_seconds = remaining_seconds
        self._catalog_cursors = {}
        self._result_cursors = {}
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
            minutes = max(1, int(candidate.get("expected_minutes") or 1))
            candidate["_expected_seconds"] = (max(1, (minutes + len(units) - 1) // len(units)) if units else minutes) * 60
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

    def _catalog_rows(self, competency=None):
        answered = {answer["turn_id"] for answer in self.session.get("answers", [])}
        gaps = {}
        for item in self.contract.get("competencies", []):
            observed = sum(turn["id"] in answered and not turn.get("is_followup")
                           and item["id"] in turn.get("competency_ids", [])
                           for turn in self.session.get("turns", []))
            gaps[item["id"]] = (max(0, item.get("min_evidence_roots", 1) - observed),
                                float(item.get("weight", 1)), bool(item.get("required", True)))
        rows = []
        for key, question in self.candidates.items():
            for unit in question.get("inquiry_units") or [None]:
                competencies = (unit or {}).get("competency_ids", question.get("competency_ids", []))
                if competency is not None and competency not in competencies:
                    continue
                rows.append({"question_id": key, "competency_ids": competencies,
                    "source_type": question.get("source_type", "position_bank"),
                    "difficulty": (question.get("frozen_question") or {}).get("difficulty", "mid"),
                    "title": str((question.get("frozen_question") or {}).get("title") or "")[:100],
                    "expected_seconds": question["_expected_seconds"],
                    "inquiry_units": [{"id": unit["id"], "question_text": unit["question_text"],
                                       "competency_ids": competencies}] if unit else []})

        supply = {name: sum(name in row["competency_ids"] for row in rows) for name in gaps}

        def priority(row):
            needs = [gaps.get(name, (0, 0, False)) for name in row["competency_ids"]]
            required_gap = any(count and required for count, _, required in needs)
            gain = sum(gaps.get(name, (0, 0, False))[0] * gaps.get(name, (0, 0, False))[1]
                       / max(1, supply.get(name, 0)) for name in row["competency_ids"])
            fits = self.remaining_seconds is None or row["expected_seconds"] <= self.remaining_seconds
            return (not fits, not required_gap, -gain, row["expected_seconds"], row["question_id"],
                    (row["inquiry_units"] or [{}])[0].get("id", ""))
        return sorted(rows, key=priority)

    def catalog_page(self, argument=None):
        if argument in self._catalog_cursors:
            competency, offset = self._catalog_cursors[argument]
        else:
            competency, offset = argument, 0
        rows = self._catalog_rows(competency)
        items = []
        for row in rows[offset:offset + CATALOG_PAGE_ITEMS]:
            if serialized_size(items + [row]) > TOOL_RESULT_MAX_CHARS - 400:
                break
            items.append(row)
        if not items and offset < len(rows):
            raise ApiError("INTERVIEW_PLANNING_CONTEXT_TOO_LARGE", "An approved catalog entry exceeds the planning budget.", status_code=503)
        next_offset = offset + len(items)
        cursor = None
        if next_offset < len(rows):
            cursor = "catalog:" + hashlib.sha256(json.dumps([competency, next_offset]).encode()).hexdigest()[:32]
            self._catalog_cursors[cursor] = (competency, next_offset)
        return {"status": "ok", "items": items, "total": len(rows), "offset": offset, "next_cursor": cursor}

    def catalog(self):
        return self.catalog_page()["items"]

    def fallback_selection(self):
        """Use the full approved catalog's existing evidence ranking, not a model page."""
        for row in self._catalog_rows():
            if self.remaining_seconds is not None and row["expected_seconds"] > self.remaining_seconds:
                continue
            unit_id = (row["inquiry_units"] or [{}])[0].get("id")
            self.require_selection(row["question_id"], unit_id)
            return row["question_id"], unit_id
        return None

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
        if argument is not None and (not isinstance(argument, str) or not 1 <= len(argument) <= 128):
            raise ApiError("INTERVIEW_TOOL_REFERENCE_INVALID", "A bounded current reference is required.", status_code=409)
        if (name, argument) in self._result_cursors:
            text, offset = self._result_cursors[(name, argument)]
            return self._result_page(name, text, offset)
        if isinstance(argument, str) and argument.startswith(("catalog:", "result:")):
            if name != "questions.search" or argument not in self._catalog_cursors:
                raise ApiError("INTERVIEW_TOOL_REFERENCE_INVALID", "A current tool cursor is required.", status_code=409)
        result = self._execute(name, argument)
        if serialized_size(result) <= TOOL_RESULT_MAX_CHARS:
            return result
        return self._result_page(name, json.dumps(result, ensure_ascii=False, separators=(",", ":")), 0)

    def _result_page(self, name, text, offset):
        # Large on-demand references remain accessible exactly, without adding
        # their full prose repeatedly to every subsequent model request.
        end = min(len(text), offset + 2_000)
        cursor = None
        if end < len(text):
            cursor = "result:" + hashlib.sha256((name + str(end) + text).encode()).hexdigest()[:32]
            self._result_cursors[(name, cursor)] = (text, end)
        return {"status": "ok", "format": "json_text_page", "content": text[offset:end],
                "offset": offset, "total_characters": len(text), "next_cursor": cursor}

    def _execute(self, name: str, argument: Optional[str]) -> Dict[str, Any]:
        if name == "questions.search":
            return self.catalog_page(argument)
        if name == "interview.read_context":
            if argument is not None:
                return {"status": "ok", "evidence": committed_turn_evidence(self.session, argument)}
            return {"status": "ok", "context": conversation_memory(self.session)}
        if name in {"questions.read", "resume.read_evidence"}:
            q = self.candidates.get(argument or "")
            if q is None:
                raise ApiError("INTERVIEW_TOOL_REFERENCE_INVALID", "Only an eligible frozen question can be read.", status_code=409)
            frozen = q.get("frozen_question") or {}
            if name == "resume.read_evidence" and q.get("source_type") != "resume_experience":
                raise ApiError("INTERVIEW_TOOL_REFERENCE_INVALID", "An approved experience reference is required.", status_code=409)
            # Explicit allow-list prevents standard answers, credentials and private locations escaping.
            fields = ("question_text", "title", "skills", "difficulty")
            result = {field: deepcopy(frozen[field]) for field in fields if field in frozen}
            if frozen.get("spoken_text") and frozen["spoken_text"] != frozen.get("question_text"):
                result["spoken_text"] = frozen["spoken_text"]
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
