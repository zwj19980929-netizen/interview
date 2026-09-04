"""Candidate-bound delivery grants for the licensed local VRM asset."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

from app.core.errors import ApiError
from app.domain.avatar_asset import inspect_licensed_vrm
from app.services.avatar_performance import SUPPORTED_VISEMES
from app.services.interviews import InterviewService


_GRANT_VERSION = "licensed-vrm.v1"
_LOCAL_SECRET = "local-development-avatar-delivery-grant-secret"


@dataclass(frozen=True)
class AuthorizedAvatarAsset:
    asset_path: Path
    asset_sha256: str
    organization_id: str
    interview_id: str
    candidate_id: str
    expires_at: int


class AvatarAssetAccessService:
    """Validate the candidate once, then issue one narrow short-lived grant."""

    def __init__(
        self,
        store: Any,
        *,
        clock: Callable[[], int] = lambda: int(time.time()),
        secret_provider: Optional[Callable[[], bytes]] = None,
    ) -> None:
        self.interviews = InterviewService(store)
        self._clock = clock
        self._secret_provider = secret_provider or _grant_secret

    def issue_config(
        self,
        interview_id: str,
        candidate_session_token: Optional[str],
        *,
        organization_id: str = "org_default",
        ttl_seconds: int = 60,
    ) -> Dict[str, Any]:
        self.interviews.validate_candidate_token(
            interview_id, candidate_session_token, organization_id
        )
        session = self.interviews.get_interview(interview_id, organization_id)
        if session.get("status") not in {"in_progress", "paused"}:
            raise ApiError(
                "AVATAR_ASSET_SESSION_NOT_ACTIVE",
                "The licensed avatar is available only during an active interview.",
                status_code=409,
            )
        candidate_id = str(
            session.get("candidate_id")
            or session.get("candidate", {}).get("id")
            or ""
        ).strip()
        if not candidate_id:
            raise ApiError(
                "AVATAR_ASSET_CANDIDATE_BINDING_INVALID",
                "The interview has no candidate-bound avatar identity.",
                status_code=409,
            )
        asset = _required_asset()
        now = self._clock()
        expires_at = now + max(15, min(int(ttl_seconds), 90))
        payload = {
            "version": _GRANT_VERSION,
            "purpose": "licensed_interviewer_vrm",
            "organization_id": organization_id,
            "interview_id": interview_id,
            "candidate_id": candidate_id,
            "asset_sha256": asset["asset_sha256"],
            "issued_at": now,
            "expires_at": expires_at,
            "nonce": secrets.token_urlsafe(12),
        }
        grant = self._sign(payload)
        asset_url = (
            "/api/v1/public/interviews/%s/avatar-model?grant=%s"
            % (quote(interview_id, safe=""), quote(grant, safe=""))
        )
        return {
            "renderer": "three-vrm-local",
            "vrm_spec": "1.0",
            "ready": True,
            "asset_url": asset_url,
            "asset_sha256": asset["asset_sha256"],
            "asset_grant_expires_at": expires_at,
            "visemes": list(SUPPORTED_VISEMES),
            "minimum_fps": 30,
            "amplitude_lipsync_formal": False,
            "failure_action": "pause_or_human_takeover",
        }

    def authorize(self, interview_id: str, grant: str) -> AuthorizedAvatarAsset:
        payload = self._verify(grant)
        if payload.get("interview_id") != interview_id:
            raise ApiError(
                "AVATAR_ASSET_GRANT_SCOPE_INVALID",
                "The avatar asset grant belongs to another interview.",
                status_code=403,
            )
        organization_id = str(payload.get("organization_id") or "")
        candidate_id = str(payload.get("candidate_id") or "")
        if not organization_id or not candidate_id:
            raise ApiError(
                "AVATAR_ASSET_GRANT_INVALID",
                "The avatar asset grant is invalid.",
                status_code=403,
            )
        try:
            session = self.interviews.get_interview(interview_id, organization_id)
        except ApiError as exc:
            raise ApiError(
                "AVATAR_ASSET_GRANT_SCOPE_INVALID",
                "The avatar asset grant no longer has an interview scope.",
                status_code=403,
            ) from exc
        expected_candidate = str(
            session.get("candidate_id")
            or session.get("candidate", {}).get("id")
            or ""
        )
        asset = _required_asset()
        if (
            session.get("status") not in {"in_progress", "paused"}
            or not expected_candidate
            or not hmac.compare_digest(expected_candidate, candidate_id)
            or not hmac.compare_digest(
                str(asset["asset_sha256"]), str(payload.get("asset_sha256") or "")
            )
        ):
            raise ApiError(
                "AVATAR_ASSET_GRANT_SCOPE_INVALID",
                "The avatar asset grant no longer matches the candidate or licensed asset.",
                status_code=403,
            )
        return AuthorizedAvatarAsset(
            asset_path=Path(asset["asset_path"]),
            asset_sha256=str(asset["asset_sha256"]),
            organization_id=organization_id,
            interview_id=interview_id,
            candidate_id=candidate_id,
            expires_at=int(payload["expires_at"]),
        )

    def _sign(self, payload: Dict[str, Any]) -> str:
        encoded = _encode(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signature = _encode(
            hmac.new(
                self._secret_provider(),
                ("%s.%s" % (_GRANT_VERSION, encoded)).encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        return "%s.%s.%s" % (_GRANT_VERSION, encoded, signature)

    def _verify(self, grant: str) -> Dict[str, Any]:
        parts = str(grant or "").split(".")
        # The version includes one dot, producing four total segments.
        if len(parts) != 4 or ".".join(parts[:2]) != _GRANT_VERSION:
            raise _invalid_grant()
        encoded, supplied_signature = parts[2], parts[3]
        expected = _encode(
            hmac.new(
                self._secret_provider(),
                ("%s.%s" % (_GRANT_VERSION, encoded)).encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(expected, supplied_signature):
            raise _invalid_grant()
        try:
            payload = json.loads(_decode(encoded).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _invalid_grant() from exc
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "purpose",
            "organization_id",
            "interview_id",
            "candidate_id",
            "asset_sha256",
            "issued_at",
            "expires_at",
            "nonce",
        }:
            raise _invalid_grant()
        try:
            issued_at = int(payload["issued_at"])
            expires_at = int(payload["expires_at"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise _invalid_grant() from exc
        now = self._clock()
        if (
            payload.get("version") != _GRANT_VERSION
            or payload.get("purpose") != "licensed_interviewer_vrm"
            or issued_at > now + 5
            or expires_at <= now
            or expires_at - issued_at > 90
            or not str(payload.get("nonce") or "")
        ):
            raise ApiError(
                "AVATAR_ASSET_GRANT_EXPIRED",
                "The avatar asset grant is expired or out of scope.",
                status_code=403,
            )
        return payload


def _required_asset() -> Dict[str, Any]:
    asset = inspect_licensed_vrm()
    if not asset.get("ready"):
        raise ApiError(
            "LICENSED_VRM_NOT_READY",
            "The licensed VRM 1.0 interviewer asset is not configured.",
            status_code=503,
        )
    return asset


def _invalid_grant() -> ApiError:
    return ApiError(
        "AVATAR_ASSET_GRANT_INVALID",
        "The avatar asset grant is invalid.",
        status_code=403,
    )


def _grant_secret() -> bytes:
    secret = (
        os.getenv("INTERVIEWER_MEDIA_SIGNING_SECRET", "").strip()
        or os.getenv("INTERVIEWER_CANDIDATE_TOKEN_SECRET", "").strip()
    )
    production = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production"
    if production and len(secret) < 32:
        raise ApiError(
            "AVATAR_ASSET_SIGNING_NOT_READY",
            "Avatar asset grant signing is not configured.",
            status_code=503,
        )
    return (secret if len(secret) >= 32 else _LOCAL_SECRET).encode("utf-8")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
