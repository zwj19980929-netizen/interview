import os
from pathlib import Path
from typing import Optional

from app.file_storage.interface import StoredFile
from app.file_storage.signing import FileAccessSigner


class LocalPrivateFileAdapter:
    backend_name = "local_private"

    def __init__(self, root: Optional[Path] = None, *, signer: Optional[FileAccessSigner] = None) -> None:
        self.root = (root or Path(os.getenv("INTERVIEWER_PRIVATE_FILE_ROOT", "data/private-files"))).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.signer = signer or FileAccessSigner()

    def healthcheck(self) -> None:
        if not self.root.is_dir():
            raise RuntimeError("Local private file root is unavailable.")

    def store(
        self,
        *,
        organization_id: str,
        object_id: str,
        content: bytes,
        content_type: str,
        checksum: str,
    ) -> StoredFile:
        extensions = {
            "application/pdf": ".pdf",
            "audio/wav": ".wav",
            "audio/mpeg": ".mp3",
            "audio/ogg": ".ogg",
            "audio/webm": ".webm",
            "audio/mp4": ".m4a",
            "text/plain": ".txt",
        }
        extension = extensions.get(content_type.split(";", 1)[0].lower(), ".bin")
        object_key = "%s/%s%s" % (self._safe(organization_id), self._safe(object_id), extension)
        path = self._path(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".uploading")
        temporary.write_bytes(content)
        os.replace(str(temporary), str(path))
        return StoredFile(self.backend_name, object_key, len(content), checksum, content_type)

    def open(self, object_key: str) -> bytes:
        return self._path(object_key).read_bytes()

    def delete(self, object_key: str) -> None:
        path = self._path(object_key)
        if path.exists():
            path.unlink()

    def issue_read_access(self, object_key: str, *, expires_seconds: int = 300) -> str:
        return self.signer.issue(object_key, expires_seconds=expires_seconds)

    def _path(self, object_key: str) -> Path:
        path = (self.root / object_key).resolve()
        if self.root not in path.parents:
            raise ValueError("Private object key escapes the configured root.")
        return path

    def _safe(self, value: str) -> str:
        if not value or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in value):
            raise ValueError("Private object identifier is invalid.")
        return value
