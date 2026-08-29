import base64
import hashlib
import json
import wave
from io import BytesIO
from typing import Any, Callable, Dict, Optional

import httpx

from app.core.prompt.contracts import structured_output_instruction
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    ChatJSONRequest,
    ChatJSONResponse,
    ChatTextRequest,
    ChatTextResponse,
    ProviderMeta,
    TextEmbeddingRequest,
    TextEmbeddingResponse,
    TTSSynthesizeRequest,
    TTSSynthesizeResponse,
    Usage,
    ProviderContext,
)
from app.model_gateway import capabilities as cap


AsyncClientFactory = Callable[..., httpx.AsyncClient]


def parse_json_object_content(content: str) -> Dict[str, Any]:
    """Accept one complete JSON object with optional reasoning/fence wrappers."""
    text = content.strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        remainder = text[index + end :].strip()
        if isinstance(value, dict) and remainder in {"", "```"}:
            return value
    raise json.JSONDecodeError("No complete JSON object found", text, 0)


class OpenAICompatibleProvider:
    provider_id = "openai_compatible"
    structured_output_mode = "json_schema"

    def __init__(self, client_factory: Optional[AsyncClientFactory] = None) -> None:
        self.client_factory = client_factory or httpx.AsyncClient

    async def validate_credentials(
        self, config: Dict[str, Any], credentials: Dict[str, Any], *, timeout_s: int = 10
    ) -> Dict[str, str]:
        response = await self._get(
            "%s/models" % _base_url(config),
            api_key=_api_key(credentials),
            timeout_s=timeout_s,
            use_environment_proxy=bool(config.get("use_environment_proxy", False)),
        )
        models = response.get("data")
        if not isinstance(models, list):
            raise ProviderError(
                "provider_schema_invalid",
                "Provider model-list response was invalid.",
                retryable=True,
            )
        return {
            "status": "valid",
            "message": "API Key authenticated successfully; provider returned %s accessible models." % len(models),
        }

    async def invoke(self, capability: str, request: Any, context: ProviderContext) -> Any:
        if capability == cap.LLM_CHAT_JSON and isinstance(request, ChatJSONRequest):
            return await self.chat_json(
                request,
                config=context.config,
                credentials=context.credentials,
                model=context.model,
                timeout_s=max(1, int(context.timeout_s)),
            )
        if capability == cap.LLM_CHAT_TEXT and isinstance(request, ChatTextRequest):
            return await self.chat_text(
                request,
                config=context.config,
                credentials=context.credentials,
                model=context.model,
                timeout_s=max(1, int(context.timeout_s)),
            )
        if capability == cap.EMBEDDING_TEXT and isinstance(request, TextEmbeddingRequest):
            return await self.embed_text(
                request,
                config=context.config,
                credentials=context.credentials,
                model=context.model,
                timeout_s=max(1, int(context.timeout_s)),
            )
        if capability == cap.TTS_SYNTHESIZE and isinstance(request, TTSSynthesizeRequest):
            return await self.synthesize_speech(
                request,
                config=context.config,
                credentials=context.credentials,
                model=context.model,
                timeout_s=max(1, int(context.timeout_s)),
            )
        raise ProviderError(
            "provider_capability_missing",
            "OpenAI-compatible provider has no runtime adapter for %s." % capability,
            retryable=False,
        )

    async def chat_json(
        self,
        request: ChatJSONRequest,
        *,
        config: Dict[str, Any],
        credentials: Dict[str, Any],
        model: str,
        timeout_s: int,
    ) -> ChatJSONResponse:
        base_url = _base_url(config)
        api_key = _api_key(credentials)
        messages = [message.model_dump() for message in request.messages]
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }
        if request.json_schema:
            output_mode = str(config.get("structured_output_mode") or self.structured_output_mode)
            if output_mode == "json_schema":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "interviewer_response",
                        "schema": request.json_schema,
                        "strict": True,
                    },
                }
            elif output_mode in {"json_object", "prompt"}:
                payload["messages"] = self._schema_prompt_messages(messages, request.json_schema)
                if output_mode == "json_object":
                    payload["response_format"] = {"type": "json_object"}
            else:
                raise ProviderError(
                    "provider_bad_request",
                    "Unsupported structured_output_mode %s." % output_mode,
                    retryable=False,
                )

        response = await self._post(
            "%s/chat/completions" % base_url,
            payload,
            api_key=api_key,
            timeout_s=timeout_s,
            use_environment_proxy=bool(config.get("use_environment_proxy", False)),
        )
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        finish_reason = choice.get("finish_reason")
        usage_data = response.get("usage") or {}
        diagnostics = _completion_diagnostics(
            usage_data,
            finish_reason=finish_reason,
            requested_max_output_tokens=request.max_output_tokens,
            content_length=len(content) if isinstance(content, str) else 0,
        )
        if finish_reason == "length":
            raise ProviderError(
                "provider_output_truncated",
                "Provider output reached the configured token limit before completing JSON "
                "(finish_reason=length, content_length=%s)."
                % diagnostics["content_length"],
                retryable=False,
                details=diagnostics,
            )
        if not isinstance(content, str) or not content.strip():
            raise ProviderError(
                "provider_schema_invalid",
                "Provider returned empty JSON content.",
                retryable=True,
                details=diagnostics,
            )
        try:
            data = parse_json_object_content(content)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "provider_schema_invalid",
                "Provider response was not valid JSON (finish_reason=%s, content_length=%s)."
                % (finish_reason or "unknown", len(content)),
                retryable=True,
                details=diagnostics,
            ) from exc

        usage = Usage(
            input_tokens=usage_data.get("prompt_tokens", 0),
            output_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )
        return ChatJSONResponse(
            data=data,
            usage=usage,
            provider=ProviderMeta(
                provider_id=self.provider_id,
                model=model,
                request_id=response.get("id") or "",
                latency_ms=0,
            ),
        )

    def _schema_prompt_messages(self, messages: list[Dict[str, Any]], json_schema: Dict[str, Any]) -> list[Dict[str, Any]]:
        instruction = structured_output_instruction(json_schema)
        return [{"role": "system", "content": instruction}, *messages]

    async def embed_text(
        self,
        request: TextEmbeddingRequest,
        *,
        config: Dict[str, Any],
        credentials: Dict[str, Any],
        model: str,
        timeout_s: int,
    ) -> TextEmbeddingResponse:
        base_url = _base_url(config)
        api_key = _api_key(credentials)
        response = await self._post(
            "%s/embeddings" % base_url,
            {"model": model, "input": request.texts},
            api_key=api_key,
            timeout_s=timeout_s,
            use_environment_proxy=bool(config.get("use_environment_proxy", False)),
        )
        data = response.get("data") or []
        vectors = [item["embedding"] for item in sorted(data, key=lambda item: item.get("index", 0))]
        dimensions = len(vectors[0]) if vectors else 0
        return TextEmbeddingResponse(
            vectors=vectors,
            dimensions=dimensions,
            provider=ProviderMeta(
                provider_id=self.provider_id,
                model=model,
                request_id=response.get("id") or "",
                latency_ms=0,
            ),
        )

    async def chat_text(
        self,
        request: ChatTextRequest,
        *,
        config: Dict[str, Any],
        credentials: Dict[str, Any],
        model: str,
        timeout_s: int,
    ) -> ChatTextResponse:
        response = await self._post(
            "%s/chat/completions" % _base_url(config),
            {
                "model": model,
                "messages": [message.model_dump() for message in request.messages],
                "temperature": request.temperature,
                "max_tokens": request.max_output_tokens,
            },
            api_key=_api_key(credentials),
            timeout_s=timeout_s,
            use_environment_proxy=bool(config.get("use_environment_proxy", False)),
        )
        content = str((((response.get("choices") or [{}])[0].get("message") or {}).get("content") or "")).strip()
        usage_data = response.get("usage") or {}
        return ChatTextResponse(
            text=content,
            usage=Usage(
                input_tokens=usage_data.get("prompt_tokens", 0),
                output_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
            ),
            provider=ProviderMeta(
                provider_id=self.provider_id,
                model=model,
                request_id=response.get("id") or "",
                latency_ms=0,
            ),
        )

    async def synthesize_speech(
        self,
        request: TTSSynthesizeRequest,
        *,
        config: Dict[str, Any],
        credentials: Dict[str, Any],
        model: str,
        timeout_s: int,
    ) -> TTSSynthesizeResponse:
        response_format, default_content_type = _speech_format(request.format)
        voice_map = config.get("voice_map") or {}
        voice = voice_map.get(request.voice_profile_id)
        if not voice:
            voice = config.get("default_voice") if request.voice_profile_id.startswith("voice_default") else request.voice_profile_id
        voice = str(voice or "alloy")
        payload: Dict[str, Any] = {
            "model": model,
            "input": request.text,
            "voice": voice,
            "response_format": response_format,
            "speed": request.speaking_rate,
        }
        instructions = str(config.get("tts_instructions") or "").strip()
        if instructions:
            payload["instructions"] = instructions
        response = await self._post_binary(
            "%s/audio/speech" % _base_url(config),
            payload,
            api_key=_api_key(credentials),
            timeout_s=timeout_s,
            use_environment_proxy=bool(config.get("use_environment_proxy", False)),
        )
        audio = response.content
        if not audio:
            raise ProviderError("provider_schema_invalid", "Provider returned empty speech audio.", retryable=True)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if not content_type.startswith("audio/"):
            content_type = default_content_type
        digest = hashlib.sha256(audio).hexdigest()
        return TTSSynthesizeResponse(
            audio_uri="data:%s;base64,%s" % (content_type, base64.b64encode(audio).decode("ascii")),
            content_type=content_type,
            duration_ms=_audio_duration_ms(audio, response_format, request.text, request.speaking_rate),
            content_hash="sha256:%s" % digest,
            provider=ProviderMeta(
                provider_id=self.provider_id,
                model=model,
                request_id=response.headers.get("x-request-id", ""),
                latency_ms=0,
            ),
        )

    async def _post(
        self,
        url: str,
        payload: Dict[str, Any],
        *,
        api_key: str,
        timeout_s: int,
        use_environment_proxy: bool = False,
    ) -> Dict[str, Any]:
        headers = {
            "Authorization": "Bearer %s" % api_key,
            "Content-Type": "application/json",
        }
        try:
            async with self.client_factory(
                timeout=timeout_s,
                trust_env=use_environment_proxy,
            ) as client:
                response = await client.post(url, json=payload, headers=headers)
        except ImportError as exc:
            raise ProviderError(
                "provider_transport_unavailable",
                "Provider HTTP transport or configured proxy dependency is unavailable.",
                retryable=False,
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderError("provider_timeout", "Provider request timed out.", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("provider_server_error", "Provider request failed.", retryable=True) from exc

        if response.status_code == 401 or response.status_code == 403:
            raise ProviderError("provider_auth_failed", "Provider credentials were rejected.", retryable=False)
        if response.status_code == 429:
            raise ProviderError("provider_rate_limited", "Provider rate limited the request.", retryable=True)
        if response.status_code >= 500:
            raise ProviderError("provider_server_error", "Provider returned a server error.", retryable=True)
        if response.status_code >= 400:
            raise ProviderError("provider_bad_request", "Provider rejected the request.", retryable=False)
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(
                "provider_schema_invalid",
                "Provider response was not valid JSON.",
                retryable=True,
            ) from exc

    async def _get(
        self,
        url: str,
        *,
        api_key: str,
        timeout_s: int,
        use_environment_proxy: bool = False,
    ) -> Dict[str, Any]:
        try:
            async with self.client_factory(timeout=timeout_s, trust_env=use_environment_proxy) as client:
                response = await client.get(url, headers={"Authorization": "Bearer %s" % api_key})
        except ImportError as exc:
            raise ProviderError(
                "provider_transport_unavailable",
                "Provider HTTP transport or configured proxy dependency is unavailable.",
                retryable=False,
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderError("provider_timeout", "Provider credential validation timed out.", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("provider_server_error", "Provider credential validation failed.", retryable=True) from exc
        if response.status_code in {401, 403}:
            raise ProviderError("provider_auth_failed", "Provider credentials were rejected.", retryable=False)
        if response.status_code == 429:
            raise ProviderError("provider_rate_limited", "Provider rate limited credential validation.", retryable=True)
        if response.status_code >= 500:
            raise ProviderError("provider_server_error", "Provider returned a server error.", retryable=True)
        if response.status_code >= 400:
            raise ProviderError("provider_bad_request", "Provider rejected credential validation.", retryable=False)
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(
                "provider_schema_invalid", "Provider credential response was not valid JSON.", retryable=True
            ) from exc

    async def _post_binary(
        self,
        url: str,
        payload: Dict[str, Any],
        *,
        api_key: str,
        timeout_s: int,
        use_environment_proxy: bool = False,
    ) -> httpx.Response:
        headers = {
            "Authorization": "Bearer %s" % api_key,
            "Content-Type": "application/json",
        }
        try:
            async with self.client_factory(
                timeout=timeout_s,
                trust_env=use_environment_proxy,
            ) as client:
                response = await client.post(url, json=payload, headers=headers)
        except ImportError as exc:
            raise ProviderError(
                "provider_transport_unavailable",
                "Provider HTTP transport or configured proxy dependency is unavailable.",
                retryable=False,
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderError("provider_timeout", "Provider request timed out.", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("provider_server_error", "Provider request failed.", retryable=True) from exc
        if response.status_code in {401, 403}:
            raise ProviderError("provider_auth_failed", "Provider credentials were rejected.", retryable=False)
        if response.status_code == 429:
            raise ProviderError("provider_rate_limited", "Provider rate limited the request.", retryable=True)
        if response.status_code >= 500:
            raise ProviderError("provider_server_error", "Provider returned a server error.", retryable=True)
        if response.status_code >= 400:
            raise ProviderError("provider_bad_request", "Provider rejected the request.", retryable=False)
        return response


def _completion_diagnostics(
    usage_data: Dict[str, Any],
    *,
    finish_reason: Any,
    requested_max_output_tokens: int,
    content_length: int,
) -> Dict[str, Any]:
    """Return token/termination facts that are safe to persist on failed calls."""
    completion_details = usage_data.get("completion_tokens_details") or {}
    if not isinstance(completion_details, dict):
        completion_details = {}
    reasoning_tokens = completion_details.get("reasoning_tokens")
    if reasoning_tokens is None:
        reasoning_tokens = usage_data.get("reasoning_tokens")
    diagnostics: Dict[str, Any] = {
        "finish_reason": str(finish_reason or "unknown"),
        "requested_max_output_tokens": int(requested_max_output_tokens),
        "content_length": max(0, int(content_length)),
        "input_tokens": _nonnegative_int(usage_data.get("prompt_tokens")),
        "output_tokens": _nonnegative_int(usage_data.get("completion_tokens")),
        "total_tokens": _nonnegative_int(usage_data.get("total_tokens")),
    }
    if reasoning_tokens is not None:
        diagnostics["reasoning_tokens"] = _nonnegative_int(reasoning_tokens)
    return diagnostics


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _base_url(config: Dict[str, Any]) -> str:
    base_url = (config.get("base_url") or "").rstrip("/")
    if not base_url:
        raise ProviderError("provider_bad_request", "Provider base_url is required.", retryable=False)
    return base_url


def _api_key(credentials: Dict[str, Any]) -> str:
    api_key = credentials.get("api_key") or ""
    if not api_key:
        raise ProviderError("provider_auth_failed", "Provider api_key is required.", retryable=False)
    return api_key


def _speech_format(content_type: str) -> tuple[str, str]:
    normalized = content_type.split(";", 1)[0].strip().lower()
    formats = {
        "audio/wav": ("wav", "audio/wav"),
        "audio/x-wav": ("wav", "audio/wav"),
        "audio/mpeg": ("mp3", "audio/mpeg"),
        "audio/mp3": ("mp3", "audio/mpeg"),
        "audio/ogg": ("opus", "audio/ogg"),
        "audio/flac": ("flac", "audio/flac"),
        "audio/aac": ("aac", "audio/aac"),
        "audio/pcm": ("pcm", "audio/pcm"),
    }
    resolved = formats.get(normalized)
    if resolved is None:
        raise ProviderError(
            "provider_audio_format_unsupported",
            "OpenAI-compatible speech does not support the requested audio format.",
            retryable=False,
        )
    return resolved


def _audio_duration_ms(audio: bytes, response_format: str, text: str, speaking_rate: float) -> int:
    if response_format == "wav":
        try:
            with wave.open(BytesIO(audio), "rb") as source:
                frame_rate = source.getframerate()
                if frame_rate > 0:
                    return max(1, int(source.getnframes() * 1000 / frame_rate))
        except (EOFError, wave.Error):
            pass
    return max(250, int(len(text.strip()) * 180 / max(0.5, speaking_rate)))
