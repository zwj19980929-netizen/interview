import math
from typing import Any, Dict, List, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.model_gateway.gateway import ModelGateway
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import TextEmbeddingRequest
from app.persistence.interface import Persistence, new_work_item
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.text import normalize_skill, overlap_score, tokenize


class QuestionService:
    def __init__(
        self,
        store: InMemoryStore,
        *,
        gateway: Optional[ModelGateway] = None,
        persistence: Optional[Persistence] = None,
    ) -> None:
        self.gateway = gateway or ModelGateway(store, persistence=persistence)
        self.persistence = persistence or persistence_for(store)

    def _normalize_key_points(self, key_points: List[Any]) -> List[Dict[str, Any]]:
        normalized = []
        for index, item in enumerate(key_points):
            if isinstance(item, str):
                text = item
                weight = 1.0
                aliases = []
            else:
                text = item.get("text", "")
                weight = float(item.get("weight", 1.0))
                aliases = item.get("aliases", [])
            if not text.strip():
                raise ApiError("QUESTION_KEY_POINT_INVALID", "Key point text cannot be empty.")
            normalized.append(
                {
                    "id": new_id("kp"),
                    "text": text.strip(),
                    "weight": weight,
                    "aliases": aliases,
                    "order": index + 1,
                }
            )
        return normalized

    async def create_question(self, payload: Dict[str, Any], organization_id: str = "org_default") -> Dict[str, Any]:
        key_points = self._normalize_key_points(payload["key_points"])
        now = utc_now()
        question_id = new_id("q")
        skills = [normalize_skill(skill) for skill in payload.get("skills", [])]
        question = {
            "id": question_id,
            "organization_id": organization_id,
            "knowledge_base_id": payload.get("knowledge_base_id", "kb_default"),
            "title": payload["title"],
            "question_text": payload["question_text"],
            "standard_answer": payload["standard_answer"],
            "key_points": key_points,
            "difficulty": payload.get("difficulty", "mid"),
            "type": payload.get("type", "open_ended"),
            "skills": skills,
            "role_families": payload.get("role_families", []),
            "rubric": payload.get("rubric", {}),
            "status": "active",
            "index_status": "pending",
            "created_at": now,
            "updated_at": now,
        }
        work_item = new_work_item(
            organization_id=organization_id,
            kind="question.index",
            aggregate_id=question_id,
            idempotency_key="question.index:%s:1" % question_id,
            payload={"question_id": question_id, "question_version": 1},
        )
        with self.persistence.transaction(organization_id) as transaction:
            question = transaction.questions.add(question)
            work_item = transaction.outbox.enqueue(work_item)
        return await self.process_index_work(work_item["id"], organization_id)

    async def process_index_work(
        self,
        work_item_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            running_work = transaction.outbox.start(work_item_id)
            question_id = running_work["payload"]["question_id"]
            question = transaction.questions.get(question_id)
            if question is None:
                raise RuntimeError("Question disappeared while indexing: %s" % question_id)

        key_points = question["key_points"]
        skills = question["skills"]
        now = utc_now()

        try:
            embedding_response = await self.gateway.invoke(
                cap.EMBEDDING_TEXT,
                TextEmbeddingRequest(
                    organization_id=organization_id,
                    purpose="question_indexing",
                    texts=[question["question_text"], question["standard_answer"]]
                    + [kp["text"] for kp in key_points],
                )
            )
        except Exception as exc:
            with self.persistence.transaction(organization_id) as transaction:
                current = transaction.questions.get(question_id)
                if current is None:
                    raise RuntimeError("Question disappeared while indexing: %s" % question_id) from exc
                current["index_status"] = "failed"
                current["updated_at"] = utc_now()
                question = transaction.questions.update(current, expected_version=current["version"])
                transaction.outbox.fail(
                    work_item_id,
                    str(exc),
                    lease_token=running_work["lease_token"],
                )
            if isinstance(exc, ProviderError):
                return question
            raise
        vector_docs = [
            {
                "id": new_id("vec"),
                "organization_id": organization_id,
                "knowledge_base_id": question["knowledge_base_id"],
                "question_id": question_id,
                "doc_type": "question_doc",
                "text": question["question_text"] + "\n" + question["standard_answer"],
                "vector": embedding_response.vectors[0],
                "metadata": {
                    "skills": skills,
                    "difficulty": question["difficulty"],
                    "status": question["status"],
                },
                "created_at": now,
                "updated_at": now,
            }
        ]
        for index, key_point in enumerate(key_points, start=2):
            vector_docs.append(
                {
                    "id": new_id("vec"),
                    "organization_id": organization_id,
                    "knowledge_base_id": question["knowledge_base_id"],
                    "question_id": question_id,
                    "doc_type": "key_point_doc",
                    "text": key_point["text"],
                    "vector": embedding_response.vectors[index],
                    "metadata": {
                        "key_point_id": key_point["id"],
                        "skills": skills,
                        "difficulty": question["difficulty"],
                        "status": question["status"],
                    },
                    "created_at": now,
                    "updated_at": now,
                }
            )
        with self.persistence.transaction(organization_id) as transaction:
            current = transaction.questions.get(question_id)
            if current is None:
                raise RuntimeError("Question disappeared while indexing: %s" % question_id)
            current["index_status"] = "indexed"
            current["updated_at"] = utc_now()
            question = transaction.questions.update(current, expected_version=current["version"])
            transaction.vector_documents.replace_for_question(question_id, vector_docs)
            transaction.outbox.complete(work_item_id, lease_token=running_work["lease_token"])
            return question

    def list_questions(self, organization_id: str = "org_default") -> List[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return transaction.questions.list()

    def get_question(self, question_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            question = transaction.questions.get(question_id)
        if not question:
            raise ApiError("QUESTION_NOT_FOUND", "Question does not exist or is not accessible.", status_code=404)
        return question

    async def search_questions(
        self,
        payload: Dict[str, Any],
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        query = payload.get("query", "")
        filters = payload.get("filters", {}) or {}
        limit = int(payload.get("limit", 20))
        include_answer = bool(payload.get("include_answer", False))
        query_tokens = set(tokenize(query))
        filter_skills = {normalize_skill(skill) for skill in filters.get("skills", [])}
        filter_difficulty = set(filters.get("difficulty", []))
        filter_kbs = set(filters.get("knowledge_base_ids", []))

        embedding_response = await self.gateway.invoke(
            cap.EMBEDDING_TEXT,
            TextEmbeddingRequest(organization_id=organization_id, purpose="question_search", texts=[query])
        )
        query_vector = embedding_response.vectors[0]
        vector_scores: Dict[str, float] = {}
        with self.persistence.transaction(organization_id) as transaction:
            vector_documents = transaction.vector_documents.list()
            questions = transaction.questions.list()
        for vector_doc in vector_documents:
            question_id = vector_doc["question_id"]
            vector_score = cosine_similarity(query_vector, vector_doc.get("vector", []))
            vector_scores[question_id] = max(vector_scores.get(question_id, 0.0), vector_score)

        scored: List[Dict[str, Any]] = []
        for question in questions:
            if question["status"] != "active" or question["index_status"] != "indexed":
                continue
            if filter_kbs and question["knowledge_base_id"] not in filter_kbs:
                continue
            if filter_difficulty and question["difficulty"] not in filter_difficulty:
                continue
            question_skills = set(question.get("skills", []))
            if filter_skills and not question_skills.intersection(filter_skills):
                continue

            text_tokens = set(
                tokenize(
                    " ".join(
                        [
                            question["title"],
                            question["question_text"],
                            question["standard_answer"],
                            " ".join(kp["text"] for kp in question["key_points"]),
                        ]
                    )
                )
            )
            skill_score = overlap_score(filter_skills or query_tokens, question_skills)
            text_score = overlap_score(query_tokens, text_tokens)
            vector_score = vector_scores.get(question["id"], 0.0)
            score = round(min(1.0, 0.35 * vector_score + 0.30 * text_score + 0.30 * skill_score + 0.05), 4)
            if score <= 0.1 and (filter_skills or query_tokens):
                continue

            reasons = []
            if vector_score > 0.4:
                reasons.append("向量相似度 %.2f" % vector_score)
            matched_skills = sorted(question_skills.intersection(filter_skills or query_tokens))
            if matched_skills:
                reasons.append("命中技能: %s" % ", ".join(matched_skills))
            matched_terms = sorted(query_tokens.intersection(text_tokens))[:5]
            if matched_terms:
                reasons.append("命中关键词: %s" % ", ".join(matched_terms))
            if not reasons:
                reasons.append("作为兜底题目返回")

            item = {
                "question_id": question["id"],
                "title": question["title"],
                "score": score,
                "match_reasons": reasons,
                "skills": question["skills"],
                "difficulty": question["difficulty"],
            }
            if include_answer:
                item["question_text"] = question["question_text"]
                item["standard_answer"] = question["standard_answer"]
                item["key_points"] = question["key_points"]
            scored.append(item)

        scored.sort(key=lambda item: item["score"], reverse=True)
        return {"items": scored[:limit], "next_cursor": None}


def cosine_similarity(left: List[float], right: List[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))
