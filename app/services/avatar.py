from typing import Any, Dict, Optional, Protocol

from app.core.errors import ApiError
from app.core.ids import new_id
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import AvatarSpeakRequest, AvatarSpeakResponse, ProviderMeta
from app.persistence.interface import Persistence
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.catalog import CatalogService
from app.services.agent_expression_audio import expression_audio_reference
from app.services.interviews import InterviewService


class AvatarDelivery(Protocol):
    """Small delivery seam shared by local and cloud avatar implementations."""

    async def speak(
        self,
        interview: Dict[str, Any],
        turn: Dict[str, Any],
        payload: Dict[str, Any],
        *,
        actor_id: str,
        fallback_reason: Optional[str] = None,
    ) -> AvatarSpeakResponse: ...


class LocalAvatarDelivery:
    """Delivers frozen TTS audio to the browser-rendered interviewer."""

    def __init__(self, catalog: CatalogService, persistence: Persistence) -> None:
        self.catalog = catalog
        self.persistence = persistence

    async def speak(
        self,
        interview: Dict[str, Any],
        turn: Dict[str, Any],
        payload: Dict[str, Any],
        *,
        actor_id: str,
        fallback_reason: Optional[str] = None,
    ) -> AvatarSpeakResponse:
        organization_id = interview.get("organization_id", "org_default")
        speech_asset_id = turn.get("question_snapshot", {}).get("speech_asset_id")
        asset: Optional[Dict[str, Any]] = None
        if speech_asset_id:
            with self.persistence.transaction(organization_id) as transaction:
                asset = transaction.question_speech_assets.get(speech_asset_id)
        if asset and asset.get("status") == "ready":
            try:
                return AvatarSpeakResponse(
                    speech_id=asset["id"],
                    mode="audio",
                    text=turn["question_spoken_text"],
                    # Persist only an opaque reference in AgentEvent history.
                    # The runtime projection mints a fresh, participant-scoped
                    # read grant for initial delivery and every reconnect.
                    audio_uri=expression_audio_reference(
                        str(asset["file_object_id"])
                    ),
                    duration_ms=(
                        int(asset["duration_ms"])
                        if asset.get("duration_ms")
                        else None
                    ),
                    visemes=list(asset.get("visemes") or []),
                    avatar_mode="local",
                    fallback_reason=fallback_reason,
                    provider=ProviderMeta.model_validate(asset["provider"]),
                )
            except ApiError as exc:
                if exc.code not in {
                    "QUESTION_SPEECH_PREVIEW_UNAVAILABLE",
                    "QUESTION_SPEECH_ASSET_NOT_PRIVATE",
                }:
                    raise

        # Development fixtures can have text-only mock TTS. The browser speech
        # fallback keeps the local avatar usable without exposing an internal URI.
        return AvatarSpeakResponse(
            speech_id=new_id("local_avatar_speech"),
            mode="browser_speech",
            text=turn["question_spoken_text"],
            avatar_mode="local",
            fallback_reason=fallback_reason,
            provider=ProviderMeta(
                provider_id="browser_local_avatar",
                model="portrait-avatar-v1",
                request_id=new_id("local_avatar_request"),
                latency_ms=0,
            ),
        )


class CloudAvatarDelivery:
    """Preserves the routed cloud-avatar/WebRTC implementation."""

    def __init__(self, gateway: ModelGateway, persistence: Persistence) -> None:
        self.gateway = gateway
        self.persistence = persistence

    async def speak(
        self,
        interview: Dict[str, Any],
        turn: Dict[str, Any],
        payload: Dict[str, Any],
        *,
        actor_id: str,
        fallback_reason: Optional[str] = None,
    ) -> AvatarSpeakResponse:
        route = self.route(interview.get("organization_id", "org_default"))
        response = await self._invoke(
            interview,
            turn,
            payload,
            route={**route, "fallbacks": []} if route is not None else None,
        )
        return response.model_copy(update={"avatar_mode": "cloud"})

    async def close(self, interview: Dict[str, Any], session_id: str) -> AvatarSpeakResponse:
        organization_id = interview.get("organization_id", "org_default")
        route = self.route(organization_id)
        return await self.gateway.invoke(
            cap.AVATAR_SPEAK,
            AvatarSpeakRequest(
                organization_id=organization_id,
                purpose="interview_question_delivery",
                text="close avatar session",
                avatar_id=interview.get("settings", {}).get("avatar_id", "avatar_default_cn"),
                operation="close",
                session_id=session_id,
                metadata={"interview_id": interview["id"]},
            ),
            route={**route, "fallbacks": []} if route is not None else None,
        )

    async def _invoke(
        self,
        interview: Dict[str, Any],
        turn: Dict[str, Any],
        payload: Dict[str, Any],
        *,
        route: Optional[Dict[str, Any]],
    ) -> AvatarSpeakResponse:
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

    def available(self, organization_id: str) -> bool:
        route = self.route(organization_id)
        if route is None:
            return False
        with self.persistence.transaction(organization_id) as transaction:
            model_id = str((route.get("primary") or {}).get("model_configuration_id") or "")
            model = transaction.model_configurations.get(model_id)
            connection = (
                transaction.provider_connections.get(model.get("provider_connection_id"))
                if model
                else None
            )
        return bool(model and connection and connection.get("provider_id") not in {"", "mock"})

    def route(self, organization_id: str) -> Optional[Dict[str, Any]]:
        with self.persistence.transaction(organization_id) as transaction:
            return next(
                (
                    route
                    for route in transaction.model_routes.list()
                    if route.get("enabled", True)
                    and route.get("capability") == cap.AVATAR_SPEAK
                    and route.get("purpose") == "interview_question_delivery"
                ),
                None,
            )


class AvatarService:
    """Selects one delivery adapter while exposing one candidate-facing contract."""

    def __init__(self, store: InMemoryStore, *, persistence: Optional[Persistence] = None) -> None:
        self.persistence = persistence or persistence_for(store)
        self.interviews = InterviewService(store, persistence=self.persistence)
        self.gateway = ModelGateway(store, persistence=self.persistence)
        self.local: AvatarDelivery = LocalAvatarDelivery(
            CatalogService(store, persistence=self.persistence, gateway=self.gateway),
            self.persistence,
        )
        self.cloud = CloudAvatarDelivery(self.gateway, self.persistence)

    async def speak(
        self,
        interview_id: str,
        payload: Dict[str, Any],
        *,
        actor_id: str = "interview_runtime",
    ) -> Dict[str, Any]:
        context = self.interviews.active_turn_context(interview_id, payload.get("turn_id"))
        interview = context["interview"]
        turn = context["turn"]
        speech_asset_id = turn.get("question_snapshot", {}).get("speech_asset_id")
        # Historical sessions predate avatar_mode and therefore keep their cloud behavior.
        avatar_mode = interview.get("settings", {}).get("avatar_mode", "cloud")
        if avatar_mode == "cloud":
            # TODO(cloud-avatar-expansion): keep this Tencent WebRTC/SFU route as
            # the extension point for richer cloud avatars and additional vendors.
            if self.cloud.available(interview.get("organization_id", "org_default")):
                try:
                    response = await self.cloud.speak(
                        interview,
                        turn,
                        payload,
                        actor_id=actor_id,
                    )
                    result = response.model_dump()
                    result["speech_asset_id"] = speech_asset_id
                    return result
                except ProviderError:
                    pass
            response = await self.local.speak(
                interview,
                turn,
                payload,
                actor_id=actor_id,
                fallback_reason="cloud_unavailable",
            )
        else:
            response = await self.local.speak(
                interview,
                turn,
                payload,
                actor_id=actor_id,
            )
        result = response.model_dump()
        result["speech_asset_id"] = speech_asset_id
        return result

    async def close(self, interview_id: str, session_id: str) -> Dict[str, Any]:
        interview = self.interviews.get_interview(interview_id)
        response = await self.cloud.close(interview, session_id)
        return response.model_dump()
