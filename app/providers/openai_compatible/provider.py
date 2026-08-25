import json
from typing import Any, Callable, Dict, Optional

import httpx

from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    ChatJSONRequest,
    ChatJSONResponse,
    ProviderMeta,
    TextEmbeddingRequest,
    TextEmbeddingResponse,
    Usage,
    ProviderContext,
)
from app.model_gateway import capabilities as cap


AsyncClientFactory = Callable[..., httpx.AsyncClient]


class OpenAICompatibleProvider:
    provider_id = "openai_compatible"

    def __init__(self, client_factory: Optional[AsyncClientFactory] = None) -> None:
        self.client_factory = client_factory or httpx.AsyncClient

    async def invoke(self, capability: str, request: Any, context: ProviderContext) -> Any:
        if capability == cap.LLM_CHAT_JSON and isinstance(request, ChatJSONRequest):
            return await self.chat_json(
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
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [message.model_dump() for message in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }
        if request.json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "interviewer_response",
                    "schema": request.json_schema,
                    "strict": True,
                },
            }

        response = await self._post(
            "%s/chat/completions" % base_url,
            payload,
            api_key=api_key,
            timeout_s=timeout_s,
        )
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or "{}"
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ProviderError("provider_schema_invalid", "Provider response was not valid JSON.", retryable=True) from exc

        usage_data = response.get("usage") or {}
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

    async def _post(self, url: str, payload: Dict[str, Any], *, api_key: str, timeout_s: int) -> Dict[str, Any]:
        headers = {
            "Authorization": "Bearer %s" % api_key,
            "Content-Type": "application/json",
        }
        try:
            async with self.client_factory(timeout=timeout_s) as client:
                response = await client.post(url, json=payload, headers=headers)
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
        return response.json()


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
