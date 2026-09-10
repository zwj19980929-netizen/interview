"""Semantic understanding and controlled follow-up decision deep module."""

from __future__ import annotations

import asyncio
import logging
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.domain.speech_quality import limit_semantic_confidence
from app.core.ids import new_id
from app.core.prompt.contracts import prompt_contract, understanding_canonical_schema, supplement_reply_canonical_schema
from app.core.prompt.understanding_references import (
    understanding_references,
    resolve_understanding_references,
)
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
from app.model_gateway.schemas import ChatJSONRequest, StableTranscriptPreview, InvocationExecutionBudget
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.services.meta_intent import MetaIntentDetector


_SENSITIVE = re.compile(
    r"年龄|岁数|出生|性别|男生|女生|婚姻|结婚|未婚|生育|怀孕|民族|种族|宗教|信仰|"
    r"政治|党派|残疾|疾病|病史|健康状况|家庭住址|户籍|籍贯|sexual|gender|age|religion|disability",
    re.IGNORECASE,
)

_LOGGER = logging.getLogger(__name__)


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
    if isinstance(exc, StructuredResponseValidationError):
        reason = exc.reason_code
        path += exc.schema_path[1:]
    elif isinstance(exc, (UnderstandingContentError, FollowupProposalError)):
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

    async def _understand(
        self,
        utterance: ConversationUtterance,
        turn: Dict[str, Any],
        interview: Dict[str, Any],
    ) -> TurnUnderstanding:
        """Shared inference for validated final evidence or a private preview."""
        capability_points = self._capability_points(turn)
        completion_confirmed = interview.get("_answer_completion_confirmed") is True
        deterministic = None if completion_confirmed else self.meta_intents.detect(utterance.text)
        context = {
            "question_text": turn.get("question_spoken_text")
            or turn.get("question_snapshot", {}).get("question_text", ""),
            "capability_points": capability_points,
            "transcript": utterance.text,
            "completion_confirmed": completion_confirmed,
        }
        contract = prompt_contract("interview_turn_understanding", context)
        references = understanding_references(utterance.text, capability_points)
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
            for attempt in range(1, 3):
                stage = "provider"
                request_id = None
                try:
                    response = await asyncio.wait_for(
                        self.gateway.invoke(
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
                        ),
                        timeout=20.0,
                    )
                    request_id = response.provider.request_id
                    stage = "wire_schema"
                    validate_structured_response(response.data, contract.response_schema)
                    stage = "understanding_references"
                    data = resolve_understanding_references(response.data, references)
                    stage = "understanding_canonical"
                    validate_structured_response(data, understanding_canonical_schema())
                    stage = "understanding_content"
                    self._validate_understanding_content(data, utterance.text, capability_points)
                    provider = response.provider.model_dump()
                    confidence = limit_semantic_confidence(float(data["confidence"]), utterance.stt_confidence)
                    break
                except (ProviderError, asyncio.TimeoutError) as exc:
                    if getattr(exc, "code", None) != "provider_schema_invalid":
                        return self._safe_failure_understanding(
                            utterance, turn, capability_points, source="provider",
                            retryable=isinstance(exc, asyncio.TimeoutError) or bool(exc.retryable),
                            reason_code="provider_unavailable", attempts=attempt,
                            prompt_version=contract.version,
                        )
                    reason = "wire_schema_invalid"
                    _log_validation_rejection(
                        "understanding_contract_rejected", exc, stage=stage, attempt=attempt,
                        interview_id=interview.get("id"), turn_id=turn.get("id"), provider_request_id=request_id,
                    )
                except (StructuredResponseValidationError, ValueError, KeyError, TypeError) as exc:
                    reason = exc.reason_code if isinstance(exc, UnderstandingContentError) else "wire_schema_invalid"
                    _log_validation_rejection(
                        "understanding_contract_rejected", exc, stage=stage, attempt=attempt,
                        interview_id=interview.get("id"), turn_id=turn.get("id"), provider_request_id=request_id,
                    )
                if attempt == 2:
                    return self._safe_failure_understanding(
                        utterance, turn, capability_points, source="schema_or_content",
                        retryable=False, reason_code=reason, attempts=attempt,
                        prompt_version=contract.version,
                    )
                contract = prompt_contract("interview_turn_understanding", {**context, "correction_reason": reason})
        if deterministic:
            validate_structured_response(data, understanding_canonical_schema())
            self._validate_understanding_content(
                data, utterance.text, capability_points
            )
            confidence = limit_semantic_confidence(float(data["confidence"]), utterance.stt_confidence)
        # An exact, deterministic meta-intent is a control instruction rather
        # than an answer.  It is safe to honour even when the provider reports
        # low acoustic confidence; otherwise "请再说一遍" could be turned into
        # an unrelated clarification loop.
        if (
            deterministic is None
            and confidence < float(self.DEFAULT_POLICY["low_confidence_threshold"])
        ):
            data["suggested_action"] = "clarify"
        confidence = self._apply_ambiguity_policy(data, confidence)
        current = turn.get("current_understanding") or {}
        return TurnUnderstanding(
            understanding_id=new_id("understanding"),
            revision=int(current.get("revision", 0)) + 1,
            prompt_version=contract.version,
            utterance_id=utterance.utterance_id,
            provider=provider,
            created_at=utc_now(),
            **{**data, "confidence": confidence},
        )

    async def prepare_decision(
        self,
        utterance: ConversationUtterance,
        turn: Dict[str, Any],
        interview: Dict[str, Any],
    ) -> tuple[TurnUnderstanding, Dict[str, Any]]:
        """Prepare a revocable understanding/probe pair with one inference.

        Nothing is persisted, submitted or spoken here. The caller must fence
        the input revision and current interview state again at commit time.
        Partial or client-authored transcripts are never accepted by this seam.
        """

        self._require_authoritative_final(utterance)
        return await self._prepare_decision(utterance, turn, interview)

    async def classify_supplement_reply(self, reply: str, organization_id: str) -> Dict[str, Any]:
        if not isinstance(reply, str) or not reply.strip() or len(reply) > 100000:
            raise ValueError("A bounded nonempty server reply is required")
        contract = prompt_contract("supplement_reply", {"reply": reply})
        response = await self.gateway.invoke(cap.LLM_CHAT_JSON, ChatJSONRequest(
            organization_id=organization_id, purpose="interview_turn_understanding",
            messages=contract.messages, json_schema=contract.response_schema,
            temperature=0, max_output_tokens=350,
            execution_budget=InvocationExecutionBudget(timeout_s=8, max_provider_retries=0),
            metadata={"prompt_version": contract.version},
        ))
        validate_structured_response(response.data, contract.response_schema)
        references = understanding_references(reply, [])
        data = {"intent": response.data["intent"], "confidence": response.data["confidence"],
                "evidence_quote": references["evidence"][response.data["evidence_id"]]}
        validate_structured_response(data, supplement_reply_canonical_schema())
        return data

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
            source="server_streaming", created_at=utc_now(),
        )
        return await self._prepare_decision(utterance, turn, interview)

    async def _prepare_decision(
        self,
        utterance: ConversationUtterance,
        turn: Dict[str, Any],
        interview: Dict[str, Any],
    ) -> tuple[TurnUnderstanding, Dict[str, Any]]:
        scope = self._followup_scope(interview, turn, utterance)
        if (not interview.get("_answer_completion_confirmed") and self.meta_intents.detect(utterance.text)) or scope.get("selected") is False:
            understanding = await self._understand(utterance, turn, interview)
            # No second invocation when the budget or acoustic confidence
            # already rules out a probe. Deterministic controls invoke none.
            decision = self._followup_scope(interview, turn, utterance, understanding)
            if decision.get("selected") is not False:
                decision = self._no_followup("preflight_followup_disabled", scope["policy"])
            return understanding, decision

        capability_points = self._capability_points(turn)
        references = understanding_references(utterance.text, capability_points)
        context = {
            "question_text": turn.get("question_spoken_text")
            or turn.get("question_snapshot", {}).get("question_text", ""),
            "root_question_text": scope["root"].get("question_spoken_text", ""),
            "capability_points": capability_points,
            "transcript": utterance.text,
            "completion_confirmed": interview.get("_answer_completion_confirmed") is True,
            "difficulty": scope["difficulty"],
            "max_probe_chars": int(scope["policy"]["max_probe_chars"]),
            "low_confidence_threshold": float(scope["policy"]["low_confidence_threshold"]),
            "previously_probed_ids": [
                key for key, point in references["capabilities"].items()
                if point in scope["previously_probed"]
            ],
        }
        contract = prompt_contract("interview_turn_decision", context)
        for attempt in range(1, 3):
            stage = "provider"
            request_id = None
            try:
                response = await asyncio.wait_for(
                    self.gateway.invoke(
                        cap.LLM_CHAT_JSON,
                        ChatJSONRequest(
                            organization_id=interview.get("organization_id", "org_default"),
                            purpose="interview_turn_understanding",
                            messages=contract.messages,
                            json_schema=contract.response_schema,
                            temperature=0.0,
                            max_output_tokens=2000,
                            metadata={
                                "prompt_version": contract.version,
                                "interview_id": interview["id"],
                                "turn_id": turn["id"],
                                "transcript": utterance.text,
                                "capability_points": capability_points,
                                "previously_probed_ids": context["previously_probed_ids"],
                                "difficulty": scope["difficulty"],
                            },
                        ),
                    ),
                    timeout=20.0,
                )
                request_id = response.provider.request_id
                # Validate the entire composite response before indexing or
                # resolving any vendor-authored value into domain semantics.
                stage = "wire_schema"
                validate_structured_response(response.data, contract.response_schema)
                stage = "understanding_references"
                data = resolve_understanding_references(response.data["understanding"], references)
                stage = "understanding_canonical"
                validate_structured_response(data, understanding_canonical_schema())
                stage = "understanding_content"
                self._validate_understanding_content(data, utterance.text, capability_points)
                confidence = limit_semantic_confidence(float(data["confidence"]), utterance.stt_confidence)
                if confidence < float(self.DEFAULT_POLICY["low_confidence_threshold"]):
                    data["suggested_action"] = "clarify"
                confidence = self._apply_ambiguity_policy(data, confidence)
                current = turn.get("current_understanding") or {}
                stage = "understanding_domain"
                understanding = TurnUnderstanding(
                    understanding_id=new_id("understanding"),
                    revision=int(current.get("revision", 0)) + 1,
                    prompt_version=contract.version,
                    utterance_id=utterance.utterance_id,
                    provider=response.provider.model_dump(),
                    created_at=utc_now(),
                    **{**data, "confidence": confidence},
                )
                approved_scope = self._followup_scope(interview, turn, utterance, understanding)
                if approved_scope.get("selected") is False:
                    return understanding, approved_scope
                if understanding.suggested_action not in {"accept", "followup", "next"}:
                    return understanding, self._no_followup("understanding_not_accepted", scope["policy"])
                return understanding, self._resolve_optional_followup(
                    response.data["followup"], references, utterance, turn, approved_scope,
                    provider=response.provider.model_dump(), prompt_version=contract.version,
                    attempt=attempt, interview_id=interview.get("id"),
                )
            except (ProviderError, asyncio.TimeoutError) as exc:
                if getattr(exc, "code", None) != "provider_schema_invalid":
                    failed = self._safe_failure_understanding(
                        utterance, turn, capability_points, source="provider",
                        retryable=isinstance(exc, asyncio.TimeoutError) or bool(exc.retryable),
                        reason_code="provider_unavailable", attempts=attempt,
                        prompt_version=contract.version,
                    )
                    return failed, self._no_followup("understanding_unavailable", scope["policy"])
                reason = "wire_schema_invalid"
                _log_validation_rejection(
                    "turn_decision_contract_rejected", exc, stage=stage, attempt=attempt,
                    interview_id=interview.get("id"), turn_id=turn.get("id"), provider_request_id=request_id,
                )
            except (StructuredResponseValidationError, ValueError, KeyError, TypeError) as exc:
                reason = exc.reason_code if isinstance(exc, UnderstandingContentError) else "wire_schema_invalid"
                _log_validation_rejection(
                    "turn_decision_contract_rejected", exc, stage=stage, attempt=attempt,
                    path="$.understanding" if stage.startswith("understanding_") else "$",
                    interview_id=interview.get("id"), turn_id=turn.get("id"), provider_request_id=request_id,
                )
            if attempt == 2:
                failed = self._safe_failure_understanding(
                    utterance, turn, capability_points, source="schema_or_content",
                    retryable=False, reason_code=reason, attempts=attempt,
                    prompt_version=contract.version,
                )
                return failed, self._no_followup("understanding_unavailable", scope["policy"])
            contract = prompt_contract("interview_turn_decision", {**context, "correction_reason": reason})
        raise AssertionError("bounded decision preparation exhausted without a result")

    def _resolve_optional_followup(
        self, proposal: Dict[str, Any], references: Dict[str, Any],
        utterance: ConversationUtterance, turn: Dict[str, Any], scope: Dict[str, Any],
        *, provider: Dict[str, Any], prompt_version: str, attempt: int, interview_id: Optional[str] = None,
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
                interview_id=interview_id, turn_id=turn.get("id"), provider_request_id=provider.get("request_id"),
            )
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
        scope = self._followup_scope(interview, turn, utterance, understanding, now=now)
        if scope.get("selected") is False:
            return scope
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
                turn, targets, evidence, "model_unavailable_or_rejected", policy,
            )
        return self._approved_followup_selection(
            turn, data, scope, provider=provider, prompt_version=contract.version,
        )

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
        primary_count = len([item for item in interview.get("turns", []) if not item.get("is_followup")])
        policy["max_total"] = min(4, primary_count)
        depth = int(turn.get("followup_depth", 0))
        root_turn_id = str(turn.get("root_turn_id") or turn["id"])
        followups = [item for item in interview.get("turns", []) if item.get("is_followup")]
        root_followups = [item for item in followups if item.get("root_turn_id") == root_turn_id]
        if understanding is not None and understanding.intent != "answer":
            return self._no_followup("meta_or_non_answer", policy)
        confidence = understanding.confidence if understanding is not None else utterance.stt_confidence
        if confidence is not None and confidence < float(policy["low_confidence_threshold"]):
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
        missing_points = understanding.missing_capability_points if understanding is not None else self._capability_points(turn)
        targets = [
            point
            for point in missing_points
            if point not in previously_probed
        ][:2]
        if not targets:
            return self._no_followup(
                "capability_points_covered_or_already_probed", policy
            )
        if any(_SENSITIVE.search(item) for item in targets):
            return self._no_followup("sensitive_capability_target", policy)
        evidence_quotes = (
            understanding.evidence_quotes if understanding is not None
            else list(understanding_references(utterance.text, [])["evidence"].values())
        )
        evidence = [
            quote
            for quote in evidence_quotes[:4]
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
            "reason": "missing_capability_evidence",
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
        data: Dict[str, Any], transcript: str, capability_points: Sequence[str]
    ) -> None:
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
        if target and (target["evidence_quote"] not in transcript
                       or target["focus_quote"] not in target["evidence_quote"]
                       or not data["ambiguities"] or data["intent"] not in {"answer", "clarification_request"}):
            raise UnderstandingContentError("evidence_not_verbatim")
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
        if data.get("intent") == "answer":
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
        if data["intent"] == "answer_declined" and confidence < .75:
            # A clear lack of technical knowledge can be understood with high
            # confidence. Unreliable speech evidence is a separate limitation;
            # it must not authorize advancement as a high-confidence decline.
            data["intent"] = "clarification_request"
            data["suggested_action"] = "clarify"
            return min(confidence, .64)
        if data["ambiguities"] and data["intent"] in {"answer", "clarification_request"}:
            data["suggested_action"] = "clarify"
            return min(confidence, 0.64)
        return confidence

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
            raise FollowupProposalError("question_shape_invalid", "$.followup.question_text")
        if quote not in transcript or quote not in allowed_evidence:
            raise FollowupProposalError("evidence_binding_invalid", "$.followup.evidence_id")
        if not targets or not set(targets).issubset(set(allowed_targets)):
            raise FollowupProposalError("capability_binding_invalid", "$.followup.target_point_ids")
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
