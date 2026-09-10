"""Word hints from frozen interview material, never candidate text or prose."""
import re
from app.domain.recognition_lexicon import CHINESE_TECHNICAL_TERMS, TERM_FAMILIES

_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_+.#-])[A-Za-z][A-Za-z0-9_+.#-]{1,63}(?![A-Za-z0-9_+.#-])")


def recognition_terms(turn: dict) -> list[str]:
    snapshot = turn.get("question_snapshot") or {}
    sources = [turn.get("question_spoken_text") or snapshot.get("question_text") or ""]
    sources.extend(item for item in snapshot.get("skills", []) if isinstance(item, str))
    sources.extend(item.get("text", "") for item in snapshot.get("key_points", []) if isinstance(item, dict))
    result, seen = [], set()
    for source in sources:
        for match in _IDENTIFIER.finditer(str(source)):
            term = match.group().rstrip(".-")
            if len(term) < 2 or term.casefold() in seen:
                continue
            seen.add(term.casefold())
            result.append(term)
            if len(result) == 100:
                return result
    # Preserve all canonical identifiers first so aliases cannot displace the
    # terms actually frozen in this question. Never derive hints from answers.
    canonical = list(result)
    for term in canonical:
        spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", term.replace("_", " "))
        if len(spaced) > 64 or not re.fullmatch(r"[A-Za-z]{2,}(?: [A-Za-z]{2,}){1,5}", spaced):
            continue
        if spaced.casefold() not in seen:
            seen.add(spaced.casefold())
            result.append(spaced)
            if len(result) == 100:
                break
    frozen_text = "\n".join(str(source) for source in sources).casefold()
    additions = [term for term in sorted(CHINESE_TECHNICAL_TERMS) if term in frozen_text]
    for triggers, terms in TERM_FAMILIES:
        if any(re.search(r"(?<![a-z0-9_])" + re.escape(trigger.casefold()) + r"(?![a-z0-9_])", frozen_text)
               for trigger in triggers):
            additions.extend(terms)
    for term in additions:
        if len(result) >= 100:
            break
        if term.casefold() not in seen:
            result.append(term)
            seen.add(term.casefold())
    return result
