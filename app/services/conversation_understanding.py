"""Semantic understanding and controlled follow-up decision deep module."""

from __future__ import annotations

import asyncio
import logging
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.domain.speech_quality import limit_semantic_confidence, control_evidence_confidence, transcript_confidence_segments
from app.domain.adaptive_interview import is_adaptive, assessment_contract, budget_summary
from app.domain.session_assessment_scope import scope_allows_followup
from app.core.ids import new_id
from app.core.answer_processing_budget import (
    consume_answer_model_call, new_correction_attempt, remember_answer_correction,
    deferred_answer_correction, answer_model_call_budget_exhausted,
)
from app.core.model_diagnostics import diagnosed_wait_for, record_model_diagnostic, safe_model_call_context
from app.core.prompt.contracts import prompt_contract, understanding_canonical_schema, supplement_reply_canonical_schema
from app.core.prompt.semantic_turn import (
    with_semantic_turn_schema, validate_semantic_turn, validate_current_speech, validate_unresolved_completion, SemanticContractError,
    normalize_answer_relevance, normalize_ambiguous_completion,
    derive_semantic_controls, validate_conversation_clarification,
)
from app.core.prompt.understanding_references import (
    understanding_references,
    resolve_understanding_references,
    completion_reference_ranges,
)
from app.core.prompt.validation import (
    StructuredResponseValidationError,
    validate_structured_response,
)
from app.core.time import utc_now
from app.core.speech_diagnostics import record_turn_control
from app.domain.interview_agent import (
    ApprovedConversationAct,
    ConversationUtterance,
    TurnUnderstanding,
)
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import ChatJSONRequest, StableTranscriptPreview, InvocationExecutionBudget
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.services.meta_intent import MetaIntentDetector


_SENSITIVE = re.compile(
    r"年龄|岁数|出生|性别|男生|女生|婚姻|结婚|未婚|生育|怀孕|民族|种族|宗教|信仰|"
    r"政治|党派|残疾|疾病|病史|健康状况|家庭住址|户籍|籍贯|"
    r"(?<![A-Za-z0-9])(?:sexual|gender|age|religion|disability)(?![A-Za-z0-9])",
    re.IGNORECASE,
)

_LOGGER = logging.getLogger(__name__)
_DECISION_TIMEOUT = 20.0
_FOLLOWUP_REPAIR_TIMEOUT = 3.0
_DEADLINE_RESERVE = .02


class UnderstandingContentError(ValueError):
    """Fixed diagnostic vocabulary; never contains candidate/model text."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class FollowupProposalError(ValueError):
    """A rejected optional proposal cannot invalidate verified answer evidence."""

    def __init__(self, reason_code: str, schema_path: str):
        self.reason_code = reason_code
        self.schema_path = schema_path
        super().__init__(reason_code)


def _log_validation_rejection(
    event: str, exc: Exception, *, stage: str, attempt: int, path: str = "$",
    interview_id: Optional[str] = None, turn_id: Optional[str] = None,
    provider_request_id: Optional[str] = None,
) -> None:
    """Log fixed diagnostic categories and schema paths, never exception text."""
    stage = getattr(exc, "understanding_stage", stage)
    if isinstance(exc, StructuredResponseValidationError):
        reason = exc.reason_code
        path += exc.schema_path[1:]
    elif isinstance(exc, (UnderstandingContentError, FollowupProposalError, SemanticContractError)):
        reason = exc.reason_code
        path = getattr(exc, "schema_path", path)
    elif isinstance(exc, ProviderError):
        reason = "wire_schema_invalid"
    elif isinstance(exc, KeyError):
        reason = "reference_resolution_invalid"
    else:
        reason = "%s_invalid" % stage
    def safe_id(value: Optional[str]) -> str:
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else "-"

    record_model_diagnostic("understanding_rejected", **{
        **safe_model_call_context(), "interview_id": safe_id(interview_id), "turn_id": safe_id(turn_id),
        "validation_stage": stage, "failure_reason": reason, "schema_path": path,
        "attempt": attempt, "provider_request_id": safe_id(provider_request_id),
    })
    _LOGGER.warning(
        "%s validation_stage=%s reason=%s path=%s attempt=%d interview_id=%s turn_id=%s provider_request_id=%s",
        event, stage, reason, path, attempt,
        safe_id(interview_id), safe_id(turn_id), safe_id(provider_request_id),
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
        self._require_authoritative_final(utterance)
        return await self._understand(utterance, turn, interview)

    @staticmethod
    def _require_authoritative_final(utterance: ConversationUtterance) -> None:
        if (
            not isinstance(utterance, ConversationUtterance)
            or not utterance.authoritative
            or not utterance.is_final
            or utterance.source not in {"server_streaming", "server_batch"}
            or not utterance.audio_uri
        ):
            raise ValueError("formal understanding requires an authoritative final utterance")

    def _materialize_understanding(
        self, raw: Dict[str, Any], references: Dict[str, Any], utterance: ConversationUtterance,
        turn: Dict[str, Any], context: Dict[str, Any], *, provider: Dict[str, Any],
        prompt_version: str, deterministic: bool = False,
    ) -> TurnUnderstanding:
        """One seam for source resolution, semantic policy and domain creation.

        Callers validate their whole wire envelope first. This projection never
        adds claims, ending evidence or confirmation receipts. Optional followup
        planning cannot change the verified understanding returned here.
        """
        stage = "understanding_references"
        try:
            data = deepcopy(raw) if deterministic else resolve_understanding_references(raw, references)
            semantic_first = context.get("semantic_first") is True
            if semantic_first:
                data = derive_semantic_controls(data)
            stage = "understanding_canonical"
            validate_structured_response(data, self._canonical_schema(semantic_first))
            if semantic_first:
                data = normalize_answer_relevance(data, utterance.text, context["capability_points"])
            stage = "understanding_content"
            dialogue = context.get("interviewer_context") or {}
            if semantic_first:
                # This flag comes only from the already validated joint wire
                # branch; source resolution has checked its exact new E/focus.
                new_scope = bool(references.get("clarification_sources")
                    and isinstance(raw.get("clarification_target"), dict)
                    and raw["clarification_target"].get("resolution") == "new_focus_unresolved")
                data = normalize_ambiguous_completion(data, utterance.text,
                    input_context=dialogue.get("input_context"),
                    content_clarification=dialogue.get("content_clarification"),
                    new_clarification_scope=new_scope,
                    completion_ranges=completion_reference_ranges(raw, references) if new_scope else None)
                validate_structured_response(data, self._canonical_schema(semantic_first))
            self._validate_understanding_content(data, utterance.text, context["capability_points"],
                input_context=dialogue.get("input_context"), exit_confirmation=dialogue.get("exit_confirmation"),
                content_clarification=dialogue.get("content_clarification"))
            acoustic = utterance.stt_confidence
            if semantic_first and data.get("completion_basis"):
                ranges = completion_reference_ranges(raw, references)
                scoped = self._control_evidence_quality(utterance, ranges, dialogue)
                if scoped is not None:
                    acoustic = scoped
            confidence = limit_semantic_confidence(float(data["confidence"]), acoustic)
            if (not deterministic and confidence < float(self.DEFAULT_POLICY["low_confidence_threshold"])
                    and data.get("turn_intent") != "thinking"):
                data["suggested_action"] = "clarify"
            confidence = self._apply_ambiguity_policy(data, confidence)
            stage = "understanding_domain"
            current = turn.get("current_understanding") or {}
            return TurnUnderstanding(
                understanding_id=new_id("understanding"), revision=int(current.get("revision", 0)) + 1,
                prompt_version=prompt_version, utterance_id=utterance.utterance_id,
                provider=provider, created_at=utc_now(), **{**data, "confidence": confidence},
            )
        except (ValueError, KeyError, TypeError) as exc:
            exc.understanding_stage = stage
            raise

    @staticmethod
    def _control_evidence_quality(utterance, ranges, dialogue):
        """Scope quality to fresh, unambiguous ending references only.

        Complete source segments are required. Acoustic uncertainty in earlier
        technical evidence remains on the utterance; it is never upgraded by
        a clearer request to finish. All semantic/receipt validation runs first.
        """
        text, start, has_scope = utterance.text, 0, False
        context = dialogue.get("input_context") or {}
        boundary = context.get("current_speech_start")
        if (context.get("version") == "conversation_input_context.v1" and type(boundary) is int
                and 0 <= boundary <= len(text) and text[boundary:] == context.get("current_speech")):
            has_scope = True
            start = boundary
            if not context["current_speech"]:
                # A successful quick response can consume the latest request;
                # only its exact recorded occurrence remains eligible for review.
                exchanges = context.get("responded_exchanges") or []
                if exchanges:
                    last = exchanges[-1]
                    candidate_start, candidate_end = last.get("request_start"), last.get("request_end")
                    if (type(candidate_start) is int and type(candidate_end) is int
                            and 0 <= candidate_start < candidate_end <= len(text)
                            and text[candidate_start:candidate_end] == last.get("request_text")):
                        start = candidate_start
        for key in ("content_clarification", "exit_confirmation"):
            exchange = dialogue.get(key)
            if isinstance(exchange, dict) and isinstance(exchange.get("request_text"), str):
                has_scope = True
                start = max(start, len(exchange["request_text"]))
        if not has_scope:
            return None
        for begin, end in ranges:
            quote = text[begin:end]
            if (begin < start or not quote or text.find(quote, start) != begin
                    or text.find(quote, begin + 1) >= 0):
                return None
        return control_evidence_confidence(text, utterance.stt_segments, ranges)

    async def _understand(
        self,
        utterance: ConversationUtterance,
        turn: Dict[str, Any],
        interview: Dict[str, Any],
    ) -> TurnUnderstanding:
        """Shared inference for validated final evidence or a private preview."""
        capability_points = self._capability_points(turn)
        completion_confirmed = interview.get("_answer_completion_confirmed") is True
        semantic_first = interview.get("_semantic_first") is True
        deterministic = None if completion_confirmed or semantic_first else self.meta_intents.detect(utterance.text)
        context = {
            "question_text": turn.get("question_spoken_text")
            or turn.get("question_snapshot", {}).get("question_text", ""),
            "capability_points": capability_points,
            "transcript": utterance.text,
            "completion_confirmed": completion_confirmed,
            "semantic_first": semantic_first,
            "interviewer_context": interview.get("_interviewer_context") or {},
        }
        contract = prompt_contract("interview_turn_understanding", context)
        references = understanding_references(utterance.text, capability_points,
            clarification_context=((context.get("interviewer_context") or {}).get("content_clarification")
                                   if semantic_first else None))
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
                "confidence": limit_semantic_confidence(1.0, utterance.stt_confidence),
                "suggested_action": deterministic.action,
            }
            provider = {
                "provider_id": "deterministic_meta_intent",
                "model": "bounded-bilingual-meta-intent-v1",
                "request_id": new_id("meta_intent"),
                "latency_ms": 0,
            }
        else:
            base_request = ChatJSONRequest(
                organization_id=interview.get("organization_id", "org_default"),
                purpose="interview_turn_understanding",
                messages=contract.messages,
                json_schema=contract.response_schema,
                temperature=0.0,
                max_output_tokens=1800,
                execution_budget=InvocationExecutionBudget(timeout_s=18, max_provider_retries=0,
                                                           max_provider_attempts=1),
                metadata={
                    "prompt_version": contract.version,
                    "interview_id": interview["id"],
                    "turn_id": turn["id"],
                    "transcript": utterance.text,
                    "capability_points": capability_points,
                },
            )
            correction = new_correction_attempt(base_request)
            for attempt in range(1, 3):
                if attempt > 1 and correction is not None:
                    correction = correction.following_attempt()
                if not consume_answer_model_call(correction=correction):
                    return self._safe_failure_understanding(
                        utterance, turn, capability_points, source="provider", retryable=False,
                        reason_code="call_budget_exhausted", attempts=attempt, prompt_version=contract.version,
                    )
                if correction is not None and correction.correction_reason is not None:
                    contract = prompt_contract("interview_turn_understanding", {**context, "correction_reason": correction.correction_reason})
                    record_model_diagnostic("correction_applied", **safe_model_call_context(base_request.metadata),
                        attempt=attempt, failure_reason=correction.correction_reason)
                request = base_request.model_copy(update={"messages": contract.messages,
                    "json_schema": contract.response_schema,
                    "metadata": {**base_request.metadata, "prompt_version": contract.version}})
                stage = "provider"
                request_id = None
                try:
                    response = await diagnosed_wait_for(
                        self.gateway.invoke(
                            cap.LLM_CHAT_JSON,
                            request,
                        ),
                        timeout=20.0,
                        stage="answer_understanding", timeout_reason="understanding_timeout",
                        interview_id=interview.get("id"), turn_id=turn.get("id"),
                    )
                    request_id = response.provider.request_id
                    stage = "wire_schema"
                    validate_structured_response(response.data, contract.response_schema)
                    return self._materialize_understanding(response.data, references, utterance, turn, context,
                        provider=response.provider.model_dump(), prompt_version=contract.version)
                except (ProviderError, asyncio.TimeoutError) as exc:
                    if getattr(exc, "code", None) != "provider_schema_invalid":
                        return self._safe_failure_understanding(
                            utterance, turn, capability_points, source="provider",
                            retryable=isinstance(exc, asyncio.TimeoutError) or bool(exc.retryable),
                            reason_code=self._provider_failure_reason(exc), attempts=attempt,
                            prompt_version=contract.version,
                        )
                    reason = "wire_schema_invalid"
                    _log_validation_rejection(
                        "understanding_contract_rejected", exc, stage=stage, attempt=attempt,
                        interview_id=interview.get("id"), turn_id=turn.get("id"), provider_request_id=request_id,
                    )
                except (StructuredResponseValidationError, ValueError, KeyError, TypeError) as exc:
                    reason = exc.reason_code if isinstance(exc, (UnderstandingContentError, SemanticContractError)) else "wire_schema_invalid"
                    _log_validation_rejection(
                        "understanding_contract_rejected", exc, stage=stage, attempt=attempt,
                        interview_id=interview.get("id"), turn_id=turn.get("id"), provider_request_id=request_id,
                    )
                remember_answer_correction(correction, reason)
                continuation = deferred_answer_correction(correction, reason)
                if continuation is not None:
                    raise continuation
                if attempt == 2 or answer_model_call_budget_exhausted():
                    return self._safe_failure_understanding(
                        utterance, turn, capability_points, source="schema_or_content",
                        retryable=False, reason_code=reason, attempts=attempt,
                        prompt_version=contract.version,
                    )
                contract = prompt_contract("interview_turn_understanding", {**context, "correction_reason": reason})
        # Exact deterministic controls retain their existing acoustic policy.
        return self._materialize_understanding(data, references, utterance, turn, context,
            provider=provider, prompt_version=contract.version, deterministic=True)

    async def prepare_decision(
        self,
        utterance: ConversationUtterance,
        turn: Dict[str, Any],
        interview: Dict[str, Any],
    ) -> tuple[TurnUnderstanding, Dict[str, Any]]:
        """Prepare a revocable understanding/probe pair within one deadline.

        Nothing is persisted, submitted or spoken here. The caller must fence
        the input revision and current interview state again at commit time.
        Partial or client-authored transcripts are never accepted by this seam.
        """

        self._require_authoritative_final(utterance)
        deadline = asyncio.get_running_loop().time() + _DECISION_TIMEOUT
        result = await diagnosed_wait_for(self._prepare_decision(utterance, turn, interview, deadline=deadline),
            timeout=_DECISION_TIMEOUT, stage="turn_decision", timeout_reason="decision_timeout",
            interview_id=interview.get("id"), turn_id=turn.get("id"))
        self._record_followup_decision(result[1], interview, turn, phase="prepared", prompt_version=result[0].prompt_version)
        return result

    async def classify_reception(self, text: str, organization_id: str, *, phase: str = "listening",
                                 question: str = "", preceding_text: str = "",
                                 current_question: Optional[str] = None, root_question: str = "",
                                 last_clarification: str = "", current_turn_id: Optional[str] = None,
                                 root_turn_id: Optional[str] = None, parent_turn_id: Optional[str] = None,
                                 history: Optional[List[Dict[str, Any]]] = None,
                                 process_facts: Optional[Dict[str, Any]] = None,
                                 skill_instructions: str = "") -> Dict[str, Any]:
        """Understand and answer a bounded social request in a single invocation.

        No lifecycle/answer authority is returned. Callers must fence the current
        server utterance and ownership again before speaking the validated reply.
        """
        if not isinstance(text, str) or not text.strip() or len(text) > 100000:
            raise ValueError("A bounded nonempty server utterance is required")
        if phase not in {"listening", "awaiting_reply"}:
            raise ValueError("Unknown conversational phase")
        contract = prompt_contract("conversation_reception", {
            "text": text, "phase": phase, "question": question[:2000], "preceding_text": preceding_text[-1000:],
            "current_question": current_question, "root_question": root_question,
            "last_clarification": last_clarification, "current_turn_id": current_turn_id,
            "root_turn_id": root_turn_id, "parent_turn_id": parent_turn_id,
            "history": history, "process_facts": process_facts, "skill_instructions": skill_instructions,
        })
        response = await diagnosed_wait_for(self.gateway.invoke(cap.LLM_CHAT_JSON, ChatJSONRequest(
            organization_id=organization_id, purpose="interview_turn_understanding",
            messages=contract.messages, json_schema=contract.response_schema,
            temperature=0, max_output_tokens=320,
            execution_budget=InvocationExecutionBudget(timeout_s=4, max_provider_retries=0),
            metadata={"prompt_version": contract.version},
        )), timeout=5, stage="reception", timeout_reason="reception_timeout", turn_id=current_turn_id)
        from app.core.prompt.conversation_reception import validate_reception_response, resolve_reception_repeat_target
        try:
            validate_reception_response(response.data, contract.response_schema, current_speech=text)
        except StructuredResponseValidationError as exc:
            _log_validation_rejection("conversation_reception_rejected", exc, stage="reception_content",
                attempt=1, turn_id=current_turn_id, provider_request_id=response.provider.request_id)
            raise
        data = response.data
        result = {"kind": data["kind"], "confidence": data["confidence"],
                "evidence_quote": understanding_references(text, [])["evidence"][data["evidence_id"]],
                "reply_text": data["reply_text"].strip()}
        if data["kind"] == "repeat":
            result["repeat_target"] = resolve_reception_repeat_target(text, data.get("repeat_target"))
        elif "repeat_target" in data:
            result["repeat_target"] = data["repeat_target"]
        return result

    async def classify_supplement_reply(
        self, reply: str, organization_id: str, *, prior_confirmation: bool = False,
        preceding_text: str = "", question: str = "",
        conversation_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(reply, str) or not reply.strip() or len(reply) > 100000:
            raise ValueError("A bounded nonempty server reply is required")
        contract = prompt_contract("supplement_reply", {
            "reply": reply, "prior_confirmation": prior_confirmation,
            "preceding_text": preceding_text, "question": question,
            "conversation_context": conversation_context,
        })
        response = await self.gateway.invoke(cap.LLM_CHAT_JSON, ChatJSONRequest(
            organization_id=organization_id, purpose="interview_turn_understanding",
            messages=contract.messages, json_schema=contract.response_schema,
            temperature=0, max_output_tokens=350,
            execution_budget=InvocationExecutionBudget(timeout_s=8, max_provider_retries=0, max_provider_attempts=1),
            metadata={"prompt_version": contract.version},
        ))
        from app.core.prompt.supplement_reply import validate_supplement_reply
        return validate_supplement_reply(response.data, contract.response_schema, reply=reply)

    async def classify_exit_reply(self, reply: str, organization_id: str) -> Dict[str, Any]:
        """Classify fresh authoritative speech after a delivered exit question.

        The caller owns delivery, capture, owner and freshness checks. A model
        result alone is never permission to complete the interview.
        """
        from app.core.prompt.candidate_exit import exit_reply_contract, validate_exit_reply_response

        contract = exit_reply_contract({"reply": reply})
        response = await diagnosed_wait_for(self.gateway.invoke(cap.LLM_CHAT_JSON, ChatJSONRequest(
            organization_id=organization_id, purpose="interview_turn_understanding",
            messages=contract.messages, json_schema=contract.response_schema,
            temperature=0, max_output_tokens=400,
            execution_budget=InvocationExecutionBudget(timeout_s=8, max_provider_retries=0),
            metadata={"prompt_version": contract.version},
        )), timeout=8, stage="supplement_reply", timeout_reason="supplement_timeout")
        validate_exit_reply_response(response.data, reply, contract.response_schema)
        return dict(response.data)

    async def prepare_preview(
        self,
        preview: StableTranscriptPreview,
        turn: Dict[str, Any],
        interview: Dict[str, Any],
        *,
        snapshot_ref: str,
    ) -> tuple[TurnUnderstanding, Dict[str, Any]]:
        """Precompute from a validated stable prefix without claiming finality.

        This process-local input cannot advance the lifecycle or become answer
        evidence. A caller must compare the eventual authoritative final and
        current decision context before using the prepared result.
        """
        if not isinstance(preview, StableTranscriptPreview):
            raise ValueError("decision preparation requires a stable transcript preview")
        # Revalidate a detached value even if a caller used model_copy/construct;
        # those helpers deliberately bypass Pydantic field and model validation.
        preview = StableTranscriptPreview.model_validate(preview.model_dump(mode="json"))
        if preview.has_unstable_tail or not preview.text.strip():
            raise ValueError("decision preparation requires a non-empty stable prefix without an unstable tail")
        if not isinstance(snapshot_ref, str) or not snapshot_ref.strip():
            raise ValueError("decision preparation requires an audio checkpoint reference")
        utterance = ConversationUtterance(
            utterance_id=new_id("utterance_preview"), revision=1, speaker="candidate",
            text=preview.text.strip(), is_final=False, authoritative=False,
            audio_uri=snapshot_ref, stt_confidence=preview.confidence,
            stt_segments=transcript_confidence_segments(preview.text.strip(), preview.segments),
            source="server_streaming", created_at=utc_now(),
        )
        return await self._prepare_decision(utterance, turn, interview)

    async def _prepare_decision(
        self,
        utterance: ConversationUtterance,
        turn: Dict[str, Any],
        interview: Dict[str, Any],
        *, deadline: Optional[float] = None,
    ) -> tuple[TurnUnderstanding, Dict[str, Any]]:
        deadline = deadline or (asyncio.get_running_loop().time() + _DECISION_TIMEOUT)
        scope = self._followup_scope(interview, turn, utterance)
        if len(utterance.text) > int(scope["policy"]["max_answer_chars"]):
            # Full authoritative text still goes to the existing understanding
            # contract. Only verified, bounded evidence goes to the probe call.
            understanding = await diagnosed_wait_for(self._understand(utterance, turn, interview),
                timeout=max(.001, deadline - asyncio.get_running_loop().time() - _DEADLINE_RESERVE),
                stage="answer_understanding", timeout_reason="understanding_timeout",
                interview_id=interview.get("id"), turn_id=turn.get("id"))
            if understanding.problem is not None:
                return understanding, self._no_followup("understanding_unavailable", scope["policy"])
            decision, _ = await self._select_followup(interview, turn, utterance, understanding, deadline=deadline)
            return understanding, decision
        if (not interview.get("_answer_completion_confirmed") and not interview.get("_semantic_first")
                and self.meta_intents.detect(utterance.text)) or scope.get("selected") is False:
            understanding = await diagnosed_wait_for(self._understand(utterance, turn, interview),
                timeout=max(.001, deadline - asyncio.get_running_loop().time() - _DEADLINE_RESERVE),
                stage="answer_understanding", timeout_reason="understanding_timeout",
                interview_id=interview.get("id"), turn_id=turn.get("id"))
            # No second invocation when the budget or acoustic confidence
            # already rules out a probe. Deterministic controls invoke none.
            decision = self._followup_scope(interview, turn, utterance, understanding)
            if decision.get("selected") is not False:
                decision = self._no_followup("preflight_followup_disabled", scope["policy"])
            return understanding, decision

        capability_points = self._capability_points(turn)
        references = understanding_references(utterance.text, capability_points,
            clarification_context=((interview.get("_interviewer_context") or {}).get("content_clarification")
                                   if interview.get("_semantic_first") is True else None))
        context = {
            "question_text": turn.get("question_spoken_text")
            or turn.get("question_snapshot", {}).get("question_text", ""),
            "root_question_text": scope["root"].get("question_spoken_text", ""),
            "capability_points": capability_points,
            "transcript": utterance.text,
            "completion_confirmed": interview.get("_answer_completion_confirmed") is True,
            "semantic_first": interview.get("_semantic_first") is True,
            "interviewer_context": interview.get("_interviewer_context") or {},
            "difficulty": scope["difficulty"],
            "max_probe_chars": int(scope["policy"]["max_probe_chars"]),
            "low_confidence_threshold": float(scope["policy"]["low_confidence_threshold"]),
            "previously_probed_ids": [
                key for key, point in references["capabilities"].items()
                if point in scope["previously_probed"]
            ],
        }
        contract = prompt_contract("interview_turn_decision", context)
        base_request = ChatJSONRequest(
            organization_id=interview.get("organization_id", "org_default"),
            purpose="interview_turn_understanding",
            messages=contract.messages,
            json_schema=contract.response_schema,
            temperature=0.0,
            max_output_tokens=2000,
            execution_budget=InvocationExecutionBudget(timeout_s=18, max_provider_retries=0,
                                                       max_provider_attempts=1),
            metadata={
                "prompt_version": contract.version,
                "interview_id": interview["id"],
                "turn_id": turn["id"],
                "transcript": utterance.text,
                "capability_points": capability_points,
                "previously_probed_ids": context["previously_probed_ids"],
                "difficulty": scope["difficulty"],
            },
        )
        correction = new_correction_attempt(base_request)
        for attempt in range(1, 3):
            if attempt > 1 and correction is not None:
                correction = correction.following_attempt()
            if not consume_answer_model_call(correction=correction):
                failed = self._safe_failure_understanding(
                    utterance, turn, capability_points, source="provider", retryable=False,
                    reason_code="call_budget_exhausted", attempts=attempt, prompt_version=contract.version,
                )
                return failed, self._no_followup("understanding_unavailable", scope["policy"])
            if correction is not None and correction.correction_reason is not None:
                contract = prompt_contract("interview_turn_decision", {**context, "correction_reason": correction.correction_reason})
                record_model_diagnostic("correction_applied", **safe_model_call_context(base_request.metadata),
                    attempt=attempt, failure_reason=correction.correction_reason)
            request = base_request.model_copy(update={"messages": contract.messages,
                "json_schema": contract.response_schema,
                "metadata": {**base_request.metadata, "prompt_version": contract.version}})
            stage = "provider"
            request_id = None
            try:
                response = await diagnosed_wait_for(
                    self.gateway.invoke(
                        cap.LLM_CHAT_JSON,
                        request,
                    ),
                    timeout=max(.001, deadline - asyncio.get_running_loop().time() - _DEADLINE_RESERVE),
                    stage="turn_decision", timeout_reason="decision_timeout",
                    interview_id=interview.get("id"), turn_id=turn.get("id"),
                )
                request_id = response.provider.request_id
                # Validate the entire composite response before indexing or
                # resolving any vendor-authored value into domain semantics.
                stage = "wire_schema"
                validate_structured_response(response.data, contract.response_schema)
                understanding = self._materialize_understanding(response.data["understanding"], references,
                    utterance, turn, context, provider=response.provider.model_dump(), prompt_version=contract.version)
                approved_scope = self._followup_scope(interview, turn, utterance, understanding)
                if approved_scope.get("selected") is False:
                    return understanding, approved_scope
                if understanding.suggested_action not in {"accept", "followup", "next"}:
                    return understanding, self._no_followup("understanding_not_accepted", scope["policy"])
                return understanding, await self._resolve_optional_followup(
                    response.data["followup"], references, utterance, turn, approved_scope,
                    provider=response.provider.model_dump(), prompt_version=contract.version,
                    attempt=attempt, interview=interview, understanding=understanding, deadline=deadline,
                )
            except (ProviderError, asyncio.TimeoutError) as exc:
                if getattr(exc, "code", None) != "provider_schema_invalid":
                    failed = self._safe_failure_understanding(
                        utterance, turn, capability_points, source="provider",
                        retryable=isinstance(exc, asyncio.TimeoutError) or bool(exc.retryable),
                        reason_code=self._provider_failure_reason(exc), attempts=attempt,
                        prompt_version=contract.version,
                    )
                    return failed, self._no_followup("understanding_unavailable", scope["policy"])
                reason = "wire_schema_invalid"
                _log_validation_rejection(
                    "turn_decision_contract_rejected", exc, stage=stage, attempt=attempt,
                    interview_id=interview.get("id"), turn_id=turn.get("id"), provider_request_id=request_id,
                )
            except (StructuredResponseValidationError, ValueError, KeyError, TypeError) as exc:
                reason = exc.reason_code if isinstance(exc, (UnderstandingContentError, SemanticContractError)) else "wire_schema_invalid"
                _log_validation_rejection(
                    "turn_decision_contract_rejected", exc, stage=stage, attempt=attempt,
                    path="$.understanding" if getattr(exc, "understanding_stage", stage).startswith("understanding_") else "$",
                    interview_id=interview.get("id"), turn_id=turn.get("id"), provider_request_id=request_id,
                )
            remember_answer_correction(correction, reason)
            continuation = deferred_answer_correction(correction, reason)
            if continuation is not None:
                raise continuation
            if attempt == 2 or answer_model_call_budget_exhausted():
                failed = self._safe_failure_understanding(
                    utterance, turn, capability_points, source="schema_or_content",
                    retryable=False, reason_code=reason, attempts=attempt,
                    prompt_version=contract.version,
                )
                return failed, self._no_followup("understanding_unavailable", scope["policy"])
            contract = prompt_contract("interview_turn_decision", {**context, "correction_reason": reason})
        raise AssertionError("bounded decision preparation exhausted without a result")

    async def _resolve_optional_followup(
        self, proposal: Dict[str, Any], references: Dict[str, Any],
        utterance: ConversationUtterance, turn: Dict[str, Any], scope: Dict[str, Any],
        *, provider: Dict[str, Any], prompt_version: str, attempt: int, interview: Dict[str, Any],
        understanding: TurnUnderstanding, deadline: float,
    ) -> Dict[str, Any]:
        # Called only after the complete wire response and canonical/business
        # understanding have passed validation. A bad optional probe may not
        # discard that understanding or rerun it against unchanged evidence.
        stage = "followup_references"
        try:
            candidate = deepcopy(proposal)
            evidence_id = candidate.pop("evidence_id")
            candidate["evidence_quote"] = references["evidence"][evidence_id] if evidence_id else ""
            candidate["target_capability_points"] = [
                references["capabilities"][key] for key in candidate.pop("target_point_ids")
            ]
            stage = "followup_gate"
            self._gate_followup(
                candidate,
                transcript=utterance.text,
                allowed_evidence=scope["evidence"],
                allowed_targets=scope["targets"],
                missing_targets=scope["missing_targets"],
                claim_evidence=scope["claim_evidence"],
                difficulty=scope["difficulty"],
                root=scope["root"],
                max_chars=int(scope["policy"]["max_probe_chars"]),
            )
            stage = "followup_act"
            return self._approved_followup_selection(
                turn, candidate, scope, provider=provider, prompt_version=prompt_version,
            )
        except (ValueError, KeyError, TypeError) as exc:
            _log_validation_rejection(
                "turn_followup_proposal_rejected", exc, stage=stage, attempt=attempt, path="$.followup",
                interview_id=interview.get("id"), turn_id=turn.get("id"), provider_request_id=provider.get("request_id"),
            )
            if isinstance(exc, FollowupProposalError) and exc.reason_code == "question_shape_invalid":
                return await self._repair_followup(candidate, utterance, turn, interview, understanding,
                                                   deadline=deadline)
            # No unapproved/model-authored act and no invented fallback probe.
            return self._no_followup("followup_proposal_rejected", scope["policy"])

    async def select_followup(
        self,
        interview: Dict[str, Any],
        turn: Dict[str, Any],
        utterance: ConversationUtterance,
        understanding: TurnUnderstanding,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        decision, source_version = await self._select_followup(interview, turn, utterance, understanding, now=now,
            deadline=asyncio.get_running_loop().time() + _DECISION_TIMEOUT)
        self._record_followup_decision(decision, interview, turn, phase="selected",
                                      prompt_version=source_version)
        return decision

    async def _select_followup(
        self, interview: Dict[str, Any], turn: Dict[str, Any], utterance: ConversationUtterance,
        understanding: TurnUnderstanding, *, now: Optional[datetime] = None, deadline: Optional[float] = None,
    ) -> tuple[Dict[str, Any], Optional[str]]:
        deadline = deadline or (asyncio.get_running_loop().time() + _DECISION_TIMEOUT)
        scope = self._followup_scope(interview, turn, utterance, understanding, now=now)
        if scope.get("selected") is False:
            return scope, None
        remaining = deadline - asyncio.get_running_loop().time() - _DEADLINE_RESERVE
        if remaining <= 0:
            return self._no_followup("followup_deadline_exhausted", scope["policy"]), None
        policy, targets, evidence = scope["policy"], scope["targets"], scope["evidence"]
        root, difficulty = scope["root"], scope["difficulty"]
        root_turn_id = scope["root_turn_id"]
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
        if not consume_answer_model_call():
            return self._no_followup("followup_call_budget_exhausted", policy), contract.version
        try:
            response = await diagnosed_wait_for(self.gateway.invoke(
                cap.LLM_CHAT_JSON,
                ChatJSONRequest(
                    organization_id=interview.get("organization_id", "org_default"),
                    purpose="controlled_followup",
                    messages=contract.messages,
                    json_schema=contract.response_schema,
                    temperature=0.0,
                    max_output_tokens=800,
                    execution_budget=InvocationExecutionBudget(timeout_s=remaining, max_provider_retries=0,
                                                               max_provider_attempts=1),
                    metadata={
                        "prompt_version": contract.version,
                        "interview_id": interview["id"],
                        "root_turn_id": root_turn_id,
                        "evidence_quotes": evidence,
                        "target_capability_points": targets,
                        "difficulty": difficulty,
                    },
                ),
            ), timeout=remaining, stage="followup", timeout_reason="followup_timeout",
                interview_id=interview.get("id"), turn_id=turn.get("id"))
            data = deepcopy(response.data)
            validate_structured_response(data, contract.response_schema)
            scope = self._followup_scope(interview, turn, utterance, understanding, now=now)
            if scope.get("selected") is False:
                return scope, contract.version
            evidence, targets = scope["evidence"], scope["targets"]
            self._gate_followup(
                data,
                transcript=utterance.text,
                allowed_evidence=evidence,
                allowed_targets=targets,
                missing_targets=scope["missing_targets"],
                claim_evidence=scope["claim_evidence"],
                difficulty=difficulty,
                root=root,
                max_chars=int(policy["max_probe_chars"]),
            )
            provider = response.provider.model_dump()
        except asyncio.TimeoutError:
            return self._no_followup("followup_deadline_exhausted", policy), contract.version
        except (ProviderError, ValueError, KeyError) as exc:
            # A failed invocation may outlive the question's remaining budget
            # or permission just as a successful one can. Never approve an
            # existing fallback using the scope captured before that wait.
            scope = self._followup_scope(interview, turn, utterance, understanding, now=now)
            if scope.get("selected") is False:
                return scope, contract.version
            policy, targets, evidence = scope["policy"], scope["targets"], scope["evidence"]
            if isinstance(exc, FollowupProposalError) and exc.reason_code == "question_shape_invalid":
                repaired = await self._repair_followup(data, utterance, turn, interview, understanding,
                                                       deadline=deadline, now=now)
                return repaired, "controlled_followup_repair.v1"
            if not scope["missing_targets"]:
                return self._no_followup("optional_detail_probe_unavailable", policy), contract.version
            return self._fallback_probe(
                turn, scope["missing_targets"], evidence, "model_unavailable_or_rejected", policy,
            ), contract.version
        return self._approved_followup_selection(
            turn, data, scope, provider=provider, prompt_version=contract.version,
        ), contract.version

    async def _repair_followup(self, candidate, utterance, turn, interview, understanding, *, deadline, now=None):
        """Repair only wording after every other proposal gate already passed.

        The caller owns cancellation and the commit fence. No understanding,
        evidence binding, rationale or target may be replaced by this response.
        """
        scope = self._followup_scope(interview, turn, utterance, understanding, now=now)
        if scope.get("selected") is False:
            return scope
        remaining = min(_FOLLOWUP_REPAIR_TIMEOUT,
                        deadline - asyncio.get_running_loop().time() - _DEADLINE_RESERVE)
        if remaining <= 0:
            return self._no_followup("followup_deadline_exhausted", scope["policy"])
        contract = prompt_contract("controlled_followup_repair", {
            "question_text": scope["root"].get("question_spoken_text", ""),
            "evidence_quote": candidate["evidence_quote"],
            "target_capability_points": candidate["target_capability_points"],
            "rationale": candidate["rationale"], "draft": candidate["question_text"],
            "difficulty": scope["difficulty"], "max_probe_chars": int(scope["policy"]["max_probe_chars"]),
        })
        request_id = None
        if not consume_answer_model_call():
            return self._no_followup("followup_call_budget_exhausted", scope["policy"])
        try:
            response = await diagnosed_wait_for(self.gateway.invoke(cap.LLM_CHAT_JSON, ChatJSONRequest(
                organization_id=interview.get("organization_id", "org_default"), purpose="controlled_followup",
                messages=contract.messages, json_schema=contract.response_schema, temperature=0,
                max_output_tokens=180,
                execution_budget=InvocationExecutionBudget(timeout_s=remaining, max_provider_retries=0,
                                                           max_provider_attempts=1),
                metadata={"prompt_version": contract.version, "interview_id": interview["id"],
                          "turn_id": turn["id"], "repair_reason": "question_shape_invalid"},
            )), timeout=remaining, stage="followup", timeout_reason="followup_timeout",
                interview_id=interview.get("id"), turn_id=turn.get("id"))
            request_id = response.provider.request_id
            validate_structured_response(response.data, contract.response_schema)
            if not response.data["selected"]:
                return {**self._no_followup("model_declined", scope["policy"]), "probe_source": contract.version}
            repaired = {**deepcopy(candidate), "question_text": response.data["question_text"]}
            scope = self._followup_scope(interview, turn, utterance, understanding, now=now)
            if scope.get("selected") is False:
                return {**scope, "probe_source": contract.version}
            self._gate_followup(repaired, transcript=utterance.text, allowed_evidence=scope["evidence"],
                allowed_targets=scope["targets"], missing_targets=scope["missing_targets"],
                claim_evidence=scope["claim_evidence"], difficulty=scope["difficulty"], root=scope["root"],
                max_chars=int(scope["policy"]["max_probe_chars"]))
            return self._approved_followup_selection(turn, repaired, scope,
                provider=response.provider.model_dump(), prompt_version=contract.version)
        except asyncio.TimeoutError:
            return {**self._no_followup("followup_repair_timeout", scope["policy"]), "probe_source": contract.version}
        except (ProviderError, ValueError, KeyError, TypeError) as exc:
            _log_validation_rejection("turn_followup_repair_rejected", exc, stage="followup_repair", attempt=1,
                path="$.followup", interview_id=interview.get("id"), turn_id=turn.get("id"),
                provider_request_id=request_id)
            return {**self._no_followup("followup_repair_rejected", scope["policy"]), "probe_source": contract.version}

    def _followup_scope(
        self,
        interview: Dict[str, Any],
        turn: Dict[str, Any],
        utterance: ConversationUtterance,
        understanding: Optional[TurnUnderstanding] = None,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Same budget/evidence policy for separate and single-inference calls.

        Without understanding this is a conservative preflight, never approval.
        The complete scope must be recomputed from validated understanding.
        """

        policy = {**self.DEFAULT_POLICY, **(interview.get("followup_policy") or {})}
        if is_adaptive(interview):
            budget = assessment_contract(interview)["budget"]
            policy.update(max_depth=budget["max_followups_per_root"],
                          max_per_root=budget["max_followups_per_root"],
                          max_total=budget["max_total_followups"])
        else:
            primary_count = len([item for item in interview.get("turns", []) if not item.get("is_followup")])
            policy.update(max_total=min(4, primary_count), max_depth=min(2, int(policy["max_depth"])),
                          max_per_root=min(2, int(policy["max_per_root"])))
        depth = int(turn.get("followup_depth", 0))
        root_turn_id = str(turn.get("root_turn_id") or turn["id"])
        followups = [item for item in interview.get("turns", []) if item.get("is_followup")]
        root_followups = [item for item in followups if item.get("root_turn_id") == root_turn_id]
        if understanding is not None and understanding.intent != "answer":
            return self._no_followup("meta_or_non_answer", policy)
        if understanding is not None and not understanding.followup_allowed:
            return self._no_followup("candidate_topic_boundary", policy)
        confidence = understanding.confidence if understanding is not None else utterance.stt_confidence
        if confidence is not None and confidence < float(policy["low_confidence_threshold"]):
            return self._no_followup("low_confidence_clarification_required", policy)
        if understanding is not None and (understanding.problem is not None or
                understanding.suggested_action not in {"accept", "followup", "next"}):
            return self._no_followup("understanding_not_accepted", policy)
        if not bool(turn.get("allow_followup", False)):
            return self._no_followup("followup_disabled", policy)
        if depth >= int(policy["max_depth"]):
            return self._no_followup("depth_budget_exhausted", policy)
        if len(followups) >= int(policy["max_total"]):
            return self._no_followup("session_budget_exhausted", policy)
        if len(root_followups) >= int(policy["max_per_root"]):
            return self._no_followup("root_budget_exhausted", policy)
        if not self._within_time_budget(interview, int(policy["min_remaining_seconds"]), now=now):
            return self._no_followup("time_budget_exhausted", policy)
        if len(utterance.text) < int(policy["min_answer_chars"]):
            return self._no_followup("answer_length_outside_budget", policy)

        previously_probed = {
            str(point)
            for item in root_followups
            for point in item.get("target_key_points", [])
        }
        allowed_points = self._capability_points(turn)
        missing_points = understanding.missing_capability_points if understanding is not None else allowed_points
        # Mentioning a point does not establish the details behind a claim.
        # The model may inspect any still-unprobed point in this frozen scope;
        # it must choose whether an evidence-bound probe is actually useful.
        ordered_points = list(dict.fromkeys([*missing_points, *allowed_points]))
        targets = [
            point
            for point in ordered_points
            if point in allowed_points and point not in previously_probed
        ]
        if not targets:
            return self._no_followup(
                "capability_points_already_probed", policy
            )
        if any(_SENSITIVE.search(item) for item in targets):
            return self._no_followup("sensitive_capability_target", policy)
        evidence_quotes = (
            understanding.evidence_quotes if understanding is not None
            else list(understanding_references(utterance.text, [])["evidence"].values())
        )
        evidence = [
            quote
            for quote in evidence_quotes
            if not _SENSITIVE.search(quote)
        ]
        if not evidence:
            return self._no_followup("model_evidence_unavailable", policy)

        root = next(
            (item for item in interview.get("turns", []) if item.get("id") == root_turn_id),
            turn,
        )
        difficulty = str(root.get("question_snapshot", {}).get("difficulty") or "mid")
        return {
            "policy": policy, "targets": targets, "evidence": evidence,
            "missing_targets": [point for point in targets if point in missing_points],
            "claim_evidence": [claim.evidence_quote for claim in understanding.claims] if understanding is not None else [],
            "root": root, "root_turn_id": root_turn_id, "difficulty": difficulty,
            "previously_probed": previously_probed,
        }

    def _approved_followup_selection(
        self,
        turn: Dict[str, Any],
        data: Dict[str, Any],
        scope: Dict[str, Any],
        *,
        provider: Dict[str, Any],
        prompt_version: str,
    ) -> Dict[str, Any]:
        policy = scope["policy"]
        root_turn_id = scope["root_turn_id"]
        depth = int(turn.get("followup_depth", 0))
        if not data.get("selected"):
            return self._no_followup("model_declined", policy)

        quote = str(data["evidence_quote"]).strip()
        core_question = str(data["question_text"]).strip()
        # The validated question already names the missing detail. Keep the
        # verbatim quote as evidence; repeating a long, hesitant transcript
        # in the spoken preamble delays the question and amplifies ASR errors.
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
            prompt_version=prompt_version,
            created_at=utc_now(),
        )
        return {
            "selected": True,
            "root_turn_id": root_turn_id,
            "parent_turn_id": turn["id"],
            "followup_depth": depth + 1,
            "reason": ("missing_capability_evidence" if any(point in scope["missing_targets"]
                       for point in act.target_capability_points) else "claim_detail_verification"),
            "target_key_points": list(act.target_capability_points),
            "question_text": act.text,
            "evidence_quotes": list(act.evidence_quotes),
            "probe_source": prompt_version,
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
    def _provider_failure_reason(exc: BaseException) -> str:
        if isinstance(exc, asyncio.TimeoutError):
            return "provider_timeout"
        code = getattr(exc, "code", None)
        return code if code in {
            "provider_timeout", "provider_auth_failed", "provider_billing_required",
            "provider_rate_limited", "provider_circuit_open",
        } else "provider_unavailable"

    @staticmethod
    def _safe_failure_understanding(
        utterance: ConversationUtterance,
        turn: Dict[str, Any],
        capability_points: Sequence[str],
        *,
        source: str,
        retryable: bool,
        reason_code: str = "provider_unavailable",
        attempts: int = 1,
        prompt_version: str = "interview_turn_understanding.v6",
    ) -> TurnUnderstanding:
        """Fail closed without persisting unvalidated provider semantics.

        A retryable provider outage may ask for one approved re-utterance.  A
        non-retryable provider error or a schema/content contract violation
        pauses the interview for human review.  Neither path can create a
        CandidateAnswer or a provider-authored conversation act.
        """

        record_model_diagnostic("understanding_failed", **{
            **safe_model_call_context(), "turn_id": turn.get("id"),
            "failure_reason": reason_code, "failure_source": source,
            "retryable": retryable, "attempt": attempts, "prompt_version": prompt_version,
        })
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
            prompt_version=prompt_version,
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
                "reason_code": reason_code,
                "attempts": attempts,
            },
            created_at=utc_now(),
        )

    @staticmethod
    def _validate_understanding_content(
        data: Dict[str, Any], transcript: str, capability_points: Sequence[str], *, input_context=None,
        exit_confirmation=None, content_clarification=None,
    ) -> None:
        from app.core.prompt.candidate_exit import validate_exit_understanding

        validate_semantic_turn(data, transcript)
        validate_current_speech(data, transcript, input_context)
        validate_conversation_clarification(data)
        validate_exit_understanding(data, transcript, exit_confirmation)
        validate_unresolved_completion(data, transcript, content_clarification)
        allowed = set(capability_points)
        covered_values = list(data["covered_capability_points"])
        missing_values = list(data["missing_capability_points"])
        covered = set(covered_values)
        missing = set(missing_values)
        if len(covered) != len(covered_values) or len(missing) != len(missing_values):
            raise UnderstandingContentError("point_duplicates")
        if not covered.issubset(allowed):
            raise UnderstandingContentError("point_not_frozen")
        if not missing.issubset(allowed):
            raise UnderstandingContentError("point_not_frozen")
        if covered & missing or covered | missing != allowed:
            raise UnderstandingContentError("point_partition_invalid")
        quotes = list(data["evidence_quotes"]) + [
            item["evidence_quote"] for item in data["claims"]
        ]
        if any(str(quote).strip() not in transcript for quote in quotes):
            raise UnderstandingContentError("evidence_not_verbatim")
        target = data.get("clarification_target")
        if target:
            if target["evidence_quote"] not in transcript or target["focus_quote"] not in target["evidence_quote"]:
                raise UnderstandingContentError("evidence_not_verbatim")
            allowed_target_intents = {"answer", "clarification_request", "answer_unresolved"}
            if data.get("turn_intent") == "thinking":
                allowed_target_intents.add("not_finished")
            if not data["ambiguities"] or data["intent"] not in allowed_target_intents:
                raise UnderstandingContentError("clarification_target_contract_invalid")
        if data.get("intent") == "answer_declined":
            if (
                data.get("suggested_action") != "next"
                or float(data.get("confidence", 0)) < .75
                or not str(data.get("answer_summary") or "").strip()
                or not data["evidence_quotes"]
                or any(not str(quote).strip() for quote in data["evidence_quotes"])
                or data["claims"] or covered or missing != allowed
                or data["ambiguities"] or data["contradictions"]
            ):
                raise UnderstandingContentError("answer_declined_contract_invalid")
        if data.get("intent") == "answer_unresolved":
            if (not str(data.get("answer_summary") or "").strip() or not data["evidence_quotes"]
                    or data["contradictions"]
                    or (data["answer_content"] == "none" and (covered or missing != allowed))):
                raise UnderstandingContentError("unresolved_answer_contract_invalid")
        if data.get("intent") == "off_topic":
            if (not str(data.get("answer_summary") or "").strip() or not data["evidence_quotes"]
                    or covered or missing != allowed or data["ambiguities"] or target
                    or (data.get("turn_intent") is not None and data["followup_allowed"])):
                raise UnderstandingContentError("off_topic_contract_invalid")
        preserving_claims = (data.get("intent") in {"answer_unresolved", "off_topic"} or (
            data.get("intent") == "clarification_request" and data.get("turn_intent") == "finish_answer"))
        if data.get("intent") == "answer" or (preserving_claims and data.get("answer_content") in {"partial", "technical"}):
            evidence_quotes = [str(item).strip() for item in data["evidence_quotes"]]
            claims = list(data["claims"])
            if (
                not str(data.get("answer_summary") or "").strip()
                or not evidence_quotes
                or not claims
            ):
                raise UnderstandingContentError("answer_evidence_empty")
            evidence_set = set(evidence_quotes)
            if any(str(item["evidence_quote"]).strip() not in evidence_set for item in claims):
                raise UnderstandingContentError("claim_evidence_undeclared")

    @staticmethod
    def _apply_ambiguity_policy(data: Dict[str, Any], confidence: float) -> float:
        # The model identifies the meaning/ambiguity; the server enforces its
        # consequence even if the proposed action contradicts that finding.
        # Explicit pause/repeat controls retain precedence.
        if data.get("completion_basis") and confidence < .75:
            data["completion_basis"] = None
            data["turn_intent"] = "clarify"
            data["followup_allowed"] = False
            data["suggested_action"] = "clarify"
        if data.get("turn_intent") == "ask_company" and confidence < .75:
            data.update(company_question=None, turn_intent="clarify", intent="clarification_request",
                        suggested_action="clarify", followup_allowed=False)
        if data["intent"] in {"answer_declined", "answer_unresolved"} and confidence < .75:
            # A clear lack of technical knowledge can be understood with high
            # confidence. Unreliable speech evidence is a separate limitation;
            # it must not authorize advancement as a high-confidence decline.
            data["intent"] = "clarification_request"
            data["suggested_action"] = "clarify"
            return min(confidence, .64)
        if (data["ambiguities"] and data["intent"] in {"answer", "clarification_request"}
                and data.get("turn_intent") not in {"thinking", "pause", "repeat"}):
            data["suggested_action"] = "clarify"
            return min(confidence, 0.64)
        return confidence

    @staticmethod
    def _canonical_schema(semantic_first: bool) -> Dict[str, Any]:
        schema = understanding_canonical_schema()
        if semantic_first:
            schema["properties"]["intent"]["enum"].append("answer_unresolved")
            return with_semantic_turn_schema(schema, company_questions=True)
        return schema

    @staticmethod
    def can_complete_without_confirmation(understanding: TurnUnderstanding, transcript: str) -> bool:
        """Only explicit topic closure or a verified unresolved reply may bypass.

        A model's finish_answer label and verbatim basis do not prove that an
        ordinary answer is ready to submit. Those answers need the runtime's
        supplement confirmation or explicit finish control. Unresolved replies
        retain their separate delivered-clarification check at preparation and
        commit; full withdrawal always needs its own receipt.
        """
        if (understanding.problem is not None or understanding.completion_basis is None
                or understanding.turn_intent == "stop_interview"):
            return False
        if not (understanding.turn_intent in {"finish_topic", "decline_topic"}
                or (understanding.turn_intent == "finish_answer" and understanding.intent == "answer_unresolved")):
            return False
        data = understanding.model_dump(mode="json")
        validate_semantic_turn(data, transcript)
        return understanding.confidence >= .75 and understanding.suggested_action == "next"

    @staticmethod
    def accepts_as_answer(understanding: TurnUnderstanding, transcript: str) -> bool:
        """Clear irrelevant speech is evidence only after a validated ending.

        Legacy off-topic labels, silence and ordinary ongoing speech cannot
        acquire completion authority from the broadened answer meaning.
        Whole-interview withdrawal still needs the caller's separate receipt.
        """
        if understanding.intent in {"answer", "answer_declined", "answer_unresolved"}:
            return True
        if (understanding.intent != "off_topic" or understanding.problem is not None
                or understanding.completion_basis is None or understanding.confidence < .75
                or understanding.suggested_action != "next"):
            return False
        validate_semantic_turn(understanding.model_dump(mode="json"), transcript)
        return understanding.turn_intent in {"finish_answer", "finish_topic", "decline_topic", "stop_interview"}

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
        contract = interview.get("plan_snapshot", {}).get("assessment_contract") or {}
        if contract.get("schema_version") == "interview_assessment.v2":
            return scope_allows_followup(interview,
                budget_summary(interview, current.isoformat())["remaining_seconds"], minimum_seconds)
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
        missing_targets: Optional[Sequence[str]] = None,
        claim_evidence: Optional[Sequence[str]] = None,
    ) -> None:
        if not data.get("selected"):
            return
        text = str(data.get("question_text") or "").strip()
        quote = str(data.get("evidence_quote") or "").strip()
        targets = list(data.get("target_capability_points") or [])
        if quote not in transcript or quote not in allowed_evidence:
            raise FollowupProposalError("evidence_binding_invalid", "$.followup.evidence_id")
        if not targets or not set(targets).issubset(set(allowed_targets)):
            raise FollowupProposalError("capability_binding_invalid", "$.followup.target_point_ids")
        # A new detail-only probe needs a declared claim source and an explicit
        # reason. Do not infer whether the claim is correct or sufficiently detailed.
        if missing_targets is not None and not set(targets).intersection(missing_targets):
            if not str(data.get("rationale") or "").strip():
                raise FollowupProposalError("detail_rationale_missing", "$.followup.rationale")
            if quote not in (claim_evidence or []):
                raise FollowupProposalError("detail_claim_binding_invalid", "$.followup.evidence_id")
        if data.get("difficulty") != difficulty:
            raise FollowupProposalError("difficulty_escalation", "$.followup.difficulty")
        if (
            data.get("sensitive_attribute_inference")
            or data.get("leaks_answer")
            or _SENSITIVE.search(text)
            or _SENSITIVE.search(quote)
        ):
            raise FollowupProposalError("safety_gate_rejected", "$.followup")
        standard_answer = str(root.get("question_snapshot", {}).get("standard_answer") or "")
        answer_terms = {term for term in re.findall(r"[A-Za-z0-9_]{4,}|[\u3400-\u9fff]{3,}", standard_answer)}
        leaked = [term for term in answer_terms if term.casefold() in text.casefold()]
        if len(leaked) >= 2:
            raise FollowupProposalError("answer_leakage_heuristic", "$.followup.question_text")
        # Shape is repairable only after evidence, target, difficulty and all
        # safety checks have passed. Mixed unsafe+malformed proposals cannot
        # use wording repair to reopen an otherwise rejected question.
        if not text or len(text) > max_chars or text.count("?") + text.count("？") > 1:
            raise FollowupProposalError("question_shape_invalid", "$.followup.question_text")

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
    def _record_followup_decision(decision, interview, turn, *, phase, prompt_version):
        # A proposal is not a committed child turn. Keep its phase explicit and
        # emit only server-owned categories/counts, never evidence or rationale.
        reasons = {
            "meta_or_non_answer", "candidate_topic_boundary", "low_confidence_clarification_required",
            "followup_disabled", "depth_budget_exhausted", "session_budget_exhausted", "root_budget_exhausted",
            "time_budget_exhausted", "answer_length_outside_budget", "capability_points_already_probed",
            "sensitive_capability_target", "model_evidence_unavailable", "preflight_followup_disabled",
            "understanding_not_accepted", "understanding_unavailable", "followup_proposal_rejected",
            "optional_detail_probe_unavailable", "model_declined", "missing_capability_evidence",
            "claim_detail_verification", "model_unavailable_or_rejected", "approved_fallback_unavailable",
            "followup_deadline_exhausted", "followup_repair_timeout", "followup_repair_rejected",
            "followup_call_budget_exhausted",
        }
        reason = decision.get("reason")
        policy = decision.get("policy") or {}
        prompt_version = decision.get("probe_source") or prompt_version
        version = prompt_version if isinstance(prompt_version, str) and re.fullmatch(
            r"(?:interview_turn_understanding|interview_turn_decision|controlled_followup|controlled_followup_repair)\.v[0-9]{1,3}", prompt_version
        ) else None
        record_turn_control("followup_decided", interview_id=interview["id"], turn_id=turn["id"],
            phase=phase, selected=decision.get("selected") is True,
            reason=reason if reason in reasons else "unclassified", prompt_version=version,
            target_count=len(decision.get("target_key_points") or []),
            **{key: int(policy[key]) for key in ("max_depth", "max_per_root", "max_total", "min_remaining_seconds")
               if isinstance(policy.get(key), (int, float))})

    @staticmethod
    def _no_followup(reason: str, policy: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "selected": False,
            "reason": reason,
            "target_key_points": [],
            "policy": deepcopy(policy),
        }
