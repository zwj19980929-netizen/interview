import os
from typing import Any, Optional

from app.file_storage.interface import StoredFile


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
