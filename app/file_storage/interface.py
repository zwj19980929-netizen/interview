from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class StoredFile:
    storage_backend: str
    object_key: str
    byte_count: int
    checksum: str
    content_type: str


@dataclass(frozen=True)
class RecordingProtection:
    """可审计的录像存储保护结论，不把本地文件伪装成已加密对象。"""

    descriptor: str
    encryption: Optional[str]
    development_only: bool = False


class PrivateFileStorage(Protocol):
    """Small storage seam; callers never receive paths or bucket URLs."""

    backend_name: str

    def healthcheck(self) -> None: ...

    def verify_encryption(self, object_key: Optional[str] = None) -> str:
        """Return a non-secret encryption descriptor or fail closed.

        With no object key this verifies the bucket/container default used by
        writers outside this process (notably LiveKit Egress).  With a key it
        verifies the stored object's authoritative provider metadata.
        """
        ...

    def verify_recording_protection(
        self, object_key: Optional[str] = None
    ) -> RecordingProtection:
        """验证 Egress 写入位置或最终对象满足当前环境的保护策略。"""
        ...

    def store(
        self,
        *,
        organization_id: str,
        object_id: str,
        content: bytes,
        content_type: str,
        checksum: str,
    ) -> StoredFile: ...

    def open(self, object_key: str) -> bytes: ...

    def iter_bytes(self, object_key: str, *, start: int = 0, end: Optional[int] = None):
        """Read bounded chunks and optionally an inclusive byte range."""
        ...

    def delete(self, object_key: str) -> None: ...

    def issue_read_access(self, object_key: str, *, expires_seconds: int = 300) -> str: ...
