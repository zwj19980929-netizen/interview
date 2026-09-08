"""Word hints from frozen interview material, never candidate text or prose."""
import re

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
    return result
