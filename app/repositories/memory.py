from typing import Any, Dict, List

from app.core.time import utc_now


class InMemoryStore:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.questions: Dict[str, Dict[str, Any]] = {}
        self.job_positions: Dict[str, Dict[str, Any]] = {}
        self.knowledge_bases: Dict[str, Dict[str, Any]] = {}
        self.question_speech_assets: Dict[str, Dict[str, Any]] = {}
        self.role_requirements: Dict[str, Dict[str, Any]] = {}
        self.interview_plans: Dict[str, Dict[str, Any]] = {}
        self.candidate_profiles: Dict[str, Dict[str, Any]] = {}
        self.resume_documents: Dict[str, Dict[str, Any]] = {}
        self.file_objects: Dict[str, Dict[str, Any]] = {}
        self.audit_events: Dict[str, Dict[str, Any]] = {}
        self.resume_reviews: Dict[str, Dict[str, Any]] = {}
        self.experience_questions: Dict[str, Dict[str, Any]] = {}
        self.interview_appointments: Dict[str, Dict[str, Any]] = {}
        self.candidate_intakes: Dict[str, Dict[str, Any]] = {}
        self.candidates: Dict[str, Dict[str, Any]] = {}
        self.interviews: Dict[str, Dict[str, Any]] = {}
        self.turns: Dict[str, Dict[str, Any]] = {}
        self.answers: Dict[str, Dict[str, Any]] = {}
        self.evaluations: Dict[str, Dict[str, Any]] = {}
        self.reports: Dict[str, Dict[str, Any]] = {}
        self.outbox_work_items: Dict[str, Dict[str, Any]] = {}
        self.provider_secrets: Dict[str, Dict[str, Any]] = {}
        self.provider_configs: Dict[str, Dict[str, Any]] = {
            "mpc_mock": {
                "id": "mpc_mock",
                "organization_id": "org_default",
                "provider_id": "mock",
                "display_name": "Mock Provider",
                "enabled": True,
                "config": {},
                "credential_ref": "secret://mock",
                "version": 1,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
        }
        self.model_routes: Dict[str, Dict[str, Any]] = {}
        self.model_circuit_states: Dict[str, Dict[str, Any]] = {}
        self.model_invocations: List[Dict[str, Any]] = []
        self._after_reset()

    def _after_reset(self) -> None:
        return None

    def save_item(self, collection: str, item_id: str, item: Dict[str, Any]) -> None:
        return None

    def save_many(self, collection: str, items: List[Dict[str, Any]]) -> None:
        return None

    def save_provider_secret(self, config_id: str, credentials: Dict[str, Any]) -> None:
        self.provider_secrets[config_id] = credentials

    def get_provider_secret(self, config_id: str) -> Dict[str, Any]:
        return self.provider_secrets.get(config_id, {})

    def add_model_invocation(self, item: Dict[str, Any]) -> None:
        self.model_invocations.append(item)


store = InMemoryStore()


def get_store() -> InMemoryStore:
    return store
