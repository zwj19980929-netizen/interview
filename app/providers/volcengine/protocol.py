"""Private wire helpers for Volcengine speech WebSocket protocols.

The provider facade owns vendor routing while this module owns the binary
framing contract used by Seed ASR.  No domain service should depend on these
types or on Volcengine message constants.
"""

from __future__ import annotations

from dataclasses import dataclass
import gzip
import json
import struct
from typing import Any, Dict, Optional

from app.model_gateway.errors import ProviderError


_VERSION_AND_HEADER_SIZE = 0x11
_FULL_CLIENT_REQUEST = 0x1
_AUDIO_ONLY_REQUEST = 0x2
_FULL_SERVER_RESPONSE = 0x9
_ERROR_RESPONSE = 0xF
_JSON = 0x1
_GZIP = 0x1
_FLAG_SEQUENCE = 0x1
_FLAG_LAST_WITHOUT_SEQUENCE = 0x2
_FLAG_LAST_WITH_SEQUENCE = 0x3


@dataclass(frozen=True)
class VolcengineSpeechFrame:
    message_type: int
    flags: int
    sequence: Optional[int]
    payload: Dict[str, Any]
    is_last: bool
    error_code: Optional[int] = None


def encode_full_client_request(payload: Dict[str, Any]) -> bytes:
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(serialized)
    header = bytes(
        [
            _VERSION_AND_HEADER_SIZE,
            _FULL_CLIENT_REQUEST << 4,
            (_JSON << 4) | _GZIP,
            0,
        ]
    )
    return header + struct.pack(">I", len(compressed)) + compressed


def encode_audio_only_request(audio: bytes, *, last: bool = False) -> bytes:
    compressed = gzip.compress(audio)
    flags = _FLAG_LAST_WITHOUT_SEQUENCE if last else 0
    header = bytes(
        [
            _VERSION_AND_HEADER_SIZE,
            (_AUDIO_ONLY_REQUEST << 4) | flags,
            _GZIP,
            0,
        ]
    )
    return header + struct.pack(">I", len(compressed)) + compressed


def decode_server_frame(raw: Any) -> VolcengineSpeechFrame:
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise ProviderError(
            "provider_schema_invalid",
            "Volcengine ASR returned a non-binary WebSocket frame.",
            retryable=True,
        )
    data = bytes(raw)
    if len(data) < 8:
        raise _invalid_frame("Volcengine ASR frame is truncated.")

    version = data[0] >> 4
    header_words = data[0] & 0x0F
    if version != 1 or header_words < 1:
        raise _invalid_frame("Volcengine ASR frame has an unsupported header.")
    offset = header_words * 4
    if len(data) < offset + 4:
        raise _invalid_frame("Volcengine ASR frame header is truncated.")

    message_type = data[1] >> 4
    flags = data[1] & 0x0F
    serialization = data[2] >> 4
    compression = data[2] & 0x0F
    sequence: Optional[int] = None
    error_code: Optional[int] = None

    if message_type == _FULL_SERVER_RESPONSE and flags in {
        _FLAG_SEQUENCE,
        _FLAG_LAST_WITH_SEQUENCE,
    }:
        sequence, offset = _read_i32(data, offset)
    elif message_type == _ERROR_RESPONSE:
        error_code, offset = _read_u32(data, offset)
    elif message_type != _FULL_SERVER_RESPONSE:
        raise _invalid_frame("Volcengine ASR returned an unsupported message type.")

    payload_size, offset = _read_u32(data, offset)
    if payload_size > len(data) - offset:
        raise _invalid_frame("Volcengine ASR payload length exceeds the frame size.")
    payload_bytes = data[offset : offset + payload_size]
    if compression == _GZIP:
        try:
            payload_bytes = gzip.decompress(payload_bytes)
        except (OSError, EOFError) as exc:
            raise _invalid_frame("Volcengine ASR payload is not valid gzip data.") from exc
    elif compression != 0:
        raise _invalid_frame("Volcengine ASR payload compression is unsupported.")

    if serialization not in {0, _JSON}:
        raise _invalid_frame("Volcengine ASR payload serialization is unsupported.")
    try:
        decoded = payload_bytes.decode("utf-8") if payload_bytes else ""
        if serialization == _JSON:
            payload = json.loads(decoded) if decoded else {}
        elif message_type == _ERROR_RESPONSE:
            # Vendor error frames are documented as UTF-8 text, while some
            # deployments send an object with serialization=none.  Normalize
            # both shapes before crossing the provider seam.
            try:
                parsed = json.loads(decoded) if decoded else {}
            except json.JSONDecodeError:
                parsed = {"message": decoded}
            payload = parsed if isinstance(parsed, dict) else {"message": decoded}
        elif not decoded:
            payload = {}
        else:
            raise json.JSONDecodeError("JSON serialization is required", decoded, 0)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _invalid_frame("Volcengine ASR payload is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise _invalid_frame("Volcengine ASR payload must be a JSON object.")

    is_last = flags in {_FLAG_LAST_WITHOUT_SEQUENCE, _FLAG_LAST_WITH_SEQUENCE}
    if message_type == _ERROR_RESPONSE:
        message = str(payload.get("message") or payload.get("error") or "unknown")
        raise ProviderError(
            "provider_server_error",
            "Volcengine ASR error %s: %s" % (error_code or "unknown", message),
            retryable=_retryable_vendor_error(error_code),
            details={"vendor_error_code": error_code},
        )
    return VolcengineSpeechFrame(
        message_type=message_type,
        flags=flags,
        sequence=sequence,
        payload=payload,
        is_last=is_last,
        error_code=error_code,
    )


def _read_u32(data: bytes, offset: int) -> tuple[int, int]:
    if len(data) < offset + 4:
        raise _invalid_frame("Volcengine ASR integer field is truncated.")
    return struct.unpack(">I", data[offset : offset + 4])[0], offset + 4


def _read_i32(data: bytes, offset: int) -> tuple[int, int]:
    if len(data) < offset + 4:
        raise _invalid_frame("Volcengine ASR sequence field is truncated.")
    return struct.unpack(">i", data[offset : offset + 4])[0], offset + 4


def _invalid_frame(message: str) -> ProviderError:
    return ProviderError("provider_schema_invalid", message, retryable=True)


def _retryable_vendor_error(error_code: Optional[int]) -> bool:
    if error_code is None:
        return True
    # 45xxxxxx is a request/input class error; 55xxxxxx is a transient
    # service class error in the vendor protocol.
    return int(error_code) >= 55_000_000
