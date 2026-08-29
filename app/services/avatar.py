from typing import Any, Dict, Optional

from app.model_gateway.gateway import ModelGateway
from app.model_gateway import capabilities as cap
from app.model_gateway.schemas import AvatarSpeakRequest
from app.model_gateway.errors import ProviderError
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
        speech_asset_id = turn.get("question_snapshot", {}).get("speech_asset_id")
        avatar_error: Optional[ProviderError] = None
        organization_id = interview.get("organization_id", "org_default")
        if self._has_real_avatar_route(organization_id):
            try:
                route = self._avatar_route(organization_id)
                response = await self._invoke_avatar(
                    interview,
                    turn,
                    payload,
                    route={**route, "fallbacks": []} if route is not None else None,
                )
                result = response.model_dump()
                result["speech_asset_id"] = speech_asset_id
                return result
            except ProviderError as exc:
                # A frozen, private TTS asset is the controlled degradation path
                # when the live avatar/SFU is unavailable.
                avatar_error = exc
        if speech_asset_id:
            with self.persistence.transaction(interview.get("organization_id", "org_default")) as transaction:
                asset = transaction.question_speech_assets.get(speech_asset_id)
            if asset and asset.get("status") == "ready" and not asset["audio_uri"].startswith("mock-tts://"):
                return {
                    "speech_id": asset["id"],
                    "status": "ready",
                    "mode": "audio",
                    "text": turn["question_spoken_text"],
                    "stream_url": None,
                    "audio_uri": asset["audio_uri"],
                    "visemes": [],
                    "speech_asset_id": asset["id"],
                    "provider": asset["provider"],
                }

        if avatar_error is not None:
            raise avatar_error

        response = await self._invoke_avatar(interview, turn, payload)
        result = response.model_dump()
        result["speech_asset_id"] = speech_asset_id
        return result

    async def close(self, interview_id: str, session_id: str) -> Dict[str, Any]:
        interview = self.interviews.get_interview(interview_id)
        organization_id = interview.get("organization_id", "org_default")
        route = self._avatar_route(organization_id)
        if route is not None:
            route = {**route, "fallbacks": []}
        response = await self.gateway.invoke(
            cap.AVATAR_SPEAK,
            AvatarSpeakRequest(
                organization_id=organization_id,
                purpose="interview_question_delivery",
                text="close avatar session",
                avatar_id=interview.get("settings", {}).get("avatar_id", "avatar_default_cn"),
                operation="close",
                session_id=session_id,
                metadata={"interview_id": interview_id},
            ),
            route=route,
        )
        return response.model_dump()

    async def _invoke_avatar(
        self,
        interview: Dict[str, Any],
        turn: Dict[str, Any],
        payload: Dict[str, Any],
        *,
        route: Optional[Dict[str, Any]] = None,
    ) -> Any:
        return await self.gateway.invoke(
            cap.AVATAR_SPEAK,
            AvatarSpeakRequest(
                organization_id=interview.get("organization_id", "org_default"),
                text=turn["question_spoken_text"],
                avatar_id=interview.get("settings", {}).get("avatar_id", "avatar_default_cn"),
                voice=payload.get("voice", "default"),
                language=payload.get("language", "zh-CN"),
                metadata={"interview_id": interview["id"], "turn_id": turn["id"]},
            ),
            route=route,
        )

    def _has_real_avatar_route(self, organization_id: str) -> bool:
        route = self._avatar_route(organization_id)
        if route is None:
            return False
        with self.persistence.transaction(organization_id) as transaction:
            model_id = str((route.get("primary") or {}).get("model_configuration_id") or "")
            model = transaction.model_configurations.get(model_id)
            connection = transaction.provider_connections.get(model.get("provider_connection_id")) if model else None
        return bool(model and connection and connection.get("provider_id") not in {"", "mock"})

    def _avatar_route(self, organization_id: str) -> Optional[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return next((
                route for route in transaction.model_routes.list()
                if route.get("enabled", True)
                and route.get("capability") == cap.AVATAR_SPEAK
                and route.get("purpose") == "interview_question_delivery"
            ), None)
