import hashlib
import math
import re
from typing import Any, Dict, List

from app.core.ids import new_id
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    AvatarSpeakRequest,
    AvatarSpeakResponse,
    ChatJSONRequest,
    ChatJSONResponse,
    ProviderContext,
    ProviderMeta,
    TextEmbeddingRequest,
    TextEmbeddingResponse,
    Usage,
)


class MockProvider:
    provider_id = "mock"

    async def invoke(self, capability: str, request: Any, context: ProviderContext) -> Any:
        provider = ProviderMeta(
            provider_id=self.provider_id,
            model=context.model,
            request_id=new_id("vendor_req"),
            latency_ms=0,
        )
        if capability == cap.LLM_CHAT_JSON and isinstance(request, ChatJSONRequest):
            if request.purpose == "answer_evaluation":
                data = evaluate_answer(request.metadata)
            elif request.purpose == "role_parsing":
                data = {"parsed": True, "profile": request.metadata}
            else:
                data = {"result": "mock", "purpose": request.purpose}
            usage = Usage(
                input_tokens=sum(len(message.content) for message in request.messages) // 4,
                output_tokens=len(str(data)) // 4,
            )
            usage.total_tokens = usage.input_tokens + usage.output_tokens
            return ChatJSONResponse(data=data, usage=usage, provider=provider)
        if capability == cap.EMBEDDING_TEXT and isinstance(request, TextEmbeddingRequest):
            vectors = [text_vector(text) for text in request.texts]
            return TextEmbeddingResponse(vectors=vectors, dimensions=16, provider=provider)
        if capability == cap.AVATAR_SPEAK and isinstance(request, AvatarSpeakRequest):
            return AvatarSpeakResponse(**avatar_speech_plan(request.text), provider=provider)
        raise ProviderError(
            "provider_capability_missing",
            "Mock provider has no runtime adapter for %s." % capability,
            retryable=False,
        )


def avatar_speech_plan(text: str) -> Dict[str, Any]:
    return {
        "speech_id": new_id("avatar_speech"),
        "status": "ready",
        "mode": "browser_speech",
        "text": text,
        "stream_url": None,
        "audio_uri": None,
        "visemes": [],
    }


def tokenize(text: str) -> List[str]:
    text = text.lower()
    tokens = re.findall(r"[a-z0-9_+#.]+|[\u4e00-\u9fff]{2,}", text)
    return [token for token in tokens if token]


def text_vector(text: str, dimensions: int = 16) -> List[float]:
    vector = [0.0 for _ in range(dimensions)]
    for token in tokenize(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = digest[0] % dimensions
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [round(value / norm, 6) for value in vector]


def evaluate_answer(metadata: Dict[str, Any]) -> Dict[str, Any]:
    answer_text = metadata.get("answer_text", "")
    key_points = metadata.get("key_points", [])
    answer_tokens = set(tokenize(answer_text))
    covered = []
    missing = []

    for item in key_points:
        key_point_id = item.get("id")
        text = item.get("text", "")
        kp_tokens = set(tokenize(text))
        is_covered = bool(text and text in answer_text) or bool(kp_tokens and kp_tokens.intersection(answer_tokens))
        if is_covered:
            covered.append({"key_point_id": key_point_id, "evidence": answer_text[:160]})
        else:
            missing.append({"key_point_id": key_point_id, "reason": "候选人回答未覆盖该关键点"})

    if not answer_text.strip():
        score = 0
        confidence = 0.2
    else:
        coverage = len(covered) / max(len(key_points), 1)
        length_bonus = min(len(answer_text.strip()) / 240.0, 1.0) * 10.0
        score = int(round(min(100.0, 35.0 + coverage * 55.0 + length_bonus)))
        confidence = round(0.55 + coverage * 0.35, 2)

    return {
        "score": score,
        "confidence": confidence,
        "dimension_scores": {
            "semantic_correctness": score,
            "key_point_coverage": int(round(len(covered) / max(len(key_points), 1) * 100)),
            "reasoning_depth": max(0, score - 8),
            "role_relevance": score,
            "communication": min(100, score + 5),
        },
        "covered_key_points": covered,
        "missing_key_points": missing,
        "incorrect_claims": [],
        "summary": "回答覆盖了 %s/%s 个关键点。" % (len(covered), len(key_points)),
        "suggested_followup": "请结合实际项目再展开一个具体例子。" if missing else None,
    }
