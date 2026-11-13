"""Versioned synthetic speech for model health, with no candidate material."""

import hashlib
import unicodedata
from pathlib import Path

from app.model_gateway.errors import ProviderError


BATCH_STT_PROBE_VERSION = "batch_stt_known_speech.v1"
BATCH_STT_PROBE_TEXT = "连接测试，语音识别正常。"
BATCH_STT_PROBE_PATH = Path(__file__).parent / "assets" / "stt-connection-probe-v1.wav"
BATCH_STT_PROBE_SIZE = 86366
BATCH_STT_PROBE_SHA256 = "326d0aad88aab144a81701c3f30b10a49795bee4e655e96ab583f105a31f3a5a"


def batch_stt_probe_audio() -> bytes:
    """Fail closed if the shipped synthetic evidence differs from this contract."""
    try:
        if BATCH_STT_PROBE_PATH.is_symlink():
            raise OSError("Probe asset cannot be a symlink")
        with BATCH_STT_PROBE_PATH.open("rb") as source:
            audio = source.read(BATCH_STT_PROBE_SIZE + 1)
    except OSError as exc:
        raise ProviderError("provider_probe_failed", "Synthetic speech probe is unavailable.", retryable=False) from exc
    if len(audio) != BATCH_STT_PROBE_SIZE or hashlib.sha256(audio).hexdigest() != BATCH_STT_PROBE_SHA256:
        raise ProviderError("provider_probe_failed", "Synthetic speech probe failed its integrity check.", retryable=False)
    return audio


def batch_stt_probe_matches(text: str) -> bool:
    def normalized(value):
        return "".join(character for character in unicodedata.normalize("NFKC", value)
                       if not character.isspace() and not unicodedata.category(character).startswith("P"))
    return isinstance(text, str) and normalized(text) == normalized(BATCH_STT_PROBE_TEXT)
