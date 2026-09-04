import inspect
from functools import wraps
from typing import Any, Dict, Type

from app.core.auth import current_principal
from app.repositories.provider import get_store
from app.services.appointments import AppointmentService
from app.services.agent_ticket import InterviewAgentTicketService
from app.services.avatar import AvatarService
from app.services.catalog import CatalogService
from app.services.fairness import FairnessEvaluationService
from app.services.interviews import InterviewService
from app.services.interview_agent import InterviewAgentRuntime
from app.services.media_capture import InterviewMediaCaptureService
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
    "agent_runtime": InterviewAgentRuntime,
    "avatar": AvatarService,
    "reports": ReportService,
    "appointments": AppointmentService,
    "agent_tickets": InterviewAgentTicketService,
    "media_captures": InterviewMediaCaptureService,
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
            self._instances[name] = _OrganizationBoundService(factory(self._store))
        return self._instances[name]


class _OrganizationBoundService:
    """Bind every HTTP service call to the authenticated deployment tenant.

    Service APIs retain explicit ``organization_id`` parameters for workers
    and tests.  Transport adapters no longer silently fall back to
    ``org_default`` when a production deployment is configured for another
    tenant.
    """

    def __init__(self, service: Any) -> None:
        self._service = service

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._service, name)
        if not callable(target):
            return target
        try:
            signature = inspect.signature(target)
        except (TypeError, ValueError):
            return target
        if "organization_id" not in signature.parameters:
            return target

        @wraps(target)
        def call(*args: Any, **kwargs: Any) -> Any:
            bound = signature.bind_partial(*args, **kwargs)
            if "organization_id" not in bound.arguments:
                kwargs["organization_id"] = current_principal().organization_id
            return target(*args, **kwargs)

        return call


def services() -> ServiceLocator:
    return ServiceLocator(get_store())
