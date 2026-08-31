import hashlib
import os
from tempfile import SpooledTemporaryFile
from pathlib import Path
from typing import Any, Optional

from app.adapters.local_media import (
    LocalMediaStorage,
    MIME_EXTENSIONS,
    PCM_MIME_TYPES,
    RecordingResult,
    SAFE_ID,
    pcm_wav_header,
)
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now
from app.file_storage.interface import PrivateFileStorage
from app.file_storage.provider import private_file_storage
from app.persistence.interface import Persistence


class PrivateMediaRecording:
    """Buffers one recording and atomically publishes it through PrivateFileStorage."""

    def __init__(
        self, *, persistence: Persistence, storage: PrivateFileStorage, organization_id: str,
        interview_id: str, turn_id: str, mime_type: str, max_chunk_bytes: int,
        max_recording_bytes: int, pcm_input: bool = False,
        sample_rate_hz: int = 48000, channels: int = 1,
    ) -> None:
        self.persistence = persistence
        self.storage = storage
        self.organization_id = organization_id
        self.interview_id = interview_id
        self.turn_id = turn_id
        self.mime_type = mime_type
        self.max_chunk_bytes = max_chunk_bytes
        self.max_recording_bytes = max_recording_bytes
        self.pcm_input = pcm_input
        self.sample_rate_hz = sample_rate_hz
        self.channels = channels
        self.byte_count = 0
        self.closed = False
        self.buffer = SpooledTemporaryFile(max_size=5 * 1024 * 1024, mode="w+b")
        if self.pcm_input:
            pcm_wav_header(0, sample_rate_hz=self.sample_rate_hz, channels=self.channels)
            self.buffer.write(b"\x00" * 44)

    def append(self, chunk: bytes) -> None:
        if self.closed:
            raise ApiError("MEDIA_RECORDING_CLOSED", "Media recording is already closed.", status_code=409)
        if not chunk:
            return
        if len(chunk) > self.max_chunk_bytes:
            raise ApiError("MEDIA_CHUNK_TOO_LARGE", "Audio chunk exceeds the size limit.", status_code=413)
        if self.byte_count + len(chunk) > self.max_recording_bytes:
            raise ApiError("MEDIA_RECORDING_TOO_LARGE", "Audio recording exceeds the size limit.", status_code=413)
        self.buffer.write(chunk)
        self.byte_count += len(chunk)

    def finish(self) -> RecordingResult:
        if self.closed:
            raise ApiError("MEDIA_RECORDING_CLOSED", "Media recording is already closed.", status_code=409)
        self.closed = True
        if not self.byte_count:
            self.buffer.close()
            raise ApiError("MEDIA_RECORDING_EMPTY", "Audio recording is empty.", status_code=422)
        if self.pcm_input:
            self.buffer.seek(0)
            self.buffer.write(
                pcm_wav_header(
                    self.byte_count,
                    sample_rate_hz=self.sample_rate_hz,
                    channels=self.channels,
                )
            )
        self.buffer.seek(0)
        content = self.buffer.read()
        self.buffer.close()
        checksum = "sha256:%s" % hashlib.sha256(content).hexdigest()
        file_id = new_id("file")
        stored = self.storage.store(
            organization_id=self.organization_id,
            object_id=file_id,
            content=content,
            content_type=self.mime_type,
            checksum=checksum,
        )
        try:
            now = utc_now()
            with self.persistence.transaction(self.organization_id) as transaction:
                transaction.file_objects.add(
                    {
                        "id": file_id,
                        "organization_id": self.organization_id,
                        "purpose": "candidate_answer_audio",
                        "status": "ready",
                        "storage_backend": stored.storage_backend,
                        "object_key": stored.object_key,
                        "content_type": stored.content_type,
                        "checksum": stored.checksum,
                        "byte_count": stored.byte_count,
                        "scan_status": "not_applicable",
                        "source_type": "candidate_stream",
                        "interview_id": self.interview_id,
                        "turn_id": self.turn_id,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
        except Exception:
            self.storage.delete(stored.object_key)
            raise
        return RecordingResult(
            audio_uri="private-file://%s" % file_id,
            mime_type=self.mime_type,
            byte_count=len(content),
        )

    def abort(self) -> None:
        if not self.closed:
            self.closed = True
            self.buffer.close()


class PrivateMediaStorage:
    def __init__(
        self, persistence: Persistence, organization_id: str = "org_default",
        storage: Optional[PrivateFileStorage] = None,
    ) -> None:
        self.persistence = persistence
        self.organization_id = organization_id
        self.storage = storage or private_file_storage()
        self.max_chunk_bytes = int(os.getenv("INTERVIEWER_MEDIA_MAX_CHUNK_BYTES", "1048576"))
        self.max_recording_bytes = int(os.getenv("INTERVIEWER_MEDIA_MAX_RECORDING_BYTES", "52428800"))

    def start_recording(
        self,
        interview_id: str,
        turn_id: str,
        mime_type: str,
        *,
        sample_rate_hz: int = 48000,
        channels: int = 1,
    ) -> PrivateMediaRecording:
        if not SAFE_ID.match(interview_id) or not SAFE_ID.match(turn_id):
            raise ApiError("MEDIA_PATH_INVALID", "Interview or turn identifier is invalid.")
        normalized = mime_type.split(";", 1)[0].strip().lower()
        if normalized not in MIME_EXTENSIONS:
            raise ApiError(
                "MEDIA_TYPE_UNSUPPORTED",
                "Supported audio types are WebM, Ogg, MP4, WAV and PCM16.",
                status_code=415,
            )
        pcm_input = normalized in PCM_MIME_TYPES
        if pcm_input:
            pcm_wav_header(0, sample_rate_hz=sample_rate_hz, channels=channels)
        return PrivateMediaRecording(
            persistence=self.persistence,
            storage=self.storage,
            organization_id=self.organization_id,
            interview_id=interview_id,
            turn_id=turn_id,
            mime_type="audio/wav" if pcm_input else mime_type,
            max_chunk_bytes=self.max_chunk_bytes,
            max_recording_bytes=self.max_recording_bytes,
            pcm_input=pcm_input,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
        )


def media_recording_storage(persistence: Persistence, organization_id: str = "org_default") -> Any:
    configured = os.getenv("INTERVIEWER_MEDIA_RECORDING_BACKEND", "").strip().lower()
    production = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production"
    backend = configured or ("private" if production else "local")
    if backend == "local":
        if production:
            raise RuntimeError("Production candidate recordings require private media storage.")
        return LocalMediaStorage()
    if backend in {"private", "file_storage"}:
        return PrivateMediaStorage(persistence, organization_id)
    raise RuntimeError("Unsupported INTERVIEWER_MEDIA_RECORDING_BACKEND: %s" % backend)


def read_managed_audio(
    persistence: Persistence, organization_id: str, audio_uri: str,
    storage: Optional[PrivateFileStorage] = None,
) -> bytes:
    prefix = "private-file://"
    if str(audio_uri).startswith("/media/"):
        root = Path(os.getenv("INTERVIEWER_MEDIA_PATH", "data/media")).resolve()
        path = (root / str(audio_uri).removeprefix("/media/")).resolve()
        if root not in path.parents or not path.is_file():
            raise ApiError("ANSWER_AUDIO_NOT_FOUND", "Candidate answer audio is unavailable.", status_code=404)
        return path.read_bytes()
    if not str(audio_uri).startswith(prefix):
        return b""
    file_id = str(audio_uri).removeprefix(prefix)
    with persistence.transaction(organization_id) as transaction:
        item = transaction.file_objects.get(file_id)
    if (
        item is None or item.get("status") != "ready" or
        item.get("purpose") != "candidate_answer_audio" or not item.get("object_key")
    ):
        raise ApiError("ANSWER_AUDIO_NOT_FOUND", "Candidate answer audio is unavailable.", status_code=404)
    return (storage or private_file_storage()).open(str(item["object_key"]))
