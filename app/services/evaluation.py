from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Optional, Sequence

from app.core.ids import new_id
from app.core.prompt.contracts import prompt_contract
from app.core.time import utc_now
from app.model_gateway.gateway import ModelGateway
from app.model_gateway import capabilities as cap
from app.model_gateway.schemas import ChatJSONRequest
from app.persistence.interface import Persistence
from app.repositories.memory import InMemoryStore


class EvaluationService:
    """Pure scoring boundary: provider calls in, an immutable evaluation revision out."""

    DEFAULT_FOLLOWUP_POLICY = {
        "max_depth": 1,
        "max_total": 2,
        "max_per_root": 1,
        "min_answer_chars": 24,
        "max_answer_chars": 1200,
        "min_remaining_seconds": 45,
        "max_probe_chars": 180,
    }

    def __init__(
        self,
        store: InMemoryStore,
        *,
        gateway: Optional[ModelGateway] = None,
        persistence: Optional[Persistence] = None,
    ) -> None:
        self.gateway = gateway or ModelGateway(store, persistence=persistence)

    def decide_followup(
        self,
        interview: Dict[str, Any],
        turn: Dict[str, Any],
        answer: Dict[str, Any],
        *,
        now: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Make a bounded, replayable probe decision without waiting for an LLM score."""

        policy = {**self.DEFAULT_FOLLOWUP_POLICY, **(interview.get("followup_policy") or {})}
        policy["max_depth"] = min(1, max(0, int(policy["max_depth"])))
        policy["max_total"] = max(0, int(policy["max_total"]))
        policy["max_per_root"] = min(1, max(0, int(policy["max_per_root"])))
        if turn.get("is_followup") or int(turn.get("followup_depth", 0)) >= policy["max_depth"]:
            return self._no_followup("depth_budget_exhausted", policy)
        if not bool(turn.get("allow_followup", False)):
            return self._no_followup("followup_disabled", policy)

        root_turn_id = str(turn.get("root_turn_id") or turn["id"])
        followups = [item for item in interview.get("turns", []) if item.get("is_followup")]
        if len(followups) >= policy["max_total"]:
            return self._no_followup("session_budget_exhausted", policy)
        if sum(1 for item in followups if item.get("root_turn_id") == root_turn_id) >= policy["max_per_root"]:
            return self._no_followup("root_budget_exhausted", policy)
        if not self._within_time_budget(interview, turn, policy, now=now):
            return self._no_followup("time_budget_exhausted", policy)

        transcript = str(answer.get("final_transcript") or "").strip()
        if len(transcript) > int(policy["max_answer_chars"]):
            return self._no_followup("answer_length_budget_exhausted", policy)
        key_points = self._key_point_texts(turn.get("question_snapshot", {}).get("key_points", []))
        if not key_points:
            return self._no_followup("no_approved_target", policy)
        missing = [item for item in key_points if not self._point_is_covered(item, transcript)]
        if not missing:
            return self._no_followup("key_points_covered", policy)

        targets = missing[:2]
        reason = "answer_too_short" if len(transcript) < int(policy["min_answer_chars"]) else "missing_key_points"
        question_text, source = self._select_probe(turn, targets, policy)
        if not question_text:
            return self._no_followup("probe_unavailable", policy)
        return {
            "selected": True,
            "root_turn_id": root_turn_id,
            "parent_turn_id": turn["id"],
            "followup_depth": 1,
            "reason": reason,
            "target_key_points": targets,
            "question_text": question_text,
            "probe_source": source,
            "policy": policy,
        }

    def _select_probe(
        self,
        turn: Dict[str, Any],
        targets: Sequence[str],
        policy: Dict[str, Any],
    ) -> tuple[str, str]:
        snapshot = turn.get("question_snapshot", {})
        candidates = snapshot.get("followup_probes") or snapshot.get("rubric", {}).get("followup_probes") or []
        for candidate in candidates:
            if isinstance(candidate, str):
                text = candidate.strip()
                candidate_targets: List[str] = []
            elif isinstance(candidate, dict):
                text = str(candidate.get("question_text") or candidate.get("text") or "").strip()
                candidate_targets = [str(item).strip() for item in candidate.get("target_key_points", [])]
            else:
                continue
            if candidate_targets and not set(candidate_targets).intersection(targets):
                continue
            if self._valid_probe(text, int(policy["max_probe_chars"])):
                return text, "approved_probe"

        joined = "、".join("“%s”" % item for item in targets)
        text = "请只围绕%s补充说明具体做法、依据或实例，不需要改变题目难度。" % joined
        return (text, "deterministic_template") if self._valid_probe(text, int(policy["max_probe_chars"])) else ("", "")

    @staticmethod
    def _valid_probe(text: str, max_chars: int) -> bool:
        if not text or len(text) > max_chars:
            return False
        normalized = text.casefold()
        return not any(marker in normalized for marker in ("提高难度", "更难", "升级难度", "adaptive difficulty"))

    @staticmethod
    def _key_point_texts(raw: Sequence[Any]) -> List[str]:
        result: List[str] = []
        for item in raw:
            text = str(item.get("text") if isinstance(item, dict) else item).strip()
            if text and text not in result:
                result.append(text)
        return result

    @classmethod
    def _point_is_covered(cls, point: str, transcript: str) -> bool:
        left = cls._normalized_text(point)
        right = cls._normalized_text(transcript)
        if not left or not right:
            return False
        if left in right:
            return True
        point_units = cls._semantic_units(left)
        answer_units = cls._semantic_units(right)
        return bool(point_units) and len(point_units.intersection(answer_units)) / len(point_units) >= 0.6

    @staticmethod
    def _normalized_text(value: str) -> str:
        return "".join(re.findall(r"[a-z0-9_+#.]+|[\u4e00-\u9fff]", value.casefold()))

    @staticmethod
    def _semantic_units(value: str) -> set[str]:
        ascii_words = set(re.findall(r"[a-z0-9_+#.]+", value))
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", value))
        chinese_bigrams = {chinese[index : index + 2] for index in range(max(0, len(chinese) - 1))}
        return ascii_words.union(chinese_bigrams)

    @classmethod
    def _within_time_budget(
        cls,
        interview: Dict[str, Any],
        turn: Dict[str, Any],
        policy: Dict[str, Any],
        *,
        now: Optional[str],
    ) -> bool:
        current = cls._parse_time(now or utc_now())
        started = cls._parse_time(interview.get("started_at"))
        estimated_minutes = int(interview.get("plan_snapshot", {}).get("estimated_minutes", 0))
        if current and started and estimated_minutes > 0:
            remaining = estimated_minutes * 60 - (current - started).total_seconds()
            if remaining < int(policy["min_remaining_seconds"]):
                return False
        root_started = cls._parse_time(turn.get("started_at"))
        expected_minutes = int(turn.get("expected_minutes", 0))
        if current and root_started and expected_minutes > 0:
            root_remaining = expected_minutes * 60 - (current - root_started).total_seconds()
            if root_remaining < int(policy["min_remaining_seconds"]):
                return False
        return True

    @staticmethod
    def _parse_time(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _no_followup(reason: str, policy: Dict[str, Any]) -> Dict[str, Any]:
        return {"selected": False, "reason": reason, "target_key_points": [], "policy": policy}

    async def evaluate_answer(
        self,
        answer: Dict[str, Any],
        question_snapshot: Dict[str, Any],
        role_requirement: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        contract = prompt_contract(
            "answer_evaluation",
            {
                "question_text": question_snapshot["question_text"],
                "standard_answer": question_snapshot["standard_answer"],
                "rubric": question_snapshot.get("rubric", {}),
                "role_requirement": (role_requirement or {}).get("description", ""),
                "answer_text": answer["final_transcript"],
            },
        )
        response = await self.gateway.invoke(
            cap.LLM_CHAT_JSON,
            ChatJSONRequest(
                organization_id=answer.get("organization_id", "org_default"),
                purpose="answer_evaluation",
                messages=contract.messages,
                json_schema=contract.response_schema,
                metadata={
                    "answer_text": answer["final_transcript"],
                    "key_points": question_snapshot["key_points"],
                    "question_id": question_snapshot.get("source_question_id", question_snapshot["id"]),
                    "question_snapshot_id": question_snapshot["id"],
                    "question_type": question_snapshot.get("source_type", "position_bank"),
                    "role_requirement": role_requirement or {},
                    "stt_confidence": answer.get("stt_confidence", 1.0),
                    "prompt_version": contract.version,
                },
            )
        )
        now = utc_now()
        data = response.data
        return {
            "id": new_id("eval"),
            "organization_id": answer.get("organization_id", "org_default"),
            "interview_id": answer["interview_id"],
            "answer_id": answer["id"],
            "question_id": question_snapshot.get("source_question_id", question_snapshot["id"]),
            "question_snapshot_id": question_snapshot["id"],
            "score": data["score"],
            "confidence": data["confidence"],
            "dimension_scores": data["dimension_scores"],
            "covered_key_points": data["covered_key_points"],
            "missing_key_points": data["missing_key_points"],
            "incorrect_claims": data.get("incorrect_claims", []),
            "evidence": data.get("evidence", []),
            "review_flags": data.get("review_flags", []),
            "feedback": data["summary"],
            "suggested_followup": data.get("suggested_followup"),
            "model_info": {
                "provider_id": response.provider.provider_id,
                "model": response.provider.model,
                "request_id": response.provider.request_id,
                "prompt_version": contract.version,
                "rubric_source_question_version": question_snapshot["source_question_version"],
                "scoring_profile": "resume_experience.v1"
                if question_snapshot.get("source_type") == "resume_experience"
                else "position_bank.v2",
            },
            "created_by": "system",
            "created_at": now,
            "updated_at": now,
        }
