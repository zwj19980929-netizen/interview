from typing import Any, Dict

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import TTSSynthesizeRequest, TTSSynthesizeResponse
from app.providers.openai_compatible.provider import OpenAICompatibleProvider


class ZhipuAIProvider(OpenAICompatibleProvider):
    """Zhipu BigModel Chat and GLM-TTS adapter."""

    provider_id = "zhipuai"
    structured_output_mode = "json_object"

    async def synthesize_speech(
        self,
        request: TTSSynthesizeRequest,
        *,
        config: Dict[str, Any],
        credentials: Dict[str, Any],
        model: str,
        timeout_s: int,
    ) -> TTSSynthesizeResponse:
        if model != "glm-tts":
            raise ProviderError(
                "provider_bad_request",
                "Zhipu TTS requires the glm-tts model.",
                retryable=False,
            )
        if len(request.text) > 1024:
            raise ProviderError(
                "provider_bad_request",
                "Zhipu GLM-TTS input cannot exceed 1024 characters.",
                retryable=False,
            )
        if request.format.split(";", 1)[0].strip().lower() not in {
            "audio/wav",
            "audio/x-wav",
            "audio/pcm",
        }:
            raise ProviderError(
                "provider_audio_format_unsupported",
                "Zhipu GLM-TTS supports WAV or PCM output.",
                retryable=False,
            )
        return await super().synthesize_speech(
            request,
            config={"default_voice": "tongtong", **config},
            credentials=credentials,
            model=model,
            timeout_s=timeout_s,
        )
