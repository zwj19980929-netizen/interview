from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


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
    purpose: str = "candidate_answer_transcription"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class StreamingSTTEvent(BaseModel):
    stream_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    type: Literal[
        "stream.ready",
        "transcript.partial",
        "transcript.final",
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


class BatchSTTRequest(BaseModel):
    organization_id: str = "org_default"
    purpose: str = "candidate_answer_repair"
    audio_uri: str = Field(min_length=1)
    content_type: str = "audio/webm;codecs=opus"
    language: str = "zh-CN"
    enable_word_timestamps: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)


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


class TTSSynthesizeResponse(BaseModel):
    audio_uri: str = Field(min_length=1)
    content_type: str
    duration_ms: int = Field(gt=0)
    content_hash: str = Field(min_length=1)
    provider: ProviderMeta


class AvatarSpeakRequest(BaseModel):
    organization_id: str = "org_default"
    purpose: str = "interview_question_delivery"
    text: str = Field(min_length=1)
    avatar_id: str = "avatar_default_cn"
    voice: str = "default"
    language: str = "zh-CN"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AvatarSpeakResponse(BaseModel):
    speech_id: str
    status: str = "ready"
    mode: str
    text: str
    stream_url: Optional[str] = None
    audio_uri: Optional[str] = None
    visemes: List[Dict[str, Any]] = Field(default_factory=list)
    provider: ProviderMeta


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
