"""One cognitive seam; model suggestions never mutate InterviewSession."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import time
from typing import Any, Dict, Optional, Tuple

from app.core.errors import ApiError
from app.core.prompt.interviewer_supervisor import supervisor_contract, evidence_expert_contract
from app.core.prompt.validation import validate_structured_response
from app.model_gateway import capabilities as cap
from app.model_gateway.schemas import ChatJSONRequest, InvocationExecutionBudget
from app.domain.adaptive_interview import coverage_summary, budget_summary
from app.core.time import utc_now
from .context import assessment_contract, context_fingerprint, conversation_memory
from .tools import InterviewerTools, COMPANY_CONTEXT_REFERENCE, company_context


@dataclass(frozen=True)
class NextInterviewDecision:
    interview_id: str
    revision: int
    context_fingerprint: str
    decision_id: str
    action: str
    question_id: Optional[str]
    reason_code: str
    transition_key: str
    tools_used: Tuple[str, ...]
    model_calls: int
    inquiry_unit_id: Optional[str] = None


class InterviewerSupervisor:
    def __init__(self, *, gateway: Any, conversation: Any, persistence: Any) -> None:
        self.gateway = gateway
        self.conversation = conversation
        self.persistence = persistence

    def load_skill(self, session: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = (session.get("plan_snapshot") or {}).get("enterprise_skill_snapshot")
        if not snapshot:
            return {}
        from app.services.interview_skills import InterviewSkillService
        # Loading validates current authorization, not only the frozen hash.
        return InterviewSkillService(None, persistence=self.persistence).load_compiled(
            snapshot, organization_id=session["organization_id"])

    @staticmethod
    def _skill_context(skill: Dict[str, Any]) -> Dict[str, Any]:
        # The content of company references is exposed only by its granted tool.
        return {**{key: skill[key] for key in ("compiler_version", "content_hash", "instructions") if key in skill},
                "reference_index": [{"id": item["id"], "title": item["title"]} for item in skill.get("resources", [])]}

    @staticmethod
    def _company_context(session: Dict[str, Any]) -> Dict[str, Any]:
        # Company material is independently optional and read only on demand.
        return ({"available": True, "reference_id": COMPANY_CONTEXT_REFERENCE}
                if company_context(session) else {"available": False})

    async def prepare_turn(self, utterance: Any, turn: Dict[str, Any], session: Dict[str, Any]):
        context = conversation_memory(session)
        context["skill"] = self._skill_context(self.load_skill(session))
        context["company_context"] = self._company_context(session)
        enriched = {**session, "_interviewer_context": context}
        return await self.conversation.prepare_decision(utterance, turn, enriched)

    async def prepare_preview(self, preview: Any, turn: Dict[str, Any], session: Dict[str, Any], *, snapshot_ref: str):
        context = conversation_memory(session)
        context["skill"] = self._skill_context(self.load_skill(session))
        context["company_context"] = self._company_context(session)
        return await self.conversation.prepare_preview(preview, turn,
            {**session, "_interviewer_context": context}, snapshot_ref=snapshot_ref)

    async def propose_next(self, session: Dict[str, Any]) -> NextInterviewDecision:
        if session.get("status") != "in_progress" or session.get("candidate_input_completed_at") or session.get("current_turn_id"):
            raise ApiError("INTERVIEW_DECISION_NOT_EXPECTED", "The interview is not waiting for a new question.", status_code=409)
        skill = self.load_skill(session)
        tools = InterviewerTools(session, skill)
        fingerprint = context_fingerprint(session)
        revision = int(session.get("decision_revision", 0))
        decision_id = "decision_" + hashlib.sha256((session["id"] + ":" + str(revision) + ":" + fingerprint).encode()).hexdigest()[:32]
        context = {"assessment": {k: deepcopy(v) for k, v in assessment_contract(session).items() if k != "candidate_questions"},
                   "memory": conversation_memory(session), "eligible_questions": tools.catalog(),
                   "skill": self._skill_context(skill), "company_context": self._company_context(session),
                   "tools": list(tools.allowed),
                   "questions_asked": len([t for t in session.get("turns", []) if not t.get("is_followup")]),
                   "coverage": coverage_summary(session), "budget": budget_summary(session, utc_now()),
                   "remaining_seconds": self._remaining_seconds(session)}
        observations = []
        calls = 0
        tools_used = []
        deadline = time.monotonic() + 15.0

        async def invoke(contract):
            nonlocal calls
            remaining = deadline - time.monotonic()
            if calls >= 3 or remaining <= 0:
                raise ApiError("INTERVIEW_DECISION_BUDGET_EXHAUSTED", "Interview planning reached its bounded budget.", status_code=503)
            calls += 1
            response = await asyncio.wait_for(self.gateway.invoke(cap.LLM_CHAT_JSON, ChatJSONRequest(
                organization_id=session["organization_id"], purpose="interview_turn_understanding",
                messages=contract.messages, json_schema=contract.response_schema, max_output_tokens=600, temperature=0.0,
                execution_budget=InvocationExecutionBudget(timeout_s=remaining, max_provider_retries=0, max_provider_attempts=1),
                metadata={"prompt_version": contract.version},
            )), timeout=remaining)
            data = response.data
            validate_structured_response(data, contract.response_schema)
            return data

        try:
            while calls < 3:
                usable = [name for name in tools.allowed if not (name == "specialists.consult" and (calls > 0 or name in tools_used))]
                data = await invoke(supervisor_contract(context, observations, tools=usable if calls < 2 else [], question_ids=list(tools.candidates), unit_ids=[unit["id"] for q in tools.candidates.values() for unit in q.get("inquiry_units", [])]))
                action = data["action"]
                if action == "use_tool":
                    if data["question_id"] is not None or data["inquiry_unit_id"] is not None or not data["tool_name"] or data["reason_code"] != "inspect_context":
                        raise ValueError("inconsistent tool action")
                    name = data["tool_name"]
                    if name not in usable or len(tools_used) >= 3:
                        raise ValueError("tool budget exceeded")
                    tools_used.append(name)
                    if name == "specialists.consult":
                        if data["argument"] is not None:
                            raise ValueError("specialist takes the frozen context only")
                        result = await invoke(evidence_expert_contract(context, list(tools.candidates)))
                    else:
                        result = tools.execute(name, data["argument"])
                    observations.append({"tool_name": name, "result": result})
                    continue
                if data["tool_name"] is not None or data["argument"] is not None:
                    raise ValueError("unexpected tool payload")
                if action == "select_question":
                    tools.require_selection(data["question_id"], data["inquiry_unit_id"])
                if action == "finish_interview" and (data["question_id"] is not None or data["inquiry_unit_id"] is not None):
                    raise ValueError("finish cannot select a question")
                if action == "select_question" and data["reason_code"] not in {"coverage_gap", "relevant_experience", "topic_change"}:
                    raise ValueError("question reason invalid")
                if action == "finish_interview" and data["reason_code"] not in {"coverage_complete", "budget_exhausted"}:
                    raise ValueError("autonomous finish requires coverage or budget facts")
                return NextInterviewDecision(session["id"], revision, fingerprint, decision_id, action,
                    data["question_id"], data["reason_code"], data["transition_key"], tuple(tools_used), calls, data["inquiry_unit_id"])
        except asyncio.CancelledError:
            raise
        except ApiError:
            raise
        except Exception:
            raise ApiError("INTERVIEW_DECISION_UNAVAILABLE", "面试官暂时未能准备好后续交流，请稍后重试。", status_code=503) from None
        raise ApiError("INTERVIEW_DECISION_BUDGET_EXHAUSTED", "Interview planning reached its bounded budget.", status_code=503)

    @staticmethod
    def _remaining_seconds(session: Dict[str, Any]) -> Optional[int]:
        value = session.get("scheduled_end_at")
        if not value:
            return None
        deadline = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return max(0, int((deadline - datetime.now(timezone.utc)).total_seconds()))
