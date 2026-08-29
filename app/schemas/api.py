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
    job_position_id: str = Field(min_length=1)
    knowledge_base_ids: List[str] = Field(min_length=1)
    query: str = ""
    filters: Dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=20, ge=1, le=200)
    include_answer: bool = False


class InitialRoleRequirementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    must_have_skills: List[str] = Field(default_factory=list)
    nice_to_have_skills: List[str] = Field(default_factory=list)
    seniority: str = "mid"
    interview_duration_minutes: int = Field(default=45, ge=5, le=480)


class JobPositionCreate(BaseModel):
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    status: str = "active"
    initial_requirement: Optional[InitialRoleRequirementCreate] = None


class JobPositionDeleteCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    confirmation: str = Field(min_length=1)


class VersionedPatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    expected_version: int = Field(ge=1)


class WebSocketTicketCreate(BaseModel):
    interview_id: str = Field(min_length=1)


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    language: str = "zh-CN"
    voice_profile_id: str = "voice_default_cn"
    positioning: str = ""
    tags: List[str] = Field(default_factory=list, max_length=30)


class KnowledgeBaseAssignmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_base_id: str = Field(min_length=1)
    expected_position_version: int = Field(ge=1)


class KnowledgeBaseSpeechProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    model_configuration_id: str = Field(min_length=1)
    voice_profile_id: str = Field(min_length=1)
    language: str = Field(default="zh-CN", min_length=2)
    audio_format: str = Field(default="audio/wav", pattern=r"^audio/")
    speaking_rate: float = Field(default=1.0, ge=0.5, le=2.0)


class KnowledgeBaseSpeechRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class QuestionPatch(VersionedPatch):
    title: Optional[str] = None
    question_text: Optional[str] = None
    standard_answer: Optional[str] = None
    key_points: Optional[List[Union[str, KeyPointInput]]] = None
    difficulty: Optional[str] = None
    type: Optional[str] = None
    skills: Optional[List[str]] = None
    role_families: Optional[List[str]] = None
    rubric: Optional[Dict[str, Any]] = None
    status: Optional[str] = None


class QuestionGenerationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_configuration_id: str = Field(min_length=1)
    target_count: int = Field(default=10, ge=1, le=30)
    positioning: str = Field(default="", max_length=2000)
    tags: List[str] = Field(default_factory=list, max_length=30)
    requirements: str = Field(default="", max_length=4000)


class GeneratedQuestionDraftPatch(VersionedPatch):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    question_text: Optional[str] = Field(default=None, min_length=1, max_length=4000)
    standard_answer: Optional[str] = Field(default=None, min_length=1, max_length=8000)
    key_points: Optional[List[Union[str, KeyPointInput]]] = None
    difficulty: Optional[str] = None
    type: Optional[str] = None
    skills: Optional[List[str]] = None
    rubric: Optional[Dict[str, Any]] = None


class QuestionGenerationImport(VersionedPatch):
    model_config = ConfigDict(extra="forbid")


class GeneratedQuestionDraftImport(VersionedPatch):
    model_config = ConfigDict(extra="forbid")

    expected_draft_version: Optional[int] = Field(default=None, ge=1)


class QuestionGenerationControl(VersionedPatch):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="manual", min_length=1, max_length=500)


class KnowledgeBaseImport(BaseModel):
    questions: List[QuestionCreate] = Field(min_length=1, max_length=500)


class KnowledgeBaseRebuild(BaseModel):
    reason: str = "manual_rebuild"


class RoleRequirementCreate(BaseModel):
    job_position_id: Optional[str] = None
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
    model_config = ConfigDict(extra="forbid")

    role_requirement_id: str
    job_position_id: str = Field(min_length=1)
    candidate_profile_id: str = Field(min_length=1)
    knowledge_base_ids: List[str] = Field(min_length=1)
    question_count: int = Field(default=8, ge=1, le=50)
    strategy: InterviewPlanStrategy = Field(default_factory=InterviewPlanStrategy)
    resume_review_id: Optional[str] = None
    approve: bool = False


class InterviewPlanPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    status: Optional[str] = None
    bank_slots: Optional[List[Dict[str, Any]]] = None
    experience_question_ids: Optional[List[str]] = None
    selection_policy: Optional[Dict[str, Any]] = None


class CandidateProfileCreate(BaseModel):
    name: str = Field(min_length=1)
    email: str = Field(min_length=3)
    phone: str = Field(min_length=7)
    job_position_id: Optional[str] = None
    external_ref: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    retention_expires_at: Optional[str] = None


class CandidateProfilePatch(VersionedPatch):
    name: Optional[str] = Field(default=None, min_length=1)
    email: Optional[str] = Field(default=None, min_length=3)
    phone: Optional[str] = Field(default=None, min_length=7)
    job_position_id: Optional[str] = None
    external_ref: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    retention_expires_at: Optional[str] = None


class RetentionRun(BaseModel):
    dry_run: bool = True
    now: Optional[str] = None


class ResumeUrlImport(BaseModel):
    url: str = Field(min_length=8)
    display_name: str = "resume.pdf"
    job_position_id: Optional[str] = None
    role_requirement_id: Optional[str] = None


class ResumeDocumentPatch(VersionedPatch):
    display_name: str = Field(min_length=1, max_length=255)


class ResumeReviewCreate(BaseModel):
    resume_document_id: str
    job_position_id: str
    role_requirement_id: str


class ResumeReviewRetry(VersionedPatch):
    reason: str = Field(default="interviewer_requested_retry", min_length=1, max_length=500)


class CandidateScreeningReview(VersionedPatch):
    decision: str = Field(pattern="^(qualified|unqualified)$")
    note: str = Field(default="", max_length=2000)


class ExperienceQuestionPatch(VersionedPatch):
    question_text: Optional[str] = None
    standard_answer: Optional[str] = None
    key_points: Optional[List[Dict[str, Any]]] = None
    rubric: Optional[Dict[str, Any]] = None
    status: Optional[str] = None


class InterviewControlCommand(BaseModel):
    reason: str = "manual"


class AudioAnswerSubmit(BaseModel):
    turn_id: Optional[str] = None
    audio_uri: str = Field(min_length=1)
    content_type: str = "audio/webm;codecs=opus"
    language: str = "zh-CN"
    duration_seconds: int = Field(default=0, ge=0)
    development_transcript: Optional[str] = None
    development_confidence: float = Field(default=0.9, ge=0.0, le=1.0)


class InterviewAppointmentCreate(BaseModel):
    plan_id: str
    candidate_profile_id: str
    job_position_id: str
    scheduled_start_at: str
    scheduled_end_at: str
    settings: Dict[str, Any] = Field(default_factory=dict)
    admission_policy: Dict[str, Any] = Field(default_factory=dict)


class InterviewAppointmentPatch(VersionedPatch):
    scheduled_start_at: Optional[str] = None
    scheduled_end_at: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None
    admission_policy: Optional[Dict[str, Any]] = None


class InterviewInvitationCreate(BaseModel):
    expires_at: str


class CandidateConsentCreate(BaseModel):
    accepted: bool
    version: str = Field(min_length=1)
    recording_accepted: bool = False


class CandidateIntakeCreate(BaseModel):
    name: str = Field(min_length=1)
    email: str = Field(min_length=3)
    phone: str = Field(min_length=7)
    consent: CandidateConsentCreate


class CandidateReadinessCreate(BaseModel):
    browser_supported: bool
    microphone_granted: bool
    audio_content_type: str = Field(min_length=1)


class TranscriptCorrection(BaseModel):
    final_transcript: str = Field(min_length=1)
    reason: str = "enterprise_review"
    reviewer_id: str = "reviewer_local"


class ReviewComplete(BaseModel):
    reviewer_id: str = "reviewer_local"
    notes: str = ""


class OutboxReplay(BaseModel):
    reason: str = Field(min_length=1)


class ScoreCalibrationLabel(BaseModel):
    """A human gold label linked to an existing evaluation, never raw candidate data."""

    model_config = ConfigDict(extra="forbid")

    evaluation_id: str = Field(min_length=1, max_length=128)
    human_score: float = Field(ge=0, le=100)
    fairness_cohort: Optional[str] = Field(
        default=None, pattern=r"^cohort_[a-z0-9_]{1,32}$"
    )


class ScoreCalibrationRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    labels: List[ScoreCalibrationLabel] = Field(min_length=2, max_length=5000)
    dataset_version: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


class AvatarSpeakCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: Optional[str] = None
    language: str = "zh-CN"
    voice: str = "default"


class AvatarSessionClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=256)


class ProviderConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    display_name: str
    enabled: bool = True
    connection_config: Dict[str, Any] = Field(default_factory=dict)
    credentials: Dict[str, Any] = Field(default_factory=dict)


class ProviderConnectionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    display_name: Optional[str] = None
    enabled: Optional[bool] = None
    connection_config: Optional[Dict[str, Any]] = None
    credentials: Optional[Dict[str, Any]] = None


class ModelConfigurationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_connection_id: str = Field(min_length=1)
    model_type: str = Field(min_length=1)
    provider_model_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    settings: Dict[str, Any] = Field(default_factory=dict)
    default_parameters: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ModelConfigurationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    display_name: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None
    default_parameters: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None


class ModelConfigurationTest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: Optional[str] = Field(default=None, min_length=1)


class ModelRouteTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_configuration_id: str = Field(min_length=1)
    timeout_s: float = Field(default=20, gt=0, le=300)
    pricing: Dict[str, float] = Field(default_factory=dict)


class ModelRoutePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retry_count: int = Field(default=0, ge=0, le=3)
    retry_backoff_ms: int = Field(default=0, ge=0, le=1000)
    fallback_on: Optional[List[str]] = None
    max_cost_usd_per_call: Optional[float] = Field(default=None, ge=0)
    circuit_failure_threshold: int = Field(default=3, ge=0, le=100)
    circuit_recovery_seconds: float = Field(default=30, ge=0, le=86400)
    readiness_ttl_seconds: int = Field(default=60, ge=1, le=86400)


class ModelRouteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str = Field(min_length=1)
    purpose: str = Field(default="default", min_length=1)
    primary: ModelRouteTarget
    fallbacks: List[ModelRouteTarget] = Field(default_factory=list)
    policy: ModelRoutePolicy = Field(default_factory=ModelRoutePolicy)
    enabled: bool = True
