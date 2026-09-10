"""Single-range private media responses, including bounded video streaming."""
from typing import Any, Optional
from fastapi import Request
from fastapi.responses import Response, StreamingResponse


def private_media_response(opened, request: Request):
    content = opened.get("content")
    size = len(content) if content is not None else int(opened["byte_count"])
    interval = _single_byte_range(request.headers.get("range"), size)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-store"}
    if interval is False:
        return Response(status_code=416, headers={**headers, "Content-Range": "bytes */%d" % size, "Content-Length": "0"})
    start, end = interval if interval is not None else (0, size - 1)
    headers["Content-Length"] = str(end - start + 1)
    status = 206 if interval is not None else 200
    if interval is not None:
        headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
    options = {"status_code": status, "media_type": opened["content_type"], "headers": headers}
    if request.method == "HEAD":
        return Response(content=b"", **options)
    if content is not None:
        return Response(content=content[start:end + 1], **options)
    return StreamingResponse(opened["read_range"](start, end), **options)


def _single_byte_range(value: Optional[str], total_bytes: int) -> Any:
    """Parse one RFC byte range without exposing storage paths in failures."""

    if value is None:
        return None
    raw = value.strip()
    if raw[:6].lower() != "bytes=" or "," in raw or total_bytes < 1:
        return False
    specification = raw[6:]
    if specification.count("-") != 1:
        return False
    first, last = specification.split("-", 1)
    if not first:
        if not last.isdigit() or len(last) > 20:
            return False
        suffix_length = int(last)
        if suffix_length < 1:
            return False
        start = max(0, total_bytes - suffix_length)
        return start, total_bytes - 1
    if (
        not first.isdigit()
        or len(first) > 20
        or (last and (not last.isdigit() or len(last) > 20))
    ):
        return False
    start = int(first)
    if start >= total_bytes:
        return False
    end = total_bytes - 1 if not last else min(int(last), total_bytes - 1)
    if end < start:
        return False
    return start, end
