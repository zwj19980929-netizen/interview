"""Speech evidence quality, distinct from understanding or technical ability."""
import hashlib
import json
from typing import Optional


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
