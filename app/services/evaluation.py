from typing import Any, Dict, Optional

from app.core.ids import new_id
from app.core.time import utc_now
from app.model_gateway.gateway import ModelGateway
from app.model_gateway import capabilities as cap
from app.model_gateway.schemas import ChatJSONRequest, ChatMessage
from app.persistence.interface import Persistence
from app.repositories.memory import InMemoryStore


class EvaluationService:
    """Pure scoring boundary: provider calls in, an immutable evaluation revision out."""

    def __init__(
        self,
        store: InMemoryStore,
        *,
        gateway: Optional[ModelGateway] = None,
        persistence: Optional[Persistence] = None,
    ) -> None:
        self.gateway = gateway or ModelGateway(store, persistence=persistence)

    async def evaluate_answer(
        self,
        answer: Dict[str, Any],
        question_snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        response = await self.gateway.invoke(
            cap.LLM_CHAT_JSON,
            ChatJSONRequest(
                organization_id=answer.get("organization_id", "org_default"),
                purpose="answer_evaluation",
                messages=[
                    ChatMessage(role="system", content="你是严格的面试评分助手，只输出结构化评分。"),
                    ChatMessage(
                        role="user",
                        content="题目：%s\n标准答案：%s\n候选人回答：%s"
                        % (
                            question_snapshot["question_text"],
                            question_snapshot["standard_answer"],
                            answer["final_transcript"],
                        ),
                    ),
                ],
                json_schema={
                    "type": "object",
                    "required": [
                        "score",
                        "confidence",
                        "dimension_scores",
                        "covered_key_points",
                        "missing_key_points",
                        "summary",
                    ],
                    "properties": {
                        "score": {"type": "integer", "minimum": 0, "maximum": 100},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "dimension_scores": {"type": "object"},
                        "covered_key_points": {"type": "array"},
                        "missing_key_points": {"type": "array"},
                        "incorrect_claims": {"type": "array"},
                        "summary": {"type": "string"},
                        "suggested_followup": {"type": ["string", "null"]},
                    },
                },
                metadata={
                    "answer_text": answer["final_transcript"],
                    "key_points": question_snapshot["key_points"],
                    "question_id": question_snapshot.get("source_question_id", question_snapshot["id"]),
                    "question_snapshot_id": question_snapshot["id"],
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
            "feedback": data["summary"],
            "suggested_followup": data.get("suggested_followup"),
            "model_info": {
                "provider_id": response.provider.provider_id,
                "model": response.provider.model,
                "request_id": response.provider.request_id,
                "prompt_version": "answer_evaluation.v1",
                "rubric_source_question_version": question_snapshot["source_question_version"],
            },
            "created_by": "system",
            "created_at": now,
            "updated_at": now,
        }
