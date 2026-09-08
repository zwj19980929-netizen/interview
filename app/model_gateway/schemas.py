import math
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator


class ProviderMeta(BaseModel):
    provider_id: str
    model: str
    request_id: str
    latency_ms: int


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatJSONRequest(BaseModel):
    organization_id: str = "org_default"
    purpose: str = Field(min_length=1)
    messages: List[ChatMessage] = Field(min_length=1)
    json_schema: Dict[str, Any] = Field(default_factory=dict)
    temperature: float = 0.1
    max_output_tokens: int = 1200
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ChatJSONResponse(BaseModel):
    data: Dict[str, Any]
    usage: Usage
    provider: ProviderMeta


class ChatTextRequest(BaseModel):
    organization_id: str = "org_default"
    purpose: str = Field(min_length=1)
    messages: List[ChatMessage] = Field(min_length=1)
    temperature: float = 0.1
    max_output_tokens: int = 1200
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ChatTextResponse(BaseModel):
    text: str = Field(min_length=1)
    usage: Usage
    provider: ProviderMeta


class TextEmbeddingRequest(BaseModel):
    organization_id: str = "org_default"
    purpose: str = Field(min_length=1)
    texts: List[str] = Field(min_length=1)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TextEmbeddingResponse(BaseModel):
    vectors: List[List[float]]
    dimensions: int
    provider: ProviderMeta


class TranscriptEvent(BaseModel):
    type: str
    text: str
    language: str = "zh-CN"
    confidence: float = 1.0
    start_ms: int = 0
    end_ms: int = 0
    provider: Optional[ProviderMeta] = None


class TranscriptSegment(BaseModel):
    text: str
    start_ms: int = 0
    end_ms: int = 0
    confidence: float = 1.0


class StreamingAudioConfig(BaseModel):
    content_type: str = "audio/webm;codecs=opus"
    sample_rate_hz: int = Field(default=48000, ge=8000, le=192000)
    channels: int = Field(default=1, ge=1, le=2)


class StreamingSTTRequest(BaseModel):
    organization_id: str = "org_default"
    interview_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    audio: StreamingAudioConfig = Field(default_factory=StreamingAudioConfig)
    language: str = "zh-CN"
    enable_partial: bool = True
    enable_word_timestamps: bool = True
    recognition_terms: List[str] = Field(default_factory=list, max_length=100)
    purpose: str = "candidate_answer_transcription"
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("recognition_terms")
    @classmethod
    def validate_recognition_terms(cls, terms: List[str]) -> List[str]:
        import re
        if (len({term.casefold() for term in terms}) != len(terms)
                or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+.#-]{1,63}", term) for term in terms)):
            raise ValueError("Recognition terms must be unique bounded technical identifiers")
        return terms


class StreamingSTTEvent(BaseModel):
    stream_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    type: Literal[
        "stream.ready",
        "transcript.partial",
        "transcript.final",
        "transcript.empty",
        "stream.error",
        "stream.closed",
    ]
    text: str = ""
    language: str = "zh-CN"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    segments: List[TranscriptSegment] = Field(default_factory=list)
    is_final: bool = False
    error_code: Optional[str] = None
    provider: Optional[ProviderMeta] = None

    @model_validator(mode="before")
    @classmethod
    def validate_empty_completion(cls, value: Any) -> Any:
        # This optional completion is deliberately narrower than the legacy
        # event contract. It certifies completed recognition, never an answer.
        if not isinstance(value, dict) or value.get("type") != "transcript.empty":
            return value
        if (set(value) - set(cls.model_fields) or value.get("is_final") is not True
                or value.get("text", "") != "" or value.get("segments", []) != []
                or value.get("error_code") is not None):
            raise ValueError("Empty STT completion cannot contain transcript or error data.")
        provider = value.get("provider")
        if isinstance(provider, ProviderMeta):
            provider = provider.model_dump(warnings=False)
        if not isinstance(provider, dict) or set(provider) - set(ProviderMeta.model_fields):
            raise ValueError("Empty STT completion requires provider provenance.")
        meta = ProviderMeta.model_validate(provider, strict=True)
        if (not meta.provider_id.strip() or not meta.model.strip() or not meta.request_id.strip()
                or meta.latency_ms < 0):
            raise ValueError("Empty STT completion requires valid provider provenance.")
        return value


class StableTranscriptPreview(BaseModel):
    """Read-only, stable sentence prefix; deliberately not a final STT event."""

    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")

    stream_id: str = Field(min_length=1, max_length=512)
    revision: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=100_000)
    language: str = Field(min_length=1, max_length=128)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    segments: List[TranscriptSegment] = Field(min_length=1, max_length=10_000)
    provider: ProviderMeta
    has_unstable_tail: bool

    @model_validator(mode="before")
    @classmethod
    def validate_nested_contract(cls, value: Any) -> Any:
        # The shared legacy segment/meta models are intentionally unchanged.
        # Revalidate their values here, including objects made with model_copy
        # or model_construct, so this stricter optional contract cannot bypass
        # validation through already-instantiated, mutable nested models.
        if isinstance(value, cls):
            value = value.model_dump(warnings=False)
        if not isinstance(value, dict):
            return value
        data = dict(value)
        provider = data.get("provider")
        if isinstance(provider, ProviderMeta):
            provider = provider.model_dump(warnings=False)
        if isinstance(provider, dict):
            if set(provider) - set(ProviderMeta.model_fields):
                raise ValueError("Unexpected stable preview provider field.")
            data["provider"] = ProviderMeta.model_validate(provider, strict=True)
        segments = data.get("segments")
        if isinstance(segments, list):
            checked = []
            characters = 0
            for segment in segments:
                if isinstance(segment, TranscriptSegment):
                    segment = segment.model_dump(warnings=False)
                if isinstance(segment, dict) and set(segment) - set(TranscriptSegment.model_fields):
                    raise ValueError("Unexpected stable preview segment field.")
                item = TranscriptSegment.model_validate(segment, strict=True)
                characters += len(item.text)
                if characters > 100_000:
                    raise ValueError("Stable preview exceeds the transcript limit.")
                checked.append(item)
            data["segments"] = checked
        return data

    @model_validator(mode="after")
    def validate_stable_content(self) -> "StableTranscriptPreview":
        if not self.stream_id.strip() or not self.language.strip() or not self.text.strip():
            raise ValueError("Stable preview identifiers and content must be nonempty.")
        if not self.provider.provider_id.strip() or not self.provider.model.strip() or self.provider.latency_ms < 0:
            raise ValueError("Stable preview requires valid provider provenance.")
        previous_end = 0
        for segment in self.segments:
            if (
                not segment.text.strip()
                or segment.start_ms < previous_end
                or segment.end_ms < segment.start_ms
                or not math.isfinite(segment.confidence)
                or not 0 <= segment.confidence <= 1
            ):
                raise ValueError("Stable preview segments are invalid or out of order.")
            previous_end = segment.end_ms
        if self.text != "".join(segment.text for segment in self.segments):
            raise ValueError("Stable preview text does not match its segments.")
        return self


class RealtimeSpeechDialogueRequest(BaseModel):
    organization_id: str = "org_default"
    interview_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    purpose: str = "candidate_followup_dialogue"
    input_audio: StreamingAudioConfig = Field(
        default_factory=lambda: StreamingAudioConfig(
            content_type="audio/pcm", sample_rate_hz=16000, channels=1
        )
    )
    output_audio: StreamingAudioConfig = Field(
        default_factory=lambda: StreamingAudioConfig(
            content_type="audio/pcm", sample_rate_hz=24000, channels=1
        )
    )
    language: str = "zh-CN"
    voice: str = "default"
    turn_detection: Literal["manual", "server_vad", "semantic_vad", "smart_turn"] = "manual"
    session_instructions: str = Field(min_length=1, max_length=8000)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RealtimeSpeechResponseCommand(BaseModel):
    spoken_text: str = Field(min_length=1, max_length=1000)
    response_instructions: str = Field(min_length=1, max_length=4000)


class RealtimeSpeechDialogueEvent(BaseModel):
    stream_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    type: Literal[
        "dialogue.ready",
        "input.speech.started",
        "input.speech.stopped",
        "input.transcript.partial",
        "input.transcript.final",
        "output.transcript.delta",
        "output.transcript.final",
        "output.audio.delta",
        "output.audio.done",
        "output.interrupted",
        "dialogue.error",
        "dialogue.closed",
    ]
    text: str = ""
    audio_base64: str = ""
    audio_content_type: str = "audio/pcm"
    sample_rate_hz: int = Field(default=24000, ge=8000, le=192000)
    is_final: bool = False
    error_code: Optional[str] = None
    provider: Optional[ProviderMeta] = None


class BatchSTTRequest(BaseModel):
    organization_id: str = "org_default"
    purpose: str = "candidate_answer_repair"
    audio_uri: str = Field(min_length=1)
    content_type: str = "audio/webm;codecs=opus"
    language: str = "zh-CN"
    enable_word_timestamps: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # Server-resolved private media. Excluded from dumps so logs and invocation
    # hashes never serialize raw candidate audio.
    audio_bytes: bytes = Field(default=b"", exclude=True, repr=False)


class BatchSTTResponse(BaseModel):
    text: str
    language: str = "zh-CN"
    confidence: float = 1.0
    segments: List[TranscriptSegment] = Field(default_factory=list)
    source: str = "server_batch"
    provider: ProviderMeta


class TTSSynthesizeRequest(BaseModel):
    organization_id: str = "org_default"
    purpose: str = "question_speech_generation"
    text: str = Field(min_length=1)
    language: str = "zh-CN"
    voice_profile_id: str = "voice_default_cn"
    format: str = "audio/wav"
    speaking_rate: float = Field(default=1.0, ge=0.5, le=2.0)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TTSVisemeCue(BaseModel):
    """Provider timing contract before cues enter the avatar domain."""

    model_config = ConfigDict(extra="forbid")

    at_ms: int = Field(ge=0)
    duration_ms: int = Field(gt=0, le=10_000)
    shape: Literal[
        "sil", "PP", "FF", "TH", "DD", "kk", "CH", "SS",
        "nn", "RR", "aa", "E", "ih", "oh", "ou",
    ]
    weight: float = Field(ge=0, le=1)


class TTSSynthesizeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audio_uri: str = Field(min_length=1)
    content_type: str
    duration_ms: int = Field(gt=0)
    content_hash: str = Field(min_length=1)
    visemes: List[TTSVisemeCue] = Field(default_factory=list, max_length=10_000)
    alignment_source: Literal["provider_timestamp", "none"] = "none"
    provider: ProviderMeta

    @model_validator(mode="after")
    def validate_viseme_timing(self) -> "TTSSynthesizeResponse":
        if not self.visemes:
            if self.alignment_source != "none":
                raise ValueError("provider_timestamp requires non-empty visemes")
            return self
        positions = [cue.at_ms for cue in self.visemes]
        if positions != sorted(positions):
            raise ValueError("provider viseme cues must be monotonic")
        if any(cue.at_ms + cue.duration_ms > self.duration_ms for cue in self.visemes):
            raise ValueError("provider viseme cue exceeds authoritative audio duration")
        self.alignment_source = "provider_timestamp"
        return self


class AvatarSpeakRequest(BaseModel):
    organization_id: str = "org_default"
    purpose: str = "interview_question_delivery"
    text: str = Field(min_length=1)
    avatar_id: str = "avatar_default_cn"
    voice: str = "default"
    language: str = "zh-CN"
    operation: Literal["speak", "close"] = "speak"
    session_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AvatarSpeakResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speech_id: str
    status: str = "ready"
    mode: Literal["browser_speech", "audio", "video", "webrtc"]
    text: str
    stream_url: Optional[str] = None
    audio_uri: Optional[str] = None
    duration_ms: Optional[int] = Field(default=None, gt=0)
    session_id: Optional[str] = None
    player_kind: Literal["native_url", "whep", "tencent_web_player"] = "native_url"
    avatar_mode: Literal["local", "cloud"] = "cloud"
    fallback_reason: Optional[Literal["cloud_unavailable"]] = None
    visemes: List[TTSVisemeCue] = Field(default_factory=list, max_length=10_000)
    alignment_source: Literal["provider_timestamp", "none"] = "none"
    provider: ProviderMeta

    @model_validator(mode="after")
    def validate_viseme_timing(self) -> "AvatarSpeakResponse":
        if not self.visemes:
            if self.alignment_source != "none":
                raise ValueError("provider_timestamp requires non-empty visemes")
            return self
        positions = [cue.at_ms for cue in self.visemes]
        if positions != sorted(positions):
            raise ValueError("provider viseme cues must be monotonic")
        if self.duration_ms is not None and any(
            cue.at_ms + cue.duration_ms > self.duration_ms for cue in self.visemes
        ):
            raise ValueError("provider viseme cue exceeds authoritative audio duration")
        self.alignment_source = "provider_timestamp"
        return self


class ProviderContext(BaseModel):
    organization_id: str
    invocation_id: str
    route_id: str
    provider_connection_id: str
    model_configuration_id: str
    model_type: str
    capability: str
    purpose: str
    model: str
    timeout_s: float
    attempt: int
    fallback_index: int
    connection_config: Dict[str, Any] = Field(default_factory=dict)
    model_settings: Dict[str, Any] = Field(default_factory=dict)
    default_parameters: Dict[str, Any] = Field(default_factory=dict)
    credentials: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @property
    def config(self) -> Dict[str, Any]:
        """Resolved provider options for existing adapters; model settings win on collision."""
        return {**self.connection_config, **self.model_settings}


InvocationRequest = Union[
    ChatJSONRequest,
    ChatTextRequest,
    TextEmbeddingRequest,
    BatchSTTRequest,
    TTSSynthesizeRequest,
    AvatarSpeakRequest,
]
InvocationResponse = Union[
    ChatJSONResponse,
    ChatTextResponse,
    TextEmbeddingResponse,
    BatchSTTResponse,
    TTSSynthesizeResponse,
    AvatarSpeakResponse,
]
