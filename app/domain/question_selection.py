import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple

from app.core.errors import ApiError


@dataclass(frozen=True)
class QuestionSelectionRequest:
    session_seed: str
    slot_id: str
    candidates: Tuple[Mapping[str, Any], ...]
    excluded_question_ids: Tuple[str, ...] = ()


class QuestionSelection:
    """Selects one replayable question from an approved, frozen candidate pool."""

    algorithm = "hmac_sha256.v1"

    def select(self, request: QuestionSelectionRequest) -> Dict[str, Any]:
        if not request.session_seed or not request.slot_id:
            raise ApiError("QUESTION_SELECTION_INVALID", "Selection seed and slot are required.")
        excluded = set(request.excluded_question_ids)
        eligible = [
            dict(item)
            for item in request.candidates
            if str(item.get("question_id", "")) and item.get("question_id") not in excluded
        ]
        if not eligible:
            raise ApiError(
                "QUESTION_CANDIDATE_POOL_EXHAUSTED",
                "The approved candidate pool has no unselected question.",
                status_code=409,
            )
        scored = sorted(
            (
                self._stable_score(request.session_seed, request.slot_id, item),
                str(item["question_id"]),
                item,
            )
            for item in eligible
        )
        score, _, selected = scored[0]
        pool_hash = self.pool_hash(request.candidates)
        return {
            "question_id": selected["question_id"],
            "question_version": int(selected.get("question_version", 1)),
            "question_hash": selected.get("question_hash"),
            "slot_id": request.slot_id,
            "algorithm": self.algorithm,
            "stable_rank": score,
            "candidate_pool_hash": pool_hash,
            "candidate_count": len(request.candidates),
            "excluded_question_ids": sorted(excluded),
            "reason": "从批准计划的冻结候选池按会话种子稳定随机选择；恢复时结果可重放。",
        }

    def pool_hash(self, candidates: Iterable[Mapping[str, Any]]) -> str:
        canonical = "\n".join(
            "%s:%s:%s"
            % (
                item.get("question_id", ""),
                item.get("question_version", 1),
                item.get("question_hash", ""),
            )
            for item in sorted(candidates, key=lambda value: str(value.get("question_id", "")))
        )
        return "sha256:%s" % hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _stable_score(self, seed: str, slot_id: str, candidate: Mapping[str, Any]) -> str:
        message = "%s:%s:%s" % (
            slot_id,
            candidate.get("question_id", ""),
            candidate.get("question_version", 1),
        )
        return hmac.new(seed.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
