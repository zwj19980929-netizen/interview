"""Stable scoring evidence identity, independent of its evaluation revisions."""
import hashlib
import json


def evidence_fingerprint(answers) -> str:
    identities = [{"answer_id": item["id"], "turn_id": item["turn_id"],
                   "root_turn_id": item.get("root_turn_id") or item["turn_id"],
                   "question_snapshot_id": item.get("question_snapshot_id"),
                   "transcript_revision": int(item.get("current_transcript_revision", 1)),
                   "text_hash": hashlib.sha256(str(item.get("final_transcript") or "").encode()).hexdigest()}
                  for item in answers]
    return hashlib.sha256(json.dumps(identities, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
