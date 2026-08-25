from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


class KeyPointInput(BaseModel):
    text: str
    weight: float = 1.0
    aliases: List[str] = Field(default_factory=list)


class QuestionCreate(BaseModel):
    knowledge_base_id: str = "kb_default"
    title: str
    question_text: str
    standard_answer: str
    key_points: List[Union[str, KeyPointInput]]
    difficulty: str = "mid"
    type: str = "open_ended"
    skills: List[str] = Field(default_factory=list)
    role_families: List[str] = Field(default_factory=list)
    rubric: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("key_points")
    @classmethod
    def require_key_points(cls, value: List[Union[str, KeyPointInput]]) -> List[Union[str, KeyPointInput]]:
        if not value:
            raise ValueError("at least one key point is required")
        return value


class QuestionSearchRequest(BaseModel):
    query: str
    filters: Dict[str, Any] = Field(default_factory=dict)
    limit: int = 20
    include_answer: bool = False


class RoleRequirementCreate(BaseModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    must_have_skills: List[str] = Field(default_factory=list)
    nice_to_have_skills: List[str] = Field(default_factory=list)
    seniority: str = "mid"
    interview_duration_minutes: int = Field(default=45, ge=5, le=480)


class InterviewPlanStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage: List[str] = Field(default_factory=list)
    allow_followups: bool = True
    max_same_skill_questions: int = Field(default=3, ge=1, le=50)
    difficulty_curve: bool = True
    deduplication_threshold: float = Field(default=0.72, ge=0.0, le=1.0)


class InterviewPlanGenerateRequest(BaseModel):
    role_requirement_id: str
    knowledge_base_ids: List[str] = Field(default_factory=list)
    question_count: int = Field(default=8, ge=1, le=50)
    strategy: InterviewPlanStrategy = Field(default_factory=InterviewPlanStrategy)


class InterviewPlanPatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    expected_version: int
    status: Optional[str] = None
    items: Optional[List[Dict[str, Any]]] = None


class CandidateCreate(BaseModel):
    name: str
    email: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class InterviewCreate(BaseModel):
    plan_id: str
    candidate: CandidateCreate
    scheduled_at: Optional[str] = None
    settings: Dict[str, Any] = Field(default_factory=dict)


class InterviewControlCommand(BaseModel):
    reason: str = "manual"


class AnswerSubmit(BaseModel):
    turn_id: Optional[str] = None
    question_id: Optional[str] = None
    final_transcript: str
    raw_transcript: Optional[str] = None
    stt_confidence: float = 1.0
    language: str = "zh-CN"
    duration_seconds: int = 0
    audio_uri: Optional[str] = None


class AvatarSpeakCommand(BaseModel):
    turn_id: Optional[str] = None
    language: str = "zh-CN"
    voice: str = "default"


class ProviderConfigCreate(BaseModel):
    provider_id: str
    display_name: str
    enabled: bool = True
    config: Dict[str, Any] = Field(default_factory=dict)
    credentials: Dict[str, Any] = Field(default_factory=dict)


class ProviderConfigPatch(BaseModel):
    expected_version: int
    display_name: Optional[str] = None
    enabled: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None
    credentials: Optional[Dict[str, Any]] = None


class ModelRouteCreate(BaseModel):
    capability: str
    purpose: str = "default"
    primary: Dict[str, Any]
    fallbacks: List[Dict[str, Any]] = Field(default_factory=list)
    policy: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
