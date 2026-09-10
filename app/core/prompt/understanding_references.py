"""Server-owned references for the understanding wire contract.

The model selects references, never rewrites evidence or frozen capabilities.
Resolution is exact and occurs only after the full wire schema was validated.
"""

import re
from copy import deepcopy


def understanding_references(transcript, capability_points):
    spans = []
    for match in re.finditer(r"[^。！？!?\n]+[。！？!?\n]*|[。！？!?\n]+", transcript):
        text = match.group().strip()
        for start in range(0, len(text), 240):
            span = text[start:start + 240].strip()
            if span:
                spans.append(span)
    return {
        "evidence": {"E%d" % (index + 1): text for index, text in enumerate(spans)},
        "capabilities": {"P%d" % (index + 1): text for index, text in enumerate(capability_points)},
    }


def resolve_understanding_references(data, references):
    result = deepcopy(data)
    target = result.get("clarification_target")
    if target:
        target["evidence_quote"] = references["evidence"][target.pop("evidence_id")]
        if target["focus_quote"] not in target["evidence_quote"]:
            raise ValueError("Clarification focus is not verbatim")
    result["evidence_quotes"] = [references["evidence"][key] for key in result.pop("evidence_ids")]
    for claim in result["claims"]:
        claim["evidence_quote"] = references["evidence"][claim.pop("evidence_id")]
    for state in ("covered", "missing"):
        result[state + "_capability_points"] = [
            references["capabilities"][key] for key in result.pop(state + "_point_ids")
        ]
    return result
