"""Self-hosted LiveKit adapter for scoped room grants and participant Egress.

Only transport protocol knowledge lives here. Interview policy, consent and
capture lifecycle remain in the services/domain layers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse, urlunparse

import httpx

from app.core.errors import ApiError


LOCAL_LIVEKIT_API_KEY = "interviewer-local-dev"
LOCAL_LIVEKIT_API_SECRET = "interviewer-local-dev-secret-change-me"
LOCAL_LIVEKIT_URL = "ws://127.0.0.1:7880"
LOCAL_LIVEKIT_EGRESS_URL = "http://127.0.0.1:7880"
LOCAL_LIVEKIT_EGRESS_ROOT = "/recordings"


def prepare_local_rtc_environment() -> tuple[str, ...]:
    """让本机 ws:// LiveKit 绕过不识别 no_proxy 的原生 SDK 代理逻辑。

    只移除会接管明文 WebSocket 的 HTTP/SOCKS 代理；HTTPS_PROXY 保留给外部
    模型服务。生产和未启用本地媒体的进程完全不修改代理环境。
    """

    runtime = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").strip().lower()
    if runtime != "development" or not _env_bool("INTERVIEWER_LOCAL_MEDIA"):
        return ()
    removed = []
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        if name in os.environ:
            os.environ.pop(name)
            removed.append(name)
    return tuple(removed)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class LiveKitConfiguration:
    url: str
    api_key: str
    api_secret: str
    egress_url: str
    storage_endpoint: str
    storage_bucket: str
    storage_region: str
    storage_access_key: str
    storage_secret: str
    storage_sse_algorithm: str = ""
    storage_kms_key_id: str = ""
    authoritative_ingress_enabled: bool = False
    authoritative_ingress_mode: str = "disabled"
    authoritative_ingress_grace_seconds: int = 30
    authoritative_ingress_lease_seconds: int = 15
    authoritative_ingress_renew_seconds: int = 5
    authoritative_ingress_backpressure_seconds: int = 5
    runtime_environment: str = "development"
    local_media_enabled: bool = False
    recording_storage_backend: str = "aliyun_oss"
    private_file_storage_backend: str = "local"
    local_egress_root: str = LOCAL_LIVEKIT_EGRESS_ROOT

    @classmethod
    def from_environment(cls) -> "LiveKitConfiguration":
        runtime_environment = os.getenv(
            "INTERVIEWER_RUNTIME_ENV", "development"
        ).strip().lower()
        local_media_enabled = _env_bool("INTERVIEWER_LOCAL_MEDIA")
        url = os.getenv("INTERVIEWER_LIVEKIT_URL", "").strip()
        if local_media_enabled:
            url = url or LOCAL_LIVEKIT_URL
        api_key = os.getenv("INTERVIEWER_LIVEKIT_API_KEY", "").strip()
        api_secret = os.getenv("INTERVIEWER_LIVEKIT_API_SECRET", "")
        egress_url = os.getenv("INTERVIEWER_LIVEKIT_EGRESS_URL", "").strip()
        if local_media_enabled:
            api_key = api_key or LOCAL_LIVEKIT_API_KEY
            api_secret = api_secret or LOCAL_LIVEKIT_API_SECRET
            egress_url = egress_url or LOCAL_LIVEKIT_EGRESS_URL
        configured_file_storage_backend = os.getenv(
            "INTERVIEWER_FILE_STORAGE_BACKEND", "local"
        ).strip().lower()
        private_file_storage_backend = (
            "local"
            if local_media_enabled and runtime_environment == "development"
            else configured_file_storage_backend
        )
        return cls(
            url=url,
            api_key=api_key,
            api_secret=api_secret,
            egress_url=egress_url or _http_origin(url),
            storage_endpoint=os.getenv("INTERVIEWER_OSS_ENDPOINT", "").strip(),
            storage_bucket=os.getenv("INTERVIEWER_OSS_BUCKET", "").strip(),
            storage_region=os.getenv("INTERVIEWER_OSS_REGION", "auto").strip() or "auto",
            storage_access_key=os.getenv("INTERVIEWER_OSS_ACCESS_KEY_ID", "").strip(),
            storage_secret=os.getenv("INTERVIEWER_OSS_ACCESS_KEY_SECRET", ""),
            storage_sse_algorithm=os.getenv("INTERVIEWER_OSS_SSE", "").strip().upper(),
            storage_kms_key_id=os.getenv("INTERVIEWER_OSS_KMS_KEY_ID", "").strip(),
            authoritative_ingress_enabled=(
                _env_bool("INTERVIEWER_LIVEKIT_INGRESS_ENABLED")
                if "INTERVIEWER_LIVEKIT_INGRESS_ENABLED" in os.environ
                else local_media_enabled
            ),
            authoritative_ingress_mode=(
                os.getenv("INTERVIEWER_LIVEKIT_INGRESS_MODE", "").strip().lower()
                or ("database_fenced" if local_media_enabled else "disabled")
            ),
            authoritative_ingress_grace_seconds=_bounded_env_int(
                "INTERVIEWER_LIVEKIT_INGRESS_GRACE_SECONDS",
                default=30,
                minimum=5,
                maximum=120,
            ),
            authoritative_ingress_lease_seconds=_bounded_env_int(
                "INTERVIEWER_LIVEKIT_INGRESS_LEASE_SECONDS",
                default=15,
                minimum=6,
                maximum=120,
            ),
            authoritative_ingress_renew_seconds=_bounded_env_int(
                "INTERVIEWER_LIVEKIT_INGRESS_RENEW_SECONDS",
                default=5,
                minimum=1,
                maximum=40,
            ),
            authoritative_ingress_backpressure_seconds=_bounded_env_int(
                "INTERVIEWER_LIVEKIT_INGRESS_BACKPRESSURE_SECONDS",
                default=5,
                minimum=2,
                maximum=30,
            ),
            runtime_environment=runtime_environment,
            local_media_enabled=local_media_enabled,
            recording_storage_backend=(
                "local" if local_media_enabled else "aliyun_oss"
            ),
            private_file_storage_backend=private_file_storage_backend,
            local_egress_root=os.getenv(
                "INTERVIEWER_LOCAL_EGRESS_ROOT", LOCAL_LIVEKIT_EGRESS_ROOT
            ).strip()
            or LOCAL_LIVEKIT_EGRESS_ROOT,
        )

    def media_ready(self) -> bool:
        parsed = urlparse(self.url)
        return bool(
            not (
                self.local_media_enabled
                and self.runtime_environment != "development"
            )
            and parsed.scheme in {"ws", "wss", "http", "https"}
            and parsed.netloc
            and self.api_key
            and len(self.api_secret) >= 16
        )

    def recording_ready(self) -> bool:
        if not self.media_ready() or not self.egress_url:
            return False
        if self.recording_storage_backend == "local":
            root = PurePosixPath(self.local_egress_root)
            return bool(
                self.local_media_enabled
                and self.runtime_environment == "development"
                and self.private_file_storage_backend == "local"
                and root.is_absolute()
                and ".." not in root.parts
            )
        return bool(
            self.recording_storage_backend == "aliyun_oss"
            and self.storage_endpoint
            and self.storage_bucket
            and self.storage_access_key
            and self.storage_secret
            and self.storage_sse_algorithm in {"AES256", "KMS"}
            and (
                self.storage_sse_algorithm != "KMS" or self.storage_kms_key_id
            )
        )

    def recording_readiness_issues(self) -> list[str]:
        """Return bounded diagnostics without exposing credentials."""

        issues: list[str] = []
        if self.local_media_enabled and self.runtime_environment != "development":
            issues.append("local_media_forbidden_in_production")
        if not self.media_ready():
            issues.append("livekit_transport_configuration_missing")
        if not self.egress_url:
            issues.append("livekit_egress_api_missing")
        if self.recording_storage_backend == "local":
            root = PurePosixPath(self.local_egress_root)
            if self.private_file_storage_backend != "local":
                issues.append("local_private_file_storage_required")
            if not root.is_absolute() or ".." in root.parts:
                issues.append("local_egress_root_invalid")
        else:
            if not all(
                (
                    self.storage_endpoint,
                    self.storage_bucket,
                    self.storage_access_key,
                    self.storage_secret,
                )
            ):
                issues.append("private_oss_configuration_missing")
            if self.storage_sse_algorithm not in {"AES256", "KMS"}:
                issues.append("private_oss_encryption_missing")
            if (
                self.storage_sse_algorithm == "KMS"
                and not self.storage_kms_key_id
            ):
                issues.append("private_oss_kms_key_missing")
        return list(dict.fromkeys(issues))

    def authoritative_audio_ingress_ready(self) -> bool:
        """Whether a server-side LiveKit subscriber feeds the Evidence chain.

        ``database_fenced`` is the only formal mode. A DB-time lease, fencing
        epoch, durable command journal and persistent media checkpoint select
        one subscriber owner while allowing another process to recover it.
        """

        return bool(
            self.media_ready()
            and self.authoritative_ingress_enabled
            and self.authoritative_ingress_mode == "database_fenced"
            and importlib.util.find_spec("livekit.rtc") is not None
        )


class LiveKitMediaPlane:
    """Narrow provider adapter; safe to replace with another SFU implementation."""

    def __init__(
        self,
        configuration: Optional[LiveKitConfiguration] = None,
        *,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.configuration = configuration or LiveKitConfiguration.from_environment()
        self._client = client

    @property
    def url(self) -> str:
        return self.configuration.url

    def require_ready(self, *, recording: bool) -> None:
        ready = (
            self.configuration.recording_ready()
            if recording
            else self.configuration.media_ready()
        )
        if not ready:
            issues = self.configuration.recording_readiness_issues()
            raise ApiError(
                "LIVEKIT_RECORDING_NOT_READY" if recording else "LIVEKIT_MEDIA_NOT_READY",
                (
                    "实时面试录制尚未就绪。本地开发请先启动 LiveKit/Egress，并设置 INTERVIEWER_LOCAL_MEDIA=true；生产环境必须配置 LiveKit 和加密 OSS。"
                    if recording
                    else "实时面试媒体服务尚未就绪，请先启动并配置 LiveKit。"
                ),
                status_code=503,
                details={"issues": issues},
            )

    def issue_participant_token(
        self,
        *,
        room_name: str,
        identity: str,
        participant_role: str,
        publish_sources: Iterable[str],
        can_subscribe: bool = True,
        ttl_seconds: int = 90,
    ) -> str:
        self.require_ready(recording=False)
        sources = list(dict.fromkeys(str(item) for item in publish_sources))
        allowed = {"microphone", "camera"}
        if not set(sources).issubset(allowed):
            raise ValueError("LiveKit grants may publish only microphone/camera sources")
        can_publish = bool(sources)
        now = int(time.time())
        payload: Dict[str, Any] = {
            "iss": self.configuration.api_key,
            "sub": identity,
            "nbf": now - 5,
            "exp": now + max(15, min(int(ttl_seconds), 300)),
            "name": identity,
            "metadata": json.dumps(
                {"role": participant_role}, ensure_ascii=False, separators=(",", ":")
            ),
            "video": {
                "roomJoin": True,
                "room": room_name,
                "canPublish": can_publish,
                "canSubscribe": bool(can_subscribe),
                # Agent events use the authenticated control channel. A media
                # grant must never silently become a second command channel.
                "canPublishData": False,
                "canPublishSources": sources,
            },
        }
        return self._jwt(payload)

    def issue_audio_subscriber_token(
        self,
        *,
        room_name: str,
        identity: str,
        ttl_seconds: int = 300,
    ) -> str:
        """Issue a receive-only grant for the authoritative Evidence ingress.

        This identity cannot publish media or data and is scoped to exactly one
        interview room. It is intentionally separate from browser/observer
        grants so a future adapter cannot accidentally broaden its authority.
        """

        self.require_ready(recording=False)
        now = int(time.time())
        payload: Dict[str, Any] = {
            "iss": self.configuration.api_key,
            "sub": identity,
            "nbf": now - 5,
            "exp": now + max(30, min(int(ttl_seconds), 3600)),
            "name": identity,
            "metadata": json.dumps(
                {"role": "evidence_ingress"},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "video": {
                "roomJoin": True,
                "room": room_name,
                "canPublish": False,
                "canSubscribe": True,
                "canPublishData": False,
                "canPublishSources": [],
            },
        }
        return self._jwt(payload)

    def issue_audio_publisher_token(
        self,
        *,
        room_name: str,
        performance_id: str,
        ttl_seconds: int = 90,
    ) -> str:
        """One approved expression's audio-only, non-subscribing room grant.

        The identity is derived here, never supplied by a browser. Approval
        and ownership remain the caller's domain responsibility; this grant
        deliberately cannot publish camera/data or subscribe to candidates.
        """
        self.require_ready(recording=False)
        if not isinstance(room_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}", room_name):
            raise ValueError("Publisher room name must be an exact server-issued room identifier")
        if not isinstance(performance_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", performance_id):
            raise ValueError("Publisher performance ID must be a server-issued identifier")
        identity = "expression:%s" % performance_id
        now = int(time.time())
        return self._jwt({
            "iss": self.configuration.api_key,
            "sub": identity,
            "name": identity,
            "nbf": now - 5,
            "exp": now + max(30, min(int(ttl_seconds), 300)),
            "metadata": json.dumps({"role": "approved_expression"}, separators=(",", ":")),
            "video": {
                "roomJoin": True,
                "room": room_name,
                "canPublish": True,
                "canSubscribe": False,
                "canPublishData": False,
                "canPublishSources": ["microphone"],
            },
        })

    async def start_participant_egress(
        self,
        *,
        room_name: str,
        participant_identity: str,
        object_key: str,
    ) -> Dict[str, Any]:
        self.require_ready(recording=True)
        file_output: Dict[str, Any] = {
            "file_type": "MP4",
            "filepath": self._egress_filepath(object_key),
        }
        if self.configuration.recording_storage_backend == "aliyun_oss":
            file_output["s3"] = {
                "access_key": self.configuration.storage_access_key,
                "secret": self.configuration.storage_secret,
                "bucket": self.configuration.storage_bucket,
                "region": self.configuration.storage_region,
                "endpoint": self.configuration.storage_endpoint,
                "force_path_style": True,
            }
        payload = {
            "room_name": room_name,
            "identity": participant_identity,
            "file_outputs": [file_output],
        }
        return await self._twirp("StartParticipantEgress", payload)

    def _egress_filepath(self, object_key: str) -> str:
        logical = PurePosixPath(str(object_key))
        if (
            not str(object_key)
            or logical.is_absolute()
            or any(part in {"", ".", ".."} for part in logical.parts)
        ):
            raise ValueError("LiveKit Egress object key must be a safe relative path")
        if self.configuration.recording_storage_backend != "local":
            return logical.as_posix()
        root = PurePosixPath(self.configuration.local_egress_root)
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("Local LiveKit Egress root must be an absolute safe path")
        return (root / logical).as_posix()

    def _normalize_provider_result(self, value: Dict[str, Any]) -> Dict[str, Any]:
        if self.configuration.recording_storage_backend != "local":
            return value
        prefix = PurePosixPath(self.configuration.local_egress_root).as_posix().rstrip(
            "/"
        ) + "/"

        def normalize(item: Any, *, field: str = "") -> Any:
            if isinstance(item, dict):
                return {key: normalize(child, field=key) for key, child in item.items()}
            if isinstance(item, list):
                return [normalize(child) for child in item]
            if field in {"filepath", "filename"} and isinstance(item, str):
                return item[len(prefix) :] if item.startswith(prefix) else item
            return deepcopy(item)

        return normalize(value)

    async def stop_egress(self, egress_id: str) -> Dict[str, Any]:
        self.require_ready(recording=True)
        return await self._twirp("StopEgress", {"egress_id": egress_id})

    async def list_egress(
        self,
        *,
        room_name: str = "",
        egress_id: str = "",
        active: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Read provider truth for crash recovery; this call never mutates Egress."""

        self.require_ready(recording=True)
        payload: Dict[str, Any] = {}
        if room_name:
            payload["room_name"] = room_name
        if egress_id:
            payload["egress_id"] = egress_id
        if active is not None:
            payload["active"] = bool(active)
        return await self._twirp("ListEgress", payload)

    async def remove_participant(
        self, *, room_name: str, participant_identity: str
    ) -> Dict[str, Any]:
        """Best-effort hard stop for a revoked human takeover publisher."""

        self.require_ready(recording=False)
        if not room_name or not participant_identity:
            raise ValueError("room_name and participant_identity are required")
        return await self._room_service_twirp(
            "RemoveParticipant",
            {"room": room_name, "identity": participant_identity},
        )

    async def healthcheck(self, *, recording: bool = False) -> bool:
        try:
            self.require_ready(recording=recording)
            if not recording:
                # A scoped token exercises configuration/signing without
                # creating rooms or participants.
                self.issue_participant_token(
                    room_name="readiness-probe",
                    identity="readiness-probe",
                    participant_role="probe",
                    publish_sources=("microphone",),
                    ttl_seconds=15,
                )
                return True
            await self._twirp("ListEgress", {"active": True}, timeout_seconds=3.0)
            return True
        except Exception:
            return False

    async def authoritative_ingress_healthcheck(self) -> bool:
        """Exercise the native receive-only RTC connection used by Evidence."""

        if not self.configuration.authoritative_audio_ingress_ready():
            return False
        from app.adapters.livekit_audio_ingress import (
            LiveKitAudioIngressBinding,
            LiveKitCandidateAudioIngress,
        )

        async def discard_frame(_frame: Any) -> None:
            return None

        ingress = LiveKitCandidateAudioIngress(
            self,
            LiveKitAudioIngressBinding(
                room_name="readiness-evidence-ingress",
                candidate_identity="candidate:readiness-not-present",
                subscriber_identity="evidence:readiness-probe",
            ),
            on_audio_frame=discard_frame,
        )
        try:
            await ingress.connect()
            return ingress.connected
        except Exception:
            return False
        finally:
            await ingress.close()

    def verify_webhook(self, body: bytes, authorization: str) -> Dict[str, Any]:
        """Validate LiveKit's signed body hash before returning an event."""

        scheme, _, token = str(authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise ApiError(
                "LIVEKIT_WEBHOOK_UNAUTHORIZED",
                "LiveKit webhook authorization is missing.",
                status_code=401,
            )
        segments = token.split(".")
        if len(segments) != 3:
            raise ApiError(
                "LIVEKIT_WEBHOOK_UNAUTHORIZED",
                "LiveKit webhook token is malformed.",
                status_code=401,
            )
        try:
            token.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ApiError(
                "LIVEKIT_WEBHOOK_UNAUTHORIZED",
                "LiveKit webhook token is malformed.",
                status_code=401,
            ) from exc
        signing_input = "%s.%s" % (segments[0], segments[1])
        expected_signature = _b64(
            hmac.new(
                self.configuration.api_secret.encode("utf-8"),
                signing_input.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(expected_signature, segments[2]):
            raise ApiError(
                "LIVEKIT_WEBHOOK_UNAUTHORIZED",
                "LiveKit webhook signature is invalid.",
                status_code=401,
            )
        try:
            claims = json.loads(_unb64(segments[1]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(
                "LIVEKIT_WEBHOOK_UNAUTHORIZED",
                "LiveKit webhook claims are invalid.",
                status_code=401,
            ) from exc
        if not isinstance(claims, dict):
            raise ApiError(
                "LIVEKIT_WEBHOOK_UNAUTHORIZED",
                "LiveKit webhook claims are invalid.",
                status_code=401,
            )
        try:
            expires_at = int(claims.get("exp", 0))
            not_before = int(claims.get("nbf", 0))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ApiError(
                "LIVEKIT_WEBHOOK_UNAUTHORIZED",
                "LiveKit webhook claims are invalid.",
                status_code=401,
            ) from exc
        now = int(time.time())
        if (
            claims.get("iss") != self.configuration.api_key
            or expires_at < now
            or not_before > now + 5
        ):
            raise ApiError(
                "LIVEKIT_WEBHOOK_UNAUTHORIZED",
                "LiveKit webhook claims are expired or out of scope.",
                status_code=401,
            )
        digest = hashlib.sha256(body).digest()
        accepted_hashes = {
            base64.b64encode(digest).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
            hashlib.sha256(body).hexdigest(),
        }
        if str(claims.get("sha256") or "") not in accepted_hashes:
            raise ApiError(
                "LIVEKIT_WEBHOOK_BODY_INVALID",
                "LiveKit webhook body hash is invalid.",
                status_code=401,
            )
        try:
            event = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(
                "LIVEKIT_WEBHOOK_BODY_INVALID",
                "LiveKit webhook body must be JSON.",
                status_code=422,
            ) from exc
        if not isinstance(event, dict):
            raise ApiError(
                "LIVEKIT_WEBHOOK_BODY_INVALID",
                "LiveKit webhook event must be an object.",
                status_code=422,
            )
        return self._normalize_provider_result(event)

    async def _twirp(
        self,
        method: str,
        payload: Dict[str, Any],
        *,
        timeout_seconds: float = 10.0,
    ) -> Dict[str, Any]:
        now = int(time.time())
        token = self._jwt(
            {
                "iss": self.configuration.api_key,
                "sub": "interviewer-egress",
                "nbf": now - 5,
                "exp": now + 60,
                "video": {"roomRecord": True, "roomAdmin": True},
            }
        )
        endpoint = "%s/twirp/livekit.Egress/%s" % (
            self.configuration.egress_url.rstrip("/"),
            method,
        )
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=timeout_seconds)
        try:
            response = await client.post(
                endpoint,
                json=payload,
                headers={"Authorization": "Bearer %s" % token},
            )
            response.raise_for_status()
            parsed = response.json()
            if not isinstance(parsed, dict):
                raise RuntimeError("LiveKit Egress returned a non-object response")
            return self._normalize_provider_result(parsed)
        finally:
            if owns_client:
                await client.aclose()

    async def _room_service_twirp(
        self,
        method: str,
        payload: Dict[str, Any],
        *,
        timeout_seconds: float = 5.0,
    ) -> Dict[str, Any]:
        now = int(time.time())
        room_name = str(payload.get("room") or "")
        token = self._jwt(
            {
                "iss": self.configuration.api_key,
                "sub": "interviewer-room-admin",
                "nbf": now - 5,
                "exp": now + 60,
                "video": {"roomAdmin": True, "room": room_name},
            }
        )
        endpoint = "%s/twirp/livekit.RoomService/%s" % (
            _http_origin(self.configuration.url).rstrip("/"),
            method,
        )
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=timeout_seconds)
        try:
            response = await client.post(
                endpoint,
                json=payload,
                headers={"Authorization": "Bearer %s" % token},
            )
            response.raise_for_status()
            parsed = response.json()
            if not isinstance(parsed, dict):
                raise RuntimeError("LiveKit RoomService returned a non-object response")
            return parsed
        finally:
            if owns_client:
                await client.aclose()

    def _jwt(self, payload: Dict[str, Any]) -> str:
        header = {"alg": "HS256", "typ": "JWT"}
        encoded_header = _b64(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        encoded_payload = _b64(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        signing_input = "%s.%s" % (encoded_header, encoded_payload)
        signature = hmac.new(
            self.configuration.api_secret.encode("utf-8"),
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return "%s.%s" % (signing_input, _b64(signature))


def _http_origin(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.netloc:
        return ""
    scheme = "https" if parsed.scheme in {"wss", "https"} else "http"
    return urlunparse((scheme, parsed.netloc, "", "", "", ""))


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _env_bool(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _bounded_env_int(
    name: str, *, default: int, minimum: int, maximum: int
) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))
