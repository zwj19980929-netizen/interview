"""Structured evidence input and source binding for answer_evaluation.v8."""
from typing import Any, Dict, List

from app.core.prompt.validation import StructuredResponseValidationError, validate_structured_response


_ID = {"type": "string", "minLength": 1, "maxLength": 512}
EVIDENCE_CHAIN_SCHEMA = {
    "type": "array", "minItems": 1, "maxItems": 128,
    "items": {"type": "object", "additionalProperties": False,
        "required": ["answer_id", "turn_id", "parent_turn_id", "root_turn_id", "question_text",
                     "answer_text", "target_point_ids", "transcript_revision"],
        "properties": {
            "answer_id": _ID, "turn_id": _ID, "root_turn_id": _ID,
            "parent_turn_id": {"type": ["string", "null"], "minLength": 1, "maxLength": 512},
            "question_text": {"type": "string", "minLength": 1, "maxLength": 100000},
            "answer_text": {"type": "string", "minLength": 1, "maxLength": 1000000},
            "target_point_ids": {"type": "array", "minItems": 1, "maxItems": 64,
                                 "uniqueItems": True, "items": _ID},
            "transcript_revision": {"type": "integer", "minimum": 1},
        }},
}


def validate_evaluation_chain(chain: Any, *, root_answer_id: str, root_turn_id: str,
                              point_ids: List[str]) -> None:
    validate_structured_response(chain, EVIDENCE_CHAIN_SCHEMA)
    seen_answers, seen_turns = set(), set()
    for index, item in enumerate(chain):
        if (item["answer_id"] in seen_answers or item["turn_id"] in seen_turns
                or item["root_turn_id"] != root_turn_id
                or not set(item["target_point_ids"]).issubset(point_ids)
                or (index == 0 and (item["answer_id"] != root_answer_id
                                   or item["turn_id"] != root_turn_id or item["parent_turn_id"] is not None))
                or (index > 0 and item["parent_turn_id"] not in seen_turns)):
            raise StructuredResponseValidationError("Invalid scoring evidence chain.", reason_code="evidence_chain")
        seen_answers.add(item["answer_id"])
        seen_turns.add(item["turn_id"])


def evaluation_evidence_sources(data: Dict[str, Any], chain: List[Dict[str, Any]],
                                point_ids: List[str]) -> List[Dict[str, Any]]:
    """Bind quotes to eligible candidate answers, never question/reference text.

    If a short quote occurs in more than one eligible answer, expose every
    possible source instead of inventing a unique attribution.
    """
    sources, covered, missing = [], set(), set()
    for item in data.get("covered_key_points", []):
        point, quote = item["key_point_id"], item["evidence"]
        matches = [entry for entry in chain if point in entry["target_point_ids"]
                   and quote.strip() and quote in entry["answer_text"]]
        if point not in point_ids or point in covered or not matches:
            raise StructuredResponseValidationError("Scoring evidence has no eligible answer source.", reason_code="evidence_source")
        covered.add(point)
        sources.extend({"key_point_id": point, "quote": quote, "answer_id": entry["answer_id"],
                        "turn_id": entry["turn_id"], "transcript_revision": entry["transcript_revision"]}
                       for entry in matches)
    for item in data.get("missing_key_points", []):
        point = item["key_point_id"]
        if point not in point_ids or point in covered or point in missing:
            raise StructuredResponseValidationError("Scoring point classification is inconsistent.", reason_code="evidence_scope")
        missing.add(point)
    if any(not quote.strip() or not any(quote in item["answer_text"] for item in chain)
           for quote in data.get("evidence", [])):
        raise StructuredResponseValidationError("Scoring quote has no candidate answer source.", reason_code="evidence_source")
    return sources
