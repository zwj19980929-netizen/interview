from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
import websockets

from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    BatchSTTRequest,
    BatchSTTResponse,
    ProviderContext,
    ProviderMeta,
    RealtimeSpeechDialogueRequest,
    TranscriptSegment,
)
from app.providers.openai_compatible.provider import OpenAICompatibleProvider
from app.providers.realtime_speech import OpenAIStyleRealtimeSpeechStream


class OpenAIProvider(OpenAICompatibleProvider):
    """Official OpenAI HTTP + Realtime adapter behind unified capabilities."""

    provider_id = "openai"

    def __init__(
        self,
        client_factory: Any = None,
        websocket_connect: Any = None,
    ) -> None:
        super().__init__(client_factory=client_factory) if client_factory is not None else super().__init__()
        self.websocket_connect = websocket_connect or websockets.connect

    async def invoke(self, capability: str, request: Any, context: ProviderContext) -> Any:
        if capability == cap.STT_BATCH and isinstance(request, BatchSTTRequest):
            return await self._transcribe_batch(request, context)
        return await super().invoke(capability, request, context)

    async def open_dialogue(
        self,
        request: RealtimeSpeechDialogueRequest,
        context: ProviderContext,
    ) -> OpenAIStyleRealtimeSpeechStream:
        if context.capability != cap.SPEECH_DIALOGUE_REALTIME:
            raise ProviderError(
                "provider_capability_missing",
                "OpenAI realtime speech capability is invalid.",
                retryable=False,
            )
        key = _api_key(context.credentials)
        base = str(
            context.config.get("realtime_websocket_url")
            or "wss://api.openai.com/v1/realtime"
        ).rstrip("?")
        url = "%s?%s" % (base, urlencode({"model": context.model}))
        voice_map = context.config.get("voice_map") or {}
        voice = str(voice_map.get(request.voice) or context.config.get("default_voice") or request.voice)
        if voice == "default":
            voice = "marin"
        transcription_model = str(
            context.config.get("input_transcription_model") or "gpt-4o-mini-transcribe"
        )
        session = {
            "type": "realtime",
            "model": context.model,
            "instructions": request.session_instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": transcription_model, "language": "zh"},
                    "turn_detection": None,
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": voice,
                },
            },
            "max_output_tokens": int(context.config.get("max_output_tokens", 256)),
        }
        response_payload = {
            "conversation": "auto",
            "output_modalities": ["audio"],
            "audio": {
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": voice,
                }
            },
        }
        return await OpenAIStyleRealtimeSpeechStream.open(
            provider_id=self.provider_id,
            connector=self.websocket_connect,
            url=url,
            headers={"Authorization": "Bearer %s" % key, "User-Agent": "Interviewer/0.1"},
            request=request,
            context=context,
            session=session,
            vendor_input_rate=24000,
            vendor_output_rate=24000,
            response_payload=response_payload,
        )

    async def _transcribe_batch(
        self,
        request: BatchSTTRequest,
        context: ProviderContext,
    ) -> BatchSTTResponse:
        if not request.audio_bytes:
            raise ProviderError(
                "provider_audio_unavailable",
                "OpenAI transcription requires server-resolved private audio bytes.",
                retryable=False,
            )
        key = _api_key(context.credentials)
        base_url = str(context.config.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        extension = _audio_extension(request.content_type)
        try:
            async with self.client_factory(
                timeout=max(1, float(context.timeout_s)),
                trust_env=bool(context.config.get("use_environment_proxy", False)),
            ) as client:
                response = await client.post(
                    "%s/audio/transcriptions" % base_url,
                    headers={"Authorization": "Bearer %s" % key},
                    data={
                        "model": context.model,
                        "language": request.language.split("-", 1)[0].lower(),
                        "response_format": "json",
                    },
                    files={
                        "file": (
                            "candidate-answer%s" % extension,
                            request.audio_bytes,
                            request.content_type.split(";", 1)[0],
                        )
                    },
                )
        except httpx.TimeoutException as exc:
            raise ProviderError("provider_timeout", "OpenAI transcription timed out.", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "provider_network_error", "OpenAI transcription request failed.", retryable=True
            ) from exc
        if response.status_code in {401, 403}:
            raise ProviderError("provider_auth_failed", "OpenAI API key was rejected.", retryable=False)
        if response.status_code == 429:
            raise ProviderError("provider_rate_limited", "OpenAI transcription was rate limited.", retryable=True)
        if response.status_code >= 500:
            raise ProviderError("provider_server_error", "OpenAI transcription failed upstream.", retryable=True)
        if response.status_code >= 400:
            raise ProviderError("provider_bad_request", "OpenAI transcription request was rejected.", retryable=False)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                "provider_schema_invalid", "OpenAI transcription returned invalid JSON.", retryable=True
            ) from exc
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ProviderError(
                "provider_final_transcript_missing",
                "OpenAI transcription returned no text.",
                retryable=True,
            )
        return BatchSTTResponse(
            text=text,
            language=request.language,
            confidence=None,
            segments=[TranscriptSegment(text=text, confidence=None)],
            source="server_batch_repair" if request.purpose == "candidate_answer_repair" else "server_batch",
            provider=ProviderMeta(
                provider_id=self.provider_id,
                model=context.model,
                request_id=response.headers.get("x-request-id", ""),
                latency_ms=0,
            ),
        )


def _api_key(credentials: Dict[str, Any]) -> str:
    value = str(credentials.get("api_key") or "")
    if not value:
        raise ProviderError("provider_auth_failed", "OpenAI api_key is required.", retryable=False)
    return value


def _audio_extension(content_type: str) -> str:
    return {
        "audio/wav": ".wav",
        "audio/webm": ".webm",
        "audio/ogg": ".ogg",
        "audio/mp4": ".m4a",
        "audio/mpeg": ".mp3",
    }.get(content_type.split(";", 1)[0].lower(), ".wav")
