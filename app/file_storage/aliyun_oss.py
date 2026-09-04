import os
from typing import Any, Optional

from app.file_storage.interface import RecordingProtection, StoredFile


class AliyunOssFileAdapter:
    """Private OSS adapter with lazy dependency loading and no business-layer OSS concepts."""

    backend_name = "aliyun_oss"

    def __init__(self, *, bucket: Optional[Any] = None, bucket_name: str = "") -> None:
        self.bucket_name = bucket_name or os.getenv("INTERVIEWER_OSS_BUCKET", "")
        if bucket is not None:
            self.bucket = bucket
            return
        try:
            import oss2  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install the optional 'oss2' package to use Aliyun OSS storage.") from exc
        endpoint = os.environ["INTERVIEWER_OSS_ENDPOINT"]
        access_key_id = os.environ["INTERVIEWER_OSS_ACCESS_KEY_ID"]
        access_key_secret = os.environ["INTERVIEWER_OSS_ACCESS_KEY_SECRET"]
        self.bucket = oss2.Bucket(oss2.Auth(access_key_id, access_key_secret), endpoint, self.bucket_name)

    def healthcheck(self) -> None:
        """Probe authentication and the default encryption used by Egress."""
        self.bucket.get_bucket_info()
        self.verify_encryption()

    def verify_encryption(self, object_key: Optional[str] = None) -> str:
        expected = os.getenv("INTERVIEWER_OSS_SSE", "").strip().upper()
        if expected not in {"AES256", "KMS"}:
            raise RuntimeError("INTERVIEWER_OSS_SSE must be AES256 or KMS.")
        if object_key:
            result = self.bucket.get_object_meta(object_key)
            headers = getattr(result, "headers", {}) or {}
            actual = str(
                headers.get("x-oss-server-side-encryption")
                or headers.get("X-Oss-Server-Side-Encryption")
                or ""
            ).upper()
            kms_key_id = str(
                headers.get("x-oss-server-side-encryption-key-id")
                or headers.get("X-Oss-Server-Side-Encryption-Key-Id")
                or ""
            )
        else:
            result = self.bucket.get_bucket_encryption()
            actual = str(getattr(result, "sse_algorithm", "") or "").upper()
            kms_key_id = str(getattr(result, "kms_master_keyid", "") or "")
        if actual != expected:
            raise RuntimeError(
                "OSS server-side encryption does not match the required policy."
            )
        if expected == "KMS":
            expected_key = os.getenv("INTERVIEWER_OSS_KMS_KEY_ID", "").strip()
            if expected_key and kms_key_id != expected_key:
                raise RuntimeError("OSS KMS key does not match the required policy.")
            return "aliyun_oss_kms%s" % (":%s" % kms_key_id if kms_key_id else "")
        return "aliyun_oss_aes256"

    def verify_recording_protection(
        self, object_key: Optional[str] = None
    ) -> RecordingProtection:
        encryption = self.verify_encryption(object_key)
        return RecordingProtection(
            descriptor=encryption,
            encryption=encryption,
            development_only=False,
        )

    def store(
        self,
        *,
        organization_id: str,
        object_id: str,
        content: bytes,
        content_type: str,
        checksum: str,
    ) -> StoredFile:
        object_key = "organizations/%s/private-assets/%s" % (organization_id, object_id)
        headers = {
            "Content-Type": content_type,
            "x-oss-server-side-encryption": os.getenv("INTERVIEWER_OSS_SSE", "AES256"),
            "x-oss-meta-sha256": checksum.removeprefix("sha256:"),
        }
        self.bucket.put_object(object_key, content, headers=headers)
        return StoredFile(self.backend_name, object_key, len(content), checksum, content_type)

    def open(self, object_key: str) -> bytes:
        return self.bucket.get_object(object_key).read()

    def delete(self, object_key: str) -> None:
        self.bucket.delete_object(object_key)

    def issue_read_access(self, object_key: str, *, expires_seconds: int = 300) -> str:
        return self.bucket.sign_url("GET", object_key, max(1, min(int(expires_seconds), 900)), slash_safe=True)
