import base64
import hashlib
import struct
from dataclasses import dataclass
from typing import Optional, Tuple

from app.core.errors import ApiError
from app.services.resume_ingestion import SafePdfDownloader


_WAVE_CONTENT_TYPES = frozenset(
    {"audio/wav", "audio/wave", "audio/x-wav", "audio/vnd.wave"}
)
# Exact signed-limit placeholder pair observed in non-seekable PCM WAVE output.
# A broad "declared size > received size" rule would corrupt genuinely
# truncated assets, so unrecognized placeholder values fail closed.
_SIGNED_LIMIT_STREAMING_PLACEHOLDER = (0x7FFFFFBF, 0x7FFFFF9B)


@dataclass(frozen=True)
class _WaveFormat:
    block_align: int
    repairable_stream: bool


def _invalid_wave(reason: str) -> ApiError:
    return ApiError(
        "TTS_ASSET_FORMAT_INVALID",
        "TTS WAV asset is malformed or incomplete.",
        status_code=502,
        details={"reason": reason},
    )


def _wave_format(payload: bytes) -> _WaveFormat:
    if len(payload) < 16:
        raise _invalid_wave("fmt_chunk_invalid")
    format_tag, channels, sample_rate, byte_rate, block_align, bits_per_sample = (
        struct.unpack_from("<HHIIHH", payload)
    )
    if not channels or not sample_rate or not byte_rate or not block_align:
        raise _invalid_wave("fmt_chunk_invalid")

    repairable_stream = format_tag in {1, 3}
    if format_tag in {1, 3}:
        if not bits_per_sample or bits_per_sample % 8:
            raise _invalid_wave("fmt_chunk_invalid")
        if format_tag == 3 and bits_per_sample not in {32, 64}:
            raise _invalid_wave("fmt_chunk_invalid")
        expected_align = channels * (bits_per_sample // 8)
        if block_align != expected_align or byte_rate != sample_rate * block_align:
            raise _invalid_wave("fmt_chunk_invalid")
    elif format_tag == 0xFFFE:
        if len(payload) < 40:
            raise _invalid_wave("fmt_chunk_invalid")
        extension_size, valid_bits = struct.unpack_from("<HH", payload, 16)
        if extension_size < 22 or 18 + extension_size > len(payload):
            raise _invalid_wave("fmt_chunk_invalid")
        if not bits_per_sample or bits_per_sample % 8 or not 0 < valid_bits <= bits_per_sample:
            raise _invalid_wave("fmt_chunk_invalid")
        expected_align = channels * (bits_per_sample // 8)
        if block_align != expected_align or byte_rate != sample_rate * block_align:
            raise _invalid_wave("fmt_chunk_invalid")
        subformat = payload[24:40]
        wave_subformat_tail = b"\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
        repairable_stream = (
            len(subformat) == 16
            and struct.unpack_from("<I", subformat)[0] in {1, 3}
            and subformat[4:] == wave_subformat_tail
        )
    elif len(payload) >= 18:
        extension_size = struct.unpack_from("<H", payload, 16)[0]
        if 18 + extension_size > len(payload):
            raise _invalid_wave("fmt_chunk_invalid")

    return _WaveFormat(block_align=block_align, repairable_stream=repairable_stream)


def _normalize_wave(content: bytes) -> bytes:
    if len(content) < 12 or content[:4] != b"RIFF" or content[8:12] != b"WAVE":
        raise _invalid_wave("riff_wave_header_invalid")

    declared_riff_size = struct.unpack_from("<I", content, 4)[0]
    offset = 12
    fmt: Optional[_WaveFormat] = None
    fmt_count = 0
    data_count = 0

    while offset < len(content):
        if len(content) - offset < 8:
            raise _invalid_wave("chunk_header_truncated")
        chunk_id = content[offset:offset + 4]
        chunk_size = struct.unpack_from("<I", content, offset + 4)[0]
        payload_start = offset + 8
        payload_end = payload_start + chunk_size
        padded_end = payload_end + (chunk_size & 1)

        if payload_end > len(content):
            if chunk_id != b"data":
                raise _invalid_wave("chunk_payload_truncated")
            if fmt is None:
                raise _invalid_wave("data_before_fmt")
            if data_count:
                raise _invalid_wave("data_chunk_duplicate")
            if (declared_riff_size, chunk_size) != _SIGNED_LIMIT_STREAMING_PLACEHOLDER:
                raise _invalid_wave("data_chunk_truncated")
            if declared_riff_size + 8 != payload_start + chunk_size:
                raise _invalid_wave("streaming_placeholder_inconsistent")
            actual_size = len(content) - payload_start
            if not fmt.repairable_stream or not actual_size:
                raise _invalid_wave("streaming_payload_invalid")
            if actual_size & 1 or actual_size % fmt.block_align:
                raise _invalid_wave("data_alignment_invalid")

            normalized = bytearray(content)
            struct.pack_into("<I", normalized, offset + 4, actual_size)
            struct.pack_into("<I", normalized, 4, len(normalized) - 8)
            return _normalize_wave(bytes(normalized))

        if padded_end > len(content):
            raise _invalid_wave("chunk_padding_missing")

        payload = content[payload_start:payload_end]
        if chunk_id == b"fmt ":
            fmt_count += 1
            if fmt_count > 1:
                raise _invalid_wave("fmt_chunk_duplicate")
            if data_count:
                raise _invalid_wave("fmt_after_data")
            fmt = _wave_format(payload)
        elif chunk_id == b"data":
            if fmt is None:
                raise _invalid_wave("data_before_fmt")
            data_count += 1
            if data_count > 1:
                raise _invalid_wave("data_chunk_duplicate")
            if not chunk_size:
                raise _invalid_wave("data_chunk_empty")
            if fmt.repairable_stream and chunk_size % fmt.block_align:
                raise _invalid_wave("data_alignment_invalid")
        offset = padded_end

    if fmt_count != 1:
        raise _invalid_wave("fmt_chunk_missing")
    if data_count != 1:
        raise _invalid_wave("data_chunk_missing")

    canonical_riff_size = len(content) - 8
    if declared_riff_size == canonical_riff_size:
        return content
    # A known encoder family omits the four-byte WAVE form type from RIFF's
    # outer size. Reaching this branch proves that every child chunk and pad
    # was complete and consumed exactly to EOF, so changing only the outer
    # size does not infer, discard or reinterpret any payload bytes.
    if declared_riff_size == len(content) - 12:
        normalized = bytearray(content)
        struct.pack_into("<I", normalized, 4, canonical_riff_size)
        return bytes(normalized)
    raise _invalid_wave("riff_size_mismatch")


class PrivateAssetImporter:
    def __init__(self, *, max_bytes: int = 20 * 1024 * 1024) -> None:
        self.downloader = SafePdfDownloader(max_bytes=max_bytes, accept="audio/*")
        self.max_bytes = max_bytes

    async def audio(self, uri: str, content_type: str) -> Tuple[bytes, str]:
        normalized = content_type.split(";", 1)[0].strip().lower()
        if not normalized.startswith("audio/"):
            raise ApiError("TTS_ASSET_TYPE_INVALID", "TTS asset must use an audio content type.", status_code=422)
        if uri.startswith("data:"):
            header, separator, encoded = uri.partition(",")
            if not separator or ";base64" not in header:
                raise ApiError("TTS_ASSET_URI_INVALID", "TTS data URI must be base64 encoded.", status_code=422)
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ApiError("TTS_ASSET_URI_INVALID", "TTS data URI is invalid.", status_code=422) from exc
        elif uri.startswith(("https://", "http://")):
            try:
                content, _ = await self.downloader.download(uri)
            except ApiError as exc:
                raise ApiError(
                    "TTS_ASSET_DOWNLOAD_FAILED",
                    "TTS provider asset could not be downloaded safely.",
                    status_code=502,
                    details={"source_code": exc.code},
                ) from exc
        else:
            raise ApiError(
                "TTS_ASSET_URI_UNMANAGED",
                "A real TTS provider must return an HTTPS or data audio asset.",
                status_code=502,
            )
        if not content or len(content) > self.max_bytes:
            raise ApiError("TTS_ASSET_SIZE_INVALID", "TTS asset is empty or exceeds the size limit.", status_code=422)
        if normalized in _WAVE_CONTENT_TYPES:
            content = _normalize_wave(content)
            if len(content) > self.max_bytes:
                raise ApiError("TTS_ASSET_SIZE_INVALID", "TTS asset is empty or exceeds the size limit.", status_code=422)
        elif len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WAVE":
            raise _invalid_wave("content_type_mismatch")
        return content, "sha256:%s" % hashlib.sha256(content).hexdigest()
