import hashlib
from typing import Any, Dict, Optional

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    ProviderContext,
    ProviderMeta,
    TTSSynthesizeRequest,
    TTSSynthesizeResponse,
)
from app.providers.openai_compatible.provider import OpenAICompatibleProvider


class DashScopeProvider(OpenAICompatibleProvider):
    """DashScope adapter: OpenAI-compatible Qwen calls plus native HTTP TTS."""

    provider_id = "dashscope"

    async def synthesize_speech(
        self,
        request: TTSSynthesizeRequest,
        *,
        config: Dict[str, Any],
        credentials: Dict[str, Any],
        model: str,
        timeout_s: int,
    ) -> TTSSynthesizeResponse:
        response_format, content_type = _dashscope_format(request.format)
        voice_map = config.get("voice_map") or {}
        voice = voice_map.get(request.voice_profile_id)
        if not voice:
            voice = config.get("default_voice") if request.voice_profile_id.startswith("voice_default") else request.voice_profile_id
        voice = str(voice or _default_voice(model))
        endpoint = _tts_endpoint(config, model)
        uses_speech_synthesizer = _uses_speech_synthesizer(model, endpoint)
        input_payload: Dict[str, Any] = {
            "text": request.text,
            "voice": voice,
        }
        if uses_speech_synthesizer:
            input_payload.update(
                {
                    "format": response_format,
                    "sample_rate": int(config.get("sample_rate_hz", 24000)),
                }
            )
            if request.speaking_rate != 1.0:
                input_payload["rate"] = request.speaking_rate
        else:
            if request.speaking_rate != 1.0:
                raise ProviderError(
                    "provider_bad_request",
                    "DashScope Qwen3-TTS basic HTTP integration does not expose a speaking-rate parameter.",
                    retryable=False,
                )
            if response_format != "wav":
                raise ProviderError(
                    "provider_audio_format_unsupported",
                    "DashScope Qwen3-TTS basic HTTP integration currently returns WAV audio.",
                    retryable=False,
                )
            input_payload["language_type"] = _language_type(request.language)
        vendor_response = await self._post(
            endpoint,
            {"model": model, "input": input_payload},
            api_key=_api_key(credentials),
            timeout_s=timeout_s,
            use_environment_proxy=bool(config.get("use_environment_proxy", False)),
        )
        output = vendor_response.get("output") or {}
        audio = output.get("audio") or {}
        audio_uri = str(audio.get("url") or "").strip()
        if audio_uri.startswith("http://") and bool(config.get("force_https_assets", True)):
            audio_uri = "https://%s" % audio_uri.removeprefix("http://")
        if not audio_uri.startswith(("https://", "http://")):
            raise ProviderError(
                "provider_schema_invalid",
                "DashScope TTS response did not contain a downloadable audio URL.",
                retryable=True,
            )
        locator_hash = hashlib.sha256(audio_uri.encode("utf-8")).hexdigest()
        return TTSSynthesizeResponse(
            audio_uri=audio_uri,
            content_type=content_type,
            duration_ms=max(250, int(len(request.text.strip()) * 180 / request.speaking_rate)),
            content_hash="sha256:%s" % locator_hash,
            provider=ProviderMeta(
                provider_id=self.provider_id,
                model=model,
                request_id=str(vendor_response.get("request_id") or audio.get("id") or ""),
                latency_ms=0,
            ),
        )


def _api_key(credentials: Dict[str, Any]) -> str:
    api_key = str(credentials.get("api_key") or "")
    if not api_key:
        raise ProviderError("provider_auth_failed", "DashScope api_key is required.", retryable=False)
    return api_key


def _tts_endpoint(config: Dict[str, Any], model: str) -> str:
    configured = str(config.get("tts_endpoint") or "").rstrip("/")
    if configured:
        return configured
    base_url = str(config.get("base_url") or "").rstrip("/")
    suffix = "/compatible-mode/v1"
    if not base_url.endswith(suffix):
        raise ProviderError(
            "provider_bad_request",
            "DashScope tts_endpoint is required when base_url is not an official compatible-mode URL.",
            retryable=False,
        )
    native_root = "%s/api/v1" % base_url[: -len(suffix)]
    if _uses_speech_synthesizer(model, ""):
        return "%s/services/audio/tts/SpeechSynthesizer" % native_root
    return "%s/services/aigc/multimodal-generation/generation" % native_root


def _uses_speech_synthesizer(model: str, endpoint: str) -> bool:
    normalized = model.lower()
    return "SpeechSynthesizer" in endpoint or normalized.startswith(("cosyvoice", "qwen-audio-"))


def _default_voice(model: str) -> str:
    normalized = model.lower()
    if normalized.startswith("cosyvoice"):
        return "longanyang"
    if normalized.startswith("qwen-audio-"):
        return "longanhuan_v3.6"
    return "Cherry"


def _language_type(language: str) -> str:
    normalized = language.lower()
    if normalized.startswith("zh"):
        return "Chinese"
    if normalized.startswith("en"):
        return "English"
    return language


def _dashscope_format(content_type: str) -> tuple[str, str]:
    normalized = content_type.split(";", 1)[0].strip().lower()
    formats = {
        "audio/wav": ("wav", "audio/wav"),
        "audio/x-wav": ("wav", "audio/wav"),
        "audio/mpeg": ("mp3", "audio/mpeg"),
        "audio/mp3": ("mp3", "audio/mpeg"),
        "audio/pcm": ("pcm", "audio/pcm"),
        "audio/ogg": ("opus", "audio/ogg"),
    }
    resolved: Optional[tuple[str, str]] = formats.get(normalized)
    if resolved is None:
        raise ProviderError(
            "provider_audio_format_unsupported",
            "DashScope TTS does not support the requested basic HTTP audio format.",
            retryable=False,
        )
    return resolved
