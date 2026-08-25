import hmac
import json
import os
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import FrozenSet, Optional, Union

from fastapi import WebSocket
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

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
    if principal.organization_id != configured_org or configured_org != "org_default":
        return _auth_error(
            "ORGANIZATION_SCOPE_UNAVAILABLE",
            "This deployment is not configured for the token organization.",
            403,
        )
    required = _required_roles(request.url.path)
    if required and not principal.roles.intersection(required):
        return _auth_error("AUTHORIZATION_FORBIDDEN", "The principal lacks the required role.", 403)
    return principal


def authenticate_interviewer_websocket(websocket: WebSocket) -> Optional[Response]:
    if os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() != "production":
        return None
    authorization = websocket.headers.get("Authorization", "")
    scheme, _, supplied = authorization.partition(" ")
    principal = _principal_for_token(supplied) if scheme.lower() == "bearer" else None
    if principal is None or not principal.roles.intersection({"admin", "interviewer"}):
        return _auth_error("AUTHORIZATION_FORBIDDEN", "Interviewer WebSocket authorization failed.", 403)
    return None


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


def _required_roles(path: str) -> FrozenSet[str]:
    if path.startswith("/api/v1/admin/"):
        return frozenset({"admin"})
    if any(marker in path for marker in ("/review", "/audio-url", "/transcript", "/report/export")):
        return frozenset({"admin", "reviewer"})
    return frozenset({"admin", "interviewer"})


def _is_public_path(path: str) -> bool:
    return path.startswith(
        (
            "/api/v1/public/",
            "/api/v1/private-files/",
            "/api/v1/private-media/",
            "/healthz",
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
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": {}}},
        headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
    )
