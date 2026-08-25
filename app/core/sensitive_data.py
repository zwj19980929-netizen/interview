import base64
import hashlib
import hmac
import json
import os

from cryptography.fernet import Fernet, InvalidToken


class SensitiveDataProtector:
    """Encrypts contact values and creates tenant-bound exact-match hashes."""

    def __init__(self) -> None:
        runtime = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower()
        encryption_key = os.getenv("INTERVIEWER_CONTACT_ENCRYPTION_KEY", "")
        lookup_secret = os.getenv("INTERVIEWER_CONTACT_LOOKUP_SECRET", "")
        if runtime == "production" and (not encryption_key or len(lookup_secret) < 32):
            raise RuntimeError(
                "INTERVIEWER_CONTACT_ENCRYPTION_KEY and a 32+ character "
                "INTERVIEWER_CONTACT_LOOKUP_SECRET are required in production."
            )
        if not encryption_key:
            seed = hashlib.sha256(b"interviewer-local-contact-encryption-key").digest()
            encryption_key = base64.urlsafe_b64encode(seed).decode("ascii")
        try:
            self._fernet = Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("INTERVIEWER_CONTACT_ENCRYPTION_KEY must be a valid Fernet key.") from exc
        self._lookup_secret = (lookup_secret or "interviewer-local-contact-lookup-secret").encode("utf-8")

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as exc:
            raise ValueError("Sensitive value cannot be decrypted with the configured key.") from exc

    def lookup_hash(self, organization_id: str, normalized_value: str) -> str:
        message = (organization_id + "\0" + normalized_value).encode("utf-8")
        return hmac.new(self._lookup_secret, message, hashlib.sha256).hexdigest()


class ProviderSecretVault:
    def __init__(self) -> None:
        runtime = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower()
        key = os.getenv("INTERVIEWER_PROVIDER_SECRET_ENCRYPTION_KEY", "")
        self._production = runtime == "production"
        if not key and not self._production:
            key = base64.urlsafe_b64encode(
                hashlib.sha256(b"interviewer-local-provider-secret-key").digest()
            ).decode("ascii")
        if not key:
            self._fernet = None
            return
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("INTERVIEWER_PROVIDER_SECRET_ENCRYPTION_KEY must be a valid Fernet key.") from exc

    def seal(self, value: dict) -> dict:
        if self._fernet is None:
            raise RuntimeError("INTERVIEWER_PROVIDER_SECRET_ENCRYPTION_KEY is required in production.")
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {"sealed_v1": self._fernet.encrypt(serialized).decode("ascii")}

    def open(self, value: dict) -> dict:
        if not value:
            return {}
        token = value.get("sealed_v1")
        if token:
            if self._fernet is None:
                raise RuntimeError("INTERVIEWER_PROVIDER_SECRET_ENCRYPTION_KEY is required in production.")
            try:
                return json.loads(self._fernet.decrypt(str(token).encode("ascii")).decode("utf-8"))
            except (InvalidToken, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("Provider secret cannot be decrypted with the configured key.") from exc
        if self._production:
            raise ValueError("Unsealed provider secrets are rejected in production.")
        return value


def mask_email(value: str) -> str:
    local, _, domain = value.partition("@")
    return "%s***@%s" % (local[:1] or "*", domain)


def mask_phone(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    return "***%s" % digits[-4:]
