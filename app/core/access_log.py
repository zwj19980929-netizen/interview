"""Redact short-lived credentials from Uvicorn access-log request targets."""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "grant",
        "sig",
        "signature",
        "ticket",
        "token",
    }
)

SENSITIVE_PATH_PATTERNS = (
    re.compile(r"^(/api/v1/private-files/)[^/?#]+"),
    re.compile(r"^(/api/v1/private-media/)[^/?#]+"),
    re.compile(r"^(/api/v1/public/interview-invitations/)[^/?#]+"),
)


def redact_sensitive_query(request_target: str) -> str:
    """Keep routing context while removing path and query credentials."""

    value = str(request_target or "")
    parsed = urlsplit(value)
    changed = False
    path = parsed.path
    for pattern in SENSITIVE_PATH_PATTERNS:
        redacted_path, replacement_count = pattern.subn(r"\1{token}", path, count=1)
        if replacement_count:
            path = redacted_path
            changed = True
            break

    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in SENSITIVE_QUERY_KEYS:
            item = "REDACTED"
            changed = True
        query.append((key, item))
    if not changed:
        return value
    return urlunsplit(
        (parsed.scheme, parsed.netloc, path, urlencode(query), parsed.fragment)
    )


class SensitiveQueryAccessLogFilter(logging.Filter):
    """Rewrite Uvicorn's `(client, method, path, version, status)` args."""

    def filter(self, record: logging.LogRecord) -> bool:
        args: Any = record.args
        if isinstance(args, Iterable) and not isinstance(args, (str, bytes, dict)):
            values = list(args)
            if len(values) >= 3:
                values[2] = redact_sensitive_query(str(values[2]))
                record.args = tuple(values)
        return True


def install_sensitive_access_log_filter() -> None:
    logger = logging.getLogger("uvicorn.access")
    if any(
        isinstance(item, SensitiveQueryAccessLogFilter)
        for item in logger.filters
    ):
        return
    logger.addFilter(SensitiveQueryAccessLogFilter())
