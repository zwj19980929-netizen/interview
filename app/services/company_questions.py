"""An inserted company exchange never completes an assessment turn.

The source text, candidate span and durable capture are bound before speech;
the original recording stays open and later scoring excludes only that span.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import logging

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.core.prompt.company_questions import company_reply_contract, company_reply_speech, COMPANY_REPLY_VERSION
from app.core.prompt.validation import validate_structured_response
from app.domain.interview_agent import CompanyQuestionSpan
from app.model_gateway import capabilities as cap
from app.model_gateway.schemas import ChatJSONRequest, InvocationExecutionBudget

_LOG = logging.getLogger(__name__)


def source_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


async def prepare_company_reply(gateway, *, organization_id, company_context, question):
    """One bounded extraction; unsupported or invalid output cannot become facts."""
    text = str(company_context or "").strip()
    result = {"status": "not_found", "citations": [], "source_hash": source_hash(text),
              "prompt_version": COMPANY_REPLY_VERSION}
    if text:
        contract = company_reply_contract(question, text)
        try:
            response = await asyncio.wait_for(gateway.invoke(cap.LLM_CHAT_JSON, ChatJSONRequest(
                organization_id=organization_id, purpose="interview_turn_understanding",
                messages=contract.messages, json_schema=contract.response_schema,
                max_output_tokens=1000, temperature=0,
                execution_budget=InvocationExecutionBudget(timeout_s=8, max_provider_retries=0, max_provider_attempts=1),
                metadata={"prompt_version": contract.version},
            )), timeout=8)
            data = response.data
            validate_structured_response(data, contract.response_schema)
            if (bool(data["citations"]) != (data["status"] == "supported")
                    or any(not item["quote"].strip() or item["quote"] not in text for item in data["citations"])):
                raise ValueError("company_reference_not_verbatim")
            result.update(deepcopy(data))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.warning("Company reply unavailable: error_type=%s", type(exc).__name__)
            result["status"] = "unavailable"
    result["text"] = company_reply_speech(result["status"], result["citations"])
    return result


def exchange_key(capture_id, span):
    return source_hash("%s:%s:%s:%s" % (capture_id, span["start"], span["end"], span["quote"]))


def record_company_exchange(interviews, prepared, final, reply, *, checkpoint, capture_id,
                            evidence_fence, organization_id, guard):
    """Transactional domain gate, deliberately without answer/lifecycle effects."""
    from app.services.evidence_coordination import assert_current_evidence_fence
    span = prepared.understanding.company_question
    if prepared.understanding.turn_intent != "ask_company" or span is None:
        raise ApiError("COMPANY_QUESTION_INVALID", "A grounded candidate question is required.", status_code=409)
    span = span.model_dump(mode="json")
    key = exchange_key(capture_id, span)
    guard()
    with interviews.persistence.transaction(organization_id) as transaction:
        if evidence_fence is not None:
            assert_current_evidence_fence(transaction, evidence_fence)
        session = interviews._required(transaction.interview_sessions.get(prepared.interview_id))
        turn = interviews.lifecycle.require_active_turn(session, prepared.turn_id, allowed_statuses=("asking",))
        interviews.assert_skill_authorized(transaction, session)
        state = session.get("agent_runtime") or {}
        if session.get("status") != "in_progress" or state.get("floor") == "human" or (state.get("takeover") or {}).get("status") == "active":
            raise ApiError("TURN_DECISION_STALE", "Candidate exchange is no longer current.", status_code=409)
        existing = next((item for item in turn.get("company_question_exchanges", []) if item["key"] == key), None)
        if existing:
            return deepcopy(existing), False
        interviews._assert_prepared_decision(prepared, session, turn, {
            "final_transcript": final.text, "stt_confidence": final.confidence,
            "language": final.language, "transcript_segments": [item.model_dump(mode="json") for item in final.segments],
            "stt_provider": final.provider.model_dump(mode="json"), "transcript_source": "server_streaming"})
        stream = transaction.evidence_media_streams.get(checkpoint.get("stream_id"))
        if (not stream or stream.get("interview_id") != session["id"] or stream.get("turn_id") != turn["id"]
                or stream.get("capture_revision", 1) != checkpoint.get("capture_revision")
                or stream.get("last_sealed_frame_sequence") != checkpoint.get("last_sealed_frame_sequence")
                or not checkpoint.get("last_sealed_frame_sequence") or stream.get("complete")
                or final.text[span["start"]:span["end"]] != span["quote"]
                or source_hash(str((session.get("plan_snapshot") or {}).get("company_context") or "").strip()) != reply["source_hash"]):
            raise ApiError("EVIDENCE_MEDIA_CHECKPOINT_CHANGED", "Company question capture is no longer current.", status_code=409)
        exchange = {"id": new_id("company_exchange"), "key": key, "capture_id": capture_id,
            "delivery_status": "pending", "performance_id": None,
            "turn_id": turn["id"], "question_span": span, "transcript_prefix": final.text,
            "understanding_id": prepared.understanding.understanding_id,
            "understanding_prompt_version": prepared.understanding.prompt_version,
            "media_checkpoint": deepcopy(checkpoint), "reply": deepcopy(reply), "created_at": utc_now()}
        mutable = next(item for item in session["turns"] if item["id"] == turn["id"])
        mutable.setdefault("company_question_exchanges", []).append(exchange)
        session["updated_at"] = utc_now()
        transaction.interview_sessions.update(session, expected_version=session["version"])
        return exchange, True


def update_company_delivery(interviews, interview_id, exchange_id, *, status, performance_id,
                            evidence_fence, organization_id):
    from app.services.evidence_coordination import assert_current_evidence_fence
    if status not in {"playing", "delivered", "pending"}:
        raise ValueError("Invalid company reply delivery status")
    with interviews.persistence.transaction(organization_id) as tx:
        assert_current_evidence_fence(tx, evidence_fence)
        session = interviews._required(tx.interview_sessions.get(interview_id))
        if session.get("status") != "in_progress":
            return
        for turn in session.get("turns", []):
            if turn["id"] != session.get("current_turn_id"):
                continue
            for exchange in turn.get("company_question_exchanges", []):
                if exchange["id"] != exchange_id:
                    continue
                if status != "playing" and exchange.get("performance_id") != performance_id:
                    return
                exchange.update(delivery_status=status, performance_id=performance_id, delivery_updated_at=utc_now())
                tx.interview_sessions.update(session, expected_version=session["version"])
                return


def scoring_projection(text, turn, *, media_evidence=None):
    """Keep provenance verbatim; derive scoring input from proven exclusions.

    If a recognizer revises an old prefix, do not guess new offsets or quietly
    include the company question in a score. The answer remains recoverable.
    """
    spans = []
    for exchange in turn.get("company_question_exchanges", []):
        checkpoint = exchange["media_checkpoint"]
        if media_evidence and (checkpoint.get("stream_id") != media_evidence.get("stream_id")
                               or checkpoint.get("capture_revision") != media_evidence.get("capture_revision")):
            continue
        prefix = exchange["transcript_prefix"]
        span = CompanyQuestionSpan.model_validate(exchange["question_span"])
        if not text.startswith(prefix) or text[span.start:span.end] != span.quote:
            raise ApiError("COMPANY_QUESTION_EVIDENCE_CHANGED", "公司问答之前的转写发生变化，请核实后继续提交。", status_code=409)
        spans.append(span.model_dump(mode="json"))
    # Multiple model interpretations may select overlapping question ranges.
    # A union avoids losing or duplicating neighboring technical text.
    ranges = []
    for span in sorted(spans, key=lambda item: item["start"]):
        if ranges and span["start"] <= ranges[-1][1]:
            ranges[-1][1] = max(ranges[-1][1], span["end"])
        else:
            ranges.append([span["start"], span["end"]])
    result, cursor = [], 0
    for start, end in ranges:
        result.append(text[cursor:start])
        cursor = end
    result.append(text[cursor:])
    return "".join(result).strip(), spans
