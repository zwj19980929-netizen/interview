from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class StoredFile:
    storage_backend: str
    object_key: str
    byte_count: int
    checksum: str
    content_type: str


class PrivateFileStorage(Protocol):
    """Small storage seam; callers never receive paths or bucket URLs."""

    backend_name: str

    def healthcheck(self) -> None: ...

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

    def delete(self, object_key: str) -> None: ...

    def issue_read_access(self, object_key: str, *, expires_seconds: int = 300) -> str: ...
