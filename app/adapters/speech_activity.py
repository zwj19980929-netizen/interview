"""Local PCM activity facts shared by endpoint and evidence completeness gates.

No text is produced here. ASR still owns transcripts. A pending speech onset
blocks a commit until subsequent frames resolve it; a single impulse cannot
revoke an already confirmed answer. All PCM remains in the private recording.
"""

from array import array
from collections import deque
import math
import os
from typing import Any

import webrtcvad


def speech_rms_threshold() -> float:
    value = float(os.getenv("INTERVIEWER_TURN_VOICE_RMS", "0.006"))
    if not math.isfinite(value) or not 0 < value < 0.2:
        raise ValueError("Invalid speech RMS threshold")
    return value


class ServerSpeechActivity:
    def __init__(self, *, sample_rate_hz: int = 16000, channels: int = 1,
                 vad: Any = None, rms_threshold: float = None) -> None:
        self.rate = sample_rate_hz
        self.channels = channels
        self._vad = vad if vad is not None else webrtcvad.Vad(3)
        self._frame_bytes = sample_rate_hz // 50 * 2
        self._buffer = bytearray()
        self.rms_threshold = speech_rms_threshold() if rms_threshold is None else rms_threshold
        if not math.isfinite(self.rms_threshold) or not 0 < self.rms_threshold < 0.2:
            raise ValueError("Invalid speech RMS threshold")
        self._recent = deque(maxlen=6)
        self.received_bytes = 0
        self.processed_bytes = 0
        self.last_speech_byte = 0
        self.uncertain = False

    def observe(self, pcm: bytes) -> bool:
        self.received_bytes += len(pcm)
        if self.rate not in {8000, 16000, 32000, 48000} or self.channels != 1 or len(pcm) % 2:
            # Unsupported or corrupt input is never certified as silence.
            self.last_speech_byte = self.received_bytes
            return True
        self._buffer.extend(pcm)
        detected = False
        while len(self._buffer) >= self._frame_bytes:
            frame = bytes(self._buffer[:self._frame_bytes])
            del self._buffer[:self._frame_bytes]
            self.processed_bytes += self._frame_bytes
            values = array("h", frame)
            energy = sum(x * x for x in values) / len(values)
            # A nonzero constant is a DC fault (also used by synthetic transport
            # fixtures), not evidence that a microphone captured silence.
            unknown = bool(values[0] and all(x == values[0] for x in values))
            try:
                voiced = self._vad.is_speech(frame, self.rate)
            except Exception:
                unknown = True
                voiced = True
            # VAD hangover/quiet background alone cannot continually reset the
            # silence clock. ASR receives every frame, including subthreshold
            # speech; a new server hypothesis independently revokes proposals.
            possible = voiced and energy >= (32768 * self.rms_threshold) ** 2
            self._recent.append(bool(possible))
            if unknown or (possible and sum(self._recent) >= 5):
                self.last_speech_byte = self.processed_bytes
                detected = True
            self.uncertain = bool(possible and not detected)
        # An incomplete PCM frame cannot be silently discarded at a commit cut.
        self.uncertain = self.uncertain or bool(self._buffer and any(self._buffer))
        return detected

    def since(self, byte_offset: int) -> bool:
        return self.last_speech_byte > byte_offset or (self.uncertain and self.received_bytes > byte_offset)
