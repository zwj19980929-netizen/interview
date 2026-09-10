import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.time import utc_now


SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]+$")
MIME_EXTENSIONS = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/wav": ".wav",
    "audio/pcm": ".wav",
    "audio/l16": ".wav",
    "audio/raw": ".wav",
}
PCM_MIME_TYPES = frozenset({"audio/pcm", "audio/l16", "audio/raw"})


def pcm_wav_header(data_bytes: int, *, sample_rate_hz: int, channels: int) -> bytes:
    """Build a PCM16 WAV header so browser PCM chunks remain portable evidence."""
    if sample_rate_hz < 8000 or sample_rate_hz > 192000:
        raise ApiError("MEDIA_SAMPLE_RATE_INVALID", "PCM sample rate is outside the supported range.", status_code=422)
    if channels < 1 or channels > 2:
        raise ApiError("MEDIA_CHANNELS_INVALID", "PCM recordings support one or two channels.", status_code=422)
    sample_width_bytes = 2
    block_align = channels * sample_width_bytes
    byte_rate = sample_rate_hz * block_align
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate_hz,
        byte_rate,
        block_align,
        sample_width_bytes * 8,
        b"data",
        data_bytes,
    )


@dataclass
class RecordingResult:
    audio_uri: str
    mime_type: str
    byte_count: int
    duration_seconds: Optional[float] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class LocalMediaRecording:
    def __init__(
        self,
        path: Path,
        audio_uri: str,
        mime_type: str,
        *,
        max_chunk_bytes: int,
        max_recording_bytes: int,
        pcm_input: bool = False,
        sample_rate_hz: int = 48000,
        channels: int = 1,
    ) -> None:
        self.path = path
        self.audio_uri = audio_uri
        self.mime_type = mime_type
        self.max_chunk_bytes = max_chunk_bytes
        self.max_recording_bytes = max_recording_bytes
        self.pcm_input = pcm_input
        self.sample_rate_hz = sample_rate_hz
        self.channels = channels
        self.byte_count = 0
        self.started_at = None
        self._file: Optional[BinaryIO] = path.open("wb")
        if self.pcm_input:
            pcm_wav_header(0, sample_rate_hz=self.sample_rate_hz, channels=self.channels)
            self._file.write(b"\x00" * 44)

    def append(self, chunk: bytes) -> None:
        if self._file is None:
            raise ApiError("MEDIA_RECORDING_CLOSED", "Media recording is already closed.", status_code=409)
        if not chunk:
            return
        if len(chunk) > self.max_chunk_bytes:
            raise ApiError("MEDIA_CHUNK_TOO_LARGE", "Audio chunk exceeds the size limit.", status_code=413)
        if self.byte_count + len(chunk) > self.max_recording_bytes:
            raise ApiError("MEDIA_RECORDING_TOO_LARGE", "Audio recording exceeds the size limit.", status_code=413)
        self.started_at = self.started_at or utc_now()
        self._file.write(chunk)
        self.byte_count += len(chunk)

    def finish(self) -> RecordingResult:
        if self._file is not None:
            if self.pcm_input:
                self._file.seek(0)
                self._file.write(
                    pcm_wav_header(
                        self.byte_count,
                        sample_rate_hz=self.sample_rate_hz,
                        channels=self.channels,
                    )
                )
            self._file.flush()
            self._file.close()
            self._file = None
        return RecordingResult(
            audio_uri=self.audio_uri,
            mime_type=self.mime_type,
            byte_count=self.byte_count + (44 if self.pcm_input else 0),
            duration_seconds=self.byte_count / (self.sample_rate_hz * self.channels * 2) if self.pcm_input else None,
            started_at=self.started_at, finished_at=utc_now(),
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

    def start_recording(
        self,
        interview_id: str,
        turn_id: str,
        mime_type: str,
        *,
        sample_rate_hz: int = 48000,
        channels: int = 1,
    ) -> LocalMediaRecording:
        if not SAFE_ID.match(interview_id) or not SAFE_ID.match(turn_id):
            raise ApiError("MEDIA_PATH_INVALID", "Interview or turn identifier is invalid.")
        normalized_mime = mime_type.split(";", 1)[0].strip().lower()
        extension = MIME_EXTENSIONS.get(normalized_mime)
        if not extension:
            raise ApiError(
                "MEDIA_TYPE_UNSUPPORTED",
                "Supported audio types are WebM, Ogg, MP4, WAV and PCM16.",
                status_code=415,
            )
        pcm_input = normalized_mime in PCM_MIME_TYPES
        if pcm_input:
            pcm_wav_header(0, sample_rate_hz=sample_rate_hz, channels=channels)
        relative_dir = Path(interview_id) / turn_id
        directory = self.root / relative_dir
        directory.mkdir(parents=True, exist_ok=True)
        filename = "%s%s" % (new_id("recording"), extension)
        path = directory / filename
        audio_uri = "/media/%s" % (relative_dir / filename).as_posix()
        return LocalMediaRecording(
            path,
            audio_uri,
            "audio/wav" if pcm_input else mime_type,
            max_chunk_bytes=self.max_chunk_bytes,
            max_recording_bytes=self.max_recording_bytes,
            pcm_input=pcm_input,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
        )
