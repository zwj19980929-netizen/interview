from typing import Any, Dict, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from app.model_gateway.errors import ProviderError
from app.persistence.errors import ConcurrencyConflict, PersistenceError, RecordAlreadyExists, RecordNotFound


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )


async def provider_error_handler(_: Request, exc: ProviderError) -> JSONResponse:
    status_code = 502
    if exc.code in {"provider_auth_failed", "provider_bad_request", "provider_capability_missing"}:
        status_code = 400
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": {"retryable": exc.retryable, **exc.details},
            }
        },
    )


async def persistence_error_handler(_: Request, exc: PersistenceError) -> JSONResponse:
    status_code = 500
    code = "PERSISTENCE_ERROR"
    if isinstance(exc, ConcurrencyConflict):
        status_code = 409
        code = "PERSISTENCE_CONFLICT"
    elif isinstance(exc, RecordAlreadyExists):
        status_code = 409
        code = "PERSISTENCE_RECORD_EXISTS"
    elif isinstance(exc, RecordNotFound):
        status_code = 404
        code = "PERSISTENCE_RECORD_NOT_FOUND"
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": str(exc), "details": {}}},
    )
