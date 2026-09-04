import base64
import hashlib
import hmac
import json
import os
import re
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import FrozenSet, Optional, Union

from fastapi import WebSocket
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.transport.http.responses import error_response
from app.core.ids import new_id
from app.core.rate_limit import public_rate_limiter
from app.core.time import utc_now
from app.persistence.provider import persistence_for
from app.repositories.provider import get_store


@dataclass(frozen=True)
class Principal:
    actor_id: str
    organization_id: str
    roles: FrozenSet[str]
    authenticated: bool


_principal: ContextVar[Principal] = ContextVar(
    "interviewer_principal",
    default=Principal("anonymous", "org_default", frozenset(), False),
)


def current_principal() -> Principal:
    return _principal.get()


class AuthAuditMiddleware(BaseHTTPMiddleware):
    """Production bearer/RBAC boundary plus metadata-only request audit."""

    async def dispatch(self, request: Request, call_next) -> Response:
        rate_response = await public_rate_limiter.check(request)
        if rate_response is not None:
            if request.url.path.startswith("/api/v1"):
                _audit_request(
                    request,
                    Principal("anonymous", os.getenv("INTERVIEWER_ORGANIZATION_ID", "org_default"), frozenset(), False),
                    rate_response.status_code,
                )
            return rate_response
        principal_or_response = authenticate_request(request)
        if isinstance(principal_or_response, Response):
            if request.url.path.startswith("/api/v1"):
                _audit_request(
                    request,
                    Principal("anonymous", os.getenv("INTERVIEWER_ORGANIZATION_ID", "org_default"), frozenset(), False),
                    principal_or_response.status_code,
                )
            return principal_or_response
        principal = principal_or_response
        token: Token[Principal] = _principal.set(principal)
        response: Optional[Response] = None
        try:
            response = await call_next(request)
            return response
        finally:
            try:
                if request.url.path.startswith("/api/v1"):
                    _audit_request(request, principal, response.status_code if response else 500)
            finally:
                _principal.reset(token)


def authenticate_request(request: Request) -> Union[Principal, Response]:
    runtime = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower()
    configured_org = os.getenv("INTERVIEWER_ORGANIZATION_ID", "org_default")
    if runtime != "production":
        roles = frozenset(
            item.strip() for item in request.headers.get("X-Roles", "admin,interviewer,reviewer").split(",") if item.strip()
        )
        return Principal(
            request.headers.get("X-Actor-Id", "local_development"),
            configured_org,
            roles,
            False,
        )
    if _is_public_path(request.url.path):
        return Principal("public", configured_org, frozenset({"public"}), False)
    authorization = request.headers.get("Authorization", "")
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return _auth_error("AUTHENTICATION_REQUIRED", "A bearer token is required.", 401)
    principal = _principal_for_token(supplied)
    if principal is None:
        return _auth_error("AUTHENTICATION_INVALID", "Bearer token is invalid.", 401)
    if principal.organization_id != configured_org:
        return _auth_error(
            "ORGANIZATION_SCOPE_UNAVAILABLE",
            "This deployment is not configured for the token organization.",
            403,
        )
    required = _required_roles(request.url.path, request.method)
    if required and not principal.roles.intersection(required):
        return _auth_error("AUTHORIZATION_FORBIDDEN", "The principal lacks the required role.", 403)
    return principal


def authenticate_interviewer_websocket(websocket: WebSocket) -> Optional[Response]:
    if os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() != "production":
        return None
    authorization = websocket.headers.get("Authorization", "")
    scheme, _, supplied = authorization.partition(" ")
    principal = _principal_for_token(supplied) if scheme.lower() == "bearer" else None
    if principal is None:
        principal = _principal_for_websocket_ticket(
            websocket.query_params.get("ticket", ""),
            websocket.path_params.get("interview_id", ""),
        )
    if principal is None or not principal.roles.intersection({"admin", "interviewer"}):
        return _auth_error("AUTHORIZATION_FORBIDDEN", "Interviewer WebSocket authorization failed.", 403)
    return None


def issue_interviewer_websocket_ticket(interview_id: str, ttl_seconds: int = 60) -> dict:
    principal = current_principal()
    if not principal.roles.intersection({"admin", "interviewer"}):
        raise PermissionError("Interviewer role is required for a realtime ticket.")
    expires_at = int(time.time()) + max(1, min(ttl_seconds, 60))
    payload = {
        "actor_id": principal.actor_id,
        "organization_id": principal.organization_id,
        "roles": sorted(principal.roles),
        "interview_id": interview_id,
        "expires_at": expires_at,
    }
    encoded = _urlsafe_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(_websocket_ticket_secret(), encoded.encode("ascii"), hashlib.sha256).digest()
    return {"ticket": "%s.%s" % (encoded, _urlsafe_encode(signature)), "expires_at": expires_at}


def _principal_for_websocket_ticket(ticket: str, interview_id: str) -> Optional[Principal]:
    encoded, separator, supplied_signature = ticket.partition(".")
    if not separator or not encoded or not supplied_signature:
        return None
    try:
        expected = hmac.new(_websocket_ticket_secret(), encoded.encode("ascii"), hashlib.sha256).digest()
        supplied = _urlsafe_decode(supplied_signature)
        if not hmac.compare_digest(expected, supplied):
            return None
        payload = json.loads(_urlsafe_decode(encoded).decode("utf-8"))
        if payload.get("interview_id") != interview_id or int(payload.get("expires_at", 0)) < int(time.time()):
            return None
        configured_org = os.getenv("INTERVIEWER_ORGANIZATION_ID", "org_default")
        if payload.get("organization_id") != configured_org:
            return None
        return Principal(
            actor_id=str(payload.get("actor_id") or "unknown"),
            organization_id=str(payload.get("organization_id") or ""),
            roles=frozenset(str(item) for item in payload.get("roles", [])),
            authenticated=True,
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _websocket_ticket_secret() -> bytes:
    secret = os.getenv("INTERVIEWER_WEBSOCKET_TICKET_SECRET") or os.getenv("INTERVIEWER_CANDIDATE_TOKEN_SECRET", "")
    runtime = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower()
    if runtime != "production" and len(secret) < 32:
        secret = "local-development-websocket-ticket-secret"
    if len(secret) < 32:
        raise RuntimeError("INTERVIEWER_WEBSOCKET_TICKET_SECRET must contain at least 32 characters.")
    return secret.encode("utf-8")


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _principal_for_token(supplied: str) -> Optional[Principal]:
    raw = os.getenv("INTERVIEWER_API_TOKENS_JSON", "{}")
    try:
        tokens = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("INTERVIEWER_API_TOKENS_JSON must be valid JSON.") from exc
    if not isinstance(tokens, dict):
        raise RuntimeError("INTERVIEWER_API_TOKENS_JSON must be an object keyed by token.")
    for configured, value in tokens.items():
        if hmac.compare_digest(str(configured), supplied):
            if not isinstance(value, dict):
                return None
            return Principal(
                actor_id=str(value.get("actor_id") or "unknown"),
                organization_id=str(value.get("organization_id") or ""),
                roles=frozenset(str(item) for item in value.get("roles", [])),
                authenticated=True,
            )
    return None


def _required_roles(path: str, method: str = "GET") -> FrozenSet[str]:
    if path == "/api/v1/auth/session":
        return frozenset({"admin", "interviewer", "reviewer"})
    if path.startswith("/api/v1/admin/"):
        return frozenset({"admin"})
    if path in {"/api/v1/workspace/question-catalog", "/api/v1/workspace/question-overview"}:
        return frozenset({"admin", "interviewer"})
    if any(marker in path for marker in ("/review", "/audio-url", "/transcript", "/report/export")):
        return frozenset({"admin", "reviewer"})
    if method == "GET" and (path == "/api/v1/interviews" or path.startswith("/api/v1/interviews/")):
        return frozenset({"admin", "interviewer", "reviewer"})
    return frozenset({"admin", "interviewer"})


def _is_public_path(path: str) -> bool:
    return path.startswith(
        (
            "/api/v1/public/",
            "/api/v1/private-files/",
            "/api/v1/private-media/",
            "/healthz",
            "/readyz",
        )
    )


def _audit_request(request: Request, principal: Principal, status_code: int) -> None:
    organization_id = principal.organization_id or "org_default"
    with persistence_for(get_store()).transaction(organization_id) as transaction:
        transaction.audit_events.add(
            {
                "id": new_id("audit"),
                "organization_id": organization_id,
                "actor_id": principal.actor_id,
                "action": "api.request",
                "resource_type": "http_route",
                "resource_id": _safe_route_resource(request.url.path),
                "metadata": {
                    "method": request.method,
                    "status_code": status_code,
                    "authenticated": principal.authenticated,
                },
                "created_at": utc_now(),
            }
        )


def _safe_route_resource(path: str) -> str:
    """Keep audit routing useful without persisting bearer-style URL secrets."""
    patterns = (
        (r"^(/api/v1/public/interview-invitations/)[^/]+", r"\1{token}"),
        (r"^(/api/v1/private-files/)[^/]+", r"\1{token}"),
        (r"^(/api/v1/private-media/)[^/]+", r"\1{token}"),
    )
    sanitized = path
    for pattern, replacement in patterns:
        sanitized = re.sub(pattern, replacement, sanitized)
    return sanitized


def _auth_error(code: str, message: str, status_code: int) -> JSONResponse:
    return error_response(
        code,
        message,
        status_code=status_code,
        headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
    )
