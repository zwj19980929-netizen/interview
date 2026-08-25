import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional

from app.core.errors import ApiError
from app.core.ids import new_id


SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]+$")
MIME_EXTENSIONS = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/wav": ".wav",
}


@dataclass
class RecordingResult:
    audio_uri: str
    mime_type: str
    byte_count: int


class LocalMediaRecording:
    def __init__(
        self,
        path: Path,
        audio_uri: str,
        mime_type: str,
        *,
        max_chunk_bytes: int,
        max_recording_bytes: int,
    ) -> None:
        self.path = path
        self.audio_uri = audio_uri
        self.mime_type = mime_type
        self.max_chunk_bytes = max_chunk_bytes
        self.max_recording_bytes = max_recording_bytes
        self.byte_count = 0
        self._file: Optional[BinaryIO] = path.open("wb")

    def append(self, chunk: bytes) -> None:
        if self._file is None:
            raise ApiError("MEDIA_RECORDING_CLOSED", "Media recording is already closed.", status_code=409)
        if not chunk:
            return
        if len(chunk) > self.max_chunk_bytes:
            raise ApiError("MEDIA_CHUNK_TOO_LARGE", "Audio chunk exceeds the size limit.", status_code=413)
        if self.byte_count + len(chunk) > self.max_recording_bytes:
            raise ApiError("MEDIA_RECORDING_TOO_LARGE", "Audio recording exceeds the size limit.", status_code=413)
        self._file.write(chunk)
        self.byte_count += len(chunk)

    def finish(self) -> RecordingResult:
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None
        return RecordingResult(
            audio_uri=self.audio_uri,
            mime_type=self.mime_type,
            byte_count=self.byte_count,
        )

    def abort(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        if self.path.exists():
            self.path.unlink()


class LocalMediaStorage:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or Path(os.getenv("INTERVIEWER_MEDIA_PATH", "data/media"))
        self.max_chunk_bytes = int(os.getenv("INTERVIEWER_MEDIA_MAX_CHUNK_BYTES", "1048576"))
        self.max_recording_bytes = int(os.getenv("INTERVIEWER_MEDIA_MAX_RECORDING_BYTES", "52428800"))
        self.root.mkdir(parents=True, exist_ok=True)

    def start_recording(self, interview_id: str, turn_id: str, mime_type: str) -> LocalMediaRecording:
        if not SAFE_ID.match(interview_id) or not SAFE_ID.match(turn_id):
            raise ApiError("MEDIA_PATH_INVALID", "Interview or turn identifier is invalid.")
        normalized_mime = mime_type.split(";", 1)[0].strip().lower()
        extension = MIME_EXTENSIONS.get(normalized_mime)
        if not extension:
            raise ApiError(
                "MEDIA_TYPE_UNSUPPORTED",
                "Supported audio types are WebM, Ogg, MP4 and WAV.",
                status_code=415,
            )
        relative_dir = Path(interview_id) / turn_id
        directory = self.root / relative_dir
        directory.mkdir(parents=True, exist_ok=True)
        filename = "%s%s" % (new_id("recording"), extension)
        path = directory / filename
        audio_uri = "/media/%s" % (relative_dir / filename).as_posix()
        return LocalMediaRecording(
            path,
            audio_uri,
            mime_type,
            max_chunk_bytes=self.max_chunk_bytes,
            max_recording_bytes=self.max_recording_bytes,
        )
