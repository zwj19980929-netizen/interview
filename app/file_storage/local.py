import os
from pathlib import Path
from typing import Optional

from app.file_storage.interface import RecordingProtection, StoredFile
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

    def verify_encryption(self, object_key: Optional[str] = None) -> str:
        raise RuntimeError(
            "Local private storage does not provide verifiable encryption at rest."
        )

    def verify_recording_protection(
        self, object_key: Optional[str] = None
    ) -> RecordingProtection:
        """本地录像只允许开发环境使用，并明确标记为未验证静态加密。"""

        runtime = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").strip().lower()
        local_media = os.getenv("INTERVIEWER_LOCAL_MEDIA", "false").strip().lower()
        if runtime != "development" or local_media not in {"1", "true", "yes", "on"}:
            raise RuntimeError(
                "Local recording storage requires development mode and INTERVIEWER_LOCAL_MEDIA=true."
            )
        self.healthcheck()
        if object_key is not None and not self._path(object_key).is_file():
            raise RuntimeError("Local recording object is unavailable.")
        return RecordingProtection(
            descriptor="local_private_development",
            encryption=None,
            development_only=True,
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

    def iter_bytes(self, object_key: str, *, start: int = 0, end: Optional[int] = None):
        with self._path(object_key).open("rb") as stream:
            stream.seek(start)
            remaining = None if end is None else end - start + 1
            while remaining is None or remaining > 0:
                chunk = stream.read(1024 * 1024 if remaining is None else min(1024 * 1024, remaining))
                if not chunk:
                    break
                yield chunk
                if remaining is not None:
                    remaining -= len(chunk)

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
