from typing import Any, Dict, Iterable, Optional

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.transport.http.fields import FieldSet, marshal
from app.transport.http.fields.common import ERROR_RESPONSE_FIELDS, PAGINATION_FIELDS


class ApiJSONResponse(JSONResponse):
    """Default JSON transport response for every API router.

    Keeping the class explicit gives encoding, headers, or media-type policy one
    stable seam without coupling route functions to those implementation details.
    """


def api_response(
    data: Any,
    *,
    fields: Optional[FieldSet] = None,
    status_code: int = 200,
    headers: Optional[Dict[str, str]] = None,
) -> ApiJSONResponse:
    """Serialize one JSON response through the shared API seam.

    Successful resource bodies intentionally keep their existing top-level
    shape. This centralizes encoding and optional allow-list projection without
    forcing a breaking envelope migration on current clients.
    """

    content = marshal(data, fields) if fields is not None else data
    return ApiJSONResponse(
        status_code=status_code,
        content=jsonable_encoder(content),
        headers=headers,
    )


def accepted_response(data: Any, *, fields: Optional[FieldSet] = None) -> ApiJSONResponse:
    return api_response(data, fields=fields, status_code=202)


def collection_response(
    items: Iterable[Any],
    *,
    item_fields: Optional[FieldSet] = None,
    next_cursor: Optional[str] = None,
) -> Dict[str, Any]:
    values = list(items)
    if item_fields is not None:
        values = [marshal(item, item_fields) for item in values]
    return marshal(
        {"items": values, "next_cursor": next_cursor},
        PAGINATION_FIELDS,
    )


def error_response(
    code: str,
    message: str,
    *,
    status_code: int,
    details: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> ApiJSONResponse:
    return api_response(
        {"error": {"code": code, "message": message, "details": details or {}}},
        fields=ERROR_RESPONSE_FIELDS,
        status_code=status_code,
        headers=headers,
    )
