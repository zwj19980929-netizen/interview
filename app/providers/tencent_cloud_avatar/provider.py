import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Dict, Optional
from urllib.parse import quote, urlencode, urlparse

import httpx
import websockets

from app.core.ids import new_id
from app.model_gateway import capabilities as cap
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import AvatarSpeakRequest, AvatarSpeakResponse, ProviderContext, ProviderMeta


class TencentCloudAvatarProvider:
    """Tencent Cloud Intelligent Digital Human WebRTC session adapter.

    Tencent's cloud-rendering service owns the SFU/media plane. This adapter
    owns signing and control-plane lifecycle only; browser playback remains a
    generic player concern at the candidate boundary.
    """

    provider_id = "tencent_cloud_avatar"

    def __init__(
        self,
        client_factory: Any = httpx.AsyncClient,
        sleep: Any = asyncio.sleep,
        websocket_connect: Any = None,
    ) -> None:
        self.client_factory = client_factory
        self.sleep = sleep
        self.websocket_connect = websocket_connect or websockets.connect

    async def validate_credentials(
        self, config: Dict[str, Any], credentials: Dict[str, Any], *, timeout_s: int = 10
    ) -> Dict[str, str]:
        _app_key(credentials)
        _access_token(credentials)
        _base_url(config)
        return {
            "status": "valid",
            "message": "Tencent avatar credentials are structurally valid; run the model test to create and close a billed WebRTC session.",
        }

    async def invoke(self, capability: str, request: Any, context: ProviderContext) -> AvatarSpeakResponse:
        if capability != cap.AVATAR_SPEAK or not isinstance(request, AvatarSpeakRequest):
            raise ProviderError("provider_capability_missing", "Tencent avatar only implements avatar.speak.", retryable=False)
        if request.operation == "close":
            return await self._close(request, context)
        return await self._speak(request, context)

    async def _speak(self, request: AvatarSpeakRequest, context: ProviderContext) -> AvatarSpeakResponse:
        asset_key = request.avatar_id
        if not asset_key or asset_key == "avatar_default_cn":
            asset_key = str(context.config.get("asset_virtualman_key") or "").strip()
        if not asset_key:
            raise ProviderError("provider_bad_request", "Tencent asset_virtualman_key or a concrete avatar_id is required.", retryable=False)
        user_seed = str(request.metadata.get("interview_id") or context.invocation_id)
        user_id = "interviewer_%s" % hashlib.sha256(user_seed.encode("utf-8")).hexdigest()[:24]
        request_id = _req_id()
        create_payload: Dict[str, Any] = {
            "Header": {},
            "Payload": {
                "ReqId": request_id,
                "AssetVirtualmanKey": asset_key,
                "UserId": user_id,
                "Protocol": "webrtc",
                "DriverType": 1,
            },
        }
        timbre_key = request.voice if request.voice != "default" else str(context.config.get("timbre_key") or "").strip()
        if timbre_key:
            create_payload["Payload"]["SpeechParam"] = {"TimbreKey": timbre_key}

        created = await self._post(
            context, "create_session_path",
            "/v2/ivh/sessionmanager/sessionmanagerservice/createsessionbyasset", create_payload,
        )
        session_id = str(_first(created, "SessionId", "session_id") or "").strip()
        if not session_id:
            raise ProviderError("provider_schema_invalid", "Tencent avatar create-session omitted SessionId.", retryable=True)
        stream_url = _stream_url(created)
        try:
            state = await self._wait_ready(context, session_id)
            stream_url = stream_url or _stream_url(state)
            await self._post(
                context,
                "start_session_path",
                "/v2/ivh/sessionmanager/sessionmanagerservice/startsession",
                {"Header": {}, "Payload": {"ReqId": _req_id(), "SessionId": session_id}},
            )
            drive_request_id = _req_id()
            command = {
                "Header": {},
                "Payload": {
                    "ReqId": drive_request_id,
                    "SessionId": session_id,
                    "Command": "SEND_TEXT",
                    "Data": {"Text": request.text, "ChatCommand": "NotUseChat"},
                },
            }
            await self._drive_text(context, session_id, drive_request_id, command)
            if not stream_url:
                raise ProviderError("provider_schema_invalid", "Tencent avatar did not return a WebRTC playback address.", retryable=True)
        except Exception:
            await self._best_effort_close(context, session_id)
            raise
        return AvatarSpeakResponse(
            speech_id=new_id("avatar_speech"),
            status="ready",
            mode="webrtc",
            text=request.text,
            stream_url=stream_url,
            session_id=session_id,
            player_kind=str(context.config.get("player_kind") or "tencent_web_player"),
            provider=ProviderMeta(
                provider_id=self.provider_id,
                model=context.model,
                request_id=drive_request_id,
                latency_ms=0,
            ),
        )

    async def _close(self, request: AvatarSpeakRequest, context: ProviderContext) -> AvatarSpeakResponse:
        session_id = str(request.session_id or "").strip()
        if not session_id:
            raise ProviderError("provider_bad_request", "Tencent avatar close requires session_id.", retryable=False)
        close_request_id = await self._best_effort_close(
            context,
            session_id,
            raise_errors=True,
        )
        return AvatarSpeakResponse(
            speech_id=new_id("avatar_speech"), status="closed", mode="webrtc", text=request.text,
            session_id=session_id, player_kind=str(context.config.get("player_kind") or "tencent_web_player"),
            provider=ProviderMeta(
                provider_id=self.provider_id,
                model=context.model,
                request_id=close_request_id,
                latency_ms=0,
            ),
        )

    async def _wait_ready(self, context: ProviderContext, session_id: str) -> Dict[str, Any]:
        attempts = max(1, min(int(context.config.get("ready_poll_attempts", 20)), 60))
        interval = max(0.1, min(float(context.config.get("ready_poll_interval_s", 0.5)), 5.0))
        latest: Dict[str, Any] = {}
        for _ in range(attempts):
            latest = await self._post(
                context, "session_status_path", "/v2/ivh/sessionmanager/sessionmanagerservice/statsession",
                {"Header": {}, "Payload": {"ReqId": _req_id(), "SessionId": session_id}},
            )
            status = str(_first(latest, "SessionStatus", "Status", "session_status") or "").lower()
            if status in {"1", "ready", "running", "prepared", "success"}:
                return latest
            if status in {"2", "4", "5", "failed", "closed", "error"}:
                raise ProviderError("provider_server_error", "Tencent avatar session entered a terminal state.", retryable=True)
            await self.sleep(interval)
        raise ProviderError("provider_timeout", "Tencent avatar WebRTC session was not ready in time.", retryable=True)

    async def _drive_text(
        self,
        context: ProviderContext,
        session_id: str,
        request_id: str,
        command: Dict[str, Any],
    ) -> None:
        url = _signed_url(
            context.config,
            context.credentials,
            "/v2/ws/ivh/interactdriver/interactdriverservice/commandchannel",
            extra_query={"requestid": session_id},
            websocket=True,
        )
        headers = {"Content-Type": "application/json;charset=utf-8"}
        try:
            try:
                socket = await self.websocket_connect(
                    url,
                    additional_headers=headers,
                    open_timeout=max(1, float(context.timeout_s)),
                    max_size=1024 * 1024,
                )
            except TypeError:
                socket = await self.websocket_connect(url, extra_headers=headers)
            try:
                await socket.send(json.dumps(command, ensure_ascii=False))
                deadline = asyncio.get_running_loop().time() + max(1, float(context.timeout_s))
                while asyncio.get_running_loop().time() < deadline:
                    raw = await asyncio.wait_for(
                        socket.recv(),
                        timeout=max(0.01, deadline - asyncio.get_running_loop().time()),
                    )
                    try:
                        event = json.loads(raw)
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise ProviderError(
                            "provider_schema_invalid",
                            "Tencent avatar command channel returned invalid JSON.",
                            retryable=True,
                        ) from exc
                    event_request_id = str(_first(event, "ReqId", "req_id") or "")
                    if event_request_id and event_request_id != request_id:
                        continue
                    error_code = str(_first(event, "ErrorCode", "error_code") or "0")
                    event_type = str(_first(event, "Type", "type") or "")
                    speak_status = str(_first(event, "SpeakStatus", "speak_status") or "")
                    if error_code not in {"0", "", "None"} or event_type == "9" or speak_status == "Error":
                        raise ProviderError(
                            "provider_server_error",
                            "Tencent avatar rejected the text drive command.",
                            retryable=True,
                        )
                    if speak_status in {
                        "WaitingTextStart",
                        "TextStart",
                        "WaitingTextOver",
                        "TextOver",
                    }:
                        return
                raise ProviderError(
                    "provider_timeout",
                    "Tencent avatar did not acknowledge the text drive command.",
                    retryable=True,
                )
            finally:
                await socket.close()
        except ProviderError:
            raise
        except asyncio.TimeoutError as exc:
            raise ProviderError(
                "provider_timeout",
                "Tencent avatar command channel timed out.",
                retryable=True,
            ) from exc
        except Exception as exc:
            raise ProviderError(
                "provider_stream_open_failed",
                "Tencent avatar command channel could not be used.",
                retryable=True,
            ) from exc

    async def _best_effort_close(
        self, context: ProviderContext, session_id: str, *, raise_errors: bool = False
    ) -> str:
        request_id = _req_id()
        try:
            await self._post(
                context, "close_session_path", "/v2/ivh/sessionmanager/sessionmanagerservice/closesession",
                {"Header": {}, "Payload": {"ReqId": request_id, "SessionId": session_id}},
            )
        except Exception:
            if raise_errors:
                raise
        return request_id

    async def _post(self, context: ProviderContext, path_key: str, default_path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = _signed_url(context.config, context.credentials, str(context.config.get(path_key) or default_path))
        try:
            async with self.client_factory(timeout=max(1, context.timeout_s), trust_env=bool(context.config.get("use_environment_proxy", False))) as client:
                response = await client.post(url, json=payload, headers={"Accept": "application/json", "Content-Type": "application/json"})
        except httpx.TimeoutException as exc:
            raise ProviderError("provider_timeout", "Tencent avatar API timed out.", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("provider_server_error", "Tencent avatar API request failed.", retryable=True) from exc
        if response.status_code in {401, 403}:
            raise ProviderError("provider_auth_failed", "Tencent avatar credentials were rejected.", retryable=False)
        if response.status_code == 429:
            raise ProviderError("provider_rate_limited", "Tencent avatar concurrency or rate limit was reached.", retryable=True)
        if response.status_code >= 500:
            raise ProviderError("provider_server_error", "Tencent avatar API returned a server error.", retryable=True)
        if response.status_code >= 400:
            raise ProviderError("provider_bad_request", "Tencent avatar API rejected the request.", retryable=False)
        try:
            result = response.json()
        except ValueError as exc:
            raise ProviderError("provider_schema_invalid", "Tencent avatar response was not JSON.", retryable=True) from exc
        if not isinstance(result, dict):
            raise ProviderError("provider_schema_invalid", "Tencent avatar response must be an object.", retryable=True)
        header = result.get("Header") or result.get("header") or {}
        code = header.get("Code", header.get("code", 0)) if isinstance(header, dict) else 0
        if str(code) not in {"0", "", "None", "success", "Success"}:
            raise ProviderError("provider_server_error", "Tencent avatar returned error code %s." % code, retryable=str(code) not in {"1001", "1002"})
        body = result.get("Payload") or result.get("payload") or result.get("Data") or result.get("data") or result
        return body if isinstance(body, dict) else result


def _app_key(credentials: Dict[str, Any]) -> str:
    value = str(credentials.get("app_key") or "").strip()
    if not value:
        raise ProviderError("provider_auth_failed", "Tencent avatar app_key is required.", retryable=False)
    return value


def _access_token(credentials: Dict[str, Any]) -> str:
    value = str(credentials.get("access_token") or "").strip()
    if not value:
        raise ProviderError("provider_auth_failed", "Tencent avatar access_token is required.", retryable=False)
    return value


def _base_url(config: Dict[str, Any]) -> str:
    value = str(config.get("base_url") or "https://gw.tvs.qq.com").rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ProviderError("provider_bad_request", "Tencent avatar base_url must be HTTPS.", retryable=False)
    return value


def _signed_url(
    config: Dict[str, Any],
    credentials: Dict[str, Any],
    path: str,
    *,
    extra_query: Optional[Dict[str, str]] = None,
    websocket: bool = False,
) -> str:
    query = {"appkey": _app_key(credentials), "timestamp": str(int(time.time()))}
    query.update(extra_query or {})
    canonical = "&".join("%s=%s" % (key, query[key]) for key in sorted(query))
    digest = hmac.new(_access_token(credentials).encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).digest()
    query["signature"] = base64.b64encode(digest).decode("ascii")
    base_url = _base_url(config)
    if websocket:
        parsed = urlparse(base_url)
        base_url = "wss://%s" % parsed.netloc
    return "%s/%s?%s" % (base_url, path.lstrip("/"), urlencode(query, quote_via=quote))


def _req_id() -> str:
    return uuid.uuid4().hex


def _first(payload: Dict[str, Any], *keys: str) -> Optional[Any]:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    for value in payload.values():
        if isinstance(value, dict):
            found = _first(value, *keys)
            if found is not None:
                return found
    return None


def _stream_url(payload: Dict[str, Any]) -> Optional[str]:
    value = _first(payload, "PlayStreamAddr", "WebrtcPlayUrl", "StreamUrl", "stream_url")
    if isinstance(value, dict):
        value = _first(value, "WebRTC", "Webrtc", "Url", "url")
    result = str(value or "").strip()
    return result or None
