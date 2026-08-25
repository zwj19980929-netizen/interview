import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional

from app.core.errors import ApiError


class FileAccessSigner:
    def __init__(self, secret: str = "") -> None:
        configured = secret or os.getenv("INTERVIEWER_FILE_SIGNING_SECRET", "")
        runtime = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower()
        if runtime == "production" and len(configured) < 32:
            raise RuntimeError("INTERVIEWER_FILE_SIGNING_SECRET must contain at least 32 characters in production.")
        self._secret = (configured or "local-development-file-signing-key").encode("utf-8")

    def issue(
        self,
        object_key: str,
        *,
        expires_seconds: int,
        claims: Optional[Dict[str, Any]] = None,
    ) -> str:
        payload = {
            "object_key": object_key,
            "expires_at": int(time.time()) + max(1, min(int(expires_seconds), 900)),
        }
        if claims:
            payload["claims"] = claims
        encoded = self._encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = self._encode(hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest())
        return "%s.%s" % (encoded, signature)

    def verify(self, token: str) -> Dict[str, Any]:
        try:
            encoded, supplied = token.split(".", 1)
            expected = self._encode(hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(supplied, expected):
                raise ValueError("signature")
            payload = json.loads(self._decode(encoded).decode("utf-8"))
            if int(payload["expires_at"]) < int(time.time()):
                raise ApiError("FILE_ACCESS_EXPIRED", "File access token has expired.", status_code=403)
            return payload
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError("FILE_ACCESS_INVALID", "File access token is invalid.", status_code=403) from exc

    def _encode(self, value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    def _decode(self, value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
