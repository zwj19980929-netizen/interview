"""Process-local proof that a complete new reply leaves an answer unchanged.

Only the evidence chain issues a token after its model and source fences pass.
The token contains no public payload. Copying or constructing a token does not
copy authority; its immutable private record remains in this process registry.
"""
from dataclasses import dataclass
import json
from typing import Any, Callable
from weakref import WeakKeyDictionary, ref

from app.model_gateway.schemas import StreamingSTTEvent
from app.services.prepared_answer_completion import unchanged_source_prefix


@dataclass(frozen=True, eq=False)
class SupplementCompletionProof:
    pass


@dataclass(frozen=True)
class _ProofRecord:
    final_json: str
    boundary_json: str
    finish_evidence: str
    confidence: float
    question_identity: tuple
    context_fingerprint: str
    capture_reference: Any
    assert_current: Callable[[], None]

    @property
    def capture_binding(self):
        return self.capture_reference()


_ISSUED: WeakKeyDictionary = WeakKeyDictionary()


def _encoded(final):
    return json.dumps(final.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, allow_nan=False)


def _payload(final):
    return {"final_transcript": final.text, "stt_confidence": final.confidence,
            "language": final.language, "transcript_source": "server_streaming",
            "stt_provider": final.provider.model_dump(mode="json") if final.provider else {},
            "transcript_segments": [segment.model_dump(mode="json") for segment in final.segments]}


def completion_proof_record(proof, *, final=None, capture_binding=None):
    """Resolve only an issued identity and recheck its live capture fence."""
    record = _ISSUED.get(proof) if type(proof) is SupplementCompletionProof else None
    if record is None:
        raise ValueError("A server-issued supplement proof is required")
    record.assert_current()
    if ((final is not None and record.final_json != _encoded(final))
            or (capture_binding is not None and record.capture_binding is not capture_binding)):
        raise ValueError("Supplement proof source changed")
    return record


def issue_completion_proof(decision, *, final, boundary_final, reply, capture_binding,
                           question_identity, context_fingerprint, assert_current,
                           prior_proof=None):
    """Bind a validated whole-reply effect to exact authoritative evidence.

    The caller must already have validated the classifier schema and finished
    its owner/context/final fences. This method cannot classify candidate text.
    """
    if (decision.get("intent") not in {"finish", "confirmation_unchanged"}
            or decision.get("answer_effect") != "unchanged"
            or type(decision.get("confidence")) not in {float, int}
            or not .75 <= decision["confidence"] <= 1
            or decision.get("reply_text") != ""
            or not isinstance(decision.get("evidence_quote"), str)
            or not decision["evidence_quote"].strip()
            or decision["evidence_quote"] not in reply
            or not unchanged_source_prefix(_payload(boundary_final), _payload(final), reply)):
        return None
    parent = None
    if prior_proof is not None:
        try:
            parent = completion_proof_record(prior_proof, final=boundary_final,
                                             capture_binding=capture_binding)
        except ValueError:
            return None
        if (parent.question_identity != question_identity
                or parent.context_fingerprint != context_fingerprint):
            return None
    if decision["intent"] == "confirmation_unchanged" and parent is None:
        return None
    assert_current()
    proof = SupplementCompletionProof()
    _ISSUED[proof] = _ProofRecord(
        final_json=_encoded(final),
        boundary_json=parent.boundary_json if parent else _encoded(boundary_final),
        finish_evidence=(parent.finish_evidence if decision["intent"] == "confirmation_unchanged"
                         else decision["evidence_quote"]),
        confidence=min(decision["confidence"], parent.confidence if parent else 1),
        question_identity=question_identity, context_fingerprint=context_fingerprint,
        capture_reference=ref(capture_binding), assert_current=assert_current,
    )
    return proof


def proof_boundary(record):
    return StreamingSTTEvent.model_validate_json(record.boundary_json)


def proof_final(record):
    return StreamingSTTEvent.model_validate_json(record.final_json)
