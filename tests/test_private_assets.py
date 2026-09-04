import asyncio
import base64
import hashlib
import struct

import pytest

from app.core.errors import ApiError
from app.services.private_assets import PrivateAssetImporter


def _chunk(chunk_id: bytes, payload: bytes) -> bytes:
    return (
        chunk_id
        + struct.pack("<I", len(payload))
        + payload
        + (b"\x00" if len(payload) & 1 else b"")
    )


def _pcm_format(*, bits: int = 16) -> bytes:
    channels = 1
    sample_rate = 24_000
    block_align = channels * (bits // 8)
    return struct.pack(
        "<HHIIHH",
        1,
        channels,
        sample_rate,
        sample_rate * block_align,
        block_align,
        bits,
    )


def _wave(*chunks: bytes, riff_size: int = None) -> bytes:
    body = b"WAVE" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body) if riff_size is None else riff_size) + body


def _data_uri(content: bytes, media_type: str = "audio/wav") -> str:
    return "data:%s;base64,%s" % (
        media_type,
        base64.b64encode(content).decode("ascii"),
    )


def _import(content: bytes, content_type: str = "audio/wav"):
    return asyncio.run(
        PrivateAssetImporter().audio(_data_uri(content, content_type), content_type)
    )


def test_canonical_wave_with_unknown_and_post_data_chunks_is_unchanged() -> None:
    content = _wave(
        _chunk(b"JUNK", b"odd"),
        _chunk(b"fmt ", _pcm_format()),
        _chunk(b"AIGC", b"provider metadata"),
        _chunk(b"data", b"\x01\x00\x02\x00"),
        _chunk(b"LIST", b"INFO"),
    )

    imported, checksum = _import(content)

    assert imported == content
    assert checksum == "sha256:%s" % hashlib.sha256(content).hexdigest()


def test_qwen_streaming_placeholder_is_rewritten_to_actual_lengths() -> None:
    pcm = b"\x01\x00\x02\x00\x03\x00\x04\x00"
    content = _wave(
        _chunk(b"fmt ", _pcm_format()),
        b"data" + struct.pack("<I", 0x7FFFFF9B) + pcm,
        riff_size=0x7FFFFFBF,
    )

    imported, checksum = _import(content)

    assert struct.unpack_from("<I", imported, 4)[0] == len(imported) - 8
    assert struct.unpack_from("<I", imported, 40)[0] == len(pcm)
    assert imported[:4] == content[:4]
    assert imported[8:40] == content[8:40]
    assert imported[44:] == pcm
    assert checksum == "sha256:%s" % hashlib.sha256(imported).hexdigest()


def test_known_encoder_omitted_wave_form_size_is_canonicalized() -> None:
    content = _wave(
        _chunk(b"fmt ", _pcm_format()),
        _chunk(b"AIGC", b"metadata"),
        _chunk(b"data", b"\x01\x00\x02\x00"),
    )
    content = content[:4] + struct.pack("<I", len(content) - 12) + content[8:]

    imported, checksum = _import(content)

    assert struct.unpack_from("<I", imported, 4)[0] == len(imported) - 8
    assert imported[8:] == content[8:]
    assert checksum == "sha256:%s" % hashlib.sha256(imported).hexdigest()


@pytest.mark.parametrize("size_delta", [-8, 4])
def test_other_outer_riff_size_mismatches_are_rejected(size_delta: int) -> None:
    content = _wave(
        _chunk(b"fmt ", _pcm_format()),
        _chunk(b"data", b"\x01\x00\x02\x00"),
    )
    declared_size = len(content) - 8 + size_delta
    content = content[:4] + struct.pack("<I", declared_size) + content[8:]

    with pytest.raises(ApiError) as raised:
        _import(content)

    assert raised.value.details == {"reason": "riff_size_mismatch"}


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (
            _wave(
                _chunk(b"fmt ", _pcm_format()),
                _chunk(b"data", b"\x01\x00\x02\x00"),
            )[:-2],
            "data_chunk_truncated",
        ),
        (
            _wave(
                _chunk(b"fmt ", _pcm_format()),
                b"data" + struct.pack("<I", 1_000) + b"\x01\x00",
                riff_size=1_036,
            ),
            "data_chunk_truncated",
        ),
        (
            _wave(_chunk(b"data", b"\x01\x00"), _chunk(b"fmt ", _pcm_format())),
            "data_before_fmt",
        ),
        (
            _wave(
                _chunk(b"fmt ", _pcm_format()),
                _chunk(b"data", b"\x01\x00"),
                _chunk(b"data", b"\x02\x00"),
            ),
            "data_chunk_duplicate",
        ),
        (
            _wave(
                _chunk(b"fmt ", _pcm_format()),
                _chunk(b"fmt ", _pcm_format()),
                _chunk(b"data", b"\x01\x00"),
            ),
            "fmt_chunk_duplicate",
        ),
        (
            _wave(
                _chunk(b"fmt ", _pcm_format()),
                _chunk(b"data", b"\x01\x00"),
            )
            + b"tail",
            "chunk_header_truncated",
        ),
        (
            _wave(
                _chunk(b"fmt ", _pcm_format()),
                _chunk(b"data", b"\x01\x00"),
                b"LIST" + struct.pack("<I", 1) + b"x",
            ),
            "chunk_padding_missing",
        ),
        (
            _wave(
                _chunk(b"fmt ", _pcm_format()),
                b"JUNK" + struct.pack("<I", 100) + b"short",
            ),
            "chunk_payload_truncated",
        ),
        (
            _wave(_chunk(b"fmt ", _pcm_format()), _chunk(b"data", b"")),
            "data_chunk_empty",
        ),
    ],
)
def test_malformed_or_truncated_wave_is_rejected(content: bytes, reason: str) -> None:
    with pytest.raises(ApiError) as raised:
        _import(content)

    assert raised.value.code == "TTS_ASSET_FORMAT_INVALID"
    assert raised.value.status_code == 502
    assert raised.value.details == {"reason": reason}


def test_streaming_placeholder_with_partial_pcm_frame_is_rejected() -> None:
    content = _wave(
        _chunk(b"fmt ", _pcm_format()),
        b"data" + struct.pack("<I", 0x7FFFFF9B) + b"\x01",
        riff_size=0x7FFFFFBF,
    )

    with pytest.raises(ApiError) as raised:
        _import(content)

    assert raised.value.details == {"reason": "data_alignment_invalid"}


def test_non_wave_audio_remains_opaque() -> None:
    content = b"not-a-wave-but-valid-for-the-provider-specific-codec"

    imported, checksum = _import(content, "audio/mpeg")

    assert imported == content
    assert checksum == "sha256:%s" % hashlib.sha256(content).hexdigest()


def test_riff_wave_declared_as_non_wave_audio_is_rejected() -> None:
    content = _wave(
        _chunk(b"fmt ", _pcm_format()),
        _chunk(b"data", b"\x01\x00"),
    )

    with pytest.raises(ApiError) as raised:
        _import(content, "audio/mpeg")

    assert raised.value.details == {"reason": "content_type_mismatch"}


def test_vendor_wave_media_type_is_validated() -> None:
    content = _wave(
        _chunk(b"fmt ", _pcm_format()),
        _chunk(b"data", b"\x01\x00"),
    )

    imported, _ = _import(content, "audio/vnd.wave")

    assert imported == content
