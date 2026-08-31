from app.transport.http.fields.core import list_of, nested, raw, string


ERROR_DETAIL_FIELDS = {
    "code": string(),
    "message": string(),
    "details": raw(default={}),
}

ERROR_RESPONSE_FIELDS = {
    "error": nested(ERROR_DETAIL_FIELDS),
}

PAGINATION_FIELDS = {
    "items": list_of(raw(), default=[]),
    "next_cursor": raw(default=None),
}
