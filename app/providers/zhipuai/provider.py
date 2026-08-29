from typing import Any, Dict

from app.core.prompt.contracts import prompt_contract
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ChatTextRequest, TTSSynthesizeRequest, TTSSynthesizeResponse
from app.providers.openai_compatible.provider import OpenAICompatibleProvider


class ZhipuAIProvider(OpenAICompatibleProvider):
    """Zhipu BigModel Chat and GLM-TTS adapter."""

    provider_id = "zhipuai"
    structured_output_mode = "json_object"

    async def validate_credentials(
        self, config: Dict[str, Any], credentials: Dict[str, Any], *, timeout_s: int = 10
    ) -> Dict[str, str]:
        contract = prompt_contract("provider_credential_probe", {})
        await self.chat_text(
            ChatTextRequest(
                purpose="provider_credential_validation",
                messages=contract.messages,
                temperature=0,
                max_output_tokens=1,
            ),
            config=config,
            credentials=credentials,
            model="glm-4.7-flash",
            timeout_s=timeout_s,
        )
        return {
            "status": "valid",
            "message": "API Key authenticated successfully with the provider credential probe.",
        }

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
