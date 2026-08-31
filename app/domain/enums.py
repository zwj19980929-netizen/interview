from enum import Enum


class QuestionStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class IndexStatus(str, Enum):
    PENDING = "pending"
    INDEXED = "indexed"
    FAILED = "failed"


class BuildStatus(str, Enum):
    DRAFT = "draft"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"
    ARCHIVED = "archived"


class ValidationStatus(str, Enum):
    PENDING = "pending"
    VALID = "valid"
    INVALID = "invalid"


class SpeechStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class ResumeReviewStatus(str, Enum):
    QUEUED = "queued"
    REVIEWING = "reviewing"
    READY_FOR_REVIEW = "ready_for_review"
    FAILED = "failed"


class ExperienceQuestionStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class AppointmentStatus(str, Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    INVITED = "invited"
    REGISTERED = "registered"
    CONSUMED = "consumed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class InterviewStatus(str, Enum):
    SCHEDULED = "scheduled"
    WAITING = "waiting"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    COMPLETED = "completed"
    REPORT_GENERATING = "report_generating"
    REPORT_READY = "report_ready"
    REPORT_FAILED = "report_failed"
    CANCELLED = "cancelled"


class TurnStatus(str, Enum):
    PENDING = "pending"
    ASKING = "asking"
    ANSWERING = "answering"
    TRANSCRIBING = "transcribing"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    SKIPPED = "skipped"
