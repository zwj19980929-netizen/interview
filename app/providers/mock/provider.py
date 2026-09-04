import hashlib
import math
import re
import base64
from typing import Any, Awaitable, Callable, Dict, List, Optional

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
    RealtimeSpeechDialogueEvent,
    RealtimeSpeechDialogueRequest,
    RealtimeSpeechResponseCommand,
    TTSSynthesizeRequest,
    TTSSynthesizeResponse,
    Usage,
)


class MockProvider:
    provider_id = "mock"

    async def validate_credentials(
        self, config: Dict[str, Any], credentials: Dict[str, Any], *, timeout_s: int = 10
    ) -> Dict[str, str]:
        return {"status": "valid", "message": "Local mock provider connection is valid."}

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
            elif request.purpose == "interview_turn_understanding":
                transcript = str(request.metadata.get("transcript") or "").strip()
                points = [str(item) for item in request.metadata.get("capability_points") or []]
                covered = [item for item in points if item.casefold() in transcript.casefold()]
                missing = [item for item in points if item not in covered]
                evidence = [transcript[: min(120, len(transcript))]] if transcript else []
                data = {
                    "intent": "answer",
                    "answer_summary": transcript[:800],
                    "claims": (
                        [{"claim": transcript[:600], "evidence_quote": evidence[0]}]
                        if evidence
                        else []
                    ),
                    "evidence_quotes": evidence,
                    "covered_capability_points": covered,
                    "missing_capability_points": missing,
                    "ambiguities": [],
                    "contradictions": [],
                    "confidence": 0.9,
                    "suggested_action": "followup" if missing else "next",
                }
            elif request.purpose == "controlled_followup":
                evidence = [str(item) for item in request.metadata.get("evidence_quotes") or []]
                targets = [str(item) for item in request.metadata.get("target_capability_points") or []]
                data = {
                    "selected": bool(evidence and targets),
                    "question_text": (
                        "请结合刚才的做法，具体说明你如何验证%s？" % targets[0]
                        if targets
                        else ""
                    ),
                    "evidence_quote": evidence[0] if evidence else "",
                    "target_capability_points": targets[:1],
                    "rationale": "验证候选人已提及但尚未充分覆盖的冻结能力点" if targets else "",
                    "difficulty": str(request.metadata.get("difficulty") or "mid"),
                    "sensitive_attribute_inference": False,
                    "leaks_answer": False,
                }
            elif request.purpose == "resume_review":
                phase = request.metadata.get("resume_review_phase", "single_pass")
                if phase == "evidence_map":
                    data = extract_resume_evidence(request.metadata)
                elif phase == "evidence_compaction":
                    data = compact_resume_evidence(request.metadata)
                elif phase == "final_reduce":
                    data = review_resume_evidence(request.metadata)
                else:
                    data = review_resume(request.metadata)
            elif request.purpose == "resume_experience_question_generation":
                data = generate_resume_experience_questions(request.metadata)
            elif request.purpose == "role_parsing":
                data = {"parsed": True, "profile": request.metadata}
            elif request.purpose == "model_configuration_test":
                data = {"message": "pong"}
            elif request.purpose == "question_blueprint_planning":
                data = generate_question_blueprints(request.metadata)
            elif request.purpose == "question_blueprint_generation":
                data = generate_questions(request.metadata)
            elif request.purpose == "question_generation":
                data = generate_questions(request.metadata)
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

    async def open_dialogue(
        self, request: RealtimeSpeechDialogueRequest, context: ProviderContext
    ) -> Any:
        if context.capability != cap.SPEECH_DIALOGUE_REALTIME:
            raise ProviderError(
                "provider_capability_missing",
                "Mock provider can only open a realtime speech dialogue stream.",
                retryable=False,
            )
        return MockSpeechDialogueStream(request, context)


class MockSpeechDialogueStream:
    def __init__(self, request: RealtimeSpeechDialogueRequest, context: ProviderContext) -> None:
        self.request = request
        self.context = context
        self.stream_id = new_id("dialogue_stream")
        self.sequence = 1
        self.closed = False
        self.audio_started = False
        self.provider = ProviderMeta(
            provider_id="mock",
            model=context.model,
            request_id=new_id("vendor_req"),
            latency_ms=0,
        )
        self.ready_events = [self._event("dialogue.ready")]

    async def send_audio(self, chunk: bytes) -> List[RealtimeSpeechDialogueEvent]:
        if self.closed:
            raise ProviderError("provider_stream_closed", "Mock dialogue is closed.", retryable=False)
        if not chunk or self.audio_started:
            return []
        self.audio_started = True
        return [self._event("input.speech.started")]

    async def commit(
        self,
        command: RealtimeSpeechResponseCommand,
        on_event: Optional[Callable[[RealtimeSpeechDialogueEvent], Awaitable[None]]] = None,
    ) -> List[RealtimeSpeechDialogueEvent]:
        if self.closed:
            return []
        transcript = str(self.request.metadata.get("development_transcript") or "候选人回答")
        pcm = b"\x00\x00" * max(1600, min(24000, len(command.spoken_text) * 800))
        events = [
            self._event("input.speech.stopped"),
            self._event("input.transcript.final", text=transcript, is_final=True),
            self._event("output.transcript.final", text=command.spoken_text, is_final=True),
            self._event("output.audio.delta", audio_base64=base64.b64encode(pcm).decode("ascii")),
            self._event("output.audio.done"),
            self._event("dialogue.closed"),
        ]
        self.closed = True
        if on_event:
            for event in events:
                await on_event(event)
            return []
        return events

    async def interrupt(self) -> List[RealtimeSpeechDialogueEvent]:
        if self.closed:
            return []
        return [self._event("output.interrupted")]

    async def abort(self) -> None:
        self.closed = True

    def _event(self, event_type: str, **values: Any) -> RealtimeSpeechDialogueEvent:
        event = RealtimeSpeechDialogueEvent(
            stream_id=self.stream_id,
            sequence=self.sequence,
            type=event_type,
            audio_content_type=self.request.output_audio.content_type,
            sample_rate_hz=self.request.output_audio.sample_rate_hz,
            provider=self.provider,
            **values,
        )
        self.sequence += 1
        return event


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


def generate_question_blueprints(metadata: Dict[str, Any]) -> Dict[str, Any]:
    target_count = min(30, max(1, int(metadata.get("target_count", 1))))
    tags = [str(item).strip() for item in metadata.get("tags", []) if str(item).strip()]
    topics = tags or ["综合能力"]
    blueprints = []
    for index in range(1, target_count + 1):
        topic = topics[(index - 1) % len(topics)]
        blueprints.append(
            {
                "slot_id": "slot_%02d" % index,
                "topic": topic,
                "scenario": "场景 %02d" % index,
                "focus": ["%s考察点%02d" % (topic, index)],
                "difficulty": ["junior", "mid", "senior", "expert"][(index - 1) % 4],
                "question_type": "open_ended",
            }
        )
    return {"blueprints": blueprints}


def generate_questions(metadata: Dict[str, Any]) -> Dict[str, Any]:
    blueprints = metadata.get("blueprints") or []
    if blueprints:
        tags = [str(item).strip() for item in metadata.get("tags", []) if str(item).strip()]
        questions = []
        for blueprint in blueprints:
            topic = str(blueprint["topic"])
            scenario = str(blueprint["scenario"])
            focus = [str(item) for item in blueprint.get("focus") or []]
            focus_text = "、".join(focus)
            questions.append(
                {
                    "slot_id": blueprint["slot_id"],
                    "title": "%s：%s" % (topic, scenario),
                    "question_text": "在%s中，你会如何处理%s问题？请说明分析、执行和验证过程。" % (scenario, focus_text),
                    "standard_answer": "回答应覆盖%s，并给出明确的分析依据、实施步骤、验证结果和复盘。" % focus_text,
                    "key_points": [
                        {"text": "说明%s的分析依据" % focus_text, "weight": 1.0, "aliases": [topic]},
                        {"text": "给出可验证的实施结果", "weight": 1.0, "aliases": ["验证"]},
                    ],
                    "skills": tags or [topic],
                    "difficulty": blueprint.get("difficulty", "mid"),
                    "type": blueprint.get("question_type", "open_ended"),
                }
            )
        return {"questions": questions}
    target_count = min(30, max(1, int(metadata.get("target_count", 1))))
    tags = [str(item).strip() for item in metadata.get("tags", []) if str(item).strip()]
    positioning = str(metadata.get("positioning") or "岗位能力").strip()
    primary_skill = tags[0] if tags else "综合能力"
    questions = []
    for index in range(1, target_count + 1):
        questions.append(
            {
                "title": "%s场景题 %s" % (primary_skill, index),
                "question_text": "请结合实际案例说明你如何在%s中运用%s解决第%s类问题。"
                % (positioning, primary_skill, index),
                "standard_answer": "回答应说明问题背景、分析过程、关键决策、实施结果和复盘改进。",
                "key_points": [
                    {"text": "描述明确的问题背景", "weight": 1.0, "aliases": ["场景"]},
                    {"text": "给出可验证的解决过程和结果", "weight": 1.0, "aliases": ["结果"]},
                ],
                "skills": tags or [primary_skill],
                "difficulty": "mid",
                "type": "open_ended",
            }
        )
    return {"questions": questions}


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
    matched = [skill for skill in skills if skill.lower() in text.lower()]
    missing = [skill for skill in skills if skill not in matched]
    if not text:
        recommendation = "manual_review"
        score = 0
    elif missing:
        recommendation = "unqualified"
        score = int(round(100 * len(matched) / max(len(skills), 1)))
    else:
        recommendation = "qualified"
        score = 85
    return {
        "summary": "已依据脱敏简历提取项目证据；结果仅供人工核验。",
        "project_evidence": [{"label": "项目经历", "evidence": evidence}],
        "skill_evidence": [{"label": focus, "evidence": evidence}],
        "warnings": [] if text else ["缺少可核验简历正文"],
        "screening": {
            "recommendation": recommendation,
            "score": score,
            "summary": "必备技能证据已逐项核对；结论需由招聘人员复核。",
            "matched_requirements": [
                {"requirement": skill, "evidence": evidence} for skill in matched
            ],
            "unmet_requirements": [
                {"requirement": skill, "reason": "简历中未找到明确的可核验证据"} for skill in missing
            ],
        },
    }


def generate_resume_experience_questions(metadata: Dict[str, Any]) -> Dict[str, Any]:
    evidence = metadata.get("resume_evidence") or []
    selected = [item for item in evidence if str(item.get("label") or "").strip()][:3]
    return {
        "questions": [
            {
                "question_text": "请围绕简历中的“%s”说明你本人负责的工作、关键取舍和结果如何验证？" % item["label"],
                "verification_points": ["本人职责", "具体实现", "方案取舍", "结果证据"],
                "evidence_refs": [item["label"]],
                "evaluation_guide": "回答应与所引用的简历证据一致，并说明本人行动和可核验结果。",
            }
            for item in selected
        ]
    }


def extract_resume_evidence(metadata: Dict[str, Any]) -> Dict[str, Any]:
    text = str(metadata.get("chunk_text", "")).strip()
    skills = [str(item) for item in metadata.get("must_have_skills", [])]
    excerpt = text[:480]
    matched = [skill for skill in skills if skill.lower() in text.lower()]
    return {
        "project_evidence": ([{"label": "项目或经历", "evidence": excerpt}] if excerpt else []),
        "skill_evidence": [
            {"label": skill, "evidence": excerpt or "片段中提及该技能"} for skill in matched
        ],
        "warnings": [] if excerpt else ["当前分块没有可提取文本"],
    }


def compact_resume_evidence(metadata: Dict[str, Any]) -> Dict[str, Any]:
    evidence = metadata.get("evidence") or {}
    result = {"project_evidence": [], "skill_evidence": [], "warnings": []}
    for field in ("project_evidence", "skill_evidence"):
        seen = set()
        for item in evidence.get(field, []):
            key = (str(item.get("label", "")).lower(), str(item.get("evidence", "")).lower())
            if key in seen:
                continue
            seen.add(key)
            result[field].append(
                {
                    "label": str(item.get("label") or "简历证据"),
                    "evidence": str(item.get("evidence") or "未提供证据"),
                    "source_pages": sorted(set(item.get("source_pages") or [1])),
                }
            )
    result["warnings"] = list(dict.fromkeys(str(item) for item in evidence.get("warnings", []) if str(item)))
    return result


def review_resume_evidence(metadata: Dict[str, Any]) -> Dict[str, Any]:
    normalized = metadata.get("evidence") or {}
    all_evidence = [
        *normalized.get("project_evidence", []),
        *normalized.get("skill_evidence", []),
    ]
    searchable = "\n".join(
        "%s %s" % (item.get("label", ""), item.get("evidence", "")) for item in all_evidence
    )
    result = review_resume({**metadata, "resume_text": searchable})
    result["project_evidence"] = normalized.get("project_evidence", [])
    result["skill_evidence"] = normalized.get("skill_evidence", [])
    result["warnings"] = normalized.get("warnings", [])
    for matched in result["screening"]["matched_requirements"]:
        pages = {
            page
            for item in all_evidence
            if matched["requirement"].lower()
            in ("%s %s" % (item.get("label", ""), item.get("evidence", ""))).lower()
            for page in item.get("source_pages", [])
        }
        if pages:
            matched["source_pages"] = sorted(pages)
    return result
