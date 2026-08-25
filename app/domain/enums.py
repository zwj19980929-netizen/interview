from enum import Enum


class QuestionStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class IndexStatus(str, Enum):
    PENDING = "pending"
    INDEXED = "indexed"
    FAILED = "failed"


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
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    SKIPPED = "skipped"
