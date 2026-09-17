"""Server-owned references for the understanding wire contract.

The model selects references, never rewrites evidence or frozen capabilities.
Resolution is exact and occurs only after the full wire schema was validated.
"""

import re
from copy import deepcopy


def understanding_references(transcript, capability_points):
    spans = []
    offsets = []
    for match in re.finditer(r"[^。！？!?\n]+[。！？!?\n]*|[。！？!?\n]+", transcript):
        text = match.group().strip()
        base = match.start() + len(match.group()) - len(match.group().lstrip())
        for start in range(0, len(text), 240):
            raw = text[start:start + 240]
            span = raw.strip()
            if span:
                spans.append(span)
                offsets.append(base + start + len(raw) - len(raw.lstrip()))
    return {
        "evidence": {"E%d" % (index + 1): text for index, text in enumerate(spans)},
        "offsets": {"E%d" % (index + 1): offset for index, offset in enumerate(offsets)},
        "capabilities": {"P%d" % (index + 1): text for index, text in enumerate(capability_points)},
    }


def resolve_understanding_references(data, references):
    result = deepcopy(data)
    company = result.get("company_question")
    if company:
        key, quote = company["evidence_id"], company["quote"]
        evidence = references["evidence"][key]
        if not quote.strip() or evidence.count(quote) != 1:
            raise ValueError("Company question must select an unambiguous verbatim span")
        start = references["offsets"][key] + evidence.index(quote)
        result["company_question"] = {"start": start, "end": start + len(quote), "quote": quote}
    basis = result.get("completion_basis")
    if basis:
        basis["evidence_quotes"] = _evidence_quotes(basis.pop("evidence_ids"), references)
    target = result.get("clarification_target")
    if target:
        target["evidence_quote"] = references["evidence"][target.pop("evidence_id")]
        if target["focus_quote"] not in target["evidence_quote"]:
            raise ValueError("Clarification focus is not verbatim")
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
