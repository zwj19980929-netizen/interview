"""Server-owned references for the understanding wire contract.

The model selects references, never rewrites evidence or frozen capabilities.
Resolution is exact and occurs only after the full wire schema was validated.
"""

import re
from copy import deepcopy

from app.core.prompt.semantic_turn import SemanticContractError


def _delivered_clarification_source(transcript, context):
    """Resolve a caller-validated receipt only when its exact text still fits.

    This does not establish delivery or ownership. The service validates those
    facts before passing the private context, and rechecks them before commit.
    """
    fields = {"version", "request_text", "focus_quote", "reply_text", "question_act_id", "delivered"}
    if (not isinstance(context, dict) or set(context) != fields
            or context.get("version") != "content_clarification.v1" or context.get("delivered") is not True
            or any(not isinstance(context.get(key), str) or not context[key].strip()
                   for key in fields - {"delivered"})
            or len(context["question_act_id"]) > 128 or len(context["focus_quote"]) > 80):
        return None
    request, reply, focus = (context[key] for key in ("request_text", "reply_text", "focus_quote"))
    if (not transcript.startswith(request) or transcript[len(request):].strip() != reply.strip()
            or focus not in request):
        return None
    # The exact asked-about phrase is itself sufficient verbatim source for a
    # clarification target. It stays valid even across sentence/chunk splits or
    # repeated occurrences; it is never a claim or completion evidence ID.
    return len(request), {"focus_quote": focus, "evidence_quote": focus}


def understanding_references(transcript, capability_points, *, clarification_context=None):
    spans = []
    offsets = []
    source = _delivered_clarification_source(transcript, clarification_context)
    boundary = source[0] if source is not None else len(transcript)
    for part, origin in ((transcript[:boundary], 0), (transcript[boundary:], boundary)):
        for match in re.finditer(r"[^。！？!?\n]+[。！？!?\n]*|[。！？!?\n]+", part):
            text = match.group().strip()
            base = origin + match.start() + len(match.group()) - len(match.group().lstrip())
            for start in range(0, len(text), 240):
                raw = text[start:start + 240]
                span = raw.strip()
                if span:
                    spans.append(span)
                    offsets.append(base + start + len(raw) - len(raw.lstrip()))
    result = {
        "evidence": {"E%d" % (index + 1): text for index, text in enumerate(spans)},
        "offsets": {"E%d" % (index + 1): offset for index, offset in enumerate(offsets)},
        "capabilities": {"P%d" % (index + 1): text for index, text in enumerate(capability_points)},
    }
    if source is not None:
        result["clarification_sources"] = {"C1": source[1]}
    return result


def resolve_understanding_references(data, references):
    result = deepcopy(data)
    company = result.get("company_question")
    if company:
        key, quote = company["evidence_id"], company["quote"]
        evidence = references["evidence"][key]
        if not quote.strip() or evidence.count(quote) != 1:
            raise SemanticContractError("company_question_span_invalid", "$.company_question.quote")
        start = references["offsets"][key] + evidence.index(quote)
        result["company_question"] = {"start": start, "end": start + len(quote), "quote": quote}
    basis = result.get("completion_basis")
    if basis:
        basis["evidence_quotes"] = _evidence_quotes(basis.pop("evidence_ids"), references)
    target = result.get("clarification_target")
    if references.get("clarification_sources"):
        if not isinstance(target, dict):
            raise SemanticContractError("unresolved_clarification_source_invalid", "$.clarification_target")
        resolution = target.pop("resolution", None)
        fixed = references["clarification_sources"]["C1"]
        if resolution == "resolved":
            if target or result.get("ambiguities"):
                raise SemanticContractError("unresolved_clarification_source_invalid", "$.clarification_target.resolution")
            result["clarification_target"] = target = None
        elif resolution == "same_focus_unresolved":
            if target != {"source_ref": "C1"} or not result.get("ambiguities"):
                raise SemanticContractError("unresolved_clarification_source_invalid", "$.clarification_target.resolution")
        elif resolution == "new_focus_unresolved":
            if (set(target) != {"evidence_id", "focus_quote"} or not result.get("ambiguities")
                    or target["focus_quote"] == fixed["focus_quote"]):
                raise SemanticContractError("unresolved_clarification_source_invalid", "$.clarification_target.resolution")
        else:
            raise SemanticContractError("unresolved_clarification_source_invalid", "$.clarification_target.resolution")
    if target:
        if "source_ref" in target:
            fixed = (references.get("clarification_sources") or {}).get(target["source_ref"])
            if set(target) != {"source_ref"} or fixed is None:
                raise SemanticContractError("unresolved_clarification_source_invalid", "$.clarification_target")
            result["clarification_target"] = deepcopy(fixed)
        else:
            target["evidence_quote"] = references["evidence"][target.pop("evidence_id")]
            if target["focus_quote"] not in target["evidence_quote"]:
                raise SemanticContractError("clarification_focus_not_verbatim", "$.clarification_target.focus_quote")
    result["evidence_quotes"] = _evidence_quotes(result.pop("evidence_ids"), references)
    for claim in result["claims"]:
        claim["evidence_quote"] = references["evidence"][claim.pop("evidence_id")]
    for state in ("covered", "missing"):
        result[state + "_capability_points"] = [
            references["capabilities"][key] for key in result.pop(state + "_point_ids")
        ]
    return result


def _evidence_quotes(ids, references):
    # Wire IDs identify occurrences; two valid, distinct occurrences can have
    # identical verbatim text. Canonical quote lists are sets of text, so retain
    # each quote once in selection order. Do not change occurrence IDs/offsets:
    # company-question exclusion still needs the exact selected source span.
    return list(dict.fromkeys(references["evidence"][key] for key in ids))


def completion_reference_ranges(data, references):
    """Keep exact occurrence identity for acoustic quality of ending evidence.

    Call only after the wire and complete semantic/current-speech contracts
    pass. These offsets locate evidence; they do not authorize completion.
    """
    basis = data.get("completion_basis")
    if not isinstance(basis, dict) or not isinstance(basis.get("evidence_ids"), list):
        return []
    return [(references["offsets"][key], references["offsets"][key] + len(references["evidence"][key]))
            for key in basis["evidence_ids"]]
