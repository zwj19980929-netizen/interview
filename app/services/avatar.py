from typing import Any, Dict, Optional

from app.model_gateway.gateway import ModelGateway
from app.model_gateway import capabilities as cap
from app.model_gateway.schemas import AvatarSpeakRequest
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.interviews import InterviewService


class AvatarService:
    def __init__(self, store: InMemoryStore, *, persistence: Optional[Persistence] = None) -> None:
        self.persistence = persistence or persistence_for(store)
        self.interviews = InterviewService(store, persistence=self.persistence)
        self.gateway = ModelGateway(store, persistence=self.persistence)

    async def speak(self, interview_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        context = self.interviews.active_turn_context(interview_id, payload.get("turn_id"))
        interview = context["interview"]
        turn = context["turn"]
        turn_id = turn["id"]

        response = await self.gateway.invoke(
            cap.AVATAR_SPEAK,
            AvatarSpeakRequest(
                organization_id=interview.get("organization_id", "org_default"),
                text=turn["question_spoken_text"],
                avatar_id=interview.get("settings", {}).get("avatar_id", "avatar_default_cn"),
                voice=payload.get("voice", "default"),
                language=payload.get("language", "zh-CN"),
                metadata={"interview_id": interview_id, "turn_id": turn_id},
            )
        )
        return response.model_dump()
