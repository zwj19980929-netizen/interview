"""Semantic understanding and controlled follow-up decision deep module."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.core.ids import new_id
from app.core.prompt.contracts import prompt_contract
from app.core.prompt.validation import (
    StructuredResponseValidationError,
    validate_structured_response,
)
from app.core.time import utc_now
from app.domain.interview_agent import (
    ApprovedConversationAct,
    ConversationUtterance,
    TurnUnderstanding,
)
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import ChatJSONRequest
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.services.meta_intent import MetaIntentDetector


_SENSITIVE = re.compile(
    r"年龄|岁数|出生|性别|男生|女生|婚姻|结婚|未婚|生育|怀孕|民族|种族|宗教|信仰|"
    r"政治|党派|残疾|疾病|病史|健康状况|家庭住址|户籍|籍贯|sexual|gender|age|religion|disability",
    re.IGNORECASE,
)


class ConversationUnderstandingService:
    DEFAULT_POLICY = {
        "max_depth": 2,
        "max_per_root": 2,
        "min_remaining_seconds": 90,
        "max_probe_chars": 180,
        # A short but intelligible answer can itself be evidence that a frozen
        # capability is missing. Keep the gate above filler length without
        # suppressing the clarification opportunity such an answer needs.
        "min_answer_chars": 4,
        "max_answer_chars": 4000,
        "low_confidence_threshold": 0.65,
    }

    def __init__(
        self,
        store: Any,
        *,
        gateway: Optional[ModelGateway] = None,
        persistence: Optional[Persistence] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.gateway = gateway or ModelGateway(store, persistence=self.persistence)
        self.meta_intents = MetaIntentDetector()

    async def understand(
        self,
        utterance: ConversationUtterance,
        turn: Dict[str, Any],
        interview: Dict[str, Any],
    ) -> TurnUnderstanding:
        if not utterance.authoritative:
            raise ValueError("formal understanding requires an authoritative utterance")
        capability_points = self._capability_points(turn)
        deterministic = self.meta_intents.detect(utterance.text)
        contract = prompt_contract(
            "interview_turn_understanding",
            {
                "question_text": turn.get("question_spoken_text")
                or turn.get("question_snapshot", {}).get("question_text", ""),
                "capability_points": capability_points,
                "transcript": utterance.text,
            },
        )
        if deterministic:
            data = {
                "intent": deterministic.intent,
                "answer_summary": "",
                "claims": [],
                "evidence_quotes": [deterministic.evidence],
                "covered_capability_points": [],
                "missing_capability_points": capability_points,
                "ambiguities": [],
                "contradictions": [],
                "confidence": min(1.0, utterance.stt_confidence),
                "suggested_action": deterministic.action,
            }
            provider = {
                "provider_id": "deterministic_meta_intent",
                "model": "bounded-bilingual-meta-intent-v1",
                "request_id": new_id("meta_intent"),
                "latency_ms": 0,
            }
        else:
            try:
                response = await self.gateway.invoke(
                    cap.LLM_CHAT_JSON,
                    ChatJSONRequest(
                        organization_id=interview.get("organization_id", "org_default"),
                        purpose="interview_turn_understanding",
                        messages=contract.messages,
                        json_schema=contract.response_schema,
                        temperature=0.0,
                        max_output_tokens=1800,
                        metadata={
                            "prompt_version": contract.version,
                            "interview_id": interview["id"],
                            "turn_id": turn["id"],
                            "transcript": utterance.text,
                            "capability_points": capability_points,
                        },
                    ),
                )
                data = deepcopy(response.data)
                provider = response.provider.model_dump()
                validate_structured_response(data, contract.response_schema)
                self._validate_understanding_content(
                    data, utterance.text, capability_points
                )
                confidence = min(
                    float(data["confidence"]), utterance.stt_confidence
                )
            except ProviderError as exc:
                return self._safe_failure_understanding(
                    utterance,
                    turn,
                    capability_points,
                    source="provider",
                    retryable=bool(exc.retryable),
                )
            except (
                StructuredResponseValidationError,
                ValueError,
                KeyError,
                TypeError,
            ):
                return self._safe_failure_understanding(
                    utterance,
                    turn,
                    capability_points,
                    source="schema_or_content",
                    retryable=False,
                )
        if deterministic:
            validate_structured_response(data, contract.response_schema)
            self._validate_understanding_content(
                data, utterance.text, capability_points
            )
            confidence = min(float(data["confidence"]), utterance.stt_confidence)
        # An exact, deterministic meta-intent is a control instruction rather
        # than an answer.  It is safe to honour even when the provider reports
        # low acoustic confidence; otherwise "请再说一遍" could be turned into
        # an unrelated clarification loop.
        if (
            deterministic is None
            and confidence < float(self.DEFAULT_POLICY["low_confidence_threshold"])
        ):
            data["suggested_action"] = "clarify"
        current = turn.get("current_understanding") or {}
        return TurnUnderstanding(
            understanding_id=new_id("understanding"),
            revision=int(current.get("revision", 0)) + 1,
            prompt_version="interview_turn_understanding.v1",
            utterance_id=utterance.utterance_id,
            provider=provider,
            created_at=utc_now(),
            **{**data, "confidence": confidence},
        )

    async def select_followup(
        self,
        interview: Dict[str, Any],
        turn: Dict[str, Any],
        utterance: ConversationUtterance,
        understanding: TurnUnderstanding,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        policy = {**self.DEFAULT_POLICY, **(interview.get("followup_policy") or {})}
        primary_count = len([item for item in interview.get("turns", []) if not item.get("is_followup")])
        policy["max_total"] = min(4, primary_count)
        depth = int(turn.get("followup_depth", 0))
        root_turn_id = str(turn.get("root_turn_id") or turn["id"])
        followups = [item for item in interview.get("turns", []) if item.get("is_followup")]
        root_followups = [item for item in followups if item.get("root_turn_id") == root_turn_id]
        if understanding.intent != "answer":
            return self._no_followup("meta_or_non_answer", policy)
        if understanding.confidence < float(policy["low_confidence_threshold"]):
            return self._no_followup("low_confidence_clarification_required", policy)
        if not bool(turn.get("allow_followup", False)):
            return self._no_followup("followup_disabled", policy)
        if depth >= min(2, int(policy["max_depth"])):
            return self._no_followup("depth_budget_exhausted", policy)
        if len(followups) >= int(policy["max_total"]):
            return self._no_followup("session_budget_exhausted", policy)
        if len(root_followups) >= min(2, int(policy["max_per_root"])):
            return self._no_followup("root_budget_exhausted", policy)
        if not self._within_time_budget(interview, int(policy["min_remaining_seconds"]), now=now):
            return self._no_followup("time_budget_exhausted", policy)
        if not (
            int(policy["min_answer_chars"])
            <= len(utterance.text)
            <= int(policy["max_answer_chars"])
        ):
            return self._no_followup("answer_length_outside_budget", policy)

        previously_probed = {
            str(point)
            for item in root_followups
            for point in item.get("target_key_points", [])
        }
        targets = [
            point
            for point in understanding.missing_capability_points
            if point not in previously_probed
        ][:2]
        if not targets:
            return self._no_followup(
                "capability_points_covered_or_already_probed", policy
            )
        if any(_SENSITIVE.search(item) for item in targets):
            return self._no_followup("sensitive_capability_target", policy)
        evidence = [
            quote
            for quote in understanding.evidence_quotes[:4]
            if not _SENSITIVE.search(quote)
        ]
        if not evidence:
            return self._no_followup("model_evidence_unavailable", policy)

        root = next(
            (item for item in interview.get("turns", []) if item.get("id") == root_turn_id),
            turn,
        )
        difficulty = str(root.get("question_snapshot", {}).get("difficulty") or "mid")
        contract = prompt_contract(
            "controlled_followup",
            {
                "question_text": root.get("question_spoken_text", ""),
                "answer_summary": understanding.answer_summary,
                "evidence_quotes": evidence,
                "target_capability_points": targets,
                "difficulty": difficulty,
                "max_probe_chars": int(policy["max_probe_chars"]),
            },
        )
        try:
            response = await self.gateway.invoke(
                cap.LLM_CHAT_JSON,
                ChatJSONRequest(
                    organization_id=interview.get("organization_id", "org_default"),
                    purpose="controlled_followup",
                    messages=contract.messages,
                    json_schema=contract.response_schema,
                    temperature=0.0,
                    max_output_tokens=800,
                    metadata={
                        "prompt_version": contract.version,
                        "interview_id": interview["id"],
                        "root_turn_id": root_turn_id,
                        "evidence_quotes": evidence,
                        "target_capability_points": targets,
                        "difficulty": difficulty,
                    },
                ),
            )
            data = deepcopy(response.data)
            validate_structured_response(data, contract.response_schema)
            self._gate_followup(
                data,
                transcript=utterance.text,
                allowed_evidence=evidence,
                allowed_targets=targets,
                difficulty=difficulty,
                root=root,
                max_chars=int(policy["max_probe_chars"]),
            )
            provider = response.provider.model_dump()
        except (ProviderError, ValueError, KeyError):
            return self._fallback_probe(
                turn,
                targets,
                evidence,
                "model_unavailable_or_rejected",
                policy,
            )
        if not data.get("selected"):
            return self._no_followup("model_declined", policy)

        quote = str(data["evidence_quote"]).strip()
        core_question = str(data["question_text"]).strip()
        spoken = "你刚才提到了“%s”，我想确认一个细节：%s" % (quote, core_question)
        if len(spoken) > int(policy["max_probe_chars"]):
            spoken = core_question
        act = ApprovedConversationAct(
            act_id=new_id("conversation_act"),
            act_type="followup",
            text=spoken,
            turn_id=None,
            root_turn_id=root_turn_id,
            evidence_quotes=[quote],
            target_capability_points=list(data["target_capability_points"]),
            followup_depth=depth + 1,
            evaluative=False,
            approved_by="controlled_followup_gate",
            prompt_version=contract.version,
            created_at=utc_now(),
        )
        return {
            "selected": True,
            "root_turn_id": root_turn_id,
            "parent_turn_id": turn["id"],
            "followup_depth": depth + 1,
            "reason": "missing_capability_evidence",
            "target_key_points": list(act.target_capability_points),
            "question_text": act.text,
            "evidence_quotes": list(act.evidence_quotes),
            "probe_source": "controlled_followup.v1",
            "conversation_act": act.model_dump(mode="json"),
            "provider": provider,
            "policy": policy,
        }

    def persist_understanding(
        self,
        interview_id: str,
        turn_id: str,
        utterance: ConversationUtterance,
        understanding: TurnUnderstanding,
        *,
        organization_id: str = "org_default",
    ) -> None:
        with self.persistence.transaction(organization_id) as transaction:
            session = transaction.interview_sessions.get(interview_id)
            if session is None:
                raise ValueError("interview no longer exists")
            turn = next(item for item in session.get("turns", []) if item["id"] == turn_id)
            turn.setdefault("utterances", []).append(utterance.model_dump(mode="json"))
            turn["current_understanding"] = understanding.model_dump(mode="json")
            session["updated_at"] = utc_now()
            transaction.interview_sessions.update(session, expected_version=session["version"])

    @staticmethod
    def _safe_failure_understanding(
        utterance: ConversationUtterance,
        turn: Dict[str, Any],
        capability_points: Sequence[str],
        *,
        source: str,
        retryable: bool,
    ) -> TurnUnderstanding:
        """Fail closed without persisting unvalidated provider semantics.

        A retryable provider outage may ask for one approved re-utterance.  A
        non-retryable provider error or a schema/content contract violation
        pauses the interview for human review.  Neither path can create a
        CandidateAnswer or a provider-authored conversation act.
        """

        clarify = source == "provider" and retryable
        action = "clarify" if clarify else "pause"
        code = (
            "UNDERSTANDING_PROVIDER_UNAVAILABLE"
            if source == "provider"
            else "UNDERSTANDING_RESULT_REJECTED"
        )
        current = turn.get("current_understanding") or {}
        return TurnUnderstanding(
            understanding_id=new_id("understanding"),
            revision=int(current.get("revision", 0)) + 1,
            prompt_version="interview_turn_understanding.v1",
            utterance_id=utterance.utterance_id,
            intent="clarification_request",
            answer_summary="",
            claims=[],
            evidence_quotes=[],
            covered_capability_points=[],
            missing_capability_points=list(capability_points),
            ambiguities=[],
            contradictions=[],
            confidence=0.0,
            suggested_action=action,
            provider={
                "provider_id": "understanding_safety_gate",
                "model": "fail-closed-v1",
                "request_id": new_id("understanding_failure"),
                "latency_ms": 0,
            },
            problem={
                "code": code,
                "source": source,
                "recoverable": clarify,
                "action": action,
                "retryable": retryable,
            },
            created_at=utc_now(),
        )

    @staticmethod
    def _validate_understanding_content(
        data: Dict[str, Any], transcript: str, capability_points: Sequence[str]
    ) -> None:
        allowed = set(capability_points)
        covered_values = list(data["covered_capability_points"])
        missing_values = list(data["missing_capability_points"])
        covered = set(covered_values)
        missing = set(missing_values)
        if len(covered) != len(covered_values) or len(missing) != len(missing_values):
            raise ValueError("capability point partitions contain duplicates")
        if not covered.issubset(allowed):
            raise ValueError("covered capability point was not frozen")
        if not missing.issubset(allowed):
            raise ValueError("missing capability point was not frozen")
        if covered & missing or covered | missing != allowed:
            raise ValueError("capability point coverage is not a complete disjoint partition")
        quotes = list(data["evidence_quotes"]) + [
            item["evidence_quote"] for item in data["claims"]
        ]
        if any(str(quote).strip() not in transcript for quote in quotes):
            raise ValueError("understanding evidence quote is not an exact transcript span")
        if data.get("intent") == "answer":
            evidence_quotes = [str(item).strip() for item in data["evidence_quotes"]]
            claims = list(data["claims"])
            if (
                not str(data.get("answer_summary") or "").strip()
                or not evidence_quotes
                or not claims
            ):
                raise ValueError("formal answer understanding requires non-empty evidence")
            evidence_set = set(evidence_quotes)
            if any(str(item["evidence_quote"]).strip() not in evidence_set for item in claims):
                raise ValueError("claim evidence must be declared in evidence_quotes")

    @staticmethod
    def _capability_points(turn: Dict[str, Any]) -> List[str]:
        result = []
        for item in turn.get("question_snapshot", {}).get("key_points", []):
            text = str(item.get("text") if isinstance(item, dict) else item).strip()
            if text and text not in result:
                result.append(text)
        return result

    @staticmethod
    def _within_time_budget(
        interview: Dict[str, Any], minimum_seconds: int, *, now: Optional[datetime]
    ) -> bool:
        current = now or datetime.now(timezone.utc)
        scheduled_end = interview.get("scheduled_end_at") or interview.get("settings", {}).get("scheduled_end_at")
        if scheduled_end:
            try:
                end = datetime.fromisoformat(str(scheduled_end).replace("Z", "+00:00"))
                return (end - current).total_seconds() >= minimum_seconds
            except ValueError:
                pass
        started = interview.get("started_at")
        estimated_minutes = int(interview.get("plan_snapshot", {}).get("estimated_minutes", 0))
        if started and estimated_minutes:
            try:
                end = datetime.fromisoformat(str(started).replace("Z", "+00:00")) + timedelta(minutes=estimated_minutes)
                return (end - current).total_seconds() >= minimum_seconds
            except ValueError:
                return False
        return False

    def _gate_followup(
        self,
        data: Dict[str, Any],
        *,
        transcript: str,
        allowed_evidence: Sequence[str],
        allowed_targets: Sequence[str],
        difficulty: str,
        root: Dict[str, Any],
        max_chars: int,
    ) -> None:
        if not data.get("selected"):
            return
        text = str(data.get("question_text") or "").strip()
        quote = str(data.get("evidence_quote") or "").strip()
        targets = list(data.get("target_capability_points") or [])
        if not text or len(text) > max_chars or text.count("?") + text.count("？") > 1:
            raise ValueError("follow-up length/question-count gate failed")
        if quote not in transcript or quote not in allowed_evidence:
            raise ValueError("follow-up evidence binding failed")
        if not targets or not set(targets).issubset(set(allowed_targets)):
            raise ValueError("follow-up capability binding failed")
        if data.get("difficulty") != difficulty:
            raise ValueError("follow-up difficulty escalation is forbidden")
        if (
            data.get("sensitive_attribute_inference")
            or data.get("leaks_answer")
            or _SENSITIVE.search(text)
            or _SENSITIVE.search(quote)
        ):
            raise ValueError("follow-up safety gate failed")
        standard_answer = str(root.get("question_snapshot", {}).get("standard_answer") or "")
        answer_terms = {term for term in re.findall(r"[A-Za-z0-9_]{4,}|[\u3400-\u9fff]{3,}", standard_answer)}
        leaked = [term for term in answer_terms if term.casefold() in text.casefold()]
        if len(leaked) >= 2:
            raise ValueError("follow-up answer leakage heuristic failed")

    def _fallback_probe(
        self,
        turn: Dict[str, Any],
        targets: Sequence[str],
        evidence: Sequence[str],
        reason: str,
        policy: Dict[str, Any],
    ) -> Dict[str, Any]:
        safe_evidence = next(
            (str(item).strip() for item in evidence if item and not _SENSITIVE.search(item)),
            "",
        )
        if not safe_evidence or any(_SENSITIVE.search(item) for item in targets):
            return self._no_followup("approved_fallback_unavailable", policy)
        quoted_evidence = safe_evidence[:40]
        probes = (
            turn.get("question_snapshot", {}).get("followup_probes")
            or turn.get("question_snapshot", {}).get("rubric", {}).get("followup_probes")
            or []
        )
        for value in probes:
            text = str(value.get("question_text") if isinstance(value, dict) else value).strip()
            if self._safe_fallback_text(text, turn, int(policy["max_probe_chars"])):
                return self._fallback_selection(
                    turn,
                    targets,
                    safe_evidence,
                    text,
                    reason,
                    "approved_probe",
                    "approved_followup_library",
                    policy,
                )
        text = (
            "你刚才提到了“%s”，请再补充一个具体做法或实例，并说明你如何验证效果。"
            % quoted_evidence
        )
        if self._safe_fallback_text(text, turn, int(policy["max_probe_chars"])):
            return self._fallback_selection(
                turn,
                targets,
                safe_evidence,
                text,
                reason,
                "deterministic_template",
                "deterministic_followup_gate",
                policy,
            )
        return self._no_followup("approved_fallback_unavailable", policy)

    def _fallback_selection(
        self,
        turn: Dict[str, Any],
        targets: Sequence[str],
        evidence: str,
        text: str,
        reason: str,
        source: str,
        approved_by: str,
        policy: Dict[str, Any],
    ) -> Dict[str, Any]:
        root_turn_id = str(turn.get("root_turn_id") or turn["id"])
        depth = int(turn.get("followup_depth", 0)) + 1
        act = ApprovedConversationAct(
            act_id=new_id("conversation_act"),
            act_type="followup",
            text=text,
            root_turn_id=root_turn_id,
            evidence_quotes=[evidence],
            target_capability_points=list(targets[:2]),
            followup_depth=depth,
            evaluative=False,
            approved_by=approved_by,
            prompt_version=None,
            created_at=utc_now(),
        )
        return {
            "selected": True,
            "root_turn_id": root_turn_id,
            "parent_turn_id": turn["id"],
            "followup_depth": depth,
            "reason": reason,
            "target_key_points": list(targets[:2]),
            "question_text": text,
            "evidence_quotes": [evidence],
            "probe_source": source,
            "conversation_act": act.model_dump(mode="json"),
            "policy": policy,
        }

    @staticmethod
    def _safe_fallback_text(text: str, turn: Dict[str, Any], max_chars: int) -> bool:
        if (
            not text
            or len(text) > max_chars
            or text.count("?") + text.count("？") > 1
            or _SENSITIVE.search(text)
        ):
            return False
        standard_answer = str(
            turn.get("question_snapshot", {}).get("standard_answer") or ""
        )
        answer_terms = {
            term.casefold()
            for term in re.findall(
                r"[A-Za-z0-9_]{4,}|[\u3400-\u9fff]{3,}", standard_answer
            )
        }
        leaked = [term for term in answer_terms if term in text.casefold()]
        return len(leaked) < 2

    @staticmethod
    def _no_followup(reason: str, policy: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "selected": False,
            "reason": reason,
            "target_key_points": [],
            "policy": deepcopy(policy),
        }
