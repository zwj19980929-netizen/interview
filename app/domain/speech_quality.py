"""Speech evidence quality, distinct from understanding or technical ability."""
import hashlib
import json
import math
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TranscriptConfidenceSpan(BaseModel):
    """A source segment located exactly in the complete server transcript."""

    model_config = ConfigDict(extra="forbid", strict=True)

    start: int = Field(ge=0)
    end: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=12_000)
    confidence: Optional[float] = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    start_ms: int = Field(default=0, ge=0)
    end_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def require_exact_width(self):
        if self.end - self.start != len(self.text):
            raise ValueError("Speech confidence span has inconsistent text positions")
        if self.end_ms < self.start_ms:
            raise ValueError("Speech confidence span has inconsistent audio positions")
        return self


def transcript_confidence_segments(text, segments):
    """Locate ordered provider segments without guessing through ASR rewrites.

    Only outer whitespace may differ from the server transcript. Unknown,
    malformed or differently spelled sources remain unscoped. Coarse provider
    timestamps may overlap across recognition rotations: retain their exact
    source identity, while acoustic quality below rejects overlapping ranges.
    """
    if not isinstance(text, str) or not text or not isinstance(segments, (list, tuple)) or not segments:
        return []
    checked, previous_start, previous_end = [], 0, 0
    for segment in segments:
        row = segment.model_dump() if hasattr(segment, "model_dump") else segment
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            return []
        start, end = row.get("start_ms", 0), row.get("end_ms", 0)
        confidence = row.get("confidence")
        if (type(start) is not int or type(end) is not int
                or start < previous_start or end < previous_end or end < start
                or (confidence is not None and (type(confidence) not in {int, float}
                    or not math.isfinite(confidence) or not 0 <= confidence <= 1))):
            return []
        previous_start, previous_end = start, end
        checked.append((row["text"], confidence, start, end))
    full = "".join(value for value, *_ in checked)
    if full != text:
        if full.strip() != text:
            return []
        left = len(full) - len(full.lstrip())
    else:
        left = 0
    right, cursor, result = left + len(text), 0, []
    for value, confidence, audio_start, audio_end in checked:
        start, end = max(left, cursor), min(right, cursor + len(value))
        if end > start:
            result.append({"start": start - left, "end": end - left,
                           "text": full[start:end], "confidence": confidence,
                           "start_ms": audio_start, "end_ms": audio_end})
        cursor += len(value)
    return result


def control_evidence_confidence(text, spans, ranges):
    """Return measured quality only for fully source-bound control evidence.

    A gap, duplicate range, mutated span or unknown target measurement cannot
    replace the conservative whole-answer confidence. The caller still owns
    semantic/current-speech/confirmation validation and completion authority.
    """
    if not spans or not ranges:
        return None
    cursor, audio_end, checked = 0, 0, []
    try:
        for span in spans:
            row = span.model_dump() if hasattr(span, "model_dump") else span
            item = TranscriptConfidenceSpan.model_validate(row)
            if (item.start != cursor or text[item.start:item.end] != item.text
                    or item.start_ms < audio_end):
                return None
            checked.append(item)
            cursor = item.end
            audio_end = item.end_ms
    except (TypeError, ValueError):
        return None
    if cursor != len(text):
        return None
    known, seen = [], set()
    for start, end in ranges:
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                or (start, end) in seen):
            return None
        seen.add((start, end))
        for item in checked:
            if item.start < end and item.end > start:
                if item.confidence is None:
                    return None
                known.append(item.confidence)
    return min(known) if known else None


def minimum_reported_confidence(*values: Optional[float]) -> Optional[float]:
    """Unknown measurements neither invent certainty nor erase a known low value."""
    known = [value for value in values if value is not None]
    return min(known) if known else None


def limit_semantic_confidence(semantic: float, acoustic: Optional[float]) -> float:
    return min(semantic, acoustic) if acoustic is not None else semantic


def confidence_source(value, segments=(), provider_id=None):
    if value is None:
        return "unavailable"
    if provider_id == "mock":
        return "synthetic"
    if any((item.get("confidence") if isinstance(item, dict) else item.confidence) is None for item in segments):
        return "partial"
    return "provider"


def transcript_identity(answer):
    """Bind human verification to exactly one transcript revision and recording."""
    source = {"text": answer.get("final_transcript", ""), "audio_uri": answer.get("audio_uri"),
              "revision": int(answer.get("current_transcript_revision", 1))}
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def transcript_is_verified(answer):
    verification = answer.get("transcription_verification") or {}
    return bool(verification.get("audio_reviewed") is True and verification.get("reviewer_id")
                and verification.get("verified_at") and verification.get("transcript_identity") == transcript_identity(answer))
