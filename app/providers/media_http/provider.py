import base64
import math
from tempfile import SpooledTemporaryFile
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

import httpx

from app.core.ids import new_id
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import (
    AvatarSpeakRequest,
    AvatarSpeakResponse,
    BatchSTTRequest,
    BatchSTTResponse,
    ProviderContext,
    ProviderMeta,
    StreamingSTTEvent,
    StreamingSTTRequest,
    TranscriptSegment,
)


class MediaHttpProvider:
    """Real HTTP adapter for provider gateways exposing the documented media contract."""

    provider_id = "media_http"

    def __init__(self, client_factory: Any = httpx.AsyncClient) -> None:
        self.client_factory = client_factory

    async def validate_credentials(
        self, config: Dict[str, Any], credentials: Dict[str, Any], *, timeout_s: int = 10
    ) -> Dict[str, str]:
        path = str(config.get("health_path") or "/health")
        response = await self._request(
            "GET", self._url(config, path), credentials, timeout_s=timeout_s,
            use_environment_proxy=bool(config.get("use_environment_proxy", False)),
        )
        payload = self._json(response)
        status = str(payload.get("status") or "ok").lower()
        if status not in {"ok", "ready", "healthy", "valid"}:
            raise ProviderError("provider_health_failed", "Media provider health probe was not ready.", retryable=True)
        return {"status": "valid", "message": "Media provider credentials and health endpoint are valid."}

    async def invoke(self, capability: str, request: Any, context: ProviderContext) -> Any:
        if capability == cap.STT_BATCH and isinstance(request, BatchSTTRequest):
            return await self._transcribe(request, context)
        if capability == cap.AVATAR_SPEAK and isinstance(request, AvatarSpeakRequest):
            return await self._avatar(request, context)
        raise ProviderError(
            "provider_capability_missing",
            "HTTP media provider has no invocation adapter for %s." % capability,
            retryable=False,
        )

    async def open_stream(self, request: StreamingSTTRequest, context: ProviderContext) -> Any:
        if context.capability != cap.STT_STREAMING:
            raise ProviderError("provider_capability_missing", "Provider stream capability is invalid.", retryable=False)
        return BufferedHttpSTTStream(self, request, context)

    async def _transcribe(self, request: BatchSTTRequest, context: ProviderContext) -> BatchSTTResponse:
        audio = request.audio_bytes or _decode_data_audio(request.audio_uri)
        if not audio:
            raise ProviderError(
                "provider_audio_unavailable",
                "Server-resolved audio bytes are required for HTTP transcription.",
                retryable=False,
            )
        payload = await self._post_transcription(audio, request.content_type, request.language, context)
        text = str(payload.get("text") or "").strip()
        if not text and context.purpose == "model_configuration_test":
            text = "connection_test"
        if not text:
            raise ProviderError("provider_final_transcript_missing", "Provider returned no transcript text.", retryable=True)
        segments = _segments(payload.get("segments") or [], text)
        confidence = _confidence(payload, segments)
        return BatchSTTResponse(
            text=text,
            language=str(payload.get("language") or request.language),
            confidence=confidence,
            segments=segments,
            source="server_batch",
            provider=ProviderMeta(
                provider_id=self.provider_id,
                model=context.model,
                request_id=str(payload.get("request_id") or payload.get("id") or ""),
                latency_ms=0,
            ),
        )

    async def _post_transcription(
        self, audio: bytes, content_type: str, language: str, context: ProviderContext
    ) -> Dict[str, Any]:
        path = str(context.config.get("transcription_path") or "/audio/transcriptions")
        field = str(context.config.get("audio_field") or "file")
        extension = _extension(content_type)
        headers = self._headers(context.credentials)
        files = {field: ("candidate-answer%s" % extension, audio, content_type)}
        data = {"model": context.model, "language": language, "response_format": "verbose_json"}
        response = await self._request(
            "POST", self._url(context.config, path), context.credentials,
            timeout_s=max(1, int(context.timeout_s)),
            use_environment_proxy=bool(context.config.get("use_environment_proxy", False)),
            headers=headers, files=files, data=data,
        )
        return self._json(response)

    async def _avatar(self, request: AvatarSpeakRequest, context: ProviderContext) -> AvatarSpeakResponse:
        path = str(context.config.get("speak_path") or "/avatar/speak")
        response = await self._request(
            "POST", self._url(context.config, path), context.credentials,
            timeout_s=max(1, int(context.timeout_s)),
            use_environment_proxy=bool(context.config.get("use_environment_proxy", False)),
            json={
                "model": context.model,
                "text": request.text,
                "avatar_id": request.avatar_id,
                "voice": request.voice,
                "language": request.language,
                "request_id": context.invocation_id,
            },
        )
        payload = self._json(response)
        payload = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        mode = str(payload.get("mode") or "video")
        if mode not in {"audio", "video", "webrtc"}:
            raise ProviderError("provider_schema_invalid", "Avatar provider returned an unsupported delivery mode.", retryable=True)
        allow_http = bool(context.config.get("allow_http_media", False))
        stream_url = _safe_media_url(payload.get("stream_url"), allow_http=allow_http)
        audio_uri = _safe_media_url(payload.get("audio_uri"), allow_http=allow_http)
        if mode in {"video", "webrtc"} and not stream_url:
            raise ProviderError("provider_schema_invalid", "Avatar provider omitted stream_url.", retryable=True)
        if mode == "audio" and not audio_uri:
            raise ProviderError("provider_schema_invalid", "Avatar provider omitted audio_uri.", retryable=True)
        return AvatarSpeakResponse(
            speech_id=str(payload.get("speech_id") or new_id("avatar_speech")),
            status=str(payload.get("status") or "ready"),
            mode=mode,
            text=request.text,
            stream_url=stream_url,
            audio_uri=audio_uri,
            visemes=list(payload.get("visemes") or []),
            provider=ProviderMeta(
                provider_id=self.provider_id,
                model=context.model,
                request_id=str(payload.get("request_id") or response.headers.get("x-request-id") or ""),
                latency_ms=0,
            ),
        )

    async def _request(
        self, method: str, url: str, credentials: Dict[str, Any], *, timeout_s: int,
        use_environment_proxy: bool, **kwargs: Any
    ) -> httpx.Response:
        headers = {**self._headers(credentials), **dict(kwargs.pop("headers", {}) or {})}
        try:
            async with self.client_factory(timeout=timeout_s, trust_env=use_environment_proxy) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
        except ImportError as exc:
            raise ProviderError("provider_transport_unavailable", "Media provider transport is unavailable.", retryable=False) from exc
        except httpx.TimeoutException as exc:
            raise ProviderError("provider_timeout", "Media provider request timed out.", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("provider_server_error", "Media provider request failed.", retryable=True) from exc
        if response.status_code in {401, 403}:
            raise ProviderError("provider_auth_failed", "Media provider credentials were rejected.", retryable=False)
        if response.status_code == 429:
            raise ProviderError("provider_rate_limited", "Media provider rate limited the request.", retryable=True)
        if response.status_code >= 500:
            raise ProviderError("provider_server_error", "Media provider returned a server error.", retryable=True)
        if response.status_code >= 400:
            raise ProviderError("provider_bad_request", "Media provider rejected the request.", retryable=False)
        return response

    def _url(self, config: Dict[str, Any], path: str) -> str:
        base = str(config.get("base_url") or "").rstrip("/") + "/"
        parsed = urlparse(base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ProviderError("provider_bad_request", "Media provider base_url must be HTTPS.", retryable=False)
        return urljoin(base, path.lstrip("/"))

    def _headers(self, credentials: Dict[str, Any]) -> Dict[str, str]:
        key = str(credentials.get("api_key") or "").strip()
        if not key:
            raise ProviderError("provider_auth_failed", "Media provider api_key is required.", retryable=False)
        return {"Authorization": "Bearer %s" % key, "Accept": "application/json"}

    def _json(self, response: httpx.Response) -> Dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("provider_schema_invalid", "Media provider response was not JSON.", retryable=True) from exc
        if not isinstance(payload, dict):
            raise ProviderError("provider_schema_invalid", "Media provider response must be an object.", retryable=True)
        return payload


class BufferedHttpSTTStream:
    """Streaming interface that safely buffers audio and obtains one real provider final on finish."""

    def __init__(self, provider: MediaHttpProvider, request: StreamingSTTRequest, context: ProviderContext) -> None:
        self.provider = provider
        self.request = request
        self.context = context
        self.stream_id = new_id("stt_stream")
        self.sequence = 1
        self.closed = False
        self.buffer = SpooledTemporaryFile(max_size=5 * 1024 * 1024, mode="w+b")
        self.meta = ProviderMeta(provider_id=provider.provider_id, model=context.model, request_id="", latency_ms=0)
        self.ready_events = [StreamingSTTEvent(
            stream_id=self.stream_id, sequence=1, type="stream.ready", language=request.language, provider=self.meta
        )]

    async def send_audio(self, chunk: bytes) -> List[StreamingSTTEvent]:
        if self.closed:
            raise ProviderError("provider_stream_closed", "STT stream is already closed.", retryable=False)
        self.buffer.write(chunk)
        return []

    async def finish(self) -> List[StreamingSTTEvent]:
        if self.closed:
            return []
        self.closed = True
        self.buffer.seek(0)
        audio = self.buffer.read()
        self.buffer.close()
        batch = await self.provider._transcribe(
            BatchSTTRequest(
                organization_id=self.request.organization_id,
                purpose=self.request.purpose,
                audio_uri="stream-buffer://%s" % self.stream_id,
                content_type=self.request.audio.content_type,
                language=self.request.language,
                enable_word_timestamps=self.request.enable_word_timestamps,
                metadata=self.request.metadata,
                audio_bytes=audio,
            ),
            self.context,
        )
        self.sequence += 1
        final = StreamingSTTEvent(
            stream_id=self.stream_id, sequence=self.sequence, type="transcript.final", text=batch.text,
            language=batch.language, confidence=batch.confidence, segments=batch.segments, is_final=True,
            provider=batch.provider,
        )
        self.sequence += 1
        closed = StreamingSTTEvent(
            stream_id=self.stream_id, sequence=self.sequence, type="stream.closed",
            language=batch.language, provider=batch.provider,
        )
        return [final, closed]

    async def abort(self) -> None:
        self.closed = True
        self.buffer.close()


def _decode_data_audio(uri: str) -> bytes:
    if not str(uri).startswith("data:audio/"):
        return b""
    header, separator, encoded = str(uri).partition(",")
    if not separator or ";base64" not in header:
        return b""
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError:
        return b""


def _segments(values: List[Any], fallback_text: str) -> List[TranscriptSegment]:
    result = []
    for item in values:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        start = float(item.get("start", item.get("start_ms", 0)) or 0)
        end = float(item.get("end", item.get("end_ms", 0)) or 0)
        if "start_ms" not in item:
            start *= 1000
        if "end_ms" not in item:
            end *= 1000
        result.append(TranscriptSegment(
            text=str(item["text"]).strip(), start_ms=max(0, int(start)), end_ms=max(0, int(end)),
            confidence=max(0.0, min(1.0, float(item.get("confidence", 1.0)))),
        ))
    return result or [TranscriptSegment(text=fallback_text, confidence=1.0)]


def _confidence(payload: Dict[str, Any], segments: List[TranscriptSegment]) -> float:
    if payload.get("confidence") is not None:
        return max(0.0, min(1.0, float(payload["confidence"])))
    probabilities = []
    for item in payload.get("segments") or []:
        if isinstance(item, dict) and item.get("avg_logprob") is not None:
            probabilities.append(math.exp(float(item["avg_logprob"])))
    if probabilities:
        return max(0.0, min(1.0, sum(probabilities) / len(probabilities)))
    return sum(item.confidence for item in segments) / max(1, len(segments))


def _extension(content_type: str) -> str:
    normalized = str(content_type).split(";", 1)[0].lower()
    return {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/wav": ".wav"}.get(normalized, ".bin")


def _safe_media_url(value: Any, *, allow_http: bool) -> Any:
    if value is None:
        return None
    url = str(value).strip()
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if (
        parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and (scheme == "https" or (allow_http and scheme == "http"))
    ):
        return url
    raise ProviderError("provider_media_url_invalid", "Provider media URL must use HTTPS.", retryable=False)
