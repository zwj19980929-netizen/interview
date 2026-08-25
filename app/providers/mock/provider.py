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
    ChatTextRequest,
    ChatTextResponse,
    BatchSTTRequest,
    BatchSTTResponse,
    ProviderContext,
    ProviderMeta,
    TextEmbeddingRequest,
    TextEmbeddingResponse,
    TranscriptSegment,
    StreamingSTTEvent,
    StreamingSTTRequest,
    TTSSynthesizeRequest,
    TTSSynthesizeResponse,
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
            elif request.purpose == "resume_review":
                data = review_resume(request.metadata)
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
        if capability == cap.LLM_CHAT_TEXT and isinstance(request, ChatTextRequest):
            text = str(request.metadata.get("mock_response") or request.messages[-1].content)
            usage = Usage(
                input_tokens=sum(len(message.content) for message in request.messages) // 4,
                output_tokens=len(text) // 4,
            )
            usage.total_tokens = usage.input_tokens + usage.output_tokens
            return ChatTextResponse(text=text, usage=usage, provider=provider)
        if capability == cap.EMBEDDING_TEXT and isinstance(request, TextEmbeddingRequest):
            vectors = [text_vector(text) for text in request.texts]
            return TextEmbeddingResponse(vectors=vectors, dimensions=16, provider=provider)
        if capability == cap.TTS_SYNTHESIZE and isinstance(request, TTSSynthesizeRequest):
            digest = hashlib.sha256(
                (request.language + "\n" + request.voice_profile_id + "\n" + request.text).encode("utf-8")
            ).hexdigest()
            return TTSSynthesizeResponse(
                audio_uri="mock-tts://%s.wav" % digest,
                content_type="audio/wav",
                duration_ms=max(800, len(request.text) * 180),
                content_hash="sha256:%s" % digest,
                provider=provider,
            )
        if capability == cap.STT_BATCH and isinstance(request, BatchSTTRequest):
            text = str(request.metadata.get("development_transcript", "")).strip()
            if not text:
                raise ProviderError(
                    "provider_final_transcript_missing",
                    "Mock STT requires development_transcript metadata.",
                    retryable=False,
                )
            return BatchSTTResponse(
                text=text,
                language=request.language,
                confidence=float(request.metadata.get("confidence", 0.9)),
                segments=[TranscriptSegment(text=text, start_ms=0, end_ms=int(request.metadata.get("duration_ms", 0)))],
                source="server_batch",
                provider=provider,
            )
        if capability == cap.AVATAR_SPEAK and isinstance(request, AvatarSpeakRequest):
            return AvatarSpeakResponse(**avatar_speech_plan(request.text), provider=provider)
        raise ProviderError(
            "provider_capability_missing",
            "Mock provider has no runtime adapter for %s." % capability,
            retryable=False,
        )

    async def open_stream(self, request: StreamingSTTRequest, context: ProviderContext) -> Any:
        if context.capability != cap.STT_STREAMING:
            raise ProviderError(
                "provider_capability_missing",
                "Mock provider can only open an stt.streaming stream.",
                retryable=False,
            )
        return MockSTTStream(request, context)


class MockSTTStream:
    def __init__(self, request: StreamingSTTRequest, context: ProviderContext) -> None:
        self.request = request
        self.context = context
        self.stream_id = new_id("stt_stream")
        self.sequence = 1
        self.byte_count = 0
        self.partial_sent = False
        self.closed = False
        self.provider = ProviderMeta(
            provider_id="mock",
            model=context.model,
            request_id=new_id("vendor_req"),
            latency_ms=0,
        )
        self.ready_events = [
            StreamingSTTEvent(
                stream_id=self.stream_id,
                sequence=self.sequence,
                type="stream.ready",
                language=request.language,
                provider=self.provider,
            )
        ]

    async def send_audio(self, chunk: bytes) -> List[StreamingSTTEvent]:
        if self.closed:
            raise ProviderError("provider_stream_closed", "Mock STT stream is closed.", retryable=False)
        self.byte_count += len(chunk)
        text = str(self.request.metadata.get("development_transcript", "")).strip()
        if not self.request.enable_partial or not text or self.partial_sent:
            return []
        self.partial_sent = True
        self.sequence += 1
        return [
            StreamingSTTEvent(
                stream_id=self.stream_id,
                sequence=self.sequence,
                type="transcript.partial",
                text=text[: max(1, len(text) // 2)],
                language=self.request.language,
                confidence=float(self.request.metadata.get("confidence", 0.8)),
                is_final=False,
                provider=self.provider,
            )
        ]

    async def finish(self) -> List[StreamingSTTEvent]:
        if self.closed:
            return []
        self.closed = True
        text = str(self.request.metadata.get("development_transcript", "")).strip()
        if not text:
            raise ProviderError(
                "provider_final_transcript_missing",
                "Mock streaming STT requires development_transcript metadata.",
                retryable=False,
            )
        confidence = float(self.request.metadata.get("confidence", 0.9))
        duration_ms = int(self.request.metadata.get("duration_ms", 0))
        self.sequence += 1
        final = StreamingSTTEvent(
            stream_id=self.stream_id,
            sequence=self.sequence,
            type="transcript.final",
            text=text,
            language=self.request.language,
            confidence=confidence,
            segments=[TranscriptSegment(text=text, start_ms=0, end_ms=duration_ms, confidence=confidence)],
            is_final=True,
            provider=self.provider,
        )
        self.sequence += 1
        closed = StreamingSTTEvent(
            stream_id=self.stream_id,
            sequence=self.sequence,
            type="stream.closed",
            language=self.request.language,
            provider=self.provider,
        )
        return [final, closed]

    async def abort(self) -> None:
        self.closed = True


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

    dimensions = {
        "semantic_correctness": score,
        "key_point_coverage": int(round(len(covered) / max(len(key_points), 1) * 100)),
        "reasoning_depth": max(0, score - 8),
        "role_relevance": score,
        "communication": min(100, score + 5),
    }
    if metadata.get("question_type") == "resume_experience":
        dimensions = {
            "specificity": score,
            "technical_depth": max(0, score - 5),
            "evidence_consistency": score,
            "reflection": max(0, score - 10),
        }
    return {
        "score": score,
        "confidence": confidence,
        "dimension_scores": dimensions,
        "covered_key_points": covered,
        "missing_key_points": missing,
        "incorrect_claims": [],
        "evidence": [item["evidence"] for item in covered],
        "review_flags": ["low_stt_confidence"] if float(metadata.get("stt_confidence", 1.0)) < 0.6 else [],
        "summary": "回答覆盖了 %s/%s 个关键点。" % (len(covered), len(key_points)),
        "suggested_followup": "请结合实际项目再展开一个具体例子。" if missing else None,
    }


def review_resume(metadata: Dict[str, Any]) -> Dict[str, Any]:
    text = str(metadata.get("resume_text", "")).strip()
    skills = [str(item) for item in metadata.get("must_have_skills", [])]
    evidence = text[:240] or "简历未提供可核验项目描述"
    focus = skills[0] if skills else "核心技术"
    return {
        "summary": "已依据脱敏简历提取项目证据；结果仅供人工核验。",
        "project_evidence": [{"label": "项目经历", "evidence": evidence}],
        "skill_evidence": [{"label": focus, "evidence": evidence}],
        "warnings": [] if text else ["缺少可核验简历正文"],
        "experience_questions": [
            {
                "question_text": "请具体说明你在简历项目中如何使用%s解决问题，以及结果如何衡量？" % focus,
                "verification_points": ["个人职责", "技术决策", "量化结果", "复盘改进"],
                "evidence_refs": [evidence[:120]],
                "evaluation_guide": "回答应包含与简历一致的背景、本人行动、技术取舍和可核验结果。",
            }
        ],
    }
