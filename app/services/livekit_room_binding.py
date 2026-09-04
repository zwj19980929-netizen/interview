"""Authenticated LiveKit room identities for tenant-safe provider callbacks.

The interface deliberately exposes only issue/verify.  Callers never parse room
names themselves, and provider webhooks never choose a tenant from process
configuration.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Callable, Optional


_VERSION = "interview-room.v1"
_LOCAL_SECRET = "local-development-media-room-binding-secret"


@dataclass(frozen=True)
class InterviewRoomBinding:
    organization_id: str
    interview_id: str
    room_name: str


class InterviewRoomBindingService:
    """Issue and verify HMAC-authenticated, self-routing room identities."""

    def __init__(self, *, secret_provider: Optional[Callable[[], bytes]] = None) -> None:
        self._secret_provider = secret_provider or _room_binding_secret

    def issue(self, organization_id: str, interview_id: str) -> InterviewRoomBinding:
        organization = _required_identifier(organization_id, "organization_id")
        interview = _required_identifier(interview_id, "interview_id")
        payload = _encode(
            json.dumps(
                [organization, interview],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signing_input = "%s.%s" % (_VERSION, payload)
        signature = _encode(
            hmac.new(
                self._secret_provider(),
                signing_input.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        room_name = "%s.%s" % (signing_input, signature)
        if len(room_name) > 255:
            raise ValueError("Authenticated interview room identity is too long.")
        return InterviewRoomBinding(organization, interview, room_name)

    def verify(self, room_name: str) -> InterviewRoomBinding:
        supplied = str(room_name or "")
        parts = supplied.split(".")
        # The version itself contains one dot, hence four total segments.
        if len(parts) != 4 or ".".join(parts[:2]) != _VERSION:
            raise ValueError("LiveKit room identity is not an authenticated interview room.")
        payload, supplied_signature = parts[2], parts[3]
        signing_input = "%s.%s" % (_VERSION, payload)
        expected = _encode(
            hmac.new(
                self._secret_provider(),
                signing_input.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(expected, supplied_signature):
            raise ValueError("LiveKit room identity signature is invalid.")
        try:
            decoded = json.loads(_decode(payload).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("LiveKit room identity payload is invalid.") from exc
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError("LiveKit room identity payload is invalid.")
        organization = _required_identifier(decoded[0], "organization_id")
        interview = _required_identifier(decoded[1], "interview_id")
        canonical = self.issue(organization, interview)
        if not hmac.compare_digest(canonical.room_name, supplied):
            raise ValueError("LiveKit room identity is not canonical.")
        return canonical


def interview_room_name(organization_id: str, interview_id: str) -> str:
    return InterviewRoomBindingService().issue(organization_id, interview_id).room_name


def _room_binding_secret() -> bytes:
    secret = (
        os.getenv("INTERVIEWER_MEDIA_SIGNING_SECRET", "").strip()
        or os.getenv("INTERVIEWER_CANDIDATE_TOKEN_SECRET", "").strip()
    )
    production = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production"
    if production and len(secret) < 32:
        raise RuntimeError(
            "INTERVIEWER_MEDIA_SIGNING_SECRET must contain at least 32 characters."
        )
    return (secret if len(secret) >= 32 else _LOCAL_SECRET).encode("utf-8")


def _required_identifier(value: object, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized.encode("utf-8")) > 160:
        raise ValueError("%s is invalid for an interview room binding." % field_name)
    return normalized


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
