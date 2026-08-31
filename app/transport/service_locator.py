from typing import Any, Dict, Type

from app.repositories.provider import get_store
from app.services.appointments import AppointmentService
from app.services.avatar import AvatarService
from app.services.catalog import CatalogService
from app.services.fairness import FairnessEvaluationService
from app.services.interviews import InterviewService
from app.services.knowledge_base_speech import KnowledgeBaseSpeechService
from app.services.model_admin import ModelAdminService
from app.services.operations import OperationsService
from app.services.plan_assembly import InterviewPlanAssembly
from app.services.plans import InterviewPlanService
from app.services.question_generation import QuestionGenerationService
from app.services.reports import ReportService
from app.services.resume_ingestion import ResumeIngestionService
from app.services.retention import RetentionService
from app.services.review import EnterpriseReviewService
from app.services.roles import RoleRequirementService
from app.services.session_monitor import SessionHeartbeatMonitor
from app.services.talent import TalentService


_SERVICE_FACTORIES: Dict[str, Type[Any]] = {
    "model_admin": ModelAdminService,
    "catalog": CatalogService,
    "knowledge_base_speech": KnowledgeBaseSpeechService,
    "question_generation": QuestionGenerationService,
    "talent": TalentService,
    "roles": RoleRequirementService,
    "plan_assembly": InterviewPlanAssembly,
    "plans": InterviewPlanService,
    "interviews": InterviewService,
    "avatar": AvatarService,
    "reports": ReportService,
    "appointments": AppointmentService,
    "review": EnterpriseReviewService,
    "resume_ingestion": ResumeIngestionService,
    "operations": OperationsService,
    "fairness": FairnessEvaluationService,
    "session_monitor": SessionHeartbeatMonitor,
    "retention": RetentionService,
}


class ServiceLocator:
    """Construct only the deep module requested by a transport adapter."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._instances: Dict[str, Any] = {}

    def __getitem__(self, name: str) -> Any:
        factory = _SERVICE_FACTORIES.get(name)
        if factory is None:
            raise KeyError(name)
        if name not in self._instances:
            self._instances[name] = factory(self._store)
        return self._instances[name]


def services() -> ServiceLocator:
    return ServiceLocator(get_store())
