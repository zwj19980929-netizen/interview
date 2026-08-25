from typing import Any, Dict, List, Optional, Union

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
    provider_config_id: str
    capability: str
    purpose: str
    model: str
    timeout_s: float
    attempt: int
    fallback_index: int
    config: Dict[str, Any] = Field(default_factory=dict)
    credentials: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


InvocationRequest = Union[ChatJSONRequest, TextEmbeddingRequest, AvatarSpeakRequest]
InvocationResponse = Union[ChatJSONResponse, TextEmbeddingResponse, AvatarSpeakResponse]
